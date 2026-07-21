"""Typed input value object for the handoff orchestration (Redmine #13729).

``orchestrate_handoff`` historically read every one of its parsed inputs straight
off an ``argparse.Namespace`` via scattered ``getattr(args, "<field>", <default>)``
calls, and carried its per-command *entry policy* as loose keyword parameters
(``default_kind`` / ``require_receiver_binding`` / the three ``ticketless``
variants). :class:`HandoffCommandInput` is the frozen, typed representation of
**everything the handoff run needs from its parsed input** — the entry policy plus
the flat scalar fields — so the ``argparse.Namespace`` is confined to the adapter
and the ``commands.orchestrate_handoff`` facade and never reaches a routing /
target / gate / record helper (design j#78394; review j#78706 R1).

Immutability (review j#78706 R2): every field is an immutable scalar or ``None``,
and the two repeatable list inputs — ``callback_methods`` (set to
``list(CALLBACK_METHODS)`` by the herdr forward / project-gateway callers) and
``profile_field`` (argparse ``action="append"``) — are snapshotted into tuples by
the adapter, so mutating the original Namespace list can never mutate this value
object. Fields keep the raw parser value (no coercion): the facade applies its own
normalization (``inp.mode or MODE_QUEUE_ENTER``, ``float(inp.landing_timeout or
8.0)``, ...), so the substitution is byte-for-byte behaviour-preserving.

``target_repo`` is captured here as the *initial* parsed value; the facade copies
it into a mutable local resolution scalar for the ``--target-repo auto`` /
herdr-auto resolution (design j#78394 Task 3 owns the eventual resolver port), so
no gate re-reads a mutated Namespace attribute.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HandoffCommandInput:
    """Frozen, typed snapshot of a handoff run's parsed input + entry policy."""

    # --- entry policy (formerly loose keyword parameters) ------------------
    default_kind: str | None = None
    require_receiver_binding: bool = False
    ticketless: bool = False
    ticketless_consultation: bool = False
    ticketless_work_intake: bool = False

    # --- routing / receiver ------------------------------------------------
    to: str | None = None
    source: str | None = None
    kind: str | None = None
    mode: str | None = None
    force: bool = False
    summary: str | None = None

    # --- anchor (Redmine / Asana) ------------------------------------------
    task_id: str | None = None
    comment_id: str | None = None
    anchor_url: str | None = None
    issue: str | None = None
    journal: str | None = None

    # --- ticketless payloads (callback / consultation / work-intake) -------
    work_shape: str | None = None
    consultation_kind: str | None = None
    classification: str | None = None
    dispatch_decision: str | None = None
    workflow_next_owner: str | None = None
    callback_reason: str | None = None
    callback_to_role: str | None = None
    callback_methods: tuple[str, ...] | None = None
    read_contract: str | None = None
    forward_action_id: str = ""

    # --- target / activation ----------------------------------------------
    target: str | None = None
    target_repo: str | None = None
    target_lane: str | None = None
    target_project: str | None = None
    no_target_activation: bool = False
    restore_previous_active: bool = False

    # --- route gates -------------------------------------------------------
    allow_direct_worker: bool = False
    main_lane_exception: str | None = None

    # --- execution root / profile / contract -------------------------------
    workdir: str | None = None
    role_profile: str | None = None
    profile_field: tuple[str, ...] | None = None
    transition_role: str | None = None
    workflow_contract: str | None = None

    # --- transport rail knobs ----------------------------------------------
    read_lines: int | None = 50
    landing_timeout: float | None = None
    submit_delay: float | None = 0.2
    queue_enter_retry_window: float | None = None
    queue_enter_retry_interval: float | None = None

    # --- delivery record / outcome ----------------------------------------
    record_format: str | None = None
    record_command: str | None = None
    persist_delivery: bool = False
    submit_intent: str | None = None
    submit_delivery_id: str | None = None
