"""Durable-record renderers for the #13686 actuator (Redmine #13686).

Rendering is not deciding. The two state machines
(:mod:`...domain.auto_integration_policy`, :mod:`...domain.retirement_cleanup_policy`)
answer "what may happen next"; this module turns an answer into the text a coordinator
posts to the durable record. Keeping it separate means a change to how a record reads can
never accidentally change what the actuator is allowed to do.

Both renderers emit only machine-readable decision fields plus the lane's own issue /
branch — never a private path or a pane id.

Neither writes a ``## Gate: <token>`` heading. The central preset's
``### Gate Heading Canonical Literal`` reserves that form for tokens the Gate Schema /
Journal Templates define, and these are the actuator's decision records — inputs to the
coordinator's integration journal, not that journal's gate heading. Minting
``## Gate: integration_disposition`` is exactly what the #14665 regression guard exists to
catch. For the same reason neither emits the ``integration_disposition`` evidence marker:
that marker is the coordinator's to write, from the canonical producer, on their own journal.
"""

from __future__ import annotations

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_policy import (
    IntegrationDecision,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (
    IntegrationActionRecord,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.retirement_cleanup_policy import (
    CleanupActionRecord,
    CleanupDecision,
)


def render_integration_action_journal(
    decision: IntegrationDecision,
    record: IntegrationActionRecord,
    *,
    integration_head: str = "",
) -> str:
    """Render an integration decision as a durable record (pure).

    Emits only machine-readable decision fields plus the action identity — never a private
    path or a pane id. ``integration_head`` is the exact commit the integration produced on
    the target, which for a merge commit differs from the source head; the two are kept
    separate for the same reason the Hibernate Evidence Marker Contract keeps them separate
    (a single head cannot prove a patch-equivalent or merge-commit integration).

    The heading is deliberately **not** a ``## Gate: <token>`` one. This is the actuator's
    decision record — an input to the coordinator's integration journal, not that journal's
    gate heading — and the central preset's ``### Gate Heading Canonical Literal`` reserves
    the ``## Gate:`` form for tokens the Gate Schema / Journal Templates define. Writing
    ``## Gate: integration_disposition`` would mint a gate token no vocabulary defines, which
    is exactly what the #14665 regression guard exists to catch. For the same reason this
    renderer does not emit the ``integration_disposition`` evidence marker: that marker is the
    coordinator's to write, from the canonical producer, on the coordinator's own journal.
    """
    # Keyed on ``is_blocked``, NOT on ``integrated``: an in-progress decision (push_waiting,
    # awaiting_ci, confirmation required) is neither, and rendering it under
    # ``## integration_blocked`` would put a refusal that never happened into a durable record.
    lines = [
        "## integration_blocked" if decision.is_blocked else "## integration action decision",
        "",
        f"- issue: #{record.issue}",
        f"- state: {decision.state}",
        f"- action_key: {decision.action_key}",
        f"- source_head: {record.source_head}",
        f"- integration_branch: {record.target_ref}",
        f"- expected_target_head: {record.expected_target_head}",
        f"- integration_head: {integration_head or 'none'}",
        f"- disposition: {decision.disposition}",
        f"- integration_ci: {decision.integration_ci}",
        f"- review_generation: {record.review_generation}",
    ]
    if decision.is_blocked:
        lines.append(f"- primary_reason: {decision.primary_reason}")
        lines.append("- blocked_reasons: " + ", ".join(decision.blocked_reasons))
        lines.append(
            "- next_action: coordinator callback (fail-closed; nothing integrated, "
            "no force push, no rebase, no ref deleted)"
        )
    else:
        lines.append(f"- next_step: {decision.next_step or 'none'}")
        lines.append(f"- reason: {decision.reason}")
    return "\n".join(lines)


def render_cleanup_journal(
    decision: CleanupDecision, record: CleanupActionRecord
) -> str:
    """Render a cleanup decision as a durable record (pure).

    The stage table is emitted in full so a reader sees which steps ran, which do not apply,
    and which were refused — the distinction the acceptance's "段階別 outcome" asks for.
    Only machine-readable fields and the lane's own branch / issue are emitted.

    As in the integration sibling, the heading is deliberately not a ``## Gate: <token>`` one:
    the central preset's ``### Gate Heading Canonical Literal`` reserves that form for tokens
    the Gate Schema / Journal Templates define, and ``retirement_cleanup`` is not one. This is
    the actuator's decision record, not a workflow gate journal.
    """
    lines = [
        "## retirement cleanup decision" if not decision.is_blocked else "## cleanup_blocked",
        "",
        f"- issue: #{record.issue}",
        f"- state: {decision.state}",
        f"- action_key: {decision.action_key}",
        f"- integration_action_key: {record.integration_action_key}",
        f"- branch: {record.branch}",
        f"- recorded_source_head: {record.recorded_source_head}",
    ]
    for step, outcome in decision.step_outcomes:
        lines.append(f"- step.{step}: {outcome}")
    if decision.is_blocked:
        lines.append(f"- primary_reason: {decision.primary_reason}")
        lines.append("- blocked_reasons: " + ", ".join(decision.blocked_reasons))
        lines.append(
            "- next_action: coordinator callback (fail-closed; no process released, and "
            "this machine removes no checkout and deletes no ref at all)"
        )
    else:
        lines.append(f"- next_step: {decision.next_step or 'none'}")
        lines.append(f"- reason: {decision.reason}")
    return "\n".join(lines)


__all__ = (
    "render_integration_action_journal",
    "render_cleanup_journal",
)
