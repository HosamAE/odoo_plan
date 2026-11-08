"""Git and working-tree baseline.

The graph records *which* revision it describes and *how far the working tree
has drifted from it*. That is what makes a stale graph detectable later.

Git is invoked with an explicit argument list and ``shell=False``; nothing here
is interpolated into a command line. Every call is read-only -- no ``git add``,
no ``git stash``, no index refresh -- because the indexed repository must not
change. If git is unavailable or the directory is not a repository, the
baseline degrades to ``available=False`` and the graph is still built from the
working tree; it simply cannot say what commit it corresponds to.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from .winpath import to_key

_GIT_TIMEOUT_SECONDS = 120

#: Read-only git invocations only.
#: `--no-optional-locks` matters more than it looks: plain `git status`
#: refreshes and rewrites `.git/index` as a side effect. The indexed repository
#: must come out of an index run byte-for-byte unchanged, including .git.
#: `-c core.quotepath=false` keeps non-ASCII paths verbatim rather than
#: octal-escaped.
_BASE_ARGS = ["git", "--no-optional-locks", "-c", "core.quotepath=false"]


@dataclass
class GitBaseline:
    """What git knows about the tree at index time."""

    available: bool = False
    branch: str | None = None
    base_commit: str | None = None
    dirty: bool | None = None
    error: str | None = None
    #: normalized absolute keys of files git reports as changed vs HEAD
    modified_keys: set[str] = field(default_factory=set)
    #: normalized absolute keys of files git does not track
    untracked_keys: set[str] = field(default_factory=set)
    #: normalized absolute key -> blob sha recorded in the index
    index_blobs: dict[str, str] = field(default_factory=dict)

    def state_for(self, file_key: str) -> str:
        """Working-tree state of one file: clean/modified/untracked/unknown."""
        if not self.available:
            return "unknown"
        if file_key in self.modified_keys:
            return "modified"
        if file_key in self.untracked_keys:
            return "untracked"
        if file_key in self.index_blobs:
            return "clean"
        # Inside the repo, tracked by nothing and reported by nothing: this is
        # a git-ignored file. It is real content on disk but has no baseline.
        return "ignored"

    def blob_for(self, file_key: str) -> str | None:
        return self.index_blobs.get(file_key)


def collect(repo_root_display: str, repo_root_key: str) -> GitBaseline:
    """Gather the baseline for ``repo_root``. Never raises on git failure."""
    baseline = GitBaseline()

    inside = run_read_only(["rev-parse", "--is-inside-work-tree"], repo_root_display)
    if inside is None or inside.strip() != "true":
        baseline.error = "not a git working tree (or git unavailable)"
        return baseline

    toplevel = run_read_only(["rev-parse", "--show-toplevel"], repo_root_display)
    if toplevel is not None:
        top_key = to_key(toplevel.strip().replace("/", "\\"))
        if top_key != repo_root_key:
            baseline.error = (
                f"repo_root is not the git top level ({toplevel.strip()}); "
                "paths outside the configured root are ignored"
            )

    baseline.available = True
    head = run_read_only(["rev-parse", "HEAD"], repo_root_display)
    baseline.base_commit = head.strip() if head else None
    branch = run_read_only(["rev-parse", "--abbrev-ref", "HEAD"], repo_root_display)
    baseline.branch = branch.strip() if branch else None

    _load_index(baseline, repo_root_display, repo_root_key)
    _load_status(baseline, repo_root_display, repo_root_key)
    baseline.dirty = bool(baseline.modified_keys or baseline.untracked_keys)
    return baseline


def _load_index(baseline: GitBaseline, cwd: str, root_key: str) -> None:
    """Record the blob sha the index holds for every tracked file."""
    out = run_read_only(["ls-files", "-s", "-z"], cwd)
    if out is None:
        return
    for entry in out.split("\0"):
        if not entry:
            continue
        # "<mode> <sha> <stage>\t<path>"
        meta, _, rel = entry.partition("\t")
        if not rel:
            continue
        parts = meta.split()
        if len(parts) < 2:
            continue
        baseline.index_blobs[_abs_key(root_key, rel)] = parts[1]


def _load_status(baseline: GitBaseline, cwd: str, root_key: str) -> None:
    """Record which files differ from HEAD or are untracked.

    ``--untracked-files=all`` lists individual files rather than collapsing a
    new directory into one entry, which the file-level graph needs. ``--no-
    renames`` keeps the -z record shape uniform (one path per entry).
    """
    out = run_read_only(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"],
        cwd,
    )
    if out is None:
        return
    for entry in out.split("\0"):
        if len(entry) < 4:
            continue
        code = entry[:2]
        rel = entry[3:]
        if not rel:
            continue
        key = _abs_key(root_key, rel)
        if code == "??":
            baseline.untracked_keys.add(key)
        else:
            baseline.modified_keys.add(key)


def _abs_key(root_key: str, rel_posix: str) -> str:
    return f"{root_key.rstrip('/')}/{rel_posix.strip('/').lower()}"


def run_read_only(args: list[str], cwd: str) -> str | None:
    """Run a read-only git command, returning stdout or None on any failure."""
    try:
        completed = subprocess.run(
            _BASE_ARGS + args,
            cwd=cwd,
            capture_output=True,
            shell=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", "surrogateescape")
