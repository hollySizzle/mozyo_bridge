"""Redmine ``fixed_version`` lane bucket provider (Redmine #12919).

The first concrete :class:`...lane_bucket_provider.LaneBucketProvider`: it treats a
Redmine Version / issue ``fixed_version`` as the lane bucket source of truth (#12670
j#69330), reading a **supplied snapshot** of Redmine data and normalizing it into the
provider-neutral records in ``f_110_ticket_adapter_common``.

Scope boundary, kept deliberately narrow (like the #12651 enumeration and the #12672
journal source): this reads a snapshot an operator / MCP already fetched — the flat
``/issues.json?fixed_version_id=<id>&status_id=*`` issues list and, optionally, a
``/versions.json`` versions list — and performs **no network call and no Redmine
write**. The leaf rule and the umbrella / execution-bucket judgment are the neutral
core's (:func:`...lane_bucket_provider.mark_leaves`,
:func:`...lane_bucket_provider.decide_execution_bucket`); this provider only knows how
to read Redmine shapes. A live, credentialed read adapter behind the same
:class:`...lane_bucket_provider.LaneBucketProvider` port — reusing the read-only
``redmine_context`` machinery — is an explicit follow-up, not implemented here so the
provider stays pure and side-effect free.

The custom-field execution-bucket provider (#12922) is a *different* provider behind
the same neutral boundary; it is explicitly out of scope here (#12919 non-goal).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_140_adapter_provider.f_110_ticket_adapter_common.domain.lane_bucket_provider import (
    SKIP_AMBIGUOUS_SOURCE,
    SKIP_BUCKET_NOT_FOUND,
    SKIP_ISSUE_CLOSED,
    SKIP_NO_FIXED_VERSION,
    SOURCE_KIND_FIXED_VERSION,
    BucketResolution,
    BucketSkip,
    ExecutionBucketDecision,
    LaneBucket,
    LaneBucketIssue,
    decide_execution_bucket,
    distinct_parents,
    mark_leaves,
    version_status_skip_reason,
)


def _str_or_none(value: object) -> Optional[str]:
    """Coerce a JSON scalar to ``str`` for an id/text field, or ``None``.

    Redmine ids arrive as ints; records use strings so ids stay comparable. Empty /
    missing values normalize to ``None`` (never the literal ``"None"``).
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class RedmineVersionState:
    """Normalized Redmine Version state for a bucket (status / name / dates).

    Built from a ``/versions.json`` entry or from the ``fixed_version`` object embedded
    on an issue. ``status`` is the Redmine Version status (``open`` / ``locked`` /
    ``closed``) when known; ``start_date`` / ``due_date`` map from
    ``start_date`` / ``created_on`` and ``due_date`` / ``effective_date`` respectively
    (Redmine names the milestone date ``due_date`` on a version REST object and
    ``effective_date`` on an issue's embedded ``fixed_version``).
    """

    version_id: str
    name: Optional[str] = None
    status: Optional[str] = None
    start_date: Optional[str] = None
    due_date: Optional[str] = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> Optional["RedmineVersionState"]:
        version_id = _str_or_none(payload.get("id"))
        if version_id is None:
            return None
        status = _str_or_none(payload.get("status"))
        return cls(
            version_id=version_id,
            name=_str_or_none(payload.get("name")),
            status=status.lower() if status is not None else None,
            start_date=_str_or_none(payload.get("start_date"))
            or _str_or_none(payload.get("created_on")),
            due_date=_str_or_none(payload.get("due_date"))
            or _str_or_none(payload.get("effective_date")),
        )


def _lane_bucket_issue_from_mapping(payload: Mapping[str, object]) -> Optional[LaneBucketIssue]:
    """Normalize one Redmine issue object into a :class:`LaneBucketIssue` (fail-closed).

    Drops entries with no id. Reads the nested REST shape (``tracker.name`` /
    ``status.name`` / ``status.is_closed`` / ``parent.id``); ``is_leaf`` is left ``False``
    here and computed by :func:`mark_leaves` over the whole bucket set.
    """
    issue_id = _str_or_none(payload.get("id"))
    if issue_id is None:
        return None
    status = _mapping(payload.get("status"))
    return LaneBucketIssue(
        issue_id=issue_id,
        tracker=_str_or_none(_mapping(payload.get("tracker")).get("name")),
        status_name=_str_or_none(status.get("name")),
        is_closed=bool(status.get("is_closed", False)),
        parent_id=_str_or_none(_mapping(payload.get("parent")).get("id")),
    )


def _issue_fixed_version_id(payload: Mapping[str, object]) -> Optional[str]:
    return _str_or_none(_mapping(payload.get("fixed_version")).get("id"))


@dataclass(frozen=True)
class RedmineFixedVersionLaneBucketProvider:
    """A :class:`...lane_bucket_provider.LaneBucketProvider` over a Redmine snapshot.

    ``issues_payload`` is the fetched issues list — either ``{"issues": [...]}`` or a
    bare ``[...]`` of Redmine issue objects. ``versions_payload`` is the optional
    versions list (``{"versions": [...]}`` or a bare list); when absent, Version
    status / name / dates are derived best-effort from the ``fixed_version`` embedded on
    the bucket's issues, and a closed/locked Version cannot be detected (so no
    version-status skip fires). Pure — it reads the supplied snapshot only.
    """

    issues_payload: object = ()
    versions_payload: object = None
    source_kind: str = SOURCE_KIND_FIXED_VERSION

    # -- snapshot accessors -------------------------------------------------
    def _issues(self) -> list[Mapping[str, object]]:
        raw = self.issues_payload
        if isinstance(raw, Mapping):
            raw = raw.get("issues", [])
        if isinstance(raw, str) or not isinstance(raw, Sequence):
            return []
        return [i for i in raw if isinstance(i, Mapping)]

    def _versions(self) -> list[Mapping[str, object]]:
        raw = self.versions_payload
        if isinstance(raw, Mapping):
            raw = raw.get("versions", [])
        if isinstance(raw, str) or not isinstance(raw, Sequence):
            return []
        return [v for v in raw if isinstance(v, Mapping)]

    def _find_issue(self, issue_id: str) -> Optional[Mapping[str, object]]:
        target = str(issue_id).strip()
        for issue in self._issues():
            if _str_or_none(issue.get("id")) == target:
                return issue
        return None

    def _version_state(self, bucket_id: str) -> Optional[RedmineVersionState]:
        """Version state from the versions snapshot, else from an embedded fixed_version."""
        target = str(bucket_id).strip()
        for version in self._versions():
            if _str_or_none(version.get("id")) == target:
                return RedmineVersionState.from_mapping(version)
        # Fall back to the fixed_version object embedded on a matching issue.
        for issue in self._issues():
            fixed = _mapping(issue.get("fixed_version"))
            if _str_or_none(fixed.get("id")) == target:
                return RedmineVersionState.from_mapping(fixed)
        return None

    def _bucket_issues(self, bucket_id: str) -> tuple[LaneBucketIssue, ...]:
        target = str(bucket_id).strip()
        issues: list[LaneBucketIssue] = []
        for issue in self._issues():
            if _issue_fixed_version_id(issue) != target:
                continue
            normalized = _lane_bucket_issue_from_mapping(issue)
            if normalized is not None:
                issues.append(normalized)
        return mark_leaves(issues)

    # -- provider boundary --------------------------------------------------
    def resolve_bucket(self, bucket_id: str) -> BucketResolution:
        target = _str_or_none(bucket_id)
        if target is None:
            return BucketResolution.skipped(
                BucketSkip(SKIP_AMBIGUOUS_SOURCE, detail="empty bucket id")
            )
        state = self._version_state(target)
        issues = self._bucket_issues(target)
        if state is None and not issues:
            # Neither the versions snapshot nor any issue references this bucket.
            return BucketResolution.skipped(
                BucketSkip(
                    SKIP_BUCKET_NOT_FOUND,
                    detail="no version or issue references this bucket in the snapshot",
                    bucket_id=target,
                )
            )
        status_skip = version_status_skip_reason(state.status) if state is not None else None
        if status_skip is not None:
            # A closed / locked Version is a deliberate "do not start work here" signal.
            return BucketResolution.skipped(
                BucketSkip(
                    status_skip,
                    detail=f"version status is {state.status!r}",
                    bucket_id=target,
                )
            )
        parents = distinct_parents(issues)
        bucket = LaneBucket(
            bucket_id=target,
            source_kind=self.source_kind,
            name=state.name if state is not None else None,
            status=state.status if state is not None else None,
            start_date=state.start_date if state is not None else None,
            due_date=state.due_date if state is not None else None,
            parent_us=parents[0] if len(parents) == 1 else None,
            is_umbrella=len(parents) > 1,
            issues=issues,
        )
        return BucketResolution.of(bucket)

    def resolve_issue_bucket(self, issue_id: str) -> BucketResolution:
        target = _str_or_none(issue_id)
        if target is None:
            return BucketResolution.skipped(
                BucketSkip(SKIP_AMBIGUOUS_SOURCE, detail="empty issue id")
            )
        issue = self._find_issue(target)
        if issue is None:
            return BucketResolution.skipped(
                BucketSkip(
                    SKIP_AMBIGUOUS_SOURCE,
                    detail="issue not present in the snapshot",
                    issue_id=target,
                )
            )
        status = _mapping(issue.get("status"))
        if bool(status.get("is_closed", False)):
            return BucketResolution.skipped(
                BucketSkip(
                    SKIP_ISSUE_CLOSED,
                    detail="issue is closed; no execution bucket to dispatch",
                    issue_id=target,
                )
            )
        version_id = _issue_fixed_version_id(issue)
        if version_id is None:
            return BucketResolution.skipped(
                BucketSkip(
                    SKIP_NO_FIXED_VERSION,
                    detail="issue has no fixed_version (no execution bucket)",
                    issue_id=target,
                )
            )
        return self.resolve_bucket(version_id)

    def resolve_execution_bucket(self, parent_issue_id: str) -> ExecutionBucketDecision:
        target = str(parent_issue_id).strip()
        parent = self._find_issue(target)
        parent_bucket = _issue_fixed_version_id(parent) if parent is not None else None
        children: list[tuple[str, Optional[str]]] = []
        for issue in self._issues():
            if _str_or_none(_mapping(issue.get("parent")).get("id")) != target:
                continue
            child_id = _str_or_none(issue.get("id"))
            if child_id is None:
                continue
            children.append((child_id, _issue_fixed_version_id(issue)))
        return decide_execution_bucket(
            target, children, parent_bucket=parent_bucket
        )


__all__ = (
    "RedmineVersionState",
    "RedmineFixedVersionLaneBucketProvider",
)
