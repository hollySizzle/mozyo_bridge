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
    "delivery recorded). Read `auto_target_repo.subreason` on this outcome for which "
    "step failed: an unattested sender/target identity, a target in another "
    "workspace, or a target lane whose worktree binding is absent / unbound / not "
    "uniquely resolvable. Pass an explicit `--target-repo <target lane worktree>`, or "
    "repair that lane's worktree binding. Do NOT drop `--target-repo` to get past "
    "this: without it a relative `--workdir` resolves against the SENDER's cwd, which "
    "is the lane-external execution root this fence exists to prevent."
)

#: ``DeliveryOutcome`` narrative for an ``auto_target_repo_unresolved`` outcome.
#:
#: Deliberately does NOT name one cause (review j#95843 finding 1): the R3 text asserted the
#: worktree binding "did not resolve to exactly one live worktree", which is simply false for
#: the unattested-identity and foreign-workspace subreasons. The specific step lives in the
#: durable ``auto_target_repo.subreason`` field; the narrative states only what is true of
#: every case.
AUTO_TARGET_REPO_UNRESOLVED_NARRATIVE: str = (
    "`--target-repo auto` did not resolve the target's own repo root (Redmine "
    "#14249): under the herdr backend there is no target pane cwd to read, so auto "
    "resolves the target's frame from the resolved route identity and that lane's "
    "lifecycle worktree binding — and that resolution did not complete. See "
    "`auto_target_repo.subreason` for which step. Distinct from `target_repo_mismatch` "
    "(there an OBSERVED target repo disagreed with the asserted one); here nothing was "
    "observed to compare. Auto never falls back to the sender's own root. Handoff "
    "aborted before typing; no notification was typed."
)


#: Per-subreason repair for the terminal (stderr) ``--target-repo auto`` refusal
#: (review j#95911 finding 4). R4 printed ONE sentence for every refusal — "auto resolves the
#: TARGET lane's worktree from its lifecycle binding; repair that binding" — which names a
#: step `identity_unattested` / `foreign_workspace` never reach, and prescribes a repair that
#: cannot fix `lifecycle_store_upgrade_required`. stderr is the first surface a sender reads,
#: so generalising only the durable narrative left the wrong instruction where it is seen
#: first. Keyed by ``AutoTargetRoot.reason``; the fallback is true of every subreason.
AUTO_TARGET_REPO_SUBREASON_REPAIR: dict[str, str] = {
    "identity_unattested": (
        "the sender's or target's herdr identity is not fully attested, so there is no unit "
        "to resolve a frame from. Send from an attested lane agent, or pass an explicit "
        "`--target-repo <target lane worktree>`."
    ),
    "foreign_workspace": (
        "the target runs in a different workspace than the sender, whose worktrees are not "
        "enumerable from here. Pass an explicit `--target-repo <target lane worktree>`."
    ),
    "lane_binding_absent": (
        "no lifecycle row owns the target lane, so it has no authoritative worktree binding. "
        "Declare the lane, or pass an explicit `--target-repo <target lane worktree>`."
    ),
    "lane_binding_unbound": (
        "the target lane's lifecycle row carries an EMPTY worktree binding (a legacy row). "
        "Repair the lane's worktree binding, or pass an explicit `--target-repo <root>`."
    ),
    "lane_worktree_unresolved": (
        "the target lane's binding token matched no unique live worktree of this repo "
        "(pruned, moved, or non-unique). Restore / prune the worktree so exactly one matches, "
        "or pass an explicit `--target-repo <target lane worktree>`."
    ),
    "lifecycle_store_unreadable": (
        "the lane lifecycle authority could not be read, so no binding could be checked — "
        "fail-closed by design. Pass an explicit `--target-repo <target lane worktree>`."
    ),
    "lifecycle_store_upgrade_required": (
        "the shared lifecycle authority is a NEWER schema than this runtime can read. Re-run "
        "from the current up-to-date source CLI / installed facade; do NOT downgrade or "
        "repair the store."
    ),
}

#: True of every subreason — used when a new subreason has no entry above yet.
AUTO_TARGET_REPO_GENERIC_REPAIR: str = (
    "auto could not establish the target's frame. Pass an explicit "
    "`--target-repo <target lane worktree>`."
)


def auto_target_repo_die_message(subreason: str, detail: str) -> str:
    """The terminal (stderr) message for an ``--target-repo auto`` refusal.

    Carries the subreason, its OWN repair, and the one invariant that holds for all of them:
    never drop ``--target-repo`` to get past this. ``detail`` is the resolver's bounded,
    path-free sentence (see ``AutoTargetRoot.to_structured_dict``) — never raw exception text.
    """
    repair = AUTO_TARGET_REPO_SUBREASON_REPAIR.get(
        subreason, AUTO_TARGET_REPO_GENERIC_REPAIR
    )
    return (
        f"`--target-repo auto` did not resolve the target agent's repo root under the herdr "
        f"backend (subreason={subreason}): {detail}. {repair} Do NOT drop `--target-repo` to "
        "get past this: without it a relative `--workdir` resolves against the SENDER's cwd, "
        "which is the lane-external execution root this fence exists to prevent "
        "(Redmine #14249)."
    )


def auto_target_repo_lines(payload: "dict[str, str] | None") -> "list[str]":
    """Pasteable-record lines for the ``--target-repo auto`` refusal (review j#95911 F1).

    R4 put the subreason on the WIRE outcome only, while the ``next_action`` — which the
    markdown record does render — told the reader to go read it. The markdown is what
    ``--persist-delivery`` stores and what a human pastes into the ticket, so the one fact
    that discriminates these refusals has to be legible THERE, not just in the JSON.

    Renders the CLOSED-VOCABULARY tokens only (``subreason`` / ``basis``), never the free-text
    ``detail``: this record is published to a ticket, and a bounded token cannot carry a host
    path the way an interpolated message can (finding 2, same review). Empty list when the
    outcome is not an auto refusal, so every other record is byte-identical.
    """
    if not payload:
        return []
    subreason = str(payload.get("subreason") or "").strip() or "—"
    basis = str(payload.get("basis") or "").strip()
    line = f"- Auto target-repo: subreason `{subreason}`"
    return [f"{line} (basis `{basis}`)" if basis else line]


#: The #14249 execution-root fence pair, keyed by wire ``Reason``. Both are sender-owned
#: pre-send refusals whose wording lives here, so ``handoff.py`` dispatches them through one
#: lookup instead of a per-reason branch — which is also what keeps that oversized module
#: from growing a line for every reason this issue adds.
EXECUTION_ROOT_FENCE_NEXT_ACTION: dict[str, str] = {
    "execution_root_outside_target_repo": EXECUTION_ROOT_OUTSIDE_TARGET_REPO_NEXT_ACTION,
    "auto_target_repo_unresolved": AUTO_TARGET_REPO_UNRESOLVED_NEXT_ACTION,
}

#: Narrative twin of :data:`EXECUTION_ROOT_FENCE_NEXT_ACTION`.
EXECUTION_ROOT_FENCE_NARRATIVE: dict[str, str] = {
    "execution_root_outside_target_repo": EXECUTION_ROOT_OUTSIDE_TARGET_REPO_NARRATIVE,
    "auto_target_repo_unresolved": AUTO_TARGET_REPO_UNRESOLVED_NARRATIVE,
}


__all__ = (
    "auto_target_repo_lines",
    "auto_target_repo_die_message",
    "AUTO_TARGET_REPO_SUBREASON_REPAIR",
    "EXECUTION_ROOT_FENCE_NEXT_ACTION",
    "EXECUTION_ROOT_FENCE_NARRATIVE",
    "GATEWAY_ROUTE_BLOCKED_NEXT_ACTION",
    "GATEWAY_ROUTE_BLOCKED_NARRATIVE",
    "READER_UPGRADE_REQUIRED_NEXT_ACTION",
    "READER_UPGRADE_REQUIRED_NARRATIVE",
    "EXECUTION_ROOT_OUTSIDE_TARGET_REPO_NEXT_ACTION",
    "EXECUTION_ROOT_OUTSIDE_TARGET_REPO_NARRATIVE",
    "AUTO_TARGET_REPO_UNRESOLVED_NEXT_ACTION",
    "AUTO_TARGET_REPO_UNRESOLVED_NARRATIVE",
)
