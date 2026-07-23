"""Send-time semantic preconditions for ``handoff send`` — the ONE list (Redmine #14219 T2b).

Four zero-send rules the application layer enforces after argparse has accepted the tokens:

* the effective mode (the canonical default is ``queue-enter`` when ``--mode`` is not given)
  refuses ``--force`` under ``queue-enter`` — the rail is restricted to agent panes and the
  canonical sender exits ``blocked``/``invalid_args`` before any delivery;
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

from typing import Optional

#: The canonical queue-enter mode token. Defined HERE — the shared-semantics leaf — because the
#: queue-enter/force rule lives here and ``handoff`` already imports this module (importing the
#: constant back from ``handoff`` would be circular); ``handoff`` re-exports it unchanged.
MODE_QUEUE_ENTER = "queue-enter"

#: Closed reason tokens, one per rule — the consumers key their own messages off these.
SEND_SEMANTIC_QUEUE_ENTER_FORCE = "queue_enter_refuses_force"
SEND_SEMANTIC_CUSTOM_SUMMARY = "custom_kind_requires_summary"
SEND_SEMANTIC_SELECT_TARGET = "select_conflicts_with_explicit_target"
SEND_SEMANTIC_PROJECT_REPO = "target_project_requires_target_repo"

SEND_SEMANTIC_REASONS = frozenset({
    SEND_SEMANTIC_QUEUE_ENTER_FORCE,
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
) -> Optional[str]:
    """The FIRST zero-send precondition the supplied fields violate, or ``None`` (pure).

    Fields a caller does not have are left at their defaults and their rules simply cannot fire —
    ``apply_handoff_selection`` asks with ``select``/``target`` only, the admission pipeline with
    ``target_project``/``target_repo`` only, ``cmd_handoff_send`` with ``mode``/``force`` only,
    and the evidence parser with everything. ``mode`` is the RAW option value; the canonical
    default is applied here so every consumer normalizes identically.
    """
    if effective_send_mode(mode) == MODE_QUEUE_ENTER and force:
        return SEND_SEMANTIC_QUEUE_ENTER_FORCE
    if kind == "custom" and not summary:
        return SEND_SEMANTIC_CUSTOM_SUMMARY
    if select and target:
        return SEND_SEMANTIC_SELECT_TARGET
    if target_project and not target_repo:
        return SEND_SEMANTIC_PROJECT_REPO
    return None


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
    "default_body_for_kind",
    "effective_send_mode",
    "send_semantic_gap",
]
