"""Runtime write guard.

The indexer is allowed to create exactly one thing: the graph database (plus
SQLite's own sidecar files) inside the approved local data directory. Every
write goes through :func:`assert_write_target_allowed` first, so a bug in path
handling surfaces as a refusal rather than as a modified source tree.
"""

from __future__ import annotations

import os

from .config import Config, allowed_data_root
from .errors import WriteGuardError
from .winpath import NormalizedPath, is_within, normalize, real_key

#: Directory names that must never appear anywhere in a write target.
FORBIDDEN_COMPONENTS = frozenset({".git", ".claude"})


def resolved_data_root(config: Config) -> str:
    """The project's data directory, followed to its real target, as a key.

    Resolved rather than taken literally, once, and used as the root every
    write is measured against. That is what makes redirection legitimate: an
    organisation that redirects ``%LOCALAPPDATA%`` -- roaming profiles, folder
    redirection, a developer whose data directory is a junction onto another
    drive -- has a data directory whose real target is somewhere else, and
    every write of ours lands under that same real target. Comparing resolved
    to resolved accepts all of it.

    What it does not accept is a reparse point *inside* the tree that leads out
    of it, because then the resolved target is no longer under the resolved
    root. The distinction is exactly right: redirecting the root is the
    administrator's decision, and a junction under it that escapes is not.
    """
    return real_key(config.data_dir.display)


def assert_write_target_allowed(target: str | NormalizedPath, config: Config) -> NormalizedPath:
    """Raise unless ``target`` is a legal place for generated data.

    Two containment checks, and both are needed. The lexical one rejects a path
    that does not even look contained. The resolved one rejects a path that
    looks contained and is not -- ``data_dir/generations`` replaced by a
    junction pointing at ``C:\\Windows`` passes the first check and fails the
    second. A single check would be a check for one of the two mistakes.
    """
    path = target if isinstance(target, NormalizedPath) else normalize(target)

    for component in path.key.split("/"):
        if component in FORBIDDEN_COMPONENTS:
            raise WriteGuardError(
                f"refusing to write inside a {component} directory: {path.display}"
            )

    if is_within(config.repo_root.key, path.key):
        raise WriteGuardError(
            f"refusing to write inside repo_root: {path.display}"
        )
    if config.core_root is not None and is_within(config.core_root.key, path.key):
        raise WriteGuardError(
            f"refusing to write inside core_root: {path.display}"
        )

    root = allowed_data_root()
    if not is_within(root.key, path.key):
        raise WriteGuardError(
            f"write target must be under {root.display}, got {path.display}"
        )
    if not is_within(config.data_dir.key, path.key):
        raise WriteGuardError(
            f"write target must be under the configured data_dir "
            f"{config.data_dir.display}, got {path.display}"
        )

    # The resolved check. Everything above compared the path as written; this
    # compares where it actually leads, against where the root actually leads.
    resolved_root = resolved_data_root(config)
    resolved_target = real_key(path.display)
    if not is_within(resolved_root, resolved_target):
        raise WriteGuardError(
            f"refusing to write to {path.display}: it resolves to "
            f"{resolved_target}, which is outside the data directory's real "
            f"target {resolved_root}. A reparse point or junction under "
            f"{config.data_dir.display} is leading writes out of it."
        )
    return path


def ensure_data_dir(config: Config) -> NormalizedPath:
    """Create the approved data directory if needed and return it."""
    assert_write_target_allowed(config.data_dir, config)
    os.makedirs(config.data_dir.display, exist_ok=True)
    # Checked again after creation: between the check and the mkdir the
    # directory may have appeared as a junction placed by somebody else, and
    # the first check could only see the path as written.
    assert_write_target_allowed(config.data_dir, config)
    return config.data_dir


def open_source_readonly(path: str):
    """Open a source file for binary reading.

    A single choke point for every read of the indexed tree: the mode is
    hard-coded, so no caller can accidentally open a source file for writing.
    """
    return open(path, "rb")
