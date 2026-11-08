"""Generation-based publishing: a graph is never rewritten in place.

The problem this solves
-----------------------
Until now a project had one database file, ``<data_dir>/graph.db``, and
``index --rebuild`` rewrote it. Reading was made physically safe -- ``mode=ro``,
``immutable=1``, ``query_only`` -- but ``immutable=1`` is a promise that the
file will not change while it is open, and a rebuild broke exactly that promise.
The build shipped with the assumption written down: single user, no concurrent
rebuild. That is not a constraint a team can work under.

The model
---------
An index run never touches the published database. It writes a brand-new file
in its own directory::

    <data_dir>/
        CURRENT                       the pointer; the only thing that moves
        generations/
            gen-<run_id>/graph.db     one build, written once, never rewritten
        build.lock                    held for the duration of a build

A generation file is written once and is thereafter immutable in the strongest
sense available: nothing in this program ever opens it for writing again. A
reader that has one open can keep reading it forever, because the bytes under
it cannot change -- there is no code path that would change them.

Publishing is a pointer swap. ``CURRENT`` is a small JSON manifest, written to
a temporary file and moved into place with :func:`os.replace`, which on Windows
is a single ``MoveFileEx`` with ``MOVEFILE_REPLACE_EXISTING``: either the old
manifest is there or the new one is, never a torn file. The database file is
never the thing being replaced, which is what makes this work on Windows at
all: a ``.db`` a server has open cannot be replaced, and a ~1 KB manifest that
readers open and close in microseconds can.

What follows from that
----------------------
* An interrupted build leaves a half-written generation directory and an
  untouched pointer. The published graph is exactly what it was.
* A build that fails its pre-publication checks never becomes current.
* Old generations are kept. Deleting one that a process may still have open is
  the one thing this module will not do automatically.
* Two builds at once are refused, not interleaved, with an error naming the
  holder.

Compatibility
-------------
A data directory written by an earlier build has ``graph.db`` sitting directly
in it and no ``CURRENT``. That file is still read, and it is never moved,
converted or deleted -- the resolver simply falls back to it and says so. The
next rebuild writes a generation and publishes a pointer beside it; the legacy
file stays where it is, untouched, for as long as its owner wants it.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import time
import uuid
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone

from . import SCHEMA_VERSION, __version__
from .errors import OdooKgError
from .winpath import is_within, normalize, real_key

#: The pointer. Small, JSON, replaced atomically, never appended to.
POINTER_FILENAME = "CURRENT"

#: Where generation directories live, inside the project's data directory.
GENERATIONS_DIRNAME = "generations"

#: Held for the duration of a build so two runs cannot interleave.
LOCK_FILENAME = "build.lock"

#: The database file inside a generation directory. Constant on purpose: the
#: pointer names the generation, not an arbitrary path, so a manifest can never
#: be made to point at a file somewhere else on disk.
GENERATION_DB_FILENAME = "graph.db"

#: Prefix for a generation directory name. The rest is the run id.
GENERATION_PREFIX = "gen-"

#: Files SQLite may leave beside a database. None may survive into a published
#: generation: a published generation must be a single self-contained file.
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

#: A lock older than this is assumed to belong to a process that died. Stated
#: rather than guessed at: a build that legitimately runs longer than this on a
#: very large repository would need the number raised, and the error says so.
STALE_LOCK_SECONDS = 3600

#: ``os.replace`` fails on Windows while another process holds the target open,
#: even for reading, because ``open()`` there does not grant delete sharing.
#:
#: This program's own readers do not hold the pointer: they open it, parse a
#: kilobyte of JSON, and close it, all inside :func:`read_pointer`. The window
#: this retry covers is somebody else's -- a virus scanner walking the data
#: directory, a backup agent, an editor left open on the file.
#:
#: If the retries run out the publish is refused and the previously published
#: generation stays current. That is the safe direction to fail in: a build
#: that could not be published is an inconvenience, and a half-swapped pointer
#: would be a broken project.
_REPLACE_ATTEMPTS = 100
_REPLACE_PAUSE_SECONDS = 0.05


class PublishError(OdooKgError):
    """A generation could not be published, or a pointer could not be read."""


class NoGraphError(PublishError):
    """There is nothing to read: no pointer, or it names a generation that is gone.

    Kept apart from :class:`WrongGraphError` because the two belong on opposite
    paths, the same way a missing database and another project's database do.
    Nothing to read is a state a client must be able to connect through -- the
    fix is a command, and somebody has to get far enough to be told which one.
    """


class WrongGraphError(PublishError):
    """A real published graph, but not this project's or not this schema.

    Refused before any tool is advertised. A graph from another repository
    answers every question fluently and about the wrong code, which is worse
    than answering nothing.
    """


class BuildLockError(OdooKgError):
    """Another build holds the lock for this project."""


@dataclass
class Pointer:
    """What ``CURRENT`` says. Everything needed to check a graph before opening it."""

    generation: str
    project_id: str
    schema_version: int
    indexer_version: str
    run_id: str
    base_commit: str | None
    branch: str | None
    dirty: int | None
    published_at: str
    db_sha256: str
    db_size: int
    #: Set by the resolver, not stored: where the pointer was read from.
    source: str = ""

    def as_dict(self) -> dict:
        return {
            "generation": self.generation,
            "project_id": self.project_id,
            "schema_version": self.schema_version,
            "indexer_version": self.indexer_version,
            "run_id": self.run_id,
            "base_commit": self.base_commit,
            "branch": self.branch,
            "dirty": self.dirty,
            "published_at": self.published_at,
            "db_sha256": self.db_sha256,
            "db_size": self.db_size,
        }


@dataclass
class Resolution:
    """Which database a reader should open, and how that was decided."""

    db_path: str
    kind: str                      # "generation" | "legacy"
    pointer: Pointer | None = None
    notes: list[str] = dc_field(default_factory=list)


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def pointer_path(config) -> str:
    return os.path.join(config.data_dir.display, POINTER_FILENAME)


def generations_dir(config) -> str:
    return os.path.join(config.data_dir.display, GENERATIONS_DIRNAME)


def lock_path(config) -> str:
    return os.path.join(config.data_dir.display, LOCK_FILENAME)


def generation_name(run_id: str) -> str:
    return f"{GENERATION_PREFIX}{run_id}"


def generation_dir(config, generation: str) -> str:
    return os.path.join(generations_dir(config), generation)


def generation_db(config, generation: str) -> str:
    return os.path.join(generation_dir(config, generation), GENERATION_DB_FILENAME)


def _valid_generation_name(generation: object) -> bool:
    """A generation name is a fixed shape, never a path.

    Checked before the name is ever joined to a directory, so a manifest cannot
    steer a reader outside the project by writing ``../..`` or an absolute path
    into the field. The shape is the prefix plus a hex run id and nothing else.
    """
    if not isinstance(generation, str) or not generation.startswith(GENERATION_PREFIX):
        return False
    rest = generation[len(GENERATION_PREFIX):]
    return bool(rest) and len(rest) <= 64 and all(
        character in "0123456789abcdef" for character in rest
    )


def _assert_inside_data_dir(config, path: str, what: str) -> str:
    """Refuse a path that is, or resolves, outside the project's data directory.

    Three layers, because each catches a different mistake:

    * :func:`_valid_generation_name` rejects a name that is a path;
    * the lexical check here rejects a path that does not look contained;
    * :func:`~odoo_kg.safety.real_key` resolution rejects a path that looks
      contained and leads elsewhere -- a junction placed at ``generations`` or
      at one generation directory, pointing out of the tree.

    The root is resolved too, so a legitimately redirected ``%LOCALAPPDATA%``
    or a data directory that is itself a junction is fine: both sides are
    compared after resolution.
    """
    resolved = normalize(path)
    if not is_within(config.data_dir.key, resolved.key):
        raise PublishError(
            f"refusing to use {what} at {resolved.display}: it is outside the "
            f"project data directory {config.data_dir.display}"
        )
    root = real_key(config.data_dir.display)
    target = real_key(resolved.display)
    if not is_within(root, target):
        raise PublishError(
            f"refusing to use {what} at {resolved.display}: it resolves to "
            f"{target}, outside the data directory's real target {root}. A "
            f"reparse point or junction under {config.data_dir.display} is "
            f"leading it out of the project."
        )
    return resolved.display


def assert_managed_paths_are_safe(config, *, generation: str | None = None) -> None:
    """Check every path this module writes, before anything is created.

    Called before a build starts and again immediately before the pointer
    moves. Between those two moments a junction can be dropped into the tree,
    and the second call is what notices. Nothing outside the project is written
    before this has passed.
    """
    targets = [
        (pointer_path(config), "the CURRENT pointer"),
        (pointer_path(config) + f".{os.getpid()}.tmp", "the pointer's temporary file"),
        (lock_path(config), "the build lock"),
        (generations_dir(config), "the generations directory"),
    ]
    if generation is not None:
        targets.append((generation_dir(config, generation),
                        f"generation directory {generation}"))
        targets.append((generation_db(config, generation),
                        f"the database of generation {generation}"))
    for path, what in targets:
        _assert_inside_data_dir(config, path, what)


# ---------------------------------------------------------------------------
# the build lock
# ---------------------------------------------------------------------------


def process_start_time(pid: int) -> float | None:
    """When the process with this pid started, or None if that is unknowable.

    The point is pid reuse. A lock naming pid 4312 says nothing on its own:
    the machine may have rebooted and handed 4312 to something else, and
    deleting a lock because "that pid is alive" would then be deleting a live
    build's lock. Recording *when* the holder started makes the comparison
    meaningful -- same pid and same start time is the same process; same pid
    and a different start time is a different one.

    Windows only, through ``GetProcessTimes``. Elsewhere, and whenever the
    process cannot be opened, this returns None and the caller has to say it
    could not verify rather than guess.
    """
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exit_time),
            ctypes.byref(kernel), ctypes.byref(user),
        )
        if not ok:
            return None
        return (creation.dwHighDateTime << 32) + creation.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


def process_is_alive(pid: int) -> bool | None:
    """True, False, or None when this process cannot tell."""
    if os.name != "nt":
        return None
    return process_start_time(pid) is not None


@dataclass
class LockInfo:
    """A build lock as it is on disk, and what can be concluded about it."""

    path: str
    present: bool
    readable: bool = False
    lock_id: str = ""
    pid: int | None = None
    host: str = ""
    started_at: float | None = None
    started_display: str = ""
    process_start: float | None = None
    indexer_version: str = ""
    raw: str = ""

    @property
    def age_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        return max(0.0, time.time() - self.started_at)

    @property
    def is_local(self) -> bool:
        """Whether this lock was taken on this machine.

        Compared case-insensitively. Windows hostnames are case-insensitive and
        different APIs disagree about how to spell one -- ``COMPUTERNAME`` is
        upper case, ``socket.gethostname()` often is not -- so a case-sensitive
        comparison made this program judge its own lock to be another host's.
        The consequence was not a wrong answer but a worse one: the operator
        was told to reach for ``--force`` on a lock that could have been
        checked properly, which is how a safeguard becomes a habit.
        """
        return bool(self.host) and self.host.strip().lower() == socket.gethostname(
        ).strip().lower()

    @property
    def holder_state(self) -> str:
        """``running`` / ``gone`` / ``pid_reused`` / ``unverifiable``.

        ``unverifiable`` is not a euphemism for gone. A lock written on another
        machine, or one this platform cannot inspect, is a lock whose holder
        might be working right now, and the recovery flow treats it that way.
        """
        if not self.readable or self.pid is None:
            return "unverifiable"
        if not self.is_local:
            return "unverifiable"
        alive = process_is_alive(self.pid)
        if alive is None:
            return "unverifiable"
        if not alive:
            return "gone"
        if self.process_start is not None:
            current = process_start_time(self.pid)
            if current is not None and current != self.process_start:
                return "pid_reused"
        return "running"

    @property
    def looks_abandoned(self) -> bool:
        state = self.holder_state
        if state in ("gone", "pid_reused"):
            return True
        if state == "unverifiable" and (self.age_seconds or 0) > STALE_LOCK_SECONDS:
            return True
        return False


def read_lock(config) -> LockInfo:
    """What the build lock says right now. Never modifies it."""
    path = lock_path(config)
    if not os.path.exists(path):
        return LockInfo(path=path, present=False)
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
        held = json.loads(raw)
        if not isinstance(held, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        # A half-written or corrupted lock is present and unreadable. It is
        # never treated as valid, and never treated as absent either: both
        # would be a guess, and one of them silently steamrolls a live build.
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                raw = handle.read()
        except OSError:
            raw = ""
        return LockInfo(path=path, present=True, readable=False, raw=raw)

    started = held.get("started_at")
    try:
        started = float(started)
    except (TypeError, ValueError):
        started = None
    return LockInfo(
        path=path,
        present=True,
        readable=True,
        lock_id=str(held.get("lock_id") or ""),
        pid=held.get("pid") if isinstance(held.get("pid"), int) else None,
        host=str(held.get("host") or ""),
        started_at=started,
        started_display=str(held.get("started_display") or ""),
        process_start=held.get("process_start"),
        indexer_version=str(held.get("indexer_version") or ""),
        raw=raw,
    )


class BuildLock:
    """An exclusive, advisory lock for one project's data directory.

    ``O_CREAT | O_EXCL`` is atomic on Windows and on POSIX, so two builds
    racing for the same project cannot both win. The loser is refused with an
    error naming who holds it and since when, rather than blocking: a build
    takes minutes, and a caller that waits silently for minutes looks hung.

    The lock's *contents* are published the same way the pointer is: written to
    a temporary file, then renamed into place. The exclusive create reserves
    the name; the rename makes the metadata appear whole. Without that, a
    process killed between creating the file and writing its JSON would leave
    an empty lock that a reader has to guess about -- and guessing is exactly
    what the recovery flow must never do.
    """

    def __init__(self, config):
        self.config = config
        self.path = lock_path(config)
        self.lock_id = uuid.uuid4().hex
        self.acquired = False

    def __enter__(self) -> "BuildLock":
        assert_managed_paths_are_safe(self.config)
        try:
            handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise BuildLockError(self._held_message()) from exc
        os.close(handle)
        self.acquired = True

        payload = {
            "lock_id": self.lock_id,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": time.time(),
            "started_display": datetime.now(timezone.utc).isoformat(),
            "process_start": process_start_time(os.getpid()),
            "indexer_version": __version__,
        }
        staging = f"{self.path}.{os.getpid()}.tmp"
        with open(staging, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=1, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, self.path)
        return self

    def __exit__(self, *exc_info) -> None:
        if not self.acquired:
            return
        self.acquired = False
        try:
            os.remove(self.path)
        except OSError:
            pass

    def _held_message(self) -> str:
        info = read_lock(self.config)
        if not info.readable:
            return (
                f"another build already holds {self.path}, and its contents "
                "could not be read. Inspect it with `unlock --show`; it is not "
                "removed automatically because an unreadable lock is not the "
                "same as an absent one."
            )
        age = info.age_seconds
        age_text = f"{age / 60:.1f} minutes ago" if age is not None else "at an unknown time"
        state = {
            "running": "that process is still running",
            "gone": "that process is gone",
            "pid_reused": "that pid now belongs to a different process",
            "unverifiable": "this machine cannot verify whether it is still running",
        }[info.holder_state]
        return (
            f"another build already holds {self.path}. Lock {info.lock_id} was "
            f"taken by pid {info.pid} on {info.host} {age_text}; {state}. "
            f"Run `python -m odoo_kg unlock --show` to inspect it, or "
            f"`unlock --lock-id {info.lock_id}` to release it if it is abandoned."
        )


# ---------------------------------------------------------------------------
# reading the pointer
# ---------------------------------------------------------------------------


def read_pointer(config) -> Pointer:
    """Parse ``CURRENT``, or raise :class:`PublishError` saying why not."""
    path = pointer_path(config)
    if not os.path.isfile(path):
        raise NoGraphError(
            f"no published generation: {path} does not exist. Run "
            "`python -m odoo_kg index` to build and publish one."
        )
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise NoGraphError(
            f"{path} is not a readable pointer: {exc}. It names which graph to "
            "read; rebuild with `python -m odoo_kg index` to write a new one."
        ) from exc
    if not isinstance(raw, dict):
        raise NoGraphError(f"{path} does not contain a pointer object")

    missing = [
        key for key in ("generation", "project_id", "schema_version", "run_id",
                        "published_at", "db_sha256", "db_size")
        if key not in raw
    ]
    if missing:
        raise NoGraphError(f"{path} is missing required field(s) {sorted(missing)}")
    if not _valid_generation_name(raw["generation"]):
        raise WrongGraphError(
            f"{path} names generation {raw['generation']!r}, which is not a "
            "generation name. A pointer names a generation, never a path."
        )
    try:
        schema = int(raw["schema_version"])
        size = int(raw["db_size"])
    except (TypeError, ValueError) as exc:
        raise NoGraphError(f"{path} has a non-numeric field: {exc}") from exc

    return Pointer(
        generation=raw["generation"],
        project_id=str(raw["project_id"]),
        schema_version=schema,
        indexer_version=str(raw.get("indexer_version") or ""),
        run_id=str(raw["run_id"]),
        base_commit=raw.get("base_commit"),
        branch=raw.get("branch"),
        dirty=raw.get("dirty"),
        published_at=str(raw["published_at"]),
        db_sha256=str(raw["db_sha256"]),
        db_size=size,
        source=path,
    )


def resolve(config, *, expect_schema: int | None = SCHEMA_VERSION) -> Resolution:
    """Which database file to read for this project.

    A published pointer wins. A data directory from before generations existed
    falls back to its ``graph.db``, which is read and never modified. Anything
    else is an error that says what to do.
    """
    notes: list[str] = []
    if os.path.isfile(pointer_path(config)):
        pointer = read_pointer(config)
        if pointer.project_id != config.project_id:
            raise WrongGraphError(
                f"{pointer.source} publishes project {pointer.project_id!r} but "
                f"this configuration is for {config.project_id!r}. Point "
                "--config at the matching project."
            )
        if expect_schema is not None and pointer.schema_version != expect_schema:
            raise WrongGraphError(
                f"{pointer.source} publishes schema version "
                f"{pointer.schema_version} and this build reads {expect_schema}. "
                "Rebuild with this build, or point --config at the project this "
                "build owns."
            )
        db = _assert_inside_data_dir(
            config, generation_db(config, pointer.generation),
            f"generation {pointer.generation}",
        )
        if not os.path.isfile(db):
            raise NoGraphError(
                f"{pointer.source} publishes {pointer.generation}, but "
                f"{db} is not there. The generation was removed or never "
                "finished; rebuild with `python -m odoo_kg index`."
            )
        return Resolution(db_path=db, kind="generation", pointer=pointer, notes=notes)

    legacy = config.db_path.display
    if os.path.isfile(legacy):
        notes.append(
            f"no {POINTER_FILENAME} in {config.data_dir.display}; reading the "
            f"pre-generation database at {legacy}. It is read as it is and "
            "never modified. The next `index` run publishes a generation "
            "beside it and leaves this file alone."
        )
        return Resolution(db_path=legacy, kind="legacy", notes=notes)

    raise NoGraphError(
        f"no graph database for {config.project_id}: there is no published "
        f"generation in {config.data_dir.display} and no {legacy}. "
        "Run `python -m odoo_kg index` first."
    )


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------


def list_generations(config) -> list[dict]:
    """Every generation directory on disk, newest first. Deletes nothing."""
    root = generations_dir(config)
    if not os.path.isdir(root):
        return []
    published = None
    try:
        published = read_pointer(config).generation
    except PublishError:
        published = None

    found = []
    for name in sorted(os.listdir(root)):
        if not _valid_generation_name(name):
            continue
        db = generation_db(config, name)
        row = {
            "generation": name,
            "published": name == published,
            "db_path": db,
            "present": os.path.isfile(db),
            "size": None,
            "modified_ns": None,
            "sidecars": sorted(
                suffix for suffix in SIDECAR_SUFFIXES if os.path.exists(db + suffix)
            ),
        }
        if row["present"]:
            info = os.stat(db)
            row["size"] = info.st_size
            row["modified_ns"] = info.st_mtime_ns
        found.append(row)
    found.sort(key=lambda row: (row["modified_ns"] or 0), reverse=True)
    return found


# ---------------------------------------------------------------------------
# publishing
# ---------------------------------------------------------------------------


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def check_generation(config, db_path: str, *, expect_run_id: str,
                     expect_commit: str | None) -> list[str]:
    """Everything that must be true before a build may be published.

    Returns the list of problems; empty means publishable. The checks open the
    candidate read-only and are run before the pointer moves, so a build that
    fails any of them simply never becomes current.
    """
    problems: list[str] = []
    if not os.path.isfile(db_path):
        return [f"{db_path} was not written"]

    leftover = [
        db_path + suffix for suffix in SIDECAR_SUFFIXES
        if os.path.exists(db_path + suffix)
    ]
    if leftover:
        problems.append(
            "sidecar file(s) left beside the new database: "
            + ", ".join(leftover)
            + ". A published generation must be one self-contained file."
        )

    stray = [
        name for name in os.listdir(os.path.dirname(db_path))
        if name != GENERATION_DB_FILENAME
    ]
    if stray:
        problems.append(
            f"unexpected file(s) in the generation directory: {sorted(stray)}"
        )

    from .readonly_db import open_readonly  # local: avoids an import cycle

    try:
        connection = open_readonly(db_path)
    except OdooKgError as exc:
        return problems + [f"the new database cannot be opened read-only: {exc}"]
    try:
        result = connection.execute("PRAGMA integrity_check").fetchall()
        verdict = [row[0] for row in result]
        if verdict != ["ok"]:
            problems.append(f"SQLite integrity_check said {verdict}")

        row = connection.execute(
            "SELECT value FROM kg_meta WHERE key = 'schema_version'"
        ).fetchone()
        found_schema = int(row[0]) if row else None
        if found_schema != SCHEMA_VERSION:
            problems.append(
                f"schema version is {found_schema}, expected {SCHEMA_VERSION}"
            )

        project = connection.execute(
            "SELECT project_id, base_commit, run_id FROM project"
        ).fetchall()
        ids = {row[0] for row in project}
        if ids != {config.project_id}:
            problems.append(
                f"project row(s) name {sorted(ids)}, expected "
                f"[{config.project_id!r}]"
            )
        run_ids = {row[2] for row in project}
        if run_ids != {expect_run_id}:
            problems.append(
                f"project row names run {sorted(run_ids)}, expected "
                f"[{expect_run_id!r}]"
            )
        commits = {row[1] for row in project}
        if expect_commit is not None and commits != {expect_commit}:
            problems.append(
                f"project row records commit {sorted(commits)}, expected "
                f"[{expect_commit!r}]"
            )
    except sqlite3.DatabaseError as exc:
        problems.append(f"the new database could not be queried: {exc}")
    finally:
        connection.close()
    return problems


def write_pointer_atomically(config, pointer: Pointer) -> str:
    """Write ``CURRENT`` through a temporary file and one atomic rename.

    The temporary file is written in the same directory so the rename stays on
    one volume, which is what makes it atomic. It carries the process id so two
    processes cannot collide on the temporary name either.
    """
    final = pointer_path(config)
    staging = f"{final}.{os.getpid()}.tmp"
    payload = json.dumps(pointer.as_dict(), indent=1, sort_keys=True) + "\n"
    with open(staging, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())

    last: OSError | None = None
    for _ in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(staging, final)
            return final
        except PermissionError as exc:      # Windows: a reader has it open
            last = exc
            time.sleep(_REPLACE_PAUSE_SECONDS)
        except OSError as exc:
            last = exc
            break
    try:
        os.remove(staging)
    except OSError:
        pass
    raise PublishError(
        f"could not replace {final}: {last}. The previously published "
        "generation is unchanged."
    )


def publish(config, *, generation: str, run_id: str, git, started_at: str) -> Pointer:
    """Check the built generation and, if it is sound, make it current.

    The pointer moves last and only once. Everything before it is a read.
    """
    # Re-checked here, immediately before the pointer moves, not only when the
    # build started. A junction dropped into the tree during a build would
    # otherwise be seen for the first time by the write itself.
    assert_managed_paths_are_safe(config, generation=generation)
    db = _assert_inside_data_dir(
        config, generation_db(config, generation), f"generation {generation}"
    )
    problems = check_generation(
        config, db, expect_run_id=run_id,
        expect_commit=getattr(git, "base_commit", None),
    )
    if problems:
        raise PublishError(
            "refusing to publish "
            f"{generation}: " + "; ".join(problems)
            + ". The previously published generation is unchanged."
        )
    pointer = Pointer(
        generation=generation,
        project_id=config.project_id,
        schema_version=SCHEMA_VERSION,
        indexer_version=__version__,
        run_id=run_id,
        base_commit=getattr(git, "base_commit", None),
        branch=getattr(git, "branch", None),
        dirty=None if getattr(git, "dirty", None) is None else int(git.dirty),
        published_at=started_at,
        db_sha256=_sha256(db),
        db_size=os.path.getsize(db),
    )
    pointer.source = write_pointer_atomically(config, pointer)
    return pointer


def release_lock(config, *, lock_id: str, force: bool = False) -> LockInfo:
    """Release an abandoned build lock. Refuses every case it cannot be sure of.

    Deleting a lock is deleting another build's right to finish, so this is
    deliberately hard to do by accident:

    * the caller must supply the lock's exact id, which they can only get by
      looking at it, so a habitual ``--force`` cannot remove a lock nobody
      read;
    * a lock whose holder is running on this machine is never released, force
      or not -- there is no situation where killing a live build's lock from
      another process is the right move;
    * a lock from another host, or one this platform cannot check, needs
      ``force``, because "I cannot see that process" is not "that process is
      gone".

    Touches nothing but the lock file. No database, no generation, no pointer.
    """
    info = read_lock(config)
    if not info.present:
        raise BuildLockError(
            f"no build lock at {info.path}; there is nothing to release."
        )
    if not info.readable:
        if not force:
            raise BuildLockError(
                f"{info.path} exists but cannot be parsed, so its id cannot be "
                "matched. If no build is running, release it with --force "
                "--lock-id unreadable."
            )
        if lock_id != "unreadable":
            raise BuildLockError(
                "an unreadable lock is released with --lock-id unreadable, "
                "which is deliberately awkward: you are removing a lock you "
                "could not identify."
            )
        os.remove(info.path)
        return info

    if lock_id != info.lock_id:
        raise BuildLockError(
            f"lock id does not match: this lock is {info.lock_id}, you gave "
            f"{lock_id!r}. Run `unlock --show` and pass the id it prints."
        )

    state = info.holder_state
    if state == "running":
        raise BuildLockError(
            f"refusing to release lock {info.lock_id}: pid {info.pid} on "
            f"{info.host} is still running, so a build is in progress. Wait "
            "for it, or stop that process first."
        )
    if state == "unverifiable" and not force:
        where = (
            f"host {info.host}" if info.host and not info.is_local
            else "this platform"
        )
        raise BuildLockError(
            f"refusing to release lock {info.lock_id}: it was taken on {where} "
            f"by pid {info.pid}, and whether that process is still running "
            "cannot be checked from here. If you have confirmed no build is "
            "running, repeat with --force."
        )
    os.remove(info.path)
    return info


def publish_existing(config, generation: str) -> Pointer:
    """Publish a generation that was already built, without indexing again.

    The recovery path for a build whose pointer swap was blocked: the
    generation is finished and checked, only the last step failed. This repeats
    that last step and nothing else.

    It runs the *same* validation as a fresh publish -- integrity, project,
    schema, commit, run id, sidecars, stray files -- against whatever is on
    disk, so a generation that has been tampered with, half-written, or built
    for a different project cannot be promoted by asking nicely. It takes the
    same build lock, so it cannot race an index run. It never opens the
    generation's database for writing.
    """
    if not _valid_generation_name(generation):
        raise PublishError(
            f"{generation!r} is not a generation name. Pass the exact id from "
            "`generations`, of the form gen-<run id>."
        )
    with BuildLock(config):
        assert_managed_paths_are_safe(config, generation=generation)
        db = _assert_inside_data_dir(
            config, generation_db(config, generation), f"generation {generation}"
        )
        if not os.path.isfile(db):
            raise PublishError(
                f"{generation} has no database at {db}. Only a finished "
                "generation can be published; run `index` to build one."
            )
        # The run id and commit come from the generation itself, because there
        # is no build here to compare against. Everything else is still
        # checked, and the two are cross-checked against each other below.
        from .readonly_db import GraphUnavailableError, open_readonly

        try:
            connection = open_readonly(db)
        except GraphUnavailableError as exc:
            # Unreadable, truncated, or not a graph. Reported as a refusal to
            # publish rather than as "the graph is unavailable": the caller
            # asked to promote this file, and the answer is no.
            raise PublishError(
                f"refusing to publish {generation}: {exc}. The previously "
                "published generation is unchanged."
            ) from exc
        try:
            row = connection.execute(
                "SELECT project_id, base_commit, run_id FROM project"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise PublishError(
                f"refusing to publish {generation}: its project table could "
                f"not be read ({exc}). The previously published generation is "
                "unchanged."
            ) from exc
        finally:
            connection.close()
        if len(row) != 1:
            raise PublishError(
                f"{generation} holds {len(row)} project rows; a publishable "
                "generation holds exactly one."
            )
        found_project, found_commit, found_run = row[0]
        if found_project != config.project_id:
            raise PublishError(
                f"refusing to publish {generation}: it was built for project "
                f"{found_project!r} and this configuration is "
                f"{config.project_id!r}."
            )
        if generation != generation_name(found_run):
            raise PublishError(
                f"refusing to publish {generation}: its project row names run "
                f"{found_run!r}, which is generation "
                f"{generation_name(found_run)}. The directory and its contents "
                "disagree about which build this is."
            )

        problems = check_generation(
            config, db, expect_run_id=found_run, expect_commit=found_commit
        )
        if problems:
            raise PublishError(
                f"refusing to publish {generation}: " + "; ".join(problems)
                + ". The previously published generation is unchanged."
            )

        pointer = Pointer(
            generation=generation,
            project_id=config.project_id,
            schema_version=SCHEMA_VERSION,
            indexer_version=__version__,
            run_id=found_run,
            base_commit=found_commit,
            branch=None,
            dirty=None,
            published_at=datetime.now(timezone.utc).isoformat(),
            db_sha256=_sha256(db),
            db_size=os.path.getsize(db),
        )
        pointer.source = write_pointer_atomically(config, pointer)
        return pointer


def verify_published(config) -> tuple[Pointer, list[str]]:
    """Re-check the published generation against its own manifest.

    Cheap enough to run any time and the only way to notice that a published
    file was changed or truncated by something outside this program.
    """
    resolution = resolve(config)
    if resolution.kind != "generation" or resolution.pointer is None:
        return (
            Pointer("", config.project_id, SCHEMA_VERSION, __version__, "", None,
                    None, None, "", "", 0, resolution.db_path),
            ["no generation is published; a pre-generation database is in use"],
        )
    pointer = resolution.pointer
    problems: list[str] = []
    size = os.path.getsize(resolution.db_path)
    if size != pointer.db_size:
        problems.append(f"size is {size}, manifest says {pointer.db_size}")
    digest = _sha256(resolution.db_path)
    if digest != pointer.db_sha256:
        problems.append(
            f"sha256 is {digest}, manifest says {pointer.db_sha256}"
        )
    problems.extend(
        check_generation(
            config, resolution.db_path,
            expect_run_id=pointer.run_id, expect_commit=pointer.base_commit,
        )
    )
    return pointer, problems
