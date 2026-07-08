"""Sublane callback responsibility + callback-stall recovery model (Redmine #12159).

This module is a **pure, read-only** encoding of two operating contracts that
already live in the distributed skill / logic docs, so a coordinator (or a
sublane at startup) can surface them and *replay* them from CLI output instead
of re-deriving them by hand from pane scrollback:

- the handoff-worthy states a sublane owes the coordinator a callback for
  (``skills/mozyo-bridge-agent/references/workflow.md`` ``## Sublane Coordinator
  Callback`` > ``### States that require a coordinator callback``); and
- the four callback-stall states a coordinator classifies a delivered-but-quiet
  unit of work into, plus the standard recovery path for each
  (``## Stall And No-Progress Detection Standard`` and
  ``vibes/docs/logics/coordinator-sublane-development-flow.md``
  ``### callback 欠落時の sweep``).

It deliberately takes *no* I/O: every input is a fact the caller has already
read from the durable Redmine record. The module never touches tmux, never
reads pane scrollback, never weakens a handoff / queue-enter guard, and never
self-authorizes a close, a carve-out, or an owner decision — it only names the
state and the next recoverable step. The durable record stays the source of
truth; this is a lens over it.
"""

from __future__ import annotations

from typing import Any


# --- States a sublane owes the coordinator a callback for (#11852 / #12038). ---
# Mirrors workflow.md `### States that require a coordinator callback`. Each one
# lands as its own durable gate / journal first; the callback is only the
# pointer. Routine intra-lane progress does NOT need a callback.
COORDINATOR_CALLBACK_STATES: tuple[tuple[str, str], ...] = (
    ("blocked", "cannot proceed without a decision or unblocking input"),
    ("implementation_done", "implementation finished and recorded (recorded is not completion)"),
    ("review_request", "a US-level audit request (or a task-level review under preset exceptions) is posted"),
    ("review_result", "review approved, or findings recorded that need the originator's attention"),
    ("commit_recorded", "an audit-owned commit landed and its hash is recorded"),
    ("owner_close_approval_waiting", "the work is waiting on owner close approval"),
)


# --- Callback-stall classification (#11880 j#57539). ---
# The four durable states a coordinator classifies a stall candidate into, plus
# the non-stall outcomes the same inputs can resolve to. A stall candidate is
# "delivered dispatch journal + missing expected durable journal"; if no
# dispatch was delivered there is nothing to recover.
STATE_NOT_STALL_CANDIDATE = "not_stall_candidate"
STATE_CALLBACK_COMPLETE = "callback_complete"
STATE_CALLBACK_NOT_REQUIRED = "callback_not_required"
STATE_NO_PROGRESS_AFTER_HANDOFF = "no_progress_after_handoff"
STATE_PROGRESS_WITHOUT_CALLBACK = "progress_without_callback"
STATE_CALLBACK_DELIVERY_FAILED = "callback_delivery_failed"
STATE_CALLBACK_NOT_ATTEMPTED = "callback_not_attempted"

# The four states that are genuine stalls the coordinator must recover.
STALL_STATES = frozenset(
    {
        STATE_NO_PROGRESS_AFTER_HANDOFF,
        STATE_PROGRESS_WITHOUT_CALLBACK,
        STATE_CALLBACK_DELIVERY_FAILED,
        STATE_CALLBACK_NOT_ATTEMPTED,
    }
)

# Allowed values for ``callback`` — what the durable record shows about the
# sublane's cross-lane callback to the coordinator.
CALLBACK_ACKED = "acked"  # a coordinator callback (result=sent) is recorded.
CALLBACK_DELIVERY_FAILED = "delivery_failed"  # attempt recorded but send failed.
CALLBACK_NOT_REQUIRED = "not_required"  # explicit not-attempted reason (e.g. this IS the coordinator lane).
CALLBACK_SAME_LANE_ONLY = "same_lane_only"  # surfaced to the lane's own Codex, but no cross-lane coordinator callback.
CALLBACK_ABSENT = "absent"  # no callback / receive-method journal of any kind.

_CALLBACK_VALUES = frozenset(
    {
        CALLBACK_ACKED,
        CALLBACK_DELIVERY_FAILED,
        CALLBACK_NOT_REQUIRED,
        CALLBACK_SAME_LANE_ONLY,
        CALLBACK_ABSENT,
    }
)

# Sorted, public tuple for CLI ``choices=`` (stable order for --help / tests).
CALLBACK_CHOICES: tuple[str, ...] = tuple(sorted(_CALLBACK_VALUES))

# Invariants every recovery path inherits — printed once so the recovery output
# can never be misread as a license to re-dispatch or self-authorize.
_RECOVERY_INVARIANTS: tuple[str, ...] = (
    "the durable Redmine record stays the source of truth; pane scrollback / "
    "status / doctor are corroborating only",
    "record this stall check + any re-notification as a Progress Log journal "
    "on the issue (a silent re-poke is prohibited)",
    "do not self-authorize a close, a carve-out, or an owner decision — a "
    "detected stall only finds the state",
)

# Re-used recovery fragment for the stale-CLI sub-case of a delivery failure.
# The repo-local re-send is a dogfooding fallback that only exists inside a
# mozyo-bridge checkout; an adopting project runs the installed CLI alone
# (Redmine #13379), so the hint names the update path first.
_STALE_CLI_HINT = (
    "if the lane reached a routing / preflight step then went silent, suspect a "
    "stale installed CLI; update the installed CLI (during a quiescent window — "
    "never while lanes are live) or, inside a mozyo-bridge checkout, re-send "
    "from the repo-local CLI (`PYTHONPATH=src python3 -m mozyo_bridge ...`) "
    "before concluding the lane is idle"
)


def classify_callback_stall(
    *,
    dispatch_delivered: bool,
    new_durable_progress: bool,
    callback: str = CALLBACK_ABSENT,
    stale_cli: bool = False,
) -> dict[str, Any]:
    """Classify a delivered-but-quiet unit of work from durable-record facts.

    All four inputs are read from the Redmine issue, never from a pane:

    - ``dispatch_delivered`` — a durable dispatch journal (Start /
      implementation_request / coordinator routing) exists on the issue.
    - ``new_durable_progress`` — a newer gate / Progress Log journal appeared
      after the dispatch (implementation_done, review_request, ...).
    - ``callback`` — what the record shows about the *cross-lane* coordinator
      callback; one of the ``CALLBACK_*`` values.
    - ``stale_cli`` — corroborating signal that a recorded callback attempt
      failed on a stale installed CLI (only meaningful with
      ``callback == CALLBACK_DELIVERY_FAILED``).

    Returns a dict with ``state`` (one of the ``STATE_*`` constants),
    ``is_stall`` (bool — whether this is one of the four genuine stalls),
    ``summary``, an ordered ``recovery`` step list, and the shared
    ``invariants``. Pure: it raises only on an unknown ``callback`` value.
    """
    if callback not in _CALLBACK_VALUES:
        raise ValueError(
            f"callback={callback!r} is not a recognized callback state "
            f"(choices: {', '.join(sorted(_CALLBACK_VALUES))})"
        )

    state, summary, recovery = _classify(
        dispatch_delivered=dispatch_delivered,
        new_durable_progress=new_durable_progress,
        callback=callback,
        stale_cli=stale_cli,
    )

    return {
        "state": state,
        "is_stall": state in STALL_STATES,
        "dispatch_delivered": dispatch_delivered,
        "new_durable_progress": new_durable_progress,
        "callback": callback,
        "stale_cli": bool(stale_cli),
        "summary": summary,
        "recovery": recovery,
        # Invariants only apply to a genuine stall recovery; non-stalls just
        # read the durable anchor and proceed.
        "invariants": list(_RECOVERY_INVARIANTS) if state in STALL_STATES else [],
    }


def _classify(
    *,
    dispatch_delivered: bool,
    new_durable_progress: bool,
    callback: str,
    stale_cli: bool,
) -> tuple[str, str, list[str]]:
    if not dispatch_delivered:
        return (
            STATE_NOT_STALL_CANDIDATE,
            "no dispatch journal on the issue — nothing was handed off, so this "
            "is not a stall candidate",
            [
                "a stall candidate requires a delivered dispatch journal "
                "(Start / implementation_request / coordinator routing)",
                "if work should start, record the dispatch journal first, then "
                "send the handoff (durable record before pane notification)",
            ],
        )

    # A recorded delivery failure is the discriminator regardless of whether a
    # progress journal also landed (covers the stale-CLI sub-case).
    if callback == CALLBACK_DELIVERY_FAILED:
        recovery = [
            "read the recorded callback attempt on the issue (it should carry "
            "the blocked reason, candidate panes, and a retry command)",
            "re-resolve the target: `--target coordinator` (workspace-scoped, "
            "fail-closed), else `mozyo-bridge agents targets` then explicit "
            "`--target <coordinator_codex_%pane> --target-repo auto`",
        ]
        if stale_cli:
            recovery.append(_STALE_CLI_HINT)
        recovery.append(
            "pick up the advanced durable state directly if progress already "
            "landed; do NOT re-dispatch work the record shows as done"
        )
        return (
            STATE_CALLBACK_DELIVERY_FAILED,
            "the sublane tried to call back but the send failed (target "
            "resolution / window-binding preflight / stale CLI)",
            recovery,
        )

    if callback == CALLBACK_ACKED:
        return (
            STATE_CALLBACK_COMPLETE,
            "a coordinator callback is recorded — not a stall",
            [
                "read the named durable anchor (issue + gate journal) the "
                "callback points at and take the next action (audit / approval "
                "collection / close / routing)",
            ],
        )

    if callback == CALLBACK_NOT_REQUIRED:
        return (
            STATE_CALLBACK_NOT_REQUIRED,
            "callback explicitly recorded as not-attempted with a reason "
            "(e.g. this lane is the coordinator lane) — not a stall",
            [
                "no cross-lane callback applies; resume from the durable record",
            ],
        )

    # From here, callback is SAME_LANE_ONLY or ABSENT (no coordinator pointer).
    if not new_durable_progress:
        return (
            STATE_NO_PROGRESS_AFTER_HANDOFF,
            "delivery succeeded but no newer durable journal exists at all — "
            "genuinely blocked, mid-implementation, or never started",
            [
                "read the sublane's own issue / Progress Log (not its pane) to "
                "see which gate it is waiting on",
                "re-notify with the delivery anchor + the expected gate, or "
                "convert to an explicit blocker if it is stuck",
                _STALE_CLI_HINT,
            ],
        )

    if callback == CALLBACK_SAME_LANE_ONLY:
        return (
            STATE_PROGRESS_WITHOUT_CALLBACK,
            "a newer durable journal exists (surfaced to the lane's own Codex) "
            "but the cross-lane coordinator pointer is missing — work is not "
            "stopped, only the pointer is",
            [
                "pick up the advanced durable state directly (e.g. "
                "implementation_done / review_request) and resume the "
                "review / close flow from that gate",
                "do NOT re-dispatch the work the durable record already shows "
                "as advanced",
                "record that you picked the state up directly (the "
                "`progress_without_callback` resolution), so the next "
                "coordinator sees it was handled",
            ],
        )

    # callback == CALLBACK_ABSENT with progress present.
    return (
        STATE_CALLBACK_NOT_ATTEMPTED,
        "durable progress exists but neither a callback nor a receive-method "
        "journal was recorded — a process gap on the sublane side",
        [
            "pick up the advanced durable state directly; do NOT re-dispatch "
            "done work",
            "record the gap and nudge the sublane to complete its callback "
            "checklist: durable gate -> same-lane surfacing -> cross-lane "
            "`--target coordinator` callback -> callback outcome journal",
        ],
    )
