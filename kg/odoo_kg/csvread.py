"""Defensive reading of Odoo's ``ir.model.access.csv``.

An ACL file is the one place in an Odoo module where a typo silently widens or
removes access, so this reader is written to disagree loudly rather than to
cope quietly. Every row is checked before it is believed, and a row that fails
a check is reported and dropped -- never guessed at, and never allowed to stop
the file, let alone the run.

Two spellings of the relational columns are accepted, because Odoo accepts
both: ``model_id:id`` and ``model_id/id``. testing19 contains one file using
the slash form, and a reader that knew only the colon would silently lose
thirty-one rules about who may read what.

Nothing here evaluates anything. Values are read as text and normalised; no
expression, domain or Python fragment is ever executed.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

#: Columns Odoo requires. ``id`` is what makes a row addressable at all.
REQUIRED_COLUMNS = ("id", "name")

#: The relational columns, each with the spellings Odoo accepts. Order matters
#: only for the error message.
MODEL_COLUMNS = ("model_id:id", "model_id/id")
GROUP_COLUMNS = ("group_id:id", "group_id/id")

#: The four permissions, in Odoo's own order.
PERMISSIONS = ("perm_read", "perm_write", "perm_create", "perm_unlink")

#: What Odoo's CSV loader accepts as a boolean. Anything else is a defect
#: worth naming rather than coercing: silently reading ``yes`` as false would
#: remove an access right nobody meant to remove.
_TRUE = frozenset({"1", "true", "t", "yes", "y"})
_FALSE = frozenset({"0", "false", "f", "no", "n", ""})


@dataclass
class AclRow:
    """One accepted ACL row, as written."""

    csv_id: str
    name: str
    model_target: str
    group_target: str | None
    permissions: dict[str, bool]
    line: int

    @property
    def is_global(self) -> bool:
        """No group means the rule applies to every user with model access."""
        return self.group_target is None


@dataclass
class AclProblem:
    """Why a row or a file was not believed."""

    severity: str
    category: str
    message: str
    line: int | None = None


@dataclass
class AclFile:
    rows: list[AclRow] = field(default_factory=list)
    problems: list[AclProblem] = field(default_factory=list)
    header: tuple[str, ...] = ()
    usable: bool = False


def read_acl_csv(source: bytes) -> AclFile:
    """Parse an ACL CSV. Never raises for content; reports instead."""
    result = AclFile()
    try:
        # utf-8-sig strips a BOM, which one of testing19's files carries and
        # which would otherwise make the first column name unrecognisable.
        text = source.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        result.problems.append(
            AclProblem("error", "acl_undecodable", f"not valid UTF-8: {exc}")
        )
        return result

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        raw_header = next(reader)
    except StopIteration:
        result.problems.append(
            AclProblem("error", "acl_empty", "the file has no header row")
        )
        return result
    except csv.Error as exc:
        result.problems.append(
            AclProblem("error", "acl_unreadable", f"malformed CSV: {exc}")
        )
        return result

    header = tuple(cell.strip() for cell in raw_header)
    result.header = header
    index = {name: position for position, name in enumerate(header)}

    model_column = _first_present(index, MODEL_COLUMNS)
    group_column = _first_present(index, GROUP_COLUMNS)

    missing = [name for name in REQUIRED_COLUMNS if name not in index]
    if model_column is None:
        missing.append("model_id:id")
    if missing:
        result.problems.append(
            AclProblem(
                "error", "acl_header_invalid",
                f"header is missing {', '.join(missing)}; the file is not an "
                f"ir.model.access table and no row is indexed. Header was: "
                f"{', '.join(header) or '<empty>'}",
            )
        )
        return result

    if group_column is None:
        # Legal: a file may grant only global access. Worth saying, because a
        # missing group column is more often an omission than an intention.
        result.problems.append(
            AclProblem(
                "info", "acl_no_group_column",
                "no group_id column; every row in this file is a global grant",
            )
        )

    duplicates: dict[str, int] = {}
    result.usable = True
    line = 1
    while True:
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            line += 1
            result.problems.append(
                AclProblem("error", "acl_row_unreadable", f"malformed row: {exc}", line)
            )
            continue
        line += 1

        if not any(cell.strip() for cell in row):
            continue

        if len(row) != len(header):
            result.problems.append(
                AclProblem(
                    "error", "acl_column_count",
                    f"{len(row)} values for {len(header)} columns; the row is "
                    "ambiguous and is not indexed",
                    line,
                )
            )
            continue

        def cell(name: str) -> str:
            position = index.get(name)
            return row[position].strip() if position is not None else ""

        csv_id = cell("id")
        if not csv_id:
            result.problems.append(
                AclProblem("error", "acl_missing_id", "row has no id", line)
            )
            continue
        if csv_id in duplicates:
            result.problems.append(
                AclProblem(
                    "warning", "acl_duplicate_id",
                    f"id {csv_id!r} was already used on line {duplicates[csv_id]}; "
                    "the later row overwrites the earlier one",
                    line,
                )
            )
        duplicates[csv_id] = line

        model_target = cell(model_column)
        if not model_target:
            result.problems.append(
                AclProblem(
                    "error", "acl_missing_model",
                    f"row {csv_id!r} names no model; it grants nothing and is "
                    "not indexed",
                    line,
                )
            )
            continue

        permissions: dict[str, bool] = {}
        invalid = False
        for permission in PERMISSIONS:
            value, problem = _boolean(cell(permission), permission, csv_id, line)
            if problem is not None:
                result.problems.append(problem)
                invalid = True
            permissions[permission] = value
        if invalid:
            # A row whose permissions cannot be read cannot be reported as
            # granting or withholding anything. Dropping it is the only honest
            # option; the diagnostic says which row and why.
            continue

        group_target = cell(group_column) if group_column else ""
        result.rows.append(
            AclRow(
                csv_id=csv_id,
                name=cell("name"),
                model_target=model_target,
                group_target=group_target or None,
                permissions=permissions,
                line=line,
            )
        )
    return result


def _first_present(index: dict[str, int], candidates) -> str | None:
    for name in candidates:
        if name in index:
            return name
    return None


def _boolean(raw: str, column: str, csv_id: str, line: int):
    """Odoo's CSV booleans, strictly. Returns (value, problem-or-None)."""
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True, None
    if lowered in _FALSE:
        return False, None
    return False, AclProblem(
        "error", "acl_non_boolean",
        f"row {csv_id!r}: {column}={raw!r} is neither true nor false; the row "
        "is not indexed rather than guessed at",
        line,
    )
