"""Is the graph still describing the repository as it is now?

A knowledge graph that silently answers from a stale index is worse than one
that refuses, so every MCP response carries a freshness verdict.

Two things can drift, and they are reported separately because they mean
different things:

``stale_commit``
    HEAD has moved since the graph was built. Everything in the graph may be
    out of date.
``stale_worktree``
    HEAD matches, but the set of modified/untracked files no longer matches
    what the graph recorded. Someone edited or saved files since indexing.

The worktree comparison is restricted to files the graph actually indexed, so
excluded paths (``.claude/**``, ``*.po``, ``*.conf``) never produce a false
stale verdict.

All git access is read-only and uses ``--no-optional-locks``, so asking about
freshness cannot modify the repository being asked about.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from .exclusions import ExclusionRules
from .gitbaseline import run_read_only

FRESH = "fresh"
STALE_COMMIT = "stale_commit"
STALE_WORKTREE = "stale_worktree"
UNKNOWN = "unknown"

#: Seconds a freshness verdict is reused. A burst of tool calls should not run
#: `git status` once per call, but the answer must not go stale within a session.
CACHE_TTL_SECONDS = 10.0

#: Most drifted paths named in a response. The count is always exact; the list
#: is a sample so a large drift cannot flood the client.
MAX_REPORTED_PATHS = 10


@dataclass
class Freshness:
    """The verdict plus the evidence behind it."""

    state: str
    detail: str
    graph_commit: str | None = None
    current_commit: str | None = None
    graph_branch: str | None = None
    current_branch: str | None = None
    graph_indexed_at: str | None = None
    drifted_count: int = 0
    drifted_sample: list[str] = field(default_factory=list)

    @property
    def is_stale(self) -> bool:
        return self.state in (STALE_COMMIT, STALE_WORKTREE)

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "detail": self.detail,
            "is_stale": self.is_stale,
            "graph_commit": self.graph_commit,
            "current_commit": self.current_commit,
            "graph_branch": self.graph_branch,
            "current_branch": self.current_branch,
            "graph_indexed_at": self.graph_indexed_at,
            "drifted_file_count": self.drifted_count,
            "drifted_file_sample": self.drifted_sample,
        }

    def warning(self) -> str | None:
        """A one-line warning suitable for prepending to any tool response."""
        if not self.is_stale:
            return None
        if self.state == STALE_COMMIT:
            return (
                f"STALE GRAPH: indexed at commit {_short(self.graph_commit)} but the "
                f"repository is now at {_short(self.current_commit)}. "
                "Re-run `python -m odoo_kg index` before trusting these answers."
            )
        return (
            f"STALE GRAPH: {self.drifted_count} indexed file(s) changed in the working "
            "tree since indexing. Re-run `python -m odoo_kg index`."
        )


def _short(commit: str | None) -> str:
    return commit[:7] if commit else "unknown"


class FreshnessChecker:
    """Computes freshness, with a short TTL so bursts do not re-shell out."""

    def __init__(
        self,
        repo_root: str,
        exclusions: ExclusionRules | None = None,
        ttl_seconds: float = CACHE_TTL_SECONDS,
    ):
        self.repo_root = repo_root
        self.exclusions = exclusions or ExclusionRules()
        self.ttl_seconds = ttl_seconds
        self._cached: Freshness | None = None
        self._cached_at = 0.0

    def check(self, connection: sqlite3.Connection, project_id: str) -> Freshness:
        now = time.monotonic()
        if self._cached is not None and (now - self._cached_at) < self.ttl_seconds:
            return self._cached
        verdict = evaluate(connection, project_id, self.repo_root, self.exclusions)
        self._cached = verdict
        self._cached_at = now
        return verdict

    def invalidate(self) -> None:
        self._cached = None


def evaluate(
    connection: sqlite3.Connection,
    project_id: str,
    repo_root: str,
    exclusions: ExclusionRules | None = None,
) -> Freshness:
    """Compare the stored baseline against the repository as it is now."""
    row = connection.execute(
        "SELECT branch, base_commit, indexed_at, git_available "
        "FROM project WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        return Freshness(
            state=UNKNOWN,
            detail=f"the graph holds no project row for {project_id!r}",
        )

    graph_commit = row["base_commit"]
    verdict = Freshness(
        state=UNKNOWN,
        detail="",
        graph_commit=graph_commit,
        graph_branch=row["branch"],
        graph_indexed_at=row["indexed_at"],
    )

    head = run_read_only(["rev-parse", "HEAD"], repo_root)
    if head is None:
        verdict.detail = (
            "git is unavailable or the repository root is not a working tree; "
            "freshness cannot be determined"
        )
        return verdict
    verdict.current_commit = head.strip()

    branch = run_read_only(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    verdict.current_branch = branch.strip() if branch else None

    if not graph_commit:
        verdict.detail = "the graph records no base commit; it was built without git"
        return verdict

    if verdict.current_commit != graph_commit:
        verdict.state = STALE_COMMIT
        verdict.detail = (
            f"HEAD moved from {_short(graph_commit)} to "
            f"{_short(verdict.current_commit)} since indexing"
        )
        return verdict

    drifted = _worktree_drift(connection, repo_root, exclusions or ExclusionRules())
    if drifted is None:
        verdict.detail = "HEAD matches, but `git status` could not be read"
        return verdict

    if drifted:
        verdict.state = STALE_WORKTREE
        verdict.drifted_count = len(drifted)
        verdict.drifted_sample = sorted(drifted)[:MAX_REPORTED_PATHS]
        verdict.detail = (
            f"HEAD matches, but {len(drifted)} indexed file(s) differ from the "
            "state recorded at index time"
        )
        return verdict

    verdict.state = FRESH
    verdict.detail = (
        f"graph matches HEAD {_short(graph_commit)} and the working tree"
    )
    return verdict


def _worktree_drift(
    connection: sqlite3.Connection, repo_root: str, exclusions: ExclusionRules
) -> set[str] | None:
    """Paths whose current state disagrees with what the graph recorded.

    Three cases have to be told apart, and getting the third wrong is what
    makes a stale graph look fresh:

    1. A path the graph indexed, whose dirty state changed either way.
    2. A path the graph never indexed *because the rules exclude it*
       (``.claude/**``, ``*.po``, ``*.conf``): never drift, no matter what git
       says about it.
    3. A path the graph never indexed but *would* have indexed -- a new module
       or a new source file added since the index run. This is drift, and it is
       the most important case: that content is entirely absent from the graph,
       so answering from the graph would confidently miss it.
    """
    out = run_read_only(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"],
        repo_root,
    )
    if out is None:
        return None

    current: set[str] = set()
    for entry in out.split("\0"):
        if len(entry) < 4:
            continue
        rel = entry[3:].strip()
        if rel:
            current.add(rel.replace("\\", "/").lower())

    indexed = {
        row["rel_path"].lower()
        for row in connection.execute("SELECT rel_path FROM node_file")
    }
    recorded = {
        row["rel_path"].lower()
        for row in connection.execute(
            "SELECT rel_path FROM node_file "
            "WHERE content_state IN ('modified', 'untracked')"
        )
    }

    drift = (current & indexed) ^ recorded
    for path in current - indexed:
        if not exclusions.check_relpath(path).excluded:
            drift.add(path)
    return drift
