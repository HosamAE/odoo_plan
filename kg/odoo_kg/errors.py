"""Exception hierarchy.

Every error the indexer raises deliberately is an OdooKgError, so a caller can
tell "the tool refused" apart from "the tool crashed".
"""


class OdooKgError(Exception):
    """Base class for all deliberate failures."""


class ConfigError(OdooKgError):
    """The configuration file is missing, malformed, or forbidden."""


class UnsafePathError(OdooKgError):
    """A path is malformed, escapes an include root, or crosses a symlink."""


class ContainmentError(UnsafePathError):
    """A path resolved outside every configured include root."""


class SymlinkError(UnsafePathError):
    """A path component is a symlink, junction, or other reparse point."""


class WriteGuardError(OdooKgError):
    """A write was attempted outside the approved output locations."""


class ManifestError(OdooKgError):
    """A manifest could not be parsed as a Python literal dict."""


class SchemaVersionError(OdooKgError):
    """The database on disk was written by a different schema version."""


class UnsupportedPlatformError(OdooKgError):
    """The runtime does not meet the documented minimum requirements."""
