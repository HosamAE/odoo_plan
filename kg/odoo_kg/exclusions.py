"""Exclusion rules applied *before* traversal.

Three independent layers, all case-insensitive because the target platform is
Windows:

1. Directory names pruned wherever they appear (``.git``, ``venv``, ...).
2. Relative directory paths pruned at a specific position (``static/lib``).
3. File-name glob patterns (``*.conf``, ``credentials*``, media, binaries).

The built-in sets are mandatory. Configuration may *add* patterns but can never
remove one, so no config edit can accidentally pull ``.git`` or a ``.conf``
secret into the graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase

#: Directory names pruned anywhere in the tree.
BUILTIN_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".claude",
        ".git",
        "venv",
        ".venv",
        "env",
        "__pycache__",
        "node_modules",
        "filestore",
        "data_dir",
        "sessions",
    }
)

#: Relative directory paths (posix, lower-case) pruned when matched as a
#: trailing segment pair, e.g. ``any/module/static/lib``.
BUILTIN_DIR_PATH_SUFFIXES: tuple[str, ...] = ("static/lib",)

#: Secret-bearing, archive, dump, log and translation file patterns.
BUILTIN_FILE_GLOBS: tuple[str, ...] = (
    ".env",
    ".env*",
    "*.conf",
    "*.cfg",
    "*.ini",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "*.crt",
    "*.cer",
    "*.jks",
    "*.keystore",
    "credentials*",
    "secrets*",
    "*.sql",
    "*.dump",
    "*.bak",
    "*.zip",
    "*.tar.gz",
    "*.tgz",
    "*.tar",
    "*.gz",
    "*.bz2",
    "*.xz",
    "*.7z",
    "*.rar",
    "*.log",
    "*.po",
    "*.pot",
    "*.mo",
)

#: Binary and media extensions. Kept separate from the glob list so the
#: diagnostic reason distinguishes "looked like a secret" from "is a blob".
BUILTIN_BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        # executables / compiled artefacts
        ".exe", ".dll", ".so", ".dylib", ".pyd", ".pyc", ".pyo", ".obj",
        ".lib", ".a", ".o", ".bin", ".class", ".jar", ".war", ".wasm",
        ".msi", ".cab", ".apk", ".deb", ".rpm",
        # images
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".icns", ".webp",
        ".tif", ".tiff", ".psd", ".ai", ".eps", ".svg", ".svgz",
        # audio / video
        ".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma",
        ".mp4", ".avi", ".mov", ".mkv", ".webm", ".wmv", ".flv", ".m4v",
        # fonts
        ".woff", ".woff2", ".ttf", ".otf", ".eot",
        # documents
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".odt", ".ods", ".odp", ".rtf",
        # databases
        ".db", ".sqlite", ".sqlite3", ".mdb", ".accdb",
    }
)


@dataclass(frozen=True)
class ExclusionDecision:
    """Why a path was excluded. ``excluded`` False means it survived."""

    excluded: bool
    layer: str = ""
    pattern: str = ""

    def __bool__(self) -> bool:
        return self.excluded


KEEP = ExclusionDecision(False)


@dataclass(frozen=True)
class ExclusionRules:
    """Built-in rules plus optional additive rules from configuration."""

    extra_dir_names: frozenset[str] = field(default_factory=frozenset)
    extra_dir_path_suffixes: tuple[str, ...] = ()
    extra_file_globs: tuple[str, ...] = ()

    @property
    def dir_names(self) -> frozenset[str]:
        return BUILTIN_DIR_NAMES | self.extra_dir_names

    @property
    def dir_path_suffixes(self) -> tuple[str, ...]:
        return BUILTIN_DIR_PATH_SUFFIXES + self.extra_dir_path_suffixes

    @property
    def file_globs(self) -> tuple[str, ...]:
        return BUILTIN_FILE_GLOBS + self.extra_file_globs

    # -- directories ----------------------------------------------------

    def check_dir(self, name: str, rel_dir_key: str) -> ExclusionDecision:
        """Decide on a directory about to be traversed.

        ``name`` is the directory's own name; ``rel_dir_key`` is its path
        relative to the include root, posix and lower-case.
        """
        lowered = name.lower()
        if lowered in self.dir_names:
            return ExclusionDecision(True, "dir_name", lowered)
        for suffix in self.dir_path_suffixes:
            suffix = suffix.strip("/").lower()
            if rel_dir_key == suffix or rel_dir_key.endswith("/" + suffix):
                return ExclusionDecision(True, "dir_path", suffix)
        return KEEP

    # -- files ----------------------------------------------------------

    def check_file(self, name: str) -> ExclusionDecision:
        lowered = name.lower()
        for pattern in self.file_globs:
            if fnmatchcase(lowered, pattern.lower()):
                return ExclusionDecision(True, "file_glob", pattern)
        for ext in _extensions(lowered):
            if ext in BUILTIN_BINARY_EXTENSIONS:
                return ExclusionDecision(True, "binary_ext", ext)
        return KEEP

    # -- whole relative paths -------------------------------------------

    def check_relpath(self, rel_posix: str) -> ExclusionDecision:
        """Decide on a repo-relative path without touching the filesystem.

        The scanner prunes directories as it walks; this answers the same
        question for a path it never visited -- which is what freshness needs
        in order to tell "a new source file appeared" from "an excluded file
        changed".
        """
        parts = [part for part in rel_posix.replace("\\", "/").split("/") if part]
        if not parts:
            return KEEP
        *directories, filename = parts
        prefix = ""
        for name in directories:
            prefix = f"{prefix}/{name.lower()}" if prefix else name.lower()
            decision = self.check_dir(name, prefix)
            if decision.excluded:
                return decision
        return self.check_file(filename)


def _extensions(lowered_name: str) -> tuple[str, ...]:
    """Return candidate extensions, longest compound first.

    ``archive.tar.gz`` yields ``('.tar.gz', '.gz')`` so a compound extension is
    recognised even though only its tail is in the extension set.
    """
    parts = lowered_name.split(".")
    if len(parts) < 2:
        return ()
    out = []
    if len(parts) >= 3:
        out.append("." + ".".join(parts[-2:]))
    out.append("." + parts[-1])
    return tuple(out)
