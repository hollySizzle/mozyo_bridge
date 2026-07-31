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


#: ``DeliveryOutcome.next_action`` for an ``execution_root_outside_target_repo``
#: outcome (Redmine #14249). The sender asserted a `--target-repo` AND a
#: `--workdir` that resolves outside it, so the two halves of the execution root
#: contradict each other. The owner is the SENDER: only the caller knows which of
#: the two it meant, and both repairs are sender-side.
EXECUTION_ROOT_OUTSIDE_TARGET_REPO_NEXT_ACTION: str = (
    "the resolved `--workdir` does not live under the asserted `--target-repo`, so "
    "the delivery would name an execution root outside the repo the receiver was "
    "gated into — refused before any injection (nothing typed, no Enter, no "
    "delivery recorded). Pass a `--workdir` inside the target repo (a relative "
    "`--workdir` resolves against `--target-repo`, so `.` is the target repo root), "
    "or drop `--target-repo` if the execution root genuinely lives outside it."
)

#: ``DeliveryOutcome`` narrative for an ``execution_root_outside_target_repo`` outcome.
EXECUTION_ROOT_OUTSIDE_TARGET_REPO_NARRATIVE: str = (
    "Execution-root containment fence (Redmine #14249): `--target-repo` asserted one "
    "repo root while `--workdir` resolved outside it, so the delivery would have "
    "pointed the receiver at an execution root beyond the lane it was gated into. "
    "Distinct from `target_repo_mismatch` (there the target PANE failed the repo "
    "gate); here the pane gate passed and the sender's own two flags disagree. "
    "Handoff aborted before typing; no notification was typed."
)


#: ``DeliveryOutcome.next_action`` for an ``auto_target_repo_unresolved`` outcome
#: (Redmine #14249 R2, review j#94499 finding 1). `--target-repo auto` asked which
#: repo the TARGET runs in and got no answer, so no root can be asserted. The owner
#: is the SENDER: the repairs (name the root explicitly, or repair the lane's
#: worktree binding) are both sender / operator side, and neither is "drop the flag"
#: — dropping `--target-repo` restores the sender-cwd execution root #14249 removed.
AUTO_TARGET_REPO_UNRESOLVED_NEXT_ACTION: str = (
    "`--target-repo auto` could not establish which repo the target runs in, so no "
    "repo root was asserted and nothing was injected (nothing typed, no Enter, no "
    "delivery recorded). Pass an explicit `--target-repo <target lane worktree>`, or "
    "repair the target lane's worktree binding so `auto` can resolve it. If the "
    "structured detail names the lifecycle authority as unreadable, the shared store "
    "is likely NEWER than this runtime — route through a current runtime (never "
    "downgrade the store). Do NOT drop `--target-repo` to get past this: without it a "
    "relative `--workdir` resolves against the SENDER's cwd, which is the lane-external "
    "execution root this fence exists to prevent."
)

#: ``DeliveryOutcome`` narrative for an ``auto_target_repo_unresolved`` outcome.
AUTO_TARGET_REPO_UNRESOLVED_NARRATIVE: str = (
    "`--target-repo auto` did not resolve the target's own repo root (Redmine "
    "#14249): under the herdr backend there is no target pane cwd to read, so auto "
    "resolves the target LANE's canonical worktree from its lifecycle worktree "
    "binding — and that binding did not resolve to exactly one live worktree. "
    "Distinct from `target_repo_mismatch` (there an OBSERVED target repo disagreed "
    "with the asserted one); here nothing was observed to compare. Auto never falls "
    "back to the sender's own root. Handoff aborted before typing; no notification "
    "was typed."
)


__all__ = (
    "GATEWAY_ROUTE_BLOCKED_NEXT_ACTION",
    "GATEWAY_ROUTE_BLOCKED_NARRATIVE",
    "READER_UPGRADE_REQUIRED_NEXT_ACTION",
    "READER_UPGRADE_REQUIRED_NARRATIVE",
    "EXECUTION_ROOT_OUTSIDE_TARGET_REPO_NEXT_ACTION",
    "EXECUTION_ROOT_OUTSIDE_TARGET_REPO_NARRATIVE",
    "AUTO_TARGET_REPO_UNRESOLVED_NEXT_ACTION",
    "AUTO_TARGET_REPO_UNRESOLVED_NARRATIVE",
)
