"""Retire-intent dispatch: which of the seven retire intents runs, and with what probes.

Extracted from ``cmd_sublane_retire`` (Redmine #14499). The command module sat just under the
module-health threshold, so the sixth intent (``--retire-active-unbound-live-zero``) pushed it
over; the gate's remedy is to reduce, and the dispatch's home is the retire feature. This is a
pure relocation of the existing chain — every branch, guard, probe and ordering is byte-for-byte
what ``cmd_sublane_retire`` ran, plus the later sixth and seventh branches.

The seven intents are mutually exclusive (the caller rejects any combination up front, before this
runs), and each runs ONLY when the fail-closed preflight already permits retirement. The
patch-equivalent resolver is imported and called only when the literal ancestry probe did not
pass, so a literal-ancestor lane performs no extra Redmine read or git probe (#14066 review
j#82298 F2).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class RetireIntentResults:
    """Whichever intent ran, and its verdict. At most one field is ever non-``None``."""

    close_result: Optional[object] = None
    migration_result: Optional[object] = None
    reconcile_result: Optional[object] = None
    bound_retire_result: Optional[object] = None
    active_retire_result: Optional[object] = None
    unbound_retire_result: Optional[object] = None
    hibernated_unbound_retire_result: Optional[object] = None

    @property
    def actuated(self) -> Optional[object]:
        """The single verdict this run produced, or ``None`` for a preflight-only run."""
        for value in (
            self.close_result,
            self.migration_result,
            self.reconcile_result,
            self.bound_retire_result,
            self.active_retire_result,
            self.unbound_retire_result,
            self.hibernated_unbound_retire_result,
        ):
            if value is not None:
                return value
        return None

    @property
    def ok(self) -> bool:
        """Did the intent that ran prove it did its job? ``True`` when none ran."""
        verdict = self.actuated
        return True if verdict is None else bool(verdict.ok)


def dispatch_retire_intent(
    args: argparse.Namespace,
    repo_root: Path,
    *,
    may_retire: bool,
    worktree: Optional[str],
    evidence_target=None,
    absent_worktree=None,
) -> RetireIntentResults:
    """Run the one selected retire intent, or none (preflight-only). Never raises for a
    non-selected intent: an unset flag simply does not enter its branch.

    ``absent_worktree`` (Redmine #15789) is the already-resolved, already-admissible
    absent-checkout evidence for the two BOUND terminal retires, or ``None``. It is resolved by
    the caller — :func:`...sublane_retire_application.run_retire_application` — because the
    retire preflight's checkout scope depends on it: proving the absence only here would leave
    that preflight deciding scope from an unproven assertion. Passing it through unchanged keeps
    the single resolution point.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (  # noqa: E501
        LiveSublaneLifecycleOps,
    )

    # Redmine #13331: opt-in herdr guarded close. Only under backend: herdr, only with
    # --execute, and only when the preflight already permits retirement (may_retire), close
    # the lane workspace's managed gateway/worker agents. Never removes a worktree / deletes
    # a branch (still runbook per worktree-lifecycle-boundary.md); never touches a foreign
    # agent. The default (no --execute) path is byte-for-byte the preflight-only behaviour.
    #
    # Redmine #13841: --migrate-hibernated-legacy is the metadata-only path for a hibernated /
    # released LEGACY row (empty worktree binding) the guarded close can never retire (it blocks
    # forever on worktree_binding_unverified) and #13809 backfill does not cover (active-row
    # only). It launches / closes / resumes NO process. It and --execute are conflicting
    # destructive intents, so passing both is rejected up front (review j#79150 finding 3, the
    # guard at the top of this handler) — the branch below runs the migration in the exclusive
    # case where only --migrate-hibernated-legacy is set, and never the guarded close.
    close_result = None
    migration_result = None
    reconcile_result = None
    bound_retire_result = None
    active_retire_result = None
    unbound_retire_result = None
    hibernated_unbound_retire_result = None
    if getattr(args, "migrate_hibernated_legacy", False):
        if may_retire:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_legacy_retire import (  # noqa: E501
                run_hibernated_legacy_retire_migration,
            )

            # Head integration is an action-time invariant the retire preflight (run with
            # merge_on_retire=False) does not check: probe --branch's ancestry into
            # --integration-branch read-only. Unknown / non-ancestor fails closed downstream.
            ops = LiveSublaneLifecycleOps(repo_root=repo_root)
            head_integrated = ops.branch_integrated(
                getattr(args, "branch", None) or "",
                getattr(args, "integration_branch", None) or "",
            )
            # The --worktree's ACTUAL checked-out branch (review j#79150 finding 1): the
            # migration requires it to equal --branch, so the clean + integrated evidence
            # describes the worktree's real head and not an unrelated branch name.
            worktree_branch = (
                ops.branch_for(worktree) if worktree else None
            )
            migration_result = run_hibernated_legacy_retire_migration(
                args,
                repo_root,
                head_integrated=head_integrated,
                worktree_branch=worktree_branch,
            )
    elif getattr(args, "reconcile_hibernated_live", False):
        # Redmine #13842: the bounded live-pair reconcile for a hibernated / released legacy
        # row whose exact managed pair is live (the #13756 contradiction). Like the migration
        # it runs only when the preflight permits retirement (so the callback / review
        # obligations block upstream); unlike it, it verifies the exact live pair and hands off
        # to the #13754 guarded close. Never runs the plain guarded close below.
        if may_retire:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_live_reconcile import (  # noqa: E501
                run_hibernated_live_reconcile,
            )

            ops = LiveSublaneLifecycleOps(repo_root=repo_root)
            head_integrated = ops.branch_integrated(
                getattr(args, "branch", None) or "",
                getattr(args, "integration_branch", None) or "",
            )
            worktree_branch = ops.branch_for(worktree) if worktree else None
            reconcile_result = run_hibernated_live_reconcile(
                args,
                repo_root,
                head_integrated=head_integrated,
                worktree_branch=worktree_branch,
            )
    elif getattr(args, "retire_hibernated_bound", False):
        # Redmine #13845: the metadata-only TERMINAL retire for a hibernated / released BOUND
        # row whose live pair is already gone (#13810 j#79416). The #13754 guarded close leaves
        # it a permanent `zero_close_unproven` (there is nothing to close, yet the durable row
        # is not `retired`), and the #13841 migration / #13842 reconcile both refuse it because
        # they require an EMPTY worktree binding. Like them it runs only when the preflight
        # permits retirement (so the callback / review obligations block upstream), and it
        # launches / closes / resumes NO process. Never runs the plain guarded close below.
        if may_retire:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_retire import (  # noqa: E501
                run_hibernated_bound_retire,
            )

            ops = LiveSublaneLifecycleOps(repo_root=repo_root)
            head_integrated = ops.branch_integrated(
                getattr(args, "branch", None) or "",
                getattr(args, "integration_branch", None) or "",
            )
            worktree_branch = ops.branch_for(worktree) if worktree else None
            # Redmine #14066 review j#82298 F2: the literal-ancestor path must stay byte-identical
            # to #13845 — NO file IO / git probe / Redmine read / exception surface added. So the
            # patch-equivalent resolver is only imported AND called when the literal ancestry
            # probe did NOT pass. When --branch is a literal ancestor (head_integrated is True) the
            # resolver is never constructed and the retire runs exactly as before. On the
            # non-literal path the resolver fresh-reads the exact Redmine integration journal
            # (credential-gated authority) and recomputes patch-ids / origin reachability; ``None``
            # means no integration journal was supplied (the retire keeps its literal
            # ``head_not_integrated``), and every read / probe / fence failure is fail-closed.
            patch_equivalent = None
            if head_integrated is not True:
                from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_patch_equivalent_integration import (  # noqa: E501
                    resolve_patch_equivalent_integration,
                )

                patch_equivalent = resolve_patch_equivalent_integration(args, repo_root)
            bound_retire_result = run_hibernated_bound_retire(
                args,
                repo_root,
                head_integrated=head_integrated,
                worktree_branch=worktree_branch,
                patch_equivalent=patch_equivalent,
                absent_worktree=absent_worktree,
            )
    elif getattr(args, "retire_active_live_zero", False):
        # Redmine #14242: the metadata-only TERMINAL retire for an ACTIVE bound row whose live
        # pair is already gone (#14222 j#85208). The #13754 guarded close leaves it a permanent
        # `zero_close_unproven`, and #13845 refuses it (`not_hibernated_bound_state`) because its
        # CAS requires hibernated + released — an active row has neither. Like the siblings it
        # runs only when the preflight permits retirement (so the closed-issue / callback /
        # review obligations block upstream), and it launches / closes / resumes NO process.
        if may_retire:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_active_live_zero_retire import (  # noqa: E501
                run_active_live_zero_retire,
            )

            ops = LiveSublaneLifecycleOps(repo_root=repo_root)
            head_integrated = ops.branch_integrated(
                getattr(args, "branch", None) or "",
                getattr(args, "integration_branch", None) or "",
            )
            worktree_branch = ops.branch_for(worktree) if worktree else None
            # Same #14066 discipline as #13845: the patch-equivalent resolver is imported AND
            # called ONLY when the literal ancestry probe did not pass, so a literal-ancestor
            # lane performs no extra Redmine read / git probe.
            patch_equivalent = None
            if head_integrated is not True:
                from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_patch_equivalent_integration import (  # noqa: E501
                    resolve_patch_equivalent_integration,
                )

                patch_equivalent = resolve_patch_equivalent_integration(args, repo_root)
            active_retire_result = run_active_live_zero_retire(
                args,
                repo_root,
                head_integrated=head_integrated,
                worktree_branch=worktree_branch,
                patch_equivalent=patch_equivalent,
                absent_worktree=absent_worktree,
            )
    elif getattr(args, "retire_active_unbound_live_zero", False):
        # Redmine #14499: the metadata-only TERMINAL retire for an ACTIVE row that records NO
        # canonical worktree binding and whose live pair is already gone (#14456 j#87973). The
        # guarded close returns `worktree_binding_unverified` because there is nothing to
        # attest, #14242 refuses an EMPTY binding by construction, and #13841 / #13842 /
        # #13845 all require `hibernated`. Its identity fence is the caller-declared
        # (generation, revision) rather than a worktree token, so — unlike every sibling — it
        # neither needs nor probes a worktree.
        if may_retire:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_active_unbound_live_zero_retire import (  # noqa: E501
                run_active_unbound_live_zero_retire,
            )

            ops = LiveSublaneLifecycleOps(repo_root=repo_root)
            head_integrated = ops.branch_integrated(
                getattr(args, "branch", None) or "",
                getattr(args, "integration_branch", None) or "",
            )
            # Same #14066 discipline as the bound surfaces: the patch-equivalent resolver is
            # imported AND called ONLY when the literal ancestry probe did not pass.
            patch_equivalent = None
            if head_integrated is not True:
                from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_patch_equivalent_integration import (  # noqa: E501
                    resolve_patch_equivalent_integration,
                )

                patch_equivalent = resolve_patch_equivalent_integration(args, repo_root)
            unbound_retire_result = run_active_unbound_live_zero_retire(
                args,
                repo_root,
                head_integrated=head_integrated,
                patch_equivalent=patch_equivalent,
            )
    elif getattr(args, "retire_hibernated_unbound_live_zero", False):
        # Redmine #14716: HIBERNATED + RELEASED sibling of the active-unbound rail. It
        # retains a distinct state signature and CAS while sharing only the branch/inventory
        # measurements. No checkout is required or restored.
        if may_retire:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_unbound_live_zero_retire import (  # noqa: E501
                run_hibernated_unbound_live_zero_retire,
            )

            ops = LiveSublaneLifecycleOps(repo_root=repo_root)
            head_integrated = ops.branch_integrated(
                getattr(args, "branch", None) or "",
                getattr(args, "integration_branch", None) or "",
            )
            patch_equivalent = None
            if head_integrated is not True:
                from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_patch_equivalent_integration import (  # noqa: E501
                    resolve_patch_equivalent_integration,
                )

                patch_equivalent = resolve_patch_equivalent_integration(args, repo_root)
            hibernated_unbound_retire_result = (
                run_hibernated_unbound_live_zero_retire(
                    args,
                    repo_root,
                    head_integrated=head_integrated,
                    patch_equivalent=patch_equivalent,
                )
            )
    elif getattr(args, "execute", False) and may_retire:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_retire_actuation import (  # noqa: E501
            run_guarded_retire_close,
        )

        close_result = run_guarded_retire_close(
            args, repo_root, evidence_target=evidence_target
        )

    return RetireIntentResults(
        close_result=close_result,
        migration_result=migration_result,
        reconcile_result=reconcile_result,
        bound_retire_result=bound_retire_result,
        active_retire_result=active_retire_result,
        unbound_retire_result=unbound_retire_result,
        hibernated_unbound_retire_result=hibernated_unbound_retire_result,
    )


__all__ = ("RetireIntentResults", "dispatch_retire_intent")
