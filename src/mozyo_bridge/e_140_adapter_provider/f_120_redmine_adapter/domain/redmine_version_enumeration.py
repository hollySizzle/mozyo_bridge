"""Open-leaf issue enumeration for a Redmine Version (Redmine #12651).

The MCP surface available today cannot list the open *leaf* work inside a
Version: ``list_user_stories(version_id=)`` returns only the UserStory tracker,
and ``get_project_structure(version_id=)`` walks Epic -> Feature -> UserStory
top-down and drops Task / Test / Bug leaves whose parent UserStory is not itself
tagged to the Version. This was verified live on Versions #226 / #247 / #248 /
#254, which report open issue counts while the US-only and structure listings
return none.

This module computes the open leaves for a Version from a flat
``GET /issues.json?fixed_version_id=<id>`` snapshot — the read model the current
MCP surface cannot produce — through a :class:`RedmineVersionIssueSource` port,
so a live HTTP adapter can drop in behind the same seam later. An issue is an
*open leaf* when it is not closed and no other open issue in the Version's set
names it as parent (a parent with an open child in-set is reported separately as
an open non-leaf). Pure; no network I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class VersionIssue:
    """One issue tagged to a Version, reduced to the fields enumeration needs."""

    issue_id: str
    tracker: str
    status_name: str
    is_closed: bool
    parent_id: str | None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "VersionIssue | None":
        """Parse one ``GET /issues.json`` entry. Returns ``None`` (fail-closed,
        dropped) when the entry has no id, tolerant of the nested REST shape
        ``{"id":, "tracker":{"name":}, "status":{"name":, "is_closed":},
        "parent":{"id":}}``."""
        if not isinstance(payload, Mapping):
            return None
        issue_id = str(payload.get("id", "")).strip()
        if not issue_id:
            return None
        status = payload.get("status")
        status_name = ""
        is_closed = False
        if isinstance(status, Mapping):
            status_name = str(status.get("name", "") or "")
            is_closed = bool(status.get("is_closed", False))
        parent = payload.get("parent")
        parent_id: str | None = None
        if isinstance(parent, Mapping):
            raw_parent = str(parent.get("id", "")).strip()
            parent_id = raw_parent or None
        tracker = payload.get("tracker")
        tracker_name = ""
        if isinstance(tracker, Mapping):
            tracker_name = str(tracker.get("name", "") or "")
        return cls(
            issue_id=issue_id,
            tracker=tracker_name,
            status_name=status_name,
            is_closed=is_closed,
            parent_id=parent_id,
        )


@dataclass(frozen=True)
class VersionLeafEnumeration:
    """The open-leaf read model for a Version."""

    version_id: str
    open_leaf_issues: tuple[VersionIssue, ...]
    open_nonleaf_issues: tuple[VersionIssue, ...]
    counts_by_tracker: Mapping[str, int]
    total_open: int
    total_issues: int

    def as_dict(self) -> dict[str, object]:
        return {
            "version_id": self.version_id,
            "total_issues": self.total_issues,
            "total_open": self.total_open,
            "open_leaf_count": len(self.open_leaf_issues),
            "counts_by_tracker": dict(self.counts_by_tracker),
            "open_leaf_issues": [
                {
                    "issue_id": i.issue_id,
                    "tracker": i.tracker,
                    "status": i.status_name,
                    "parent_id": i.parent_id,
                }
                for i in self.open_leaf_issues
            ],
            "open_nonleaf_issues": [
                {
                    "issue_id": i.issue_id,
                    "tracker": i.tracker,
                    "status": i.status_name,
                    "parent_id": i.parent_id,
                }
                for i in self.open_nonleaf_issues
            ],
        }


@runtime_checkable
class RedmineVersionIssueSource(Protocol):
    """Read port: returns the raw issue entries tagged to ``version_id``.

    A live adapter wraps ``GET /issues.json?fixed_version_id=<id>&status_id=*``;
    the in-memory :class:`MappingRedmineVersionIssueSource` wraps an already
    fetched snapshot so the enumeration is testable without the network.
    """

    def read_version_issues(self, version_id: str) -> Sequence[Mapping[str, object]]: ...


@dataclass(frozen=True)
class MappingRedmineVersionIssueSource:
    """A :class:`RedmineVersionIssueSource` over an already-fetched payload.

    Accepts the REST shape ``{"issues": [...]}`` or a bare list of issue
    mappings. Pure — no network I/O.
    """

    payload: object

    def read_version_issues(
        self, version_id: str | None = None
    ) -> list[Mapping[str, object]]:
        raw = self.payload
        if isinstance(raw, Mapping):
            raw = raw.get("issues", [])
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return []
        return [entry for entry in raw if isinstance(entry, Mapping)]


def enumerate_open_leaf_issues(
    issues: Iterable[VersionIssue], version_id: str
) -> VersionLeafEnumeration:
    """Compute the open-leaf read model from parsed :class:`VersionIssue` records.

    Leaf rule: an open issue is a leaf unless another open issue in the same set
    names it as ``parent_id`` (i.e. it has an open child in-set). Counts are over
    the open leaves only.
    """
    materialized = [i for i in issues if i is not None]
    open_issues = [i for i in materialized if not i.is_closed]
    parents_with_open_children = {
        i.parent_id for i in open_issues if i.parent_id is not None
    }
    leaves = tuple(
        i for i in open_issues if i.issue_id not in parents_with_open_children
    )
    nonleaves = tuple(
        i for i in open_issues if i.issue_id in parents_with_open_children
    )
    counts: dict[str, int] = {}
    for issue in leaves:
        key = issue.tracker or "(unknown)"
        counts[key] = counts.get(key, 0) + 1
    return VersionLeafEnumeration(
        version_id=version_id,
        open_leaf_issues=leaves,
        open_nonleaf_issues=nonleaves,
        counts_by_tracker=counts,
        total_open=len(open_issues),
        total_issues=len(materialized),
    )


def enumerate_from_source(
    source: RedmineVersionIssueSource, version_id: str
) -> VersionLeafEnumeration:
    """Read raw entries from ``source`` and enumerate the open leaves (pure)."""
    raw = source.read_version_issues(version_id)
    parsed = [VersionIssue.from_mapping(entry) for entry in raw]
    return enumerate_open_leaf_issues(
        [issue for issue in parsed if issue is not None], version_id
    )
