"""Configurable governed work-unit granularity for sublane dispatch (Redmine #13002).

The owner decision behind #13002 makes ``1 UserStory = 1 work unit`` the standard
governed dispatch granularity: the coordinator hands one UserStory to the
target-lane Codex gateway, the gateway routes the same US to the same-lane Claude
worker, and the worker executes the US's child Task / Test / Bug issues in one
bounded pass (the central preset's ``### US-Level Audit Model``). Dispatching every
leaf issue as its own lane made the handoff / callback / review / close overhead
dominate the implementation work, so the leaf unit becomes the *exception*, not the
default.

This module is the pure schema + decision core for that granularity:

- **Closed enum vocabulary.** A work unit is exactly one of
  :data:`WORK_UNIT_EPIC` / :data:`WORK_UNIT_FEATURE` / :data:`WORK_UNIT_USER_STORY`
  / :data:`WORK_UNIT_LEAF_ISSUE`. The default is
  :data:`DEFAULT_WORK_UNIT_GRANULARITY` (``user_story``). Anything else fails
  closed through :class:`WorkUnitGranularityError` — an unknown granularity never
  silently reads as the default.
- **Config record schema.** :meth:`WorkUnitGranularityConfig.from_record`
  normalizes the ``work_unit:`` block of ``.mozyo-bridge/config.yaml`` (already
  parsed by the repo-local loader; no IO here), with the same closed-schema /
  fail-closed rules the sibling repo-local sub-records enforce. A missing block is
  the behavior-preserving ``user_story`` default.
- **Fail-closed dispatch decision.** :func:`decide_work_unit_dispatch` is the pure
  gate the sublane dispatch surface consults: ``user_story`` is the allowed
  default; ``leaf_issue`` is allowed as the narrow exception path (it exists for
  the central preset's ``us_level_audit.task_level例外`` — guardrail / release /
  credential / destructive / CI-publish / workflow changes and the other
  task-level exception conditions; this module deliberately introduces no new
  exception vocabulary); ``epic`` / ``feature`` are **blocked unless an explicit
  owner / operator decision anchor (a durable Redmine journal id) is supplied**,
  because those units are prone to unbounded scope. The decision is
  decision-support output: it never sends, approves, or closes anything.

What the granularity knob can and cannot do (the #13002 invariant boundary,
mirroring ``spec-delegation-policy-project-config``): it selects the *standard
dispatch unit* only. It cannot relax the durable-anchor requirement, the
review_request / review / owner-close-approval gates, the US-level audit model's
per-child implementation_done / task_close records, or any callback obligation —
none of those have a key here, and the closed schema rejects authority-shaped
keys outright.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional

#: The supported ``work_unit`` config record version. Optional in a record and
#: defaults to this; any other value is rejected so a future, not-yet-understood
#: schema never reads as version 1 (mirrors the repo-local config version rule).
WORK_UNIT_CONFIG_VERSION: int = 1

#: The closed set of recognized keys in the ``work_unit:`` block.
WORK_UNIT_CONFIG_KEYS: frozenset[str] = frozenset({"version", "granularity"})

# ---------------------------------------------------------------------------
# The closed granularity vocabulary (#13002 acceptance enum).
# ---------------------------------------------------------------------------

WORK_UNIT_EPIC: str = "epic"
WORK_UNIT_FEATURE: str = "feature"
WORK_UNIT_USER_STORY: str = "user_story"
WORK_UNIT_LEAF_ISSUE: str = "leaf_issue"

#: Every recognized work-unit granularity token.
WORK_UNIT_GRANULARITIES: frozenset[str] = frozenset(
    {WORK_UNIT_EPIC, WORK_UNIT_FEATURE, WORK_UNIT_USER_STORY, WORK_UNIT_LEAF_ISSUE}
)

#: The standard governed work unit (#13002 owner decision: ``1US=1作業単位``).
DEFAULT_WORK_UNIT_GRANULARITY: str = WORK_UNIT_USER_STORY

#: Granularities whose implementation dispatch requires an explicit owner /
#: operator decision recorded as a durable anchor. ``epic`` / ``feature`` units
#: are structure / portfolio nodes whose scope balloons easily, so they are never
#: dispatched by default or by config alone.
EXPLICIT_DECISION_GRANULARITIES: frozenset[str] = frozenset(
    {WORK_UNIT_EPIC, WORK_UNIT_FEATURE}
)

# ---------------------------------------------------------------------------
# Decision status / diagnostic vocabulary (fixed tokens, durable-record safe).
# ---------------------------------------------------------------------------

DISPATCH_ALLOWED: str = "dispatch_allowed"
DISPATCH_BLOCKED: str = "dispatch_blocked"

#: The standard governed unit — allowed with no extra condition.
WORK_UNIT_STANDARD: str = "work_unit_standard"
#: The leaf-issue exception path — allowed; exists for the central preset's
#: ``us_level_audit.task_level例外`` conditions (no new exception vocabulary).
WORK_UNIT_LEAF_EXCEPTION: str = "work_unit_leaf_exception"
#: An ``epic`` / ``feature`` unit with a recorded explicit decision anchor.
WORK_UNIT_EXPLICIT_DECISION_RECORDED: str = "work_unit_explicit_decision_recorded"
#: An ``epic`` / ``feature`` unit with no explicit decision anchor — blocked.
WORK_UNIT_EXPLICIT_DECISION_REQUIRED: str = "work_unit_explicit_decision_required"


class WorkUnitGranularityError(ValueError):
    """The work-unit granularity config / request violates the closed schema.

    Inherits :class:`ValueError` for fail-closed semantics, matching the sibling
    repo-local domain errors. The composing repo-local config loader re-raises
    this as its own ``RepoLocalConfigError`` so the loader keeps a single
    fail-closed boundary.
    """


def normalize_work_unit_granularity(value: object) -> str:
    """Return the validated granularity token, failing closed on anything else.

    A non-string, blank, or unrecognized value raises
    :class:`WorkUnitGranularityError` — an unknown granularity is never silently
    coerced to the default, so a typo'd config or flag cannot quietly change the
    dispatch unit.
    """
    if not isinstance(value, str) or not value.strip():
        raise WorkUnitGranularityError(
            f"work-unit granularity must be a non-empty string, got {value!r}"
        )
    token = value.strip()
    if token not in WORK_UNIT_GRANULARITIES:
        raise WorkUnitGranularityError(
            f"unknown work-unit granularity {token!r}; allowed: "
            f"{sorted(WORK_UNIT_GRANULARITIES)}"
        )
    return token


def _checked_version(record: "Mapping[object, object]") -> int:
    """Return the supported version, failing closed on anything else.

    ``version`` is optional and defaults to :data:`WORK_UNIT_CONFIG_VERSION`.
    ``bool`` is rejected even though it is an ``int`` subclass so ``version:
    true`` does not silently read as version ``1``.
    """
    version = record.get("version", WORK_UNIT_CONFIG_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise WorkUnitGranularityError(
            f"work_unit config 'version' must be an integer, got {version!r}"
        )
    if version != WORK_UNIT_CONFIG_VERSION:
        raise WorkUnitGranularityError(
            f"unsupported work_unit config version {version!r}; this build "
            f"understands version {WORK_UNIT_CONFIG_VERSION}"
        )
    return version


@dataclass(frozen=True)
class WorkUnitGranularityConfig:
    """The closed ``work_unit:`` block of ``.mozyo-bridge/config.yaml`` (schema only).

    Carries exactly one policy value: the standard :attr:`granularity` the
    governed sublane dispatch uses when the operator does not name one
    explicitly. The default (``user_story``) is the #13002 owner decision, so a
    repo with no ``work_unit:`` block dispatches US-sized units.

    Deliberately not expressible here (the invariant side of #13002): a key that
    would relax the durable-anchor / review / owner-approval / callback gates, or
    that would let ``epic`` / ``feature`` dispatch without an explicit decision —
    the closed :data:`WORK_UNIT_CONFIG_KEYS` set admits only ``version`` /
    ``granularity``, and the explicit-decision requirement lives in
    :func:`decide_work_unit_dispatch`, keyed on a per-dispatch durable anchor,
    never on config.
    """

    granularity: str = DEFAULT_WORK_UNIT_GRANULARITY

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "granularity", normalize_work_unit_granularity(self.granularity)
        )

    @classmethod
    def default(cls) -> "WorkUnitGranularityConfig":
        """The behavior-preserving default: the ``user_story`` standard unit."""
        return cls()

    @classmethod
    def from_record(
        cls, record: "Optional[Mapping[str, object]]" = None
    ) -> "WorkUnitGranularityConfig":
        """Normalize a parsed ``work_unit:`` mapping into a typed config.

        ``None`` / an empty mapping yields the ``user_story`` default. A
        non-mapping record, an unknown key, an unsupported version, or a
        non-enum ``granularity`` fails closed with
        :class:`WorkUnitGranularityError`.
        """
        if record is None:
            return cls.default()
        if not isinstance(record, Mapping):
            raise WorkUnitGranularityError(
                "work_unit config record must be a mapping (a YAML table), got "
                f"{type(record).__name__}"
            )
        for key in record:
            if not isinstance(key, str) or not key:
                raise WorkUnitGranularityError(
                    f"work_unit config record keys must be non-empty strings; "
                    f"got {key!r}"
                )
            if key not in WORK_UNIT_CONFIG_KEYS:
                raise WorkUnitGranularityError(
                    f"work_unit config record has unknown key {key!r}; allowed "
                    f"keys: {sorted(WORK_UNIT_CONFIG_KEYS)}"
                )
        _checked_version(record)
        granularity = record.get("granularity", DEFAULT_WORK_UNIT_GRANULARITY)
        return cls(granularity=normalize_work_unit_granularity(granularity))


@dataclass(frozen=True)
class WorkUnitDispatchDecision:
    """Decision-support output of :func:`decide_work_unit_dispatch`.

    ``status`` is :data:`DISPATCH_ALLOWED` / :data:`DISPATCH_BLOCKED`;
    ``diagnostic`` is the fixed ``work_unit_*`` reason token; ``reason`` is the
    human-readable line a plan / journal renders; ``decision_anchor`` echoes the
    explicit owner / operator decision anchor when one was supplied. Every field
    is durable-record safe (fixed tokens + a journal id). The decision never
    sends, approves, or closes anything — the dispatch surface composes it into
    its own fail-closed plan.
    """

    granularity: str
    status: str
    diagnostic: str
    reason: str
    decision_anchor: Optional[str] = None

    @property
    def is_allowed(self) -> bool:
        return self.status == DISPATCH_ALLOWED

    def as_payload(self) -> dict[str, object]:
        return {
            "granularity": self.granularity,
            "status": self.status,
            "diagnostic": self.diagnostic,
            "reason": self.reason,
            "decision_anchor": self.decision_anchor,
        }


def decide_work_unit_dispatch(
    granularity: str,
    *,
    explicit_decision_anchor: Optional[str] = None,
) -> WorkUnitDispatchDecision:
    """Decide whether a work unit of ``granularity`` may be implementation-dispatched.

    Pure and fail-closed over its inputs:

    - an unrecognized ``granularity`` raises :class:`WorkUnitGranularityError`
      (never silently treated as the default);
    - ``user_story`` -> allowed (:data:`WORK_UNIT_STANDARD`) — the governed
      standard unit;
    - ``leaf_issue`` -> allowed (:data:`WORK_UNIT_LEAF_EXCEPTION`) — the narrow
      exception unit whose conditions are the central preset's
      ``us_level_audit.task_level例外`` (this decision does not verify the
      condition itself; the coordinator's dispatch decision journal owns that);
    - ``epic`` / ``feature`` -> blocked
      (:data:`WORK_UNIT_EXPLICIT_DECISION_REQUIRED`) **unless**
      ``explicit_decision_anchor`` names the durable owner / operator decision
      record (a Redmine journal id); a blank / whitespace anchor counts as
      absent, so the explicit decision must be a real durable pointer, never a
      bare ``--yes``-shaped flag.
    """
    token = normalize_work_unit_granularity(granularity)
    anchor = (explicit_decision_anchor or "").strip() or None

    if token == WORK_UNIT_USER_STORY:
        return WorkUnitDispatchDecision(
            granularity=token,
            status=DISPATCH_ALLOWED,
            diagnostic=WORK_UNIT_STANDARD,
            reason="user_story is the standard governed work unit "
            "(1 UserStory = 1 work unit); the worker executes the US's child "
            "Task / Test / Bug issues within the US scope",
            decision_anchor=anchor,
        )
    if token == WORK_UNIT_LEAF_ISSUE:
        return WorkUnitDispatchDecision(
            granularity=token,
            status=DISPATCH_ALLOWED,
            diagnostic=WORK_UNIT_LEAF_EXCEPTION,
            reason="leaf_issue is the exception unit for the governed "
            "task-level exception conditions (us_level_audit.task_level例外); "
            "record the applicable condition in the dispatch decision journal",
            decision_anchor=anchor,
        )
    # epic / feature: explicit owner / operator decision required.
    if anchor is None:
        return WorkUnitDispatchDecision(
            granularity=token,
            status=DISPATCH_BLOCKED,
            diagnostic=WORK_UNIT_EXPLICIT_DECISION_REQUIRED,
            reason=f"{token} is an oversized implementation dispatch unit; it "
            "requires an explicit owner / operator decision recorded as a "
            "durable anchor (journal id) before dispatch",
        )
    return WorkUnitDispatchDecision(
        granularity=token,
        status=DISPATCH_ALLOWED,
        diagnostic=WORK_UNIT_EXPLICIT_DECISION_RECORDED,
        reason=f"{token} dispatch allowed by explicit owner / operator decision "
        f"recorded at durable anchor {anchor}",
        decision_anchor=anchor,
    )


__all__ = (
    "WORK_UNIT_CONFIG_VERSION",
    "WORK_UNIT_CONFIG_KEYS",
    "WORK_UNIT_EPIC",
    "WORK_UNIT_FEATURE",
    "WORK_UNIT_USER_STORY",
    "WORK_UNIT_LEAF_ISSUE",
    "WORK_UNIT_GRANULARITIES",
    "DEFAULT_WORK_UNIT_GRANULARITY",
    "EXPLICIT_DECISION_GRANULARITIES",
    "DISPATCH_ALLOWED",
    "DISPATCH_BLOCKED",
    "WORK_UNIT_STANDARD",
    "WORK_UNIT_LEAF_EXCEPTION",
    "WORK_UNIT_EXPLICIT_DECISION_RECORDED",
    "WORK_UNIT_EXPLICIT_DECISION_REQUIRED",
    "WorkUnitGranularityError",
    "normalize_work_unit_granularity",
    "WorkUnitGranularityConfig",
    "WorkUnitDispatchDecision",
    "decide_work_unit_dispatch",
)
