"""Windows path normalization, include-root containment, and symlink guards.

Windows is the only supported platform for v1, so this module leans on Windows
semantics on purpose: case-insensitive comparison, backslash/forward-slash
equivalence, extended-length (``\\\\?\\``) prefixes, and reparse points
(symlinks *and* directory junctions).

Two forms of every path are kept:

``display``
    Absolute, native separators, original casing. This is what a human reads
    and what gets handed back to an editor.
``key``
    Absolute, forward slashes, lower-cased. This is the only form ever used for
    comparison, containment, deduplication, or as a database primary key.

Comparing on ``display`` would make ``C:\\Odoo`` and ``c:\\odoo`` two different
nodes; comparing on ``key`` is what keeps a re-index idempotent.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass

from .errors import ContainmentError, SymlinkError, UnsafePathError

# FILE_ATTRIBUTE_REPARSE_POINT. Present in `stat` on Windows builds; defined
# here as a fallback so the module still imports on other platforms (the tests
# for pure string normalization stay runnable anywhere).
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

_EXTENDED_PREFIX = "\\\\?\\"
_EXTENDED_UNC_PREFIX = "\\\\?\\UNC\\"
_DEVICE_PREFIX = "\\\\.\\"


@dataclass(frozen=True, slots=True)
class NormalizedPath:
    """An absolute path in both its human form and its comparison form."""

    display: str
    key: str

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.display

    @property
    def name(self) -> str:
        return os.path.basename(self.display)


def normalize(raw: object, *, base: str | None = None) -> NormalizedPath:
    """Normalize ``raw`` into an absolute :class:`NormalizedPath`.

    ``base`` anchors relative input. Relative input with no ``base`` is
    rejected rather than silently resolved against the current working
    directory, because the CWD is not something the config controls.
    """
    if raw is None:
        raise UnsafePathError("path is None")
    try:
        text = os.fspath(raw)
    except TypeError as exc:
        raise UnsafePathError(f"not a path-like value: {raw!r}") from exc
    if not isinstance(text, str):
        text = text.decode("utf-8", "surrogateescape")

    if "\x00" in text:
        raise UnsafePathError("path contains a NUL byte")

    text = text.strip().strip('"')
    if not text:
        raise UnsafePathError("path is empty")

    if text.startswith(_DEVICE_PREFIX):
        # \\.\PhysicalDrive0 and friends are devices, never source trees.
        raise UnsafePathError(f"device namespace path is not indexable: {text!r}")

    # Strip extended-length prefixes so \\?\C:\x and C:\x compare equal.
    if text.startswith(_EXTENDED_UNC_PREFIX):
        text = "\\\\" + text[len(_EXTENDED_UNC_PREFIX):]
    elif text.startswith(_EXTENDED_PREFIX):
        text = text[len(_EXTENDED_PREFIX):]

    text = text.replace("/", "\\")

    if not os.path.isabs(text) or _is_drive_relative(text):
        if base is None:
            raise UnsafePathError(f"relative path with no base: {raw!r}")
        text = os.path.join(base, text)

    # abspath collapses "." and ".." without touching the filesystem, so a
    # traversal attempt is neutralised before any containment check runs.
    display = os.path.abspath(text)
    display = display.rstrip("\\") or display
    if _is_bare_drive(display):
        display += "\\"

    return NormalizedPath(display=display, key=to_key(display))


def _is_drive_relative(text: str) -> bool:
    """True for ``C:foo`` — a drive with no root, which is CWD-relative."""
    return len(text) >= 2 and text[1] == ":" and (len(text) == 2 or text[2] != "\\")


def _is_bare_drive(text: str) -> bool:
    return len(text) == 2 and text[1] == ":"


def to_key(path: str) -> str:
    """Return the canonical comparison form of an already-absolute path."""
    # Equivalent to os.path.normcase on Windows (lower-case, unify separators),
    # spelled out so the result is identical if a test runs on another host.
    unified = path.replace("\\", "/").lower()
    trimmed = unified.rstrip("/")
    return trimmed or unified


def is_within(root_key: str, candidate_key: str) -> bool:
    """True when ``candidate_key`` is ``root_key`` or lives underneath it.

    The trailing-separator dance is the point of this function: a naive
    ``startswith`` would accept ``c:/repo-evil`` as being inside ``c:/repo``.
    """
    root = root_key.rstrip("/")
    candidate = candidate_key.rstrip("/")
    if candidate == root:
        return True
    return candidate.startswith(root + "/")


def assert_within_roots(path: NormalizedPath, root_keys: list[str]) -> str:
    """Return the include root containing ``path``, or raise."""
    for root_key in root_keys:
        if is_within(root_key, path.key):
            return root_key
    raise ContainmentError(
        f"path escapes every include root: {path.display} (roots: {root_keys})"
    )


def relative_key(root_key: str, path_key: str) -> str:
    """Return ``path_key`` expressed relative to ``root_key`` (posix, lower)."""
    root = root_key.rstrip("/")
    if path_key == root:
        return ""
    if not path_key.startswith(root + "/"):
        raise ContainmentError(f"{path_key!r} is not under {root!r}")
    return path_key[len(root) + 1:]


def relative_display(root_display: str, path_display: str) -> str:
    """Relative path preserving original casing, using forward slashes."""
    rel = os.path.relpath(path_display, root_display)
    return rel.replace("\\", "/")


def is_reparse_point(path: str) -> bool:
    """True for a symlink, a directory junction, or any other reparse point.

    ``os.path.islink`` alone is not enough on Windows: junctions created with
    ``mklink /J`` are not symlinks, and older Python versions do not report
    them as links. The file-attribute check is the reliable signal.
    """
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None and isjunction(path):
        return True
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def assert_no_reparse_point(path: str) -> None:
    if is_reparse_point(path):
        raise SymlinkError(f"refusing to follow reparse point: {path}")


def real_key(path: str) -> str:
    """Fully resolved comparison key (symlinks followed, short names expanded).

    Used as the second half of the containment check: the literal path may look
    contained while the target it resolves to is not.
    """
    try:
        resolved = os.path.realpath(path)
    except OSError:
        resolved = os.path.abspath(path)
    return to_key(os.path.abspath(resolved))


def assert_real_within_roots(path: NormalizedPath, root_keys: list[str]) -> None:
    """Verify the *resolved* target is still inside an include root.

    A symlink is normally rejected before we get here; this catches the case
    where a parent directory is a reparse point the scanner never inspected.
    """
    resolved = real_key(path.display)
    for root_key in root_keys:
        if is_within(root_key, resolved):
            return
    raise ContainmentError(
        f"path resolves outside every include root: "
        f"{path.display} -> {resolved} (roots: {root_keys})"
    )
