"""Durable-record rendering for the ticketless no-anchor rails (Redmine #12750 split).

These pure renderers previously lived inline in ``domain/handoff.py`` as the
``_ticketless_*_lines`` helpers and the ``SOURCE_TICKETLESS`` branch of
``_anchor_pointer_or_dash``. They are factored into their own module so the
ticketless consultation / callback / work-intake **record rendering** is a
single, separately-testable unit, and so the anchored delivery-record renderer
in ``handoff.py`` no longer grows every time a ticketless field is added.

Every function here is pure over the plain ``dict`` projection that the
``DeliveryOutcome`` carries (``ticketless_callback`` / ``ticketless_consultation``
/ ``ticketless_work_intake`` and the anchor ``to_dict``). The output carries only
fixed lower-snake-case tokens / derived bools — no operator free text — so it is
durable-record safe, and it never renders or implies a fabricated Redmine
issue/journal: the no-anchor rails state plainly that the structured fields ARE
the durable record.
"""

from __future__ import annotations

from typing import Any, Optional


def ticketless_anchor_pointer(anchor_payload: dict[str, Any]) -> str:
    """Render the durable-record "Durable anchor" pointer for a ticketless payload.

    Redmine #12703 / #12740 / #12748: there is no ticket anchor; this names the
    source plainly so the record never implies a fabricated issue/journal, and
    points at the matching structured block rendered below — the forward
    consultation (#12740, :func:`ticketless_consultation_lines`), the forward
    work-intake (#12748, :func:`ticketless_work_intake_lines`), or the return
    callback (#12703, :func:`ticketless_callback_lines`). The discriminator is the
    presence of the rail-specific field on the anchor ``to_dict`` payload.
    """
    if anchor_payload.get("consultation_kind"):
        return "ticketless (no Redmine anchor — see ticketless consultation below)"
    if anchor_payload.get("work_shape"):
        return "ticketless (no Redmine anchor — see ticketless work-intake below)"
    return "ticketless (no Redmine anchor — see ticketless callback below)"


def ticketless_callback_lines(
    ticketless_callback: Optional[dict[str, Any]],
) -> list[str]:
    """Render the structured ticketless no-anchor callback result (Redmine #12703).

    Carries only fixed lower-snake-case tokens + a derived bool with no operator
    free text, so the full consultation result is durable-record safe and rendered
    in place — the receiver reads the result (and whether the next worker phase
    needs a real Redmine anchor) without re-reading the pane. Returns a single
    ``—`` line when no callback was injected (every anchored send/reply).
    """
    if not ticketless_callback:
        return ["- Ticketless callback: —"]
    classification = ticketless_callback.get("classification") or "—"
    dispatch = ticketless_callback.get("dispatch_decision") or "—"
    anchor_required = bool(ticketless_callback.get("redmine_anchor_required"))
    owner = ticketless_callback.get("next_action_owner") or "—"
    reason = ticketless_callback.get("callback_reason") or "—"
    read_contract = ticketless_callback.get("read_contract") or "—"
    return [
        f"- Ticketless callback: classification `{classification}`, "
        f"dispatch `{dispatch}`",
        "  - Redmine anchor required (next worker phase): "
        f"`{str(anchor_required).lower()}`",
        f"  - Workflow next-action owner: `{owner}`",
        f"  - Callback reason: `{reason}`",
        f"  - Read contract: `{read_contract}`",
    ]


def ticketless_consultation_lines(
    ticketless_consultation: Optional[dict[str, Any]],
) -> list[str]:
    """Render the structured forward ticketless consultation (Redmine #12740).

    Carries only fixed lower-snake-case tokens with no operator free text, so the
    full forward request + return contract is durable-record safe and rendered in
    place — the receiver gateway reads what is being consulted, which role to return
    the result to, via which product primitives, and that the worker-dispatch anchor
    gate is not relaxed — without re-reading the pane. Returns a single ``—`` line
    when no forward consultation was injected (every anchored send/reply and the
    return-callback rail).
    """
    if not ticketless_consultation:
        return ["- Ticketless consultation: —"]
    kind = ticketless_consultation.get("consultation_kind") or "—"
    callback_to = ticketless_consultation.get("callback_to_role") or "—"
    methods = ticketless_consultation.get("callback_methods") or []
    read_contract = ticketless_consultation.get("read_contract") or "—"
    anchor_rule = bool(
        ticketless_consultation.get("worker_dispatch_requires_anchor")
    )
    methods_text = ", ".join(f"`{m}`" for m in methods) or "—"
    return [
        f"- Ticketless consultation: kind `{kind}`",
        f"  - Return result to role: `{callback_to}`",
        f"  - Return via: {methods_text}",
        f"  - Read contract: `{read_contract}`",
        "  - Worker dispatch requires Redmine anchor: "
        f"`{str(anchor_rule).lower()}`",
    ]


def ticketless_work_intake_lines(
    ticketless_work_intake: Optional[dict[str, Any]],
) -> list[str]:
    """Render the structured forward ticketless work-intake (Redmine #12748).

    Carries only fixed lower-snake-case tokens with no operator free text, so the
    full forward request + return contract is durable-record safe and rendered in
    place — the child coordinator reads what work shape is being handed in, that it
    (not the parent) owns the Redmine anchor decision, which role to return the
    result to, via which product primitives, and that the worker-dispatch anchor
    gate is not relaxed — without re-reading the pane. Returns a single ``—`` line
    when no forward work-intake was injected (every other rail).
    """
    if not ticketless_work_intake:
        return ["- Ticketless work-intake: —"]
    shape = ticketless_work_intake.get("work_shape") or "—"
    owner = ticketless_work_intake.get("anchor_decision_owner") or "—"
    callback_to = ticketless_work_intake.get("callback_to_role") or "—"
    methods = ticketless_work_intake.get("callback_methods") or []
    read_contract = ticketless_work_intake.get("read_contract") or "—"
    parent_no_answer = bool(
        ticketless_work_intake.get("parent_must_not_answer_domain")
    )
    anchor_rule = bool(
        ticketless_work_intake.get("worker_dispatch_requires_anchor")
    )
    methods_text = ", ".join(f"`{m}`" for m in methods) or "—"
    return [
        f"- Ticketless work-intake: shape `{shape}`",
        f"  - Anchor decision owner: `{owner}`",
        f"  - Return result to role: `{callback_to}`",
        f"  - Return via: {methods_text}",
        f"  - Read contract: `{read_contract}`",
        "  - Parent must not answer domain/design: "
        f"`{str(parent_no_answer).lower()}`",
        "  - Worker dispatch requires Redmine anchor: "
        f"`{str(anchor_rule).lower()}`",
    ]


__all__ = [
    "ticketless_anchor_pointer",
    "ticketless_callback_lines",
    "ticketless_consultation_lines",
    "ticketless_work_intake_lines",
]
