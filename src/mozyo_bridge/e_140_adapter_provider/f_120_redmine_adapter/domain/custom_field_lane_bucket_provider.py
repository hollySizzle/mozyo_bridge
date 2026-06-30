"""Redmine custom-field execution-bucket lane bucket provider (Redmine #12922).

The second concrete :class:`...lane_bucket_provider.LaneBucketProvider`, a sibling of
:class:`...fixed_version_lane_bucket_provider.RedmineFixedVersionLaneBucketProvider`
behind the same neutral #12919 boundary. Where the fixed_version provider reads a
Redmine Version / issue ``fixed_version`` as the bucket source of truth, this provider
reads a configured Redmine **custom field** value as the execution bucket — the seam the
#12919 design named explicitly (*"when a Redmine custom field … later owns
``execution_bucket`` / ``lane_set`` identity, the source can move there without rewriting
dispatch"*).

The point of the seam is migration, not a switch: Redmine Version stays the roadmap /
milestone axis and the **default** bucket source of truth; this provider lets an operator
read an *opt-in* custom field as the execution bucket so the two axes can be separated
later under a deliberate rule update. This slice does not change any project default
(#12922 non-goal).

Scope boundary, kept identical to the fixed_version provider (#12919) and the #12651
enumeration: this reads a **supplied snapshot** an operator / MCP already fetched — the
flat ``/issues.json`` list with each issue's ``custom_fields`` array — and performs **no
network call, no Redmine write, and no Redmine schema / field management** (#12922
non-goal). The leaf rule and the umbrella / execution-bucket judgment are the neutral
core's (:func:`...lane_bucket_provider.mark_leaves`,
:func:`...lane_bucket_provider.decide_execution_bucket`); this provider only knows how to
read the Redmine custom-field shape and normalize it into the provider-neutral records.

Bucket identity here is the custom-field **value** itself: there is no separate Version
id / name, so the resolved :class:`...lane_bucket_provider.LaneBucket` carries the value as
both ``bucket_id`` and ``name`` and has no Redmine Version status / dates (so no
version-status skip applies). The fail-closed contract the acceptance requires:

- an issue whose configured field is unset / empty -> :data:`SKIP_NO_EXECUTION_BUCKET`;
- an issue whose field carries more than one distinct value (a multi-value list) ->
  :data:`SKIP_AMBIGUOUS_SOURCE` (never guessed; such an issue is also excluded from any
  single bucket's membership);
- a value outside the configured allow-list (when one is set) ->
  :data:`SKIP_DISALLOWED_VALUE`;
- a value no snapshot issue carries -> :data:`SKIP_BUCKET_NOT_FOUND`;
- an empty selector -> :data:`SKIP_AMBIGUOUS_SOURCE`.

A live, credentialed read adapter behind the same port is an explicit follow-up, not
implemented here so the provider stays pure and side-effect free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_140_adapter_provider.f_110_ticket_adapter_common.domain.lane_bucket_provider import (
    SKIP_AMBIGUOUS_SOURCE,
    SKIP_BUCKET_NOT_FOUND,
    SKIP_DISALLOWED_VALUE,
    SKIP_ISSUE_CLOSED,
    SKIP_NO_EXECUTION_BUCKET,
    SOURCE_KIND_CUSTOM_FIELD,
    BucketResolution,
    BucketSkip,
    ExecutionBucketDecision,
    LaneBucket,
    LaneBucketIssue,
    LaneBucketError,
    decide_execution_bucket,
    distinct_parents,
    mark_leaves,
)


def _str_or_none(value: object) -> Optional[str]:
    """Coerce a JSON scalar to ``str`` for an id/text/value field, or ``None``.

    Redmine ids arrive as ints; records use strings so ids stay comparable. Empty /
    missing values normalize to ``None`` (never the literal ``"None"``). Mirrors the
    fixed_version provider's coercion so the two providers normalize identically.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _custom_field_values(value: object) -> tuple[str, ...]:
    """Normalize a Redmine custom-field ``value`` into the distinct non-empty values.

    Redmine renders a single-value field's ``value`` as a scalar (string / int) and a
    multi-value (list) field's ``value`` as a JSON list. Empty strings / ``None`` are
    dropped; the result is the distinct values in first-seen order so a single-value
    field yields ``("bucket",)`` and a genuine multi-value field yields more than one.
    """
    raw_items: Sequence[object]
    if isinstance(value, (list, tuple)):
        raw_items = value
    else:
        raw_items = (value,)
    seen: list[str] = []
    for item in raw_items:
        text = _str_or_none(item)
        if text is not None and text not in seen:
            seen.append(text)
    return tuple(seen)


@dataclass(frozen=True)
class CustomFieldBucketConfig:
    """Which Redmine custom field carries the execution bucket, and which values are allowed.

    The field is selected by **id or name** (acceptance: *"provider config で custom field
    名または id を指定できる"*). At least one of ``field_id`` / ``field_name`` must be set;
    a config naming neither cannot identify the field and is rejected at construction.
    ``allowed_values``, when not ``None``, is the closed set of execution-bucket values the
    provider will resolve — a value outside it fails closed
    (:data:`SKIP_DISALLOWED_VALUE`). ``None`` means any non-empty value is accepted.

    Both id and name may be set; an issue's custom field matches when *either* the id or
    the name matches (Redmine custom fields on an issue carry both), so a snapshot that
    only renders one of them still resolves.
    """

    field_id: Optional[str] = None
    field_name: Optional[str] = None
    allowed_values: Optional[frozenset[str]] = None

    def __post_init__(self) -> None:
        normalized_id = _str_or_none(self.field_id)
        normalized_name = _str_or_none(self.field_name)
        if normalized_id is None and normalized_name is None:
            raise LaneBucketError(
                "CustomFieldBucketConfig requires a field_id or field_name to identify "
                "the execution-bucket custom field"
            )
        object.__setattr__(self, "field_id", normalized_id)
        object.__setattr__(self, "field_name", normalized_name)
        if self.allowed_values is not None and not isinstance(
            self.allowed_values, frozenset
        ):
            object.__setattr__(
                self,
                "allowed_values",
                frozenset(
                    v for v in (_str_or_none(x) for x in self.allowed_values) if v is not None
                ),
            )

    def matches_field(self, custom_field: Mapping[str, object]) -> bool:
        """Whether a Redmine ``custom_fields`` entry is the configured execution-bucket field."""
        if self.field_id is not None and _str_or_none(custom_field.get("id")) == self.field_id:
            return True
        if (
            self.field_name is not None
            and _str_or_none(custom_field.get("name")) == self.field_name
        ):
            return True
        return False

    def value_allowed(self, value: str) -> bool:
        """Whether ``value`` is permitted (always true when no allow-list is configured)."""
        return self.allowed_values is None or value in self.allowed_values


def _lane_bucket_issue_from_mapping(payload: Mapping[str, object]) -> Optional[LaneBucketIssue]:
    """Normalize one Redmine issue object into a :class:`LaneBucketIssue` (fail-closed).

    Identical shape-reading to the fixed_version provider (so the two normalize the same
    issue identically): drops entries with no id, reads ``tracker.name`` / ``status.name``
    / ``status.is_closed`` / ``parent.id``; ``is_leaf`` is left ``False`` and computed by
    :func:`mark_leaves` over the whole bucket set.
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


@dataclass(frozen=True)
class RedmineCustomFieldLaneBucketProvider:
    """A :class:`...lane_bucket_provider.LaneBucketProvider` over a Redmine custom field.

    ``issues_payload`` is the fetched issues list — either ``{"issues": [...]}`` or a bare
    ``[...]`` of Redmine issue objects, each optionally carrying a ``custom_fields`` array.
    ``config`` selects the execution-bucket custom field (by id or name) and the optional
    allowed-value set. Pure — it reads the supplied snapshot only; no network, no Redmine
    write, no schema management.
    """

    issues_payload: object = ()
    config: CustomFieldBucketConfig = CustomFieldBucketConfig(field_name="execution_bucket")
    source_kind: str = SOURCE_KIND_CUSTOM_FIELD

    # -- snapshot accessors -------------------------------------------------
    def _issues(self) -> list[Mapping[str, object]]:
        raw = self.issues_payload
        if isinstance(raw, Mapping):
            raw = raw.get("issues", [])
        if isinstance(raw, str) or not isinstance(raw, Sequence):
            return []
        return [i for i in raw if isinstance(i, Mapping)]

    def _find_issue(self, issue_id: str) -> Optional[Mapping[str, object]]:
        target = str(issue_id).strip()
        for issue in self._issues():
            if _str_or_none(issue.get("id")) == target:
                return issue
        return None

    def _field_values(self, issue: Mapping[str, object]) -> tuple[str, ...]:
        """The distinct values the configured execution-bucket field carries on an issue.

        Reads the issue's ``custom_fields`` array, finds the entry matching the configured
        field (by id or name), and normalizes its ``value`` into distinct non-empty values.
        An issue without the field, or with an empty value, yields ``()``.
        """
        raw = issue.get("custom_fields")
        if not isinstance(raw, Sequence) or isinstance(raw, str):
            return ()
        for entry in raw:
            if isinstance(entry, Mapping) and self.config.matches_field(entry):
                return _custom_field_values(entry.get("value"))
        return ()

    def _issue_bucket_value(self, issue: Mapping[str, object]) -> tuple[Optional[str], bool]:
        """The single execution-bucket value for an issue, plus whether it is ambiguous.

        Returns ``(value, ambiguous)``: ``(None, False)`` when the field is unset,
        ``(value, False)`` for a single value, and ``(None, True)`` when the field carries
        more than one distinct value (multi-value list — never guessed).
        """
        values = self._field_values(issue)
        if not values:
            return (None, False)
        if len(values) > 1:
            return (None, True)
        return (values[0], False)

    def _bucket_issues(self, bucket_value: str) -> tuple[LaneBucketIssue, ...]:
        """Issues whose configured field resolves to exactly ``bucket_value`` (pure).

        An issue with an ambiguous (multi-value) field is excluded — it does not cleanly
        belong to any single bucket, so it is never silently placed in one.
        """
        target = str(bucket_value).strip()
        issues: list[LaneBucketIssue] = []
        for issue in self._issues():
            value, ambiguous = self._issue_bucket_value(issue)
            if ambiguous or value != target:
                continue
            normalized = _lane_bucket_issue_from_mapping(issue)
            if normalized is not None:
                issues.append(normalized)
        return mark_leaves(issues)

    # -- provider boundary --------------------------------------------------
    def resolve_bucket(self, bucket_id: str) -> BucketResolution:
        """Resolve an execution bucket by its custom-field **value** (fail-closed).

        For a custom-field source the bucket identity is the value itself, so ``bucket_id``
        is the value to resolve. An empty value is ambiguous; a disallowed value (outside a
        configured allow-list) fails closed before any issue is read; a value no snapshot
        issue carries is not found. A resolved :class:`LaneBucket` carries the value as both
        ``bucket_id`` and ``name`` with ``source_kind`` = :data:`SOURCE_KIND_CUSTOM_FIELD`
        and no Version status / dates — the same normalized shape the fixed_version provider
        returns, so dispatch reads it identically.
        """
        target = _str_or_none(bucket_id)
        if target is None:
            return BucketResolution.skipped(
                BucketSkip(SKIP_AMBIGUOUS_SOURCE, detail="empty bucket value")
            )
        if not self.config.value_allowed(target):
            return BucketResolution.skipped(
                BucketSkip(
                    SKIP_DISALLOWED_VALUE,
                    detail=(
                        f"bucket value {target!r} is not in the configured allowed set"
                    ),
                    bucket_id=target,
                )
            )
        issues = self._bucket_issues(target)
        if not issues:
            return BucketResolution.skipped(
                BucketSkip(
                    SKIP_BUCKET_NOT_FOUND,
                    detail="no issue carries this execution-bucket value in the snapshot",
                    bucket_id=target,
                )
            )
        parents = distinct_parents(issues)
        bucket = LaneBucket(
            bucket_id=target,
            source_kind=self.source_kind,
            name=target,
            status=None,
            start_date=None,
            due_date=None,
            parent_us=parents[0] if len(parents) == 1 else None,
            is_umbrella=len(parents) > 1,
            issues=issues,
        )
        return BucketResolution.of(bucket)

    def resolve_bucket_by_name(self, name: str) -> BucketResolution:
        """Resolve a bucket by name. For a custom-field source name == value (#12922).

        Unlike a Redmine Version (which has a distinct id and display name), a custom-field
        execution bucket has only its value, which serves as both id and name. So this
        delegates to :meth:`resolve_bucket`; it exists for parity with the fixed_version
        provider's id/name selector so a caller can use either flag uniformly.
        """
        return self.resolve_bucket(name)

    def resolve_issue_bucket(self, issue_id: str) -> BucketResolution:
        """Resolve the execution bucket an issue belongs to (fail-closed if none/ambiguous)."""
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
        value, ambiguous = self._issue_bucket_value(issue)
        if ambiguous:
            return BucketResolution.skipped(
                BucketSkip(
                    SKIP_AMBIGUOUS_SOURCE,
                    detail="issue's execution-bucket field carries multiple values",
                    issue_id=target,
                )
            )
        if value is None:
            return BucketResolution.skipped(
                BucketSkip(
                    SKIP_NO_EXECUTION_BUCKET,
                    detail="issue has no execution-bucket custom field value",
                    issue_id=target,
                )
            )
        return self.resolve_bucket(value)

    def resolve_execution_bucket(self, parent_issue_id: str) -> ExecutionBucketDecision:
        """Decide umbrella vs. per-child execution buckets for a parent US (pure).

        Each child's execution bucket is its single custom-field value; an unset or
        ambiguous (multi-value) field contributes ``None`` so the gap stays visible rather
        than being guessed into a shared bucket.
        """
        target = str(parent_issue_id).strip()
        parent = self._find_issue(target)
        parent_bucket = (
            self._issue_bucket_value(parent)[0] if parent is not None else None
        )
        children: list[tuple[str, Optional[str]]] = []
        for issue in self._issues():
            if _str_or_none(_mapping(issue.get("parent")).get("id")) != target:
                continue
            child_id = _str_or_none(issue.get("id"))
            if child_id is None:
                continue
            children.append((child_id, self._issue_bucket_value(issue)[0]))
        return decide_execution_bucket(target, children, parent_bucket=parent_bucket)


__all__ = (
    "CustomFieldBucketConfig",
    "RedmineCustomFieldLaneBucketProvider",
)
