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


#: ``DeliveryOutcome.next_action`` for a ``reader_upgrade_required`` outcome (Redmine #13844
#: design 5). The target lane's lifecycle authority is fine — THIS source CLI's schema reader
#: is stale (the shared home store was migrated to a newer version by another lane), so the
#: safe route is the current compatible facade, never a raw DB downgrade.
READER_UPGRADE_REQUIRED_NEXT_ACTION: str = (
    "the shared lifecycle authority is a NEWER schema than this source CLI can read; do NOT "
    "downgrade or repair the DB. Re-run this send from the current up-to-date source CLI / "
    "installed facade (the lane worktree whose build matches the newer schema), which reads "
    "the authority natively. The store is left untouched (downgrade-safe)."
)

#: ``DeliveryOutcome`` narrative for a ``reader_upgrade_required`` outcome.
READER_UPGRADE_REQUIRED_NARRATIVE: str = (
    "Lifecycle reader-upgrade gate (Redmine #13844): the shared home lifecycle authority "
    "carries a schema version newer than this source CLI understands (a concurrent newer-"
    "schema lane migrated it). The read fails closed rather than downgrade / misread it; this "
    "is distinct from a generic gateway route block and from a corrupt / partial store. Route "
    "the send through the current compatible high-level facade. No notification was typed."
)


__all__ = (
    "GATEWAY_ROUTE_BLOCKED_NEXT_ACTION",
    "GATEWAY_ROUTE_BLOCKED_NARRATIVE",
    "READER_UPGRADE_REQUIRED_NEXT_ACTION",
    "READER_UPGRADE_REQUIRED_NARRATIVE",
)
