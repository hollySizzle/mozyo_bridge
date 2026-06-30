"""Structured-outcome wording for the #12918 gateway-route block (pure constants).

The gateway-route enforcement *policy* lives in
:mod:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_route_enforcement`,
but that package imports ``handoff.KIND_LABELS``; ``handoff.py`` cannot import it
back without a cycle. So the two strings ``handoff.next_action_for`` /
``handoff._outcome_narrative`` need for the ``gateway_route_blocked`` reason live
here, in this small f_130 sibling, instead of growing the already-oversized
``handoff.py`` with inline prose. ``handoff.py`` references these constants; the
fail-closed ``die`` / advisory prose the CLI prints lives with the policy in the
f_140 module (``render_block_die_message`` / ``render_exception_advisory``).
"""

from __future__ import annotations

#: ``DeliveryOutcome.next_action`` for a ``gateway_route_blocked`` outcome — the
#: suggested safe route, carried in the structured command result (#12918
#: acceptance: "resolved receiver / blocked reason / suggested safe route").
GATEWAY_ROUTE_BLOCKED_NEXT_ACTION: str = (
    "route the implementation_request / review_result through the target lane's "
    "Codex gateway (`--to codex` to that lane's gateway pane), and let the gateway "
    "perform the same-lane Claude worker handoff. A direct coordinator-to-sublane-"
    "worker send is blocked; if a bypass is genuinely required, re-run with the "
    "explicit durable exception `--allow-direct-worker` (recorded distinctly)."
)

#: ``DeliveryOutcome`` narrative for a ``gateway_route_blocked`` outcome.
GATEWAY_ROUTE_BLOCKED_NARRATIVE: str = (
    "Gateway Route Enforcement gate (Redmine #12918): a governed "
    "implementation_request / review_result was addressed directly to a Claude "
    "worker in a different lane than the sender, bypassing that lane's Codex "
    "gateway. The governed route is coordinator -> sublane Codex gateway -> "
    "same-lane Claude worker; the direct send fails closed before any text is typed."
)


__all__ = (
    "GATEWAY_ROUTE_BLOCKED_NEXT_ACTION",
    "GATEWAY_ROUTE_BLOCKED_NARRATIVE",
)
