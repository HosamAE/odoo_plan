"""Project configuration: loading, validation, and the secrets guard.

Configuration is TOML and is required to be secret-free. That is enforced, not
just documented: :func:`assert_no_secrets` walks the parsed document and
rejects anything that looks like a credential, so a database password or API
token cannot be introduced by a later edit without the tool refusing to run.

``tomllib`` is standard library from Python 3.11. The minimum version is
checked explicitly rather than silently falling back to a third-party parser,
because M1 must not install dependencies.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any

from .errors import ConfigError, UnsupportedPlatformError
from .exclusions import ExclusionRules
from .winpath import NormalizedPath, is_within, normalize

MIN_PYTHON = (3, 11)

if sys.version_info >= MIN_PYTHON:
    import tomllib
else:  # pragma: no cover - guarded at import time on old interpreters
    tomllib = None  # type: ignore[assignment]

#: Recognised module origins. ``unknown`` is the default and is not a failure
#: state -- it means no explicit rule claimed the module.
VALID_ORIGINS = ("project", "vendor", "core-vendored", "core", "unknown")

#: Acceptance profiles. "testing19" is the default so every existing
#: configuration keeps exactly the checks it had.
VALID_ACCEPTANCE_PROFILES = ("testing19", "generic")

#: Only these roots may hold generated data. Anything else is refused by the
#: write guard, which is what keeps the indexer out of the source trees.
_ALLOWED_DATA_ROOT_TEMPLATE = r"{localappdata}\odoo-kg\projects"

_SECRET_KEY_PATTERN = re.compile(
    r"(password|passwd|pwd|secret|token|api[_-]?key|apikey|private[_-]?key"
    r"|credential|auth|bearer|session[_-]?id|dsn|connection[_-]?string"
    r"|access[_-]?key|client[_-]?secret|salt|passphrase)",
    re.IGNORECASE,
)

_SECRET_VALUE_PATTERNS = (
    # scheme://user:password@host
    re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|pk)-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
)


def require_supported_runtime() -> None:
    """Fail fast, and legibly, on an interpreter that cannot run M1."""
    if sys.version_info < MIN_PYTHON:
        raise UnsupportedPlatformError(
            f"odoo-kg requires Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ for the "
            f"standard-library tomllib parser; running "
            f"{sys.version_info.major}.{sys.version_info.minor}. "
            "Upgrade Python; do not install a TOML backport."
        )
    if os.name != "nt":
        # v1 targets Windows only. Non-Windows is allowed for unit tests but
        # never for a real index run (see Config.validate_platform).
        pass


@dataclass(frozen=True)
class OriginRule:
    """A glob rule assigning an origin to matching module names."""

    pattern: str
    origin: str

    def matches(self, technical_name: str) -> bool:
        from fnmatch import fnmatchcase

        return fnmatchcase(technical_name.lower(), self.pattern.lower())


@dataclass
class Config:
    """Validated project configuration."""

    project_id: str
    repo_root: NormalizedPath
    core_root: NormalizedPath | None
    include_roots: list[NormalizedPath]
    data_dir: NormalizedPath
    db_path: NormalizedPath
    expected_series: str
    module_origins: dict[str, str]
    origin_rules: list[OriginRule]
    default_origin: str
    exclusions: ExclusionRules
    max_hash_bytes: int
    source_file: NormalizedPath | None = None
    #: Which acceptance checks gate a publish. "testing19" keeps the floors and
    #: named-module expectations measured on that checkout; "generic" keeps
    #: every integrity and honesty check but drops what only testing19 can meet.
    acceptance_profile: str = "testing19"
    warnings: list[str] = field(default_factory=list)

    @property
    def include_root_keys(self) -> list[str]:
        return [root.key for root in self.include_roots]

    def resolve_origin(self, technical_name: str) -> tuple[str, str]:
        """Return ``(origin, rule_description)`` for a module.

        Explicit per-module entries win over glob rules; a module matched by
        nothing keeps the default origin, which is ``unknown``.
        """
        explicit = self.module_origins.get(technical_name.lower())
        if explicit:
            return explicit, f"module_origins[{technical_name}]"
        for rule in self.origin_rules:
            if rule.matches(technical_name):
                return rule.origin, f"origin_rule[{rule.pattern}]"
        return self.default_origin, "default"

    def validate_platform(self) -> None:
        if os.name != "nt":
            raise UnsupportedPlatformError(
                "odoo-kg v1 supports Windows only; refusing to index on "
                f"os.name={os.name!r}."
            )


def allowed_data_root() -> NormalizedPath:
    """The single directory tree generated data may live in."""
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser(
        r"~\AppData\Local"
    )
    return normalize(_ALLOWED_DATA_ROOT_TEMPLATE.format(localappdata=local))


def load_config(path: str) -> Config:
    """Load, validate, and return the configuration at ``path``."""
    require_supported_runtime()
    config_path = normalize(path, base=os.getcwd())
    if not os.path.isfile(config_path.display):
        raise ConfigError(f"config file not found: {config_path.display}")
    with open(config_path.display, "rb") as handle:
        try:
            raw = tomllib.load(handle)
        except Exception as exc:  # tomllib.TOMLDecodeError and friends
            raise ConfigError(f"{config_path.display}: invalid TOML: {exc}") from exc
    return build_config(raw, source_file=config_path)


def build_config(raw: dict[str, Any], *, source_file: NormalizedPath | None = None) -> Config:
    """Validate an already-parsed configuration mapping."""
    assert_no_secrets(raw)
    warnings: list[str] = []

    project = _section(raw, "project")
    project_id = _require_str(project, "project_id", "project")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", project_id):
        raise ConfigError(
            f"project.project_id must be a short lower-case slug, got {project_id!r}"
        )

    repo_root = normalize(_require_str(project, "repo_root", "project"))
    core_root_raw = project.get("core_root")
    core_root = normalize(core_root_raw) if core_root_raw else None

    # M1 indexes the project repository only. core_root is recorded so later
    # milestones can resolve core dependencies, but it is deliberately NOT an
    # include root: indexing Odoo core is out of scope for M1.
    include_roots = [repo_root]
    for extra in project.get("include_roots", []) or []:
        extra_path = normalize(extra)
        if not is_within(repo_root.key, extra_path.key):
            raise ConfigError(
                f"project.include_roots entry escapes repo_root: {extra_path.display}"
            )
        if extra_path.key not in [r.key for r in include_roots]:
            include_roots.append(extra_path)

    if core_root is not None and is_within(core_root.key, repo_root.key):
        warnings.append(
            "repo_root is inside core_root; M1 still indexes repo_root only."
        )

    storage = _section(raw, "storage")
    data_dir = normalize(_require_str(storage, "data_dir", "storage"))
    db_filename = storage.get("db_filename", "graph.db")
    if not isinstance(db_filename, str) or not db_filename or "/" in db_filename or "\\" in db_filename:
        raise ConfigError("storage.db_filename must be a bare file name")
    db_path = normalize(db_filename, base=data_dir.display)

    _validate_data_location(data_dir, repo_root, core_root)

    odoo = raw.get("odoo", {}) or {}
    expected_series = odoo.get("expected_series", "19.0")
    if not isinstance(expected_series, str) or not re.fullmatch(r"\d+\.\d+", expected_series):
        raise ConfigError("odoo.expected_series must look like '19.0'")

    origin = raw.get("origin", {}) or {}
    default_origin = origin.get("default", "unknown")
    if default_origin not in VALID_ORIGINS:
        raise ConfigError(
            f"origin.default must be one of {VALID_ORIGINS}, got {default_origin!r}"
        )
    module_origins: dict[str, str] = {}
    for name, value in (origin.get("modules", {}) or {}).items():
        if value not in VALID_ORIGINS:
            raise ConfigError(
                f"origin.modules.{name} must be one of {VALID_ORIGINS}, got {value!r}"
            )
        module_origins[str(name).lower()] = value

    origin_rules: list[OriginRule] = []
    for entry in origin.get("rules", []) or []:
        if not isinstance(entry, dict):
            raise ConfigError("each origin.rules entry must be a table")
        pattern = entry.get("pattern")
        value = entry.get("origin")
        if not isinstance(pattern, str) or not pattern:
            raise ConfigError("origin.rules entry needs a non-empty 'pattern'")
        if value not in VALID_ORIGINS:
            raise ConfigError(
                f"origin.rules pattern {pattern!r}: origin must be one of {VALID_ORIGINS}"
            )
        origin_rules.append(OriginRule(pattern=pattern, origin=value))

    excl = raw.get("exclusions", {}) or {}
    for removed in ("remove_dir_names", "remove_file_globs", "disable_builtin"):
        if removed in excl:
            raise ConfigError(
                f"exclusions.{removed} is not supported: built-in exclusions are "
                "mandatory and can only be extended."
            )
    exclusions = ExclusionRules(
        extra_dir_names=frozenset(
            str(n).lower() for n in (excl.get("extra_dir_names", []) or [])
        ),
        extra_dir_path_suffixes=tuple(
            str(p) for p in (excl.get("extra_dir_path_suffixes", []) or [])
        ),
        extra_file_globs=tuple(
            str(p) for p in (excl.get("extra_file_globs", []) or [])
        ),
    )

    acceptance = raw.get("acceptance", {}) or {}
    if not isinstance(acceptance, dict):
        raise ConfigError("[acceptance] must be a table")
    acceptance_profile = acceptance.get("profile", "testing19")
    if acceptance_profile not in VALID_ACCEPTANCE_PROFILES:
        raise ConfigError(
            f"acceptance.profile must be one of {VALID_ACCEPTANCE_PROFILES}, "
            f"got {acceptance_profile!r}"
        )

    limits = raw.get("limits", {}) or {}
    max_hash_bytes = limits.get("max_hash_bytes", 8 * 1024 * 1024)
    if not isinstance(max_hash_bytes, int) or max_hash_bytes <= 0:
        raise ConfigError("limits.max_hash_bytes must be a positive integer")

    return Config(
        project_id=project_id,
        repo_root=repo_root,
        core_root=core_root,
        include_roots=include_roots,
        data_dir=data_dir,
        db_path=db_path,
        expected_series=expected_series,
        acceptance_profile=acceptance_profile,
        module_origins=module_origins,
        origin_rules=origin_rules,
        default_origin=default_origin,
        exclusions=exclusions,
        max_hash_bytes=max_hash_bytes,
        source_file=source_file,
        warnings=warnings,
    )


def _validate_data_location(
    data_dir: NormalizedPath,
    repo_root: NormalizedPath,
    core_root: NormalizedPath | None,
) -> None:
    """The graph database must live outside every source tree."""
    if is_within(repo_root.key, data_dir.key):
        raise ConfigError(
            f"storage.data_dir must not be inside repo_root: {data_dir.display}"
        )
    if core_root is not None and is_within(core_root.key, data_dir.key):
        raise ConfigError(
            f"storage.data_dir must not be inside core_root: {data_dir.display}"
        )
    root = allowed_data_root()
    if not is_within(root.key, data_dir.key):
        raise ConfigError(
            f"storage.data_dir must be under {root.display}, got {data_dir.display}"
        )


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"missing or invalid [{name}] section")
    return value


def _require_str(section: dict[str, Any], key: str, section_name: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"[{section_name}].{key} is required and must be a string")
    return value.strip()


def assert_no_secrets(node: Any, path: str = "") -> None:
    """Reject a configuration that carries anything credential-shaped."""
    if isinstance(node, dict):
        for key, value in node.items():
            key_path = f"{path}.{key}" if path else str(key)
            # origin.modules keys are module names supplied by the user; a
            # module legitimately named e.g. "*_auth_*" must not trip the guard,
            # so only non-origin sections have their keys inspected.
            if not key_path.startswith("origin.modules") and _SECRET_KEY_PATTERN.search(str(key)):
                raise ConfigError(
                    f"config key {key_path!r} looks like a secret; odoo-kg "
                    "configuration must not contain credentials."
                )
            assert_no_secrets(value, key_path)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            assert_no_secrets(item, f"{path}[{index}]")
    elif isinstance(node, str):
        for pattern in _SECRET_VALUE_PATTERNS:
            if pattern.search(node):
                raise ConfigError(
                    f"config value at {path!r} looks like a secret or a "
                    "credentialed connection string; remove it."
                )
