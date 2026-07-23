"""Send-time semantic preconditions for ``handoff send`` — the ONE list (Redmine #14219 T2b).

Five zero-send rules the application layer enforces after argparse has accepted the tokens:

* the effective mode (the canonical default is ``queue-enter`` when ``--mode`` is not given)
  refuses ``--force`` under ``queue-enter`` — the rail is restricted to agent panes and the
  canonical sender exits ``blocked``/``invalid_args`` before any delivery;
* a ``--submit-delay`` that the delivering rail cannot actually sleep — outside the executable
  domain ``[0, MAX_SUBMIT_DELAY_SECONDS]`` after the rail's own clamp — is refused, but ONLY
  where the delay is consumed: the ``pending`` rail returns before the sleep and the herdr
  ``standard`` rail carries no delay at all, so those keep their behavior;
* a ``custom`` kind carries its ``--summary`` (:func:`..handoff.build_notification_body` refuses
  the body otherwise);
* ``--select`` resolves the target semantically and is mutually exclusive with an explicit
  ``--target`` (``apply_handoff_selection`` dies before sending);
* ``--target-project`` is layered UNDER the Git repo identity and requires an explicit
  ``--target-repo`` gate (the admission pipeline refuses ``invalid_args`` / zero-send).

Each rule used to live only inline at its call site, which meant any OTHER reader wanting to know
"would this invocation actually send?" had to re-enumerate them — and the auto-hibernate evidence
parser did exactly that for the first rule, missed the other two, and shipped the drift as a
finding (#14219 j#86649 R12-F1); the queue-enter/force rule repeated the same drift from the
canonical side until it moved here (#14219 j#86679 R19-F3). This module is the shared, pure
decision: the call sites keep their own error rendering and side effects, but the CONDITION comes
from here, and a new rule added here reaches every consumer at once.
"""

from __future__ import annotations

import math
from typing import Optional

#: The canonical queue-enter mode token. Defined HERE — the shared-semantics leaf — because the
#: queue-enter/force rule lives here and ``handoff`` already imports this module (importing the
#: constant back from ``handoff`` would be circular); ``handoff`` re-exports it unchanged.
MODE_QUEUE_ENTER = "queue-enter"
MODE_PENDING = "pending"

#: The executable submit-delay domain's upper bound (seconds), a CONTRACT constant. The real
#: platform limit (epoch time_t overflow at ``now + delay``) moves with the host and the clock,
#: so it cannot anchor a record-static judgment; one hour sits far above any real settling delay
#: and far below every platform limit, and both the canonical sender and the evidence reader
#: refuse beyond it (#14219 j#86693 R22-F1).
MAX_SUBMIT_DELAY_SECONDS = 3600.0

#: Closed reason tokens, one per rule — the consumers key their own messages off these.
SEND_SEMANTIC_QUEUE_ENTER_FORCE = "queue_enter_refuses_force"
SEND_SEMANTIC_SUBMIT_DELAY_UNEXECUTABLE = "submit_delay_unexecutable"
SEND_SEMANTIC_CUSTOM_SUMMARY = "custom_kind_requires_summary"
SEND_SEMANTIC_SELECT_TARGET = "select_conflicts_with_explicit_target"
SEND_SEMANTIC_PROJECT_REPO = "target_project_requires_target_repo"

SEND_SEMANTIC_REASONS = frozenset({
    SEND_SEMANTIC_QUEUE_ENTER_FORCE,
    SEND_SEMANTIC_SUBMIT_DELAY_UNEXECUTABLE,
    SEND_SEMANTIC_CUSTOM_SUMMARY,
    SEND_SEMANTIC_SELECT_TARGET,
    SEND_SEMANTIC_PROJECT_REPO,
})


def effective_send_mode(mode: Optional[str]) -> str:
    """The canonical send mode after default normalization (pure).

    ``cmd_handoff_send`` defaults an unspecified ``--mode`` to ``queue-enter``; any reader of a
    recorded invocation must apply the SAME default or a bare ``--force`` reads differently in
    the record than at the shell (#14219 j#86679 R19-F3).
    """
    return mode or MODE_QUEUE_ENTER


def send_semantic_gap(
    *,
    kind: Optional[str] = None,
    summary: Optional[str] = None,
    select: bool = False,
    target: Optional[str] = None,
    target_project: Optional[str] = None,
    target_repo: Optional[str] = None,
    mode: Optional[str] = None,
    force: bool = False,
    submit_delay: Optional[float] = None,
    submit_delay_consumed: bool = True,
) -> Optional[str]:
    """The FIRST zero-send precondition the supplied fields violate, or ``None`` (pure).

    Fields a caller does not have are left at their defaults and their rules simply cannot fire —
    ``apply_handoff_selection`` asks with ``select``/``target`` only, the admission pipeline with
    ``target_project``/``target_repo`` only, ``cmd_handoff_send`` with ``mode``/``force``/
    ``submit_delay``, and the evidence parser with everything. ``mode`` is the RAW option value;
    the canonical default is applied here so every consumer normalizes identically.

    ``submit_delay`` mirrors the transport rail's own clamp (``max(0.0, ...)``, same argument
    order) and requires the clamped delay to sit inside the executable domain
    ``[0, MAX_SUBMIT_DELAY_SECONDS]``: the rail sleeps for the delay BEFORE pressing Enter, so
    a delay the platform cannot sleep (``inf``, or a huge finite value past the host's time_t
    epoch — a moving, host-dependent line, hence the fixed contract bound) never reaches Enter
    (#14219 j#86687 R21-F2 / j#86693 R22-F1). Negative and ``nan`` delays clamp to zero under
    that very expression and DELIVER, so they pass. The rule applies ONLY where the delay is
    consumed: the ``pending`` rail returns before the sleep (skipped here, by mode), and the
    herdr ``standard`` rail has no delay field — the canonical sender passes
    ``submit_delay_consumed=False`` for that backend; a reader that cannot know the backend
    keeps the fail-closed default ``True``.
    """
    if effective_send_mode(mode) == MODE_QUEUE_ENTER and force:
        return SEND_SEMANTIC_QUEUE_ENTER_FORCE
    if (
        submit_delay is not None
        and submit_delay_consumed
        and effective_send_mode(mode) != MODE_PENDING
    ):
        clamped = max(0.0, submit_delay)
        if not math.isfinite(clamped) or clamped > MAX_SUBMIT_DELAY_SECONDS:
            return SEND_SEMANTIC_SUBMIT_DELAY_UNEXECUTABLE
    if kind == "custom" and not summary:
        return SEND_SEMANTIC_CUSTOM_SUMMARY
    if select and target:
        return SEND_SEMANTIC_SELECT_TARGET
    if target_project and not target_repo:
        return SEND_SEMANTIC_PROJECT_REPO
    return None


def submit_delay_help() -> str:
    """The operator-facing ``--submit-delay`` help, derived from the rule it explains (pure).

    Hand-written help drifted from the clamp contract the very round it was added (#14219
    j#86702 R24-F1: it claimed "must be finite" while negative and NaN values clamp to zero and
    deliver) — so the ONE text lives beside the rule and every parser site calls this.
    """
    return (
        "Seconds to sleep after the text is observed and BEFORE Enter is "
        "pressed, on the rails that consume it (tmux standard / queue-enter; "
        "`--mode pending` parks without Enter and the herdr standard rail "
        "has no delay, so both ignore it). The value is judged AFTER the "
        "rail's own clamp, max(0.0, value): a negative or NaN value clamps "
        "to 0 and is accepted; the clamped delay must be at most "
        f"{MAX_SUBMIT_DELAY_SECONDS:.0f} seconds, and anything beyond that "
        "(inf, or a larger finite value) is refused before any text is "
        "typed."
    )


def send_semantic_message(reason: str) -> str:
    """The canonical operator-facing message for a send-semantic refusal (pure).

    Lives beside the rules so a call site keying its ``die`` text off the reason cannot drift
    from the rule it renders. The force message is byte-identical to the pre-authority text.
    """
    if reason == SEND_SEMANTIC_QUEUE_ENTER_FORCE:
        return (
            "--force is not allowed under --mode queue-enter; queue-enter is "
            "restricted to Claude/Codex agent panes and rejects non-agent "
            "targets even with operator override."
        )
    if reason == SEND_SEMANTIC_SUBMIT_DELAY_UNEXECUTABLE:
        return (
            "the effective --submit-delay after the rail's clamp, "
            "max(0.0, value), must be a finite number of seconds no greater "
            f"than {MAX_SUBMIT_DELAY_SECONDS:.0f} (negative and NaN values "
            "clamp to 0 and are accepted); the rail sleeps for the effective "
            "delay before pressing Enter, so a delay beyond that domain "
            "never delivers."
        )
    return f"handoff send refused: {reason}"


def default_body_for_kind(kind: str, receiver: str) -> str:
    """The deterministic default notification body for a non-``custom`` kind (pure).

    Moved here from ``handoff.py`` (which sits exactly at its module-health baseline) to fund the
    line budget for wiring ``build_notification_body`` to :func:`send_semantic_gap` — the wiring
    the shared-authority contract requires (#14219 j#86653 R13-F1). It is a send-time semantic in
    its own right: the body a kind implies when the operator supplies no summary.
    """
    if kind == "implementation_request":
        return f"implementation request ready for {receiver}"
    if kind == "design_consultation":
        return f"design consultation ready for {receiver}"
    if kind == "review_request":
        return f"review request ready for {receiver}"
    if kind == "review_result":
        return f"review result ready for {receiver}"
    if kind == "implementation_done":
        return f"implementation done; review handoff ready for {receiver}"
    if kind == "reply":
        return f"reply ready for {receiver}"
    return f"handoff ready for {receiver}"


__all__ = [
    "SEND_SEMANTIC_CUSTOM_SUMMARY",
    "SEND_SEMANTIC_PROJECT_REPO",
    "SEND_SEMANTIC_QUEUE_ENTER_FORCE",
    "SEND_SEMANTIC_REASONS",
    "SEND_SEMANTIC_SELECT_TARGET",
    "SEND_SEMANTIC_SUBMIT_DELAY_UNEXECUTABLE",
    "MAX_SUBMIT_DELAY_SECONDS",
    "MODE_PENDING",
    "default_body_for_kind",
    "effective_send_mode",
    "send_semantic_gap",
    "send_semantic_message",
    "submit_delay_help",
]
