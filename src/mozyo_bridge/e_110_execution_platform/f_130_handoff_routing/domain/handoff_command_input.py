"""Typed input value object for the handoff orchestration (Redmine #13729).

``orchestrate_handoff`` historically read every one of its parsed inputs straight
off an ``argparse.Namespace`` via scattered ``getattr(args, "<field>", <default>)``
calls, and carried its per-command *entry policy* as loose keyword parameters
(``default_kind`` / ``require_receiver_binding`` / the three ``ticketless``
variants). :class:`HandoffCommandInput` is the frozen, typed representation of
*everything the handoff run needs from its parsed input* — the entry policy plus
the flat scalar fields — so later tranches (#13729 design j#78394) can hand one
value object to a planning / target / route / transport service instead of a
``Namespace``.

This is tranche 1 of the OOP decomposition: it establishes the typed seam. The
Namespace is converted to this value object **once** by
:class:`~mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_command_input_adapter.HandoffNamespaceAdapter`
at the ``orchestrate_handoff`` boundary. Each field mirrors the raw value the
original ``getattr`` returned; the caller keeps whatever normalization it already
applied (``inp.mode or MODE_QUEUE_ENTER``, ``float(inp.landing_timeout or 8.0)``,
etc.), so the substitution is byte-for-byte behaviour-preserving.

Deliberately excluded: ``target_repo``. That field is *mutated in place* on the
Namespace by the ``--target-repo auto`` / herdr-auto resolution inside
``orchestrate_handoff`` and re-read by the not-yet-extracted target-resolution
helpers, so a frozen top-of-function snapshot would go stale. It stays on the
Namespace until the target-resolution tranche (design j#78394 Task 3) owns it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HandoffCommandInput:
    """Frozen, typed snapshot of a handoff run's parsed input + entry policy.

    Every field holds the raw value the corresponding ``getattr(args, ...)``
    returned in ``orchestrate_handoff``; no coercion happens here so the caller's
    existing ``or`` / ``int`` / ``float`` / ``bool`` normalization stays identical.
    """

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
    callback_methods: Any = None
    read_contract: str | None = None
    forward_action_id: str = ""

    # --- target / activation ----------------------------------------------
    target: str | None = None
    target_project: str | None = None
    no_target_activation: bool = False
    restore_previous_active: bool = False

    # --- execution root / profile / contract -------------------------------
    workdir: str | None = None
    role_profile: str | None = None
    profile_field: Any = None
    transition_role: str | None = None
    workflow_contract: str | None = None

    # --- transport rail knobs ----------------------------------------------
    read_lines: Any = 50
    landing_timeout: Any = None
    submit_delay: Any = 0.2
    queue_enter_retry_window: Any = None
    queue_enter_retry_interval: Any = None
