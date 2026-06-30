"""Provider-neutral lane bucket source boundary (Redmine #12919).

A *lane bucket* is the execution grouping that lane-set / dispatch logic reads to
decide which issues are candidate work for a sublane (see
``vibes/docs/logics/coordinator-sublane-development-flow.md`` and
``vibes/docs/rules/agent-workflow.md`` ``## ロードマップUS``). Today that grouping
is a Redmine Version / issue ``fixed_version`` (#12670 j#69330: *"current
implementation should use ``fixed_version`` as the active bucket truth"*). But the
same design judgment is explicit that command internals **must not hard-code
Version as the only future bucket source** — when a Redmine custom field or a
workflow DB later owns ``execution_bucket`` / ``lane_set`` identity, the source can
move there without rewriting dispatch.

This module is that seam, mirroring the existing ticket-adapter split: the neutral
contract lives here in ``f_110_ticket_adapter_common`` (like
:class:`...ticket_adapter.TicketProvider`); the concrete Redmine ``fixed_version``
implementation lives in
``mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.domain.fixed_version_lane_bucket_provider``.
The future custom-field provider (#12922) implements this same boundary and never
imports the Redmine feature.

Boundary, kept enforced in code:

- **Core owns** the source-kind vocabulary (advisory, open for future sources), the
  *closed* skip-reason vocabulary, the leaf rule, and the umbrella vs.
  execution-bucket judgment. None of that is delegated to a provider.
- **Providers own** how to read their tracker (Version status, issue ``fixed_version``,
  custom-field value, dates) and how to normalize it into the records below. A
  provider returns either a resolved :class:`LaneBucket` or an explicit
  :class:`BucketSkip`; it never returns a partially-guessed bucket and never widens
  the skip vocabulary.

Everything here is pure: frozen dataclasses, no network, no I/O. A provider may read
a tracker, but the result records and the cross-bucket decisions are core values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol, Sequence, runtime_checkable

# ---------------------------------------------------------------------------
# Source-kind vocabulary (advisory / open). A provider reports which source it read
# the bucket from; this is *not* an allowlist gate, so a future source (workflow DB,
# a different custom field) can report its own kind without a core change. The two
# kinds the roadmap names explicitly are recorded so callers can branch on them.
# ---------------------------------------------------------------------------
SOURCE_KIND_FIXED_VERSION = "fixed_version"
#: Reserved for the #12922 execution-bucket custom-field provider (not implemented here).
SOURCE_KIND_CUSTOM_FIELD = "custom_field"

KNOWN_SOURCE_KINDS: frozenset[str] = frozenset(
    {SOURCE_KIND_FIXED_VERSION, SOURCE_KIND_CUSTOM_FIELD}
)

# ---------------------------------------------------------------------------
# Skip-reason vocabulary (CLOSED / fail-closed). When a provider cannot return a
# trustworthy bucket it returns one of these reasons rather than an empty or
# half-resolved bucket. The set is closed so a provider cannot invent a reason and
# callers can branch exhaustively; :class:`BucketSkip` enforces membership.
# ---------------------------------------------------------------------------
SKIP_NO_FIXED_VERSION = "no_fixed_version"
SKIP_ISSUE_CLOSED = "issue_closed"
SKIP_VERSION_CLOSED = "version_closed"
SKIP_VERSION_LOCKED = "version_locked"
SKIP_BUCKET_NOT_FOUND = "bucket_not_found"
SKIP_AMBIGUOUS_SOURCE = "ambiguous_source"
#: The issue carries no execution-bucket value at all — the source-neutral analog of
#: :data:`SKIP_NO_FIXED_VERSION` for a non-Version source. The #12922 custom-field
#: provider returns this when an issue's configured custom field is unset / empty, so
#: an issue with no bucket fails closed instead of being placed in a guessed bucket.
SKIP_NO_EXECUTION_BUCKET = "no_execution_bucket"
#: A resolved bucket value is outside the provider's configured allow-list. The #12922
#: custom-field provider can restrict execution-bucket values to a known set; a value
#: outside it is rejected here rather than dispatched against an unrecognized bucket.
SKIP_DISALLOWED_VALUE = "disallowed_value"

BUCKET_SKIP_REASONS: frozenset[str] = frozenset(
    {
        SKIP_NO_FIXED_VERSION,
        SKIP_ISSUE_CLOSED,
        SKIP_VERSION_CLOSED,
        SKIP_VERSION_LOCKED,
        SKIP_BUCKET_NOT_FOUND,
        SKIP_AMBIGUOUS_SOURCE,
        SKIP_NO_EXECUTION_BUCKET,
        SKIP_DISALLOWED_VALUE,
    }
)

#: Version statuses that make a bucket non-dispatchable, mapped to their skip reason.
#: A locked/closed Version is a deliberate "do not start new work here" signal, so a
#: provider fails closed on it instead of enumerating candidates.
_VERSION_STATUS_SKIP: Mapping[str, str] = {
    "closed": SKIP_VERSION_CLOSED,
    "locked": SKIP_VERSION_LOCKED,
}


def version_status_skip_reason(status: Optional[str]) -> Optional[str]:
    """The skip reason for a non-dispatchable Version status, or ``None`` (pure).

    The mapping is core-owned (closed skip vocabulary): a ``closed`` Version yields
    :data:`SKIP_VERSION_CLOSED`, a ``locked`` one :data:`SKIP_VERSION_LOCKED`; ``open``
    or an unknown / absent status yields ``None`` (the provider proceeds to enumerate).
    Case-insensitive.
    """
    if status is None:
        return None
    return _VERSION_STATUS_SKIP.get(status.strip().lower())


class LaneBucketError(ValueError):
    """A provider produced a lane-bucket record that violates the core contract."""


@dataclass(frozen=True)
class LaneBucketIssue:
    """A normalized issue inside a lane bucket.

    ``is_leaf`` is the dispatch-candidate flag: a leaf is an open issue that no other
    open issue in the same bucket names as its parent (i.e. a work leaf, not an
    umbrella). It is computed by :func:`mark_leaves` over the bucket's issue set, so a
    provider does not decide leaf-ness alone. Subjects are intentionally absent — like
    :class:`...ticket_adapter.IssueRef`, they can carry confidential summaries and never
    belong on a core-facing record.
    """

    issue_id: str
    tracker: Optional[str] = None
    status_name: Optional[str] = None
    is_closed: bool = False
    parent_id: Optional[str] = None
    is_leaf: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "issue_id": self.issue_id,
            "tracker": self.tracker,
            "status_name": self.status_name,
            "is_closed": self.is_closed,
            "parent_id": self.parent_id,
            "is_leaf": self.is_leaf,
        }


@dataclass(frozen=True)
class LaneBucket:
    """A resolved lane bucket: the structured source-of-truth dispatch reads.

    Carries the fields the acceptance condition requires — bucket id / name / source
    kind / issue list / parent US / status / dates — so a consumer never re-reads the
    tracker. ``is_umbrella`` flags a bucket whose issues belong to more than one
    distinct parent US (the bucket aggregates children of different parents, so a single
    parent US is not its owner); ``parent_us`` is the single common parent when exactly
    one is present, else ``None``. The cross-bucket umbrella case — one parent US whose
    children span several buckets — is decided by :func:`decide_execution_bucket`.
    """

    bucket_id: str
    source_kind: str
    name: Optional[str] = None
    status: Optional[str] = None
    start_date: Optional[str] = None
    due_date: Optional[str] = None
    parent_us: Optional[str] = None
    is_umbrella: bool = False
    issues: tuple[LaneBucketIssue, ...] = ()

    @property
    def open_issues(self) -> tuple[LaneBucketIssue, ...]:
        return tuple(i for i in self.issues if not i.is_closed)

    @property
    def open_leaf_issues(self) -> tuple[LaneBucketIssue, ...]:
        """Open leaf issues — the dispatch candidates for this bucket."""
        return tuple(i for i in self.issues if i.is_leaf and not i.is_closed)

    @property
    def counts_by_tracker(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for issue in self.open_issues:
            key = issue.tracker or "unknown"
            counts[key] = counts.get(key, 0) + 1
        return counts

    @property
    def total_open(self) -> int:
        return len(self.open_issues)

    @property
    def total_issues(self) -> int:
        return len(self.issues)

    def as_dict(self) -> dict[str, object]:
        return {
            "bucket_id": self.bucket_id,
            "source_kind": self.source_kind,
            "name": self.name,
            "status": self.status,
            "start_date": self.start_date,
            "due_date": self.due_date,
            "parent_us": self.parent_us,
            "is_umbrella": self.is_umbrella,
            "issues": [i.as_dict() for i in self.issues],
            "open_leaf_issues": [i.as_dict() for i in self.open_leaf_issues],
            "counts_by_tracker": self.counts_by_tracker,
            "total_open": self.total_open,
            "total_issues": self.total_issues,
        }


@dataclass(frozen=True)
class BucketSkip:
    """An explicit fail-closed reason a bucket could not be resolved.

    ``reason`` is always one of :data:`BUCKET_SKIP_REASONS`; the invariant is enforced
    in :meth:`__post_init__` so a provider cannot smuggle in an unrecognized reason.
    ``detail`` is a short human-readable note; ``bucket_id`` / ``issue_id`` point at
    whichever anchor the skip is about (a missing/locked Version, or an issue with no
    bucket).
    """

    reason: str
    detail: str = ""
    bucket_id: Optional[str] = None
    issue_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.reason not in BUCKET_SKIP_REASONS:
            raise LaneBucketError(
                f"unknown bucket skip reason: {self.reason!r}; expected one of "
                f"{sorted(BUCKET_SKIP_REASONS)}"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "detail": self.detail,
            "bucket_id": self.bucket_id,
            "issue_id": self.issue_id,
        }


@dataclass(frozen=True)
class BucketResolution:
    """The result of resolving a bucket: either a :class:`LaneBucket` or a :class:`BucketSkip`.

    Exactly one of ``bucket`` / ``skip`` is set. Callers branch on :attr:`resolved`;
    the fail-closed contract is that a provider always returns one of these, never a
    bare ``None`` and never a half-built bucket. Built via the :meth:`of` /
    :meth:`skipped` constructors so the invariant holds.
    """

    bucket: Optional[LaneBucket] = None
    skip: Optional[BucketSkip] = None

    def __post_init__(self) -> None:
        if (self.bucket is None) == (self.skip is None):
            raise LaneBucketError(
                "BucketResolution must carry exactly one of bucket / skip"
            )

    @property
    def resolved(self) -> bool:
        return self.bucket is not None

    @classmethod
    def of(cls, bucket: LaneBucket) -> "BucketResolution":
        return cls(bucket=bucket)

    @classmethod
    def skipped(cls, skip: BucketSkip) -> "BucketResolution":
        return cls(skip=skip)

    def as_dict(self) -> dict[str, object]:
        return {
            "resolved": self.resolved,
            "bucket": self.bucket.as_dict() if self.bucket is not None else None,
            "skip": self.skip.as_dict() if self.skip is not None else None,
        }


@dataclass(frozen=True)
class ExecutionBucketDecision:
    """The umbrella vs. execution-bucket judgment for a parent US (#12919 AC4).

    When a parent US is an *umbrella* whose children span more than one bucket, the
    parent's own bucket is **not** the execution-bucket source of truth — each child's
    own bucket is (``vibes/docs/rules/agent-workflow.md`` ``## ロードマップUS`` and
    ``coordinator-sublane-development-flow.md``: *"親 UserStory が umbrella で複数 bucket に
    またがる場合は、親の fixed_version を正本にしない。子 issue の fixed_version を execution
    bucket として読む"*). This value object records that decision:

    - ``is_umbrella`` — children resolve to more than one distinct bucket;
    - ``child_buckets`` — the distinct child buckets (sorted, deduped);
    - ``per_child`` — each child issue id -> its execution bucket (``None`` if the child
      has no bucket);
    - ``parent_bucket`` — the parent's own bucket, kept for evidence but *not*
      authoritative when ``is_umbrella`` is true.
    """

    parent_id: str
    is_umbrella: bool
    parent_bucket: Optional[str] = None
    child_buckets: tuple[str, ...] = ()
    per_child: Mapping[str, Optional[str]] = field(default_factory=dict)
    note: str = ""

    def execution_bucket_for(self, child_id: str) -> Optional[str]:
        """The execution bucket to read for a child issue.

        For an umbrella parent this is the child's own bucket; otherwise the single
        shared bucket (which equals the parent's bucket when present).
        """
        return self.per_child.get(child_id)

    def as_dict(self) -> dict[str, object]:
        return {
            "parent_id": self.parent_id,
            "is_umbrella": self.is_umbrella,
            "parent_bucket": self.parent_bucket,
            "child_buckets": list(self.child_buckets),
            "per_child": dict(self.per_child),
            "note": self.note,
        }


@runtime_checkable
class LaneBucketProvider(Protocol):
    """The lane-bucket source boundary lane-set / dispatch depends on.

    A provider reads one bucket source (Redmine ``fixed_version`` today, a custom field
    or workflow DB later) and answers three reads. Declared as a Protocol so dispatch
    logic stays testable with an in-memory provider and a live, credentialed adapter
    drops in behind the same seam. ``source_kind`` reports which source the provider
    reads (one of :data:`KNOWN_SOURCE_KINDS`, or a future kind).
    """

    source_kind: str

    def resolve_bucket(self, bucket_id: str) -> BucketResolution:
        """Resolve a bucket by its id into a :class:`LaneBucket` or a :class:`BucketSkip`."""
        ...

    def resolve_issue_bucket(self, issue_id: str) -> BucketResolution:
        """Resolve the execution bucket an issue belongs to (fail-closed if none)."""
        ...

    def resolve_execution_bucket(self, parent_issue_id: str) -> ExecutionBucketDecision:
        """Decide umbrella vs. per-child execution buckets for a parent US."""
        ...


def mark_leaves(issues: Sequence[LaneBucketIssue]) -> tuple[LaneBucketIssue, ...]:
    """Recompute :attr:`LaneBucketIssue.is_leaf` over a bucket's issue set (pure).

    Leaf rule: an open issue is a leaf unless another open issue *in this set* names it
    as ``parent_id``. Closed issues are never leaves (no work to dispatch). The parent
    set is built from open issues only, so an issue whose only "child" is closed is
    still a leaf candidate.
    """
    open_parent_ids = {
        issue.parent_id
        for issue in issues
        if not issue.is_closed and issue.parent_id is not None
    }
    result: list[LaneBucketIssue] = []
    for issue in issues:
        is_leaf = (not issue.is_closed) and issue.issue_id not in open_parent_ids
        result.append(
            issue if issue.is_leaf == is_leaf else _with_leaf(issue, is_leaf)
        )
    return tuple(result)


def _with_leaf(issue: LaneBucketIssue, is_leaf: bool) -> LaneBucketIssue:
    return LaneBucketIssue(
        issue_id=issue.issue_id,
        tracker=issue.tracker,
        status_name=issue.status_name,
        is_closed=issue.is_closed,
        parent_id=issue.parent_id,
        is_leaf=is_leaf,
    )


def decide_execution_bucket(
    parent_id: str,
    children: Sequence[tuple[str, Optional[str]]],
    *,
    parent_bucket: Optional[str] = None,
) -> ExecutionBucketDecision:
    """Decide the execution-bucket model for a parent US from its children (pure).

    ``children`` is a sequence of ``(child_issue_id, child_bucket_id)`` pairs; a child
    with no bucket carries ``None``. The rule (#12919 AC4):

    - if the children resolve to more than one **distinct** bucket, the parent is an
      umbrella and each child's *own* bucket is the execution bucket — the parent's
      bucket is not the source of truth;
    - if the children all share a single bucket (or there are no children), the parent
      is not an umbrella and that single bucket is the execution bucket.

    Children with no bucket do not by themselves make a parent an umbrella, but they are
    recorded in ``per_child`` as ``None`` so the gap stays visible rather than guessed.
    """
    per_child: dict[str, Optional[str]] = {}
    for child_id, bucket_id in children:
        per_child[str(child_id)] = bucket_id
    distinct = sorted({b for b in per_child.values() if b is not None})
    is_umbrella = len(distinct) > 1
    if is_umbrella:
        note = (
            "umbrella parent: children span multiple buckets; read each child's own "
            "execution bucket, not the parent's"
        )
    elif distinct:
        note = "single execution bucket shared by all children"
    else:
        note = "no child buckets resolved"
    return ExecutionBucketDecision(
        parent_id=str(parent_id),
        is_umbrella=is_umbrella,
        parent_bucket=parent_bucket,
        child_buckets=tuple(distinct),
        per_child=per_child,
        note=note,
    )


def distinct_parents(issues: Sequence[LaneBucketIssue]) -> tuple[str, ...]:
    """The distinct parent ids referenced by a bucket's issues (sorted, pure)."""
    return tuple(sorted({i.parent_id for i in issues if i.parent_id is not None}))


__all__ = (
    "SOURCE_KIND_FIXED_VERSION",
    "SOURCE_KIND_CUSTOM_FIELD",
    "KNOWN_SOURCE_KINDS",
    "SKIP_NO_FIXED_VERSION",
    "SKIP_ISSUE_CLOSED",
    "SKIP_VERSION_CLOSED",
    "SKIP_VERSION_LOCKED",
    "SKIP_BUCKET_NOT_FOUND",
    "SKIP_AMBIGUOUS_SOURCE",
    "SKIP_NO_EXECUTION_BUCKET",
    "SKIP_DISALLOWED_VALUE",
    "BUCKET_SKIP_REASONS",
    "version_status_skip_reason",
    "LaneBucketError",
    "LaneBucketIssue",
    "LaneBucket",
    "BucketSkip",
    "BucketResolution",
    "ExecutionBucketDecision",
    "LaneBucketProvider",
    "mark_leaves",
    "decide_execution_bucket",
    "distinct_parents",
)
