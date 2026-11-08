"""Filesystem traversal.

Walks each include root top-down, pruning excluded directories *before*
descending into them, and refusing to cross any reparse point. Every surviving
file is checked for containment twice: once on its literal path and once on its
fully resolved path, so neither a crafted relative path nor a symlinked parent
can pull content in from outside the configured roots.

Only hashes and metadata leave this module. File contents are read to compute
digests and then discarded.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Iterator

from .config import Config
from .errors import ContainmentError, SymlinkError
from .exclusions import ExclusionRules
from .safety import open_source_readonly
from .winpath import (
    NormalizedPath,
    assert_real_within_roots,
    is_reparse_point,
    is_within,
    normalize,
    relative_display,
    relative_key,
)

MANIFEST_FILENAME = "__manifest__.py"


@dataclass
class ScannedFile:
    """One file that passed every exclusion and containment check."""

    path: NormalizedPath
    include_root: NormalizedPath
    rel_key: str
    rel_display: str
    size_bytes: int | None = None
    content_sha: str | None = None
    git_blob_sha: str | None = None
    hash_state: str = "pending"

    @property
    def name(self) -> str:
        return os.path.basename(self.path.display)


@dataclass
class ScanResult:
    files: list[ScannedFile] = field(default_factory=list)
    excluded_count: int = 0
    excluded_dirs: list[tuple[str, str, str]] = field(default_factory=list)
    diagnostics: list[tuple] = field(default_factory=list)

    def manifests(self) -> list[ScannedFile]:
        return [f for f in self.files if f.name.lower() == MANIFEST_FILENAME]


def scan(config: Config) -> ScanResult:
    """Traverse every include root and return the surviving file inventory."""
    result = ScanResult()
    root_keys = config.include_root_keys
    seen: set[str] = set()

    for root in config.include_roots:
        if not os.path.isdir(root.display):
            result.diagnostics.append(
                ("error", "include_root", "include root does not exist", root.display)
            )
            continue
        if is_reparse_point(root.display):
            result.diagnostics.append(
                ("error", "symlink", "include root is a reparse point; skipped", root.display)
            )
            continue
        for scanned in _walk_root(config, root, root_keys, result):
            if scanned.path.key in seen:
                continue
            seen.add(scanned.path.key)
            _hash_file(scanned, config, result)
            result.files.append(scanned)

    result.files.sort(key=lambda f: f.path.key)
    return result


def _walk_root(
    config: Config,
    root: NormalizedPath,
    root_keys: list[str],
    result: ScanResult,
) -> Iterator[ScannedFile]:
    rules = config.exclusions
    # followlinks=False keeps os.walk from recursing through symlinked
    # directories; the explicit reparse-point pruning below covers junctions,
    # which os.walk does not treat as links on every Python version.
    for dirpath, dirnames, filenames in os.walk(root.display, topdown=True, followlinks=False):
        current = normalize(dirpath)
        try:
            rel_dir_key = relative_key(root.key, current.key)
        except ContainmentError:
            dirnames[:] = []
            result.diagnostics.append(
                ("error", "containment", "directory escaped include root", dirpath)
            )
            continue

        _prune_directories(dirpath, dirnames, rel_dir_key, rules, result)

        for filename in sorted(filenames):
            decision = rules.check_file(filename)
            if decision.excluded:
                result.excluded_count += 1
                continue
            full = os.path.join(dirpath, filename)
            try:
                scanned = _accept_file(full, root, root_keys)
            except SymlinkError as exc:
                result.excluded_count += 1
                result.diagnostics.append(("warning", "symlink", str(exc), full))
                continue
            except ContainmentError as exc:
                result.excluded_count += 1
                result.diagnostics.append(("error", "containment", str(exc), full))
                continue
            if scanned is not None:
                yield scanned


def _prune_directories(
    dirpath: str,
    dirnames: list[str],
    rel_dir_key: str,
    rules: ExclusionRules,
    result: ScanResult,
) -> None:
    """Drop excluded and reparse-point subdirectories in place.

    Mutating ``dirnames`` is what makes exclusion happen *before* traversal:
    os.walk never enters a directory removed here, so nothing inside ``.git``
    or ``venv`` is ever opened, listed, or hashed.
    """
    kept: list[str] = []
    for name in sorted(dirnames):
        child_rel = f"{rel_dir_key}/{name.lower()}" if rel_dir_key else name.lower()
        decision = rules.check_dir(name, child_rel)
        if decision.excluded:
            result.excluded_dirs.append((os.path.join(dirpath, name), decision.layer, decision.pattern))
            continue
        full = os.path.join(dirpath, name)
        if is_reparse_point(full):
            result.excluded_dirs.append((full, "reparse_point", ""))
            result.diagnostics.append(
                ("warning", "symlink", "directory is a reparse point; not traversed", full)
            )
            continue
        kept.append(name)
    dirnames[:] = kept


def _accept_file(
    full_path: str, root: NormalizedPath, root_keys: list[str]
) -> ScannedFile | None:
    path = normalize(full_path)
    if is_reparse_point(path.display):
        raise SymlinkError(f"refusing to index reparse point: {path.display}")
    if not is_within(root.key, path.key):
        raise ContainmentError(f"file escapes include root: {path.display}")
    # Second, independent check: where does this path actually land?
    assert_real_within_roots(path, root_keys)
    if not os.path.isfile(path.display):
        return None
    return ScannedFile(
        path=path,
        include_root=root,
        rel_key=relative_key(root.key, path.key),
        rel_display=relative_display(root.display, path.display),
    )


def _hash_file(scanned: ScannedFile, config: Config, result: ScanResult) -> None:
    """Compute sha256 and the git blob sha1 in a single pass over the bytes."""
    try:
        size = os.path.getsize(scanned.path.display)
    except OSError as exc:
        scanned.hash_state = "unreadable"
        result.diagnostics.append(("warning", "io", str(exc), scanned.path.display))
        return
    scanned.size_bytes = size
    if size > config.max_hash_bytes:
        scanned.hash_state = "skipped_large"
        return

    sha256 = hashlib.sha256()
    # Git's object id is sha1 over "blob <size>\0" followed by the contents.
    # Computing it here lets the graph compare against the index without
    # shelling out to git once per file.
    blob = hashlib.sha1(b"blob %d\0" % size)
    try:
        with open_source_readonly(scanned.path.display) as handle:
            while chunk := handle.read(1024 * 1024):
                sha256.update(chunk)
                blob.update(chunk)
    except OSError as exc:
        scanned.hash_state = "unreadable"
        result.diagnostics.append(("warning", "io", str(exc), scanned.path.display))
        return
    scanned.content_sha = sha256.hexdigest()
    scanned.git_blob_sha = blob.hexdigest()
    scanned.hash_state = "hashed"


def read_manifest_bytes(scanned: ScannedFile) -> bytes:
    with open_source_readonly(scanned.path.display) as handle:
        return handle.read()
