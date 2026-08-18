"""Lane-scoped shell-residue close (Redmine #14499 Required behavior 6).

The one destructive step the post-reboot convergence needs, kept as narrow as the evidence
allows. Live audit #13490 j#89060: 15 of 23 assigned herdr panes carry no managed agent at
all — a foreground ``-zsh``, cwd ``$HOME``, revision 0, status unknown — while their durable
assigned-name rows survive. Those rows keep the lane's units looking occupied, so the
terminal retire rails (which require a *positively empty* unit) can never fire.

Existing surfaces do not cover this:

- ``sublane retire --execute`` (#13754) closes a lane's managed slots, but only after its
  worktree-binding attestation — which is exactly what a post-reboot lane cannot pass — and
  it is a *retire*, carrying the whole close/callback/review preflight with it;
- ``sublane recover-stale`` (#13806) targets the single stale worker of an ACTIVE lane and
  **relaunches + redispatches** it under a generation-bound owner approval. Its job is to
  keep the lane working; here there is no work to resume and nothing should be launched.

So this rail does one thing: close the lane's own residue panes and nothing else. The
decision is the pure :func:`...domain.reboot_residue_close_plan.plan_residue_close` — an
exact assigned-name equality test plus the stale classification plus a stricter
no-recognised-activity guard plus a live-half pair fence. Everything not matched by name is
never even evaluated: foreign occupants, other lanes, other workspaces, and the project's
default-lane coordinator pair are structurally out of reach rather than filtered out late.

**Ordering (Required behavior 8).** This closes processes only. It never removes a worktree,
never deletes a branch or a commit, and never writes the lifecycle row — terminalization
stays with the retire rails, and worktree / git-administrative cleanup stays downstream of
*that*. Closing residue is what makes the terminal write's live-zero read honest, not a
substitute for it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reboot_residue_close_plan import (  # noqa: E501
    ResidueClosePlan,
    plan_residue_close,
)

#: Residue panes were closed. The only state a real close reports.
RESIDUE_CLOSED = "closed"
#: A verified no-op: the lane owns no residue right now. Idempotent replay lands here.
RESIDUE_NONE = "no_residue"
#: A read-only preflight run (no ``--execute``): the plan is reported, nothing was touched.
RESIDUE_PREFLIGHT = "preflight"
#: Fail-closed. Never exit 0.
RESIDUE_BLOCKED = "blocked"

#: Blocked reasons.
RESIDUE_NOT_HERDR_BACKEND = "not_herdr_backend"
RESIDUE_WORKSPACE_UNRESOLVED = "workspace_unresolved"
RESIDUE_INVENTORY_UNREADABLE = "inventory_unreadable"
RESIDUE_PROVIDER_UNRESOLVED = "provider_unresolved"
RESIDUE_LIFECYCLE_UNREADABLE = "lifecycle_unreadable"
#: No durable lifecycle row owns this ``(workspace, lane)``, or the row that does owns a
#: DIFFERENT issue. Closing then would be a foreign close driven by a caller-supplied label.
RESIDUE_LANE_OWNER_UNVERIFIED = "lane_owner_unverified"
#: A live agent occupies one of the lane's own slots, so the pair fence collapsed the plan.
RESIDUE_LIVE_PAIR_PRESENT = "live_pair_present"
#: One or more planned closes failed; the lane still holds residue.
RESIDUE_CLOSE_FAILED = "close_failed"
#: A managed launch / attestation write is in flight on this home right now.
RESIDUE_LAUNCH_IN_FLIGHT = "launch_in_flight"
#: Every planned target stopped qualifying between the plan and the close, so nothing was
#: closed (Redmine #14499 review j#89191 finding 3). Not a success: the lane still holds
#: slots that looked like residue moments ago, and reporting "no residue" would be the
#: unproven-no-op misread this surface exists to avoid.
RESIDUE_IDENTITY_MOVED = "residue_identity_moved"
#: Planned residue lacks a current-attestation/completed-v2 destructive license.
RESIDUE_GENERATION_UNVERIFIED = "residue_generation_unverified"
#: Advisory file locking is unavailable, so the launch/close exclusion cannot be honored.
RESIDUE_EXCLUSION_UNAVAILABLE = "exclusion_unavailable"


@dataclass(frozen=True)
class ResidueCloseVerdict:
    """The fail-closed verdict of a lane-scoped residue close."""

    state: str
    reason: str = ""
    detail: str = ""
    workspace_id: str = ""
    lane_id: str = ""
    plan: Optional[ResidueClosePlan] = None
    closed: tuple[tuple[str, str], ...] = ()
    failed: tuple[tuple[str, str, str], ...] = ()
    #: Targets the pre-close re-verification dropped (#14499 review j#89191 finding 3): the
    #: pair no longer qualified in a freshly read inventory, or the inventory could not be
    #: re-read. Distinct from ``failed`` — nothing was attempted on these.
    skipped: tuple[tuple[str, str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return self.state in (RESIDUE_CLOSED, RESIDUE_NONE, RESIDUE_PREFLIGHT)

    def as_payload(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "plan": None if self.plan is None else self.plan.as_payload(),
            "closed": [{"assigned_name": n, "locator": loc} for n, loc in self.closed],
            "failed": [
                {"assigned_name": n, "locator": loc, "detail": d}
                for n, loc, d in self.failed
            ],
            "skipped": [
                {"assigned_name": n, "locator": loc, "detail": d}
                for n, loc, d in self.skipped
            ],
        }


def _blocked(reason: str, *, detail: str, workspace_id: str = "", lane_id: str = "",
             plan: Optional[ResidueClosePlan] = None) -> ResidueCloseVerdict:
    return ResidueCloseVerdict(
        state=RESIDUE_BLOCKED,
        reason=reason,
        detail=detail,
        workspace_id=workspace_id,
        lane_id=lane_id,
        plan=plan,
    )


def run_residue_close(
    args: argparse.Namespace, repo_root: Path, *, execute: bool
) -> ResidueCloseVerdict:
    """Plan (and optionally perform) the lane's residue close, fail-closed throughout.

    Runs entirely under the home's attestation-store lock held EXCLUSIVE, for the same
    reason #14242's terminalizer does: every managed launch holds it SHARED across its whole
    actuation, so a launch in flight could otherwise mint a fresh pane into one of this
    lane's slots between the inventory read and the close — and this rail would then close a
    pane it never classified. Taking it exclusive makes the two mutually exclusive. The
    read-only preflight takes it too, so its reported plan describes a state that could not
    change under it.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        repo_backend_is_herdr,
        repo_scope_workspace_id,
    )

    lane_label = (getattr(args, "lane_label", "") or "").strip()
    issue = (getattr(args, "issue", "") or "").strip()
    if not repo_backend_is_herdr(repo_root):
        return _blocked(
            RESIDUE_NOT_HERDR_BACKEND,
            detail=(
                "the repo's terminal transport is not herdr; there is no assigned-name "
                "inventory to read and no residue concept to converge"
            ),
            lane_id=lane_label,
        )
    workspace_id = repo_scope_workspace_id(repo_root)
    if not workspace_id or not lane_label or not issue:
        return _blocked(
            RESIDUE_WORKSPACE_UNRESOLVED,
            detail=(
                "the lane unit needs a resolvable repo workspace identity, a --lane-label "
                "and an --issue; without all three the target cannot be established and no "
                "pane is touched"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )

    from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
        AttestationStoreLockBusy,
        AttestationStoreLockUnavailable,
        attestation_store_lock,
    )
    from mozyo_bridge.shared.paths import mozyo_bridge_home

    home = mozyo_bridge_home()
    try:
        with attestation_store_lock(
            home, exclusive=True, blocking=False
        ):
            return _close_under_exclusion(
                args,
                repo_root,
                workspace_id=workspace_id,
                lane_label=lane_label,
                issue=issue,
                execute=execute,
                home=home,
            )
    except AttestationStoreLockBusy:
        return _blocked(
            RESIDUE_LAUNCH_IN_FLIGHT,
            detail=(
                "the home's attestation-store lock is held by another operation; a "
                "managed launch or attestation write is in flight, so a slot classified now "
                "could be a live pane by the time it is closed. Nothing was read or written"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    except AttestationStoreLockUnavailable:
        return _blocked(
            RESIDUE_EXCLUSION_UNAVAILABLE,
            detail=(
                "advisory file locking is unavailable on this platform, so the "
                "launch / close exclusion cannot be honored; closing without it could close "
                "a pane that was relaunched under the read"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )


def _close_under_exclusion(
    args: argparse.Namespace,
    repo_root: Path,
    *,
    workspace_id: str,
    lane_label: str,
    issue: str,
    execute: bool,
    home: Path,
) -> ResidueCloseVerdict:
    """The action-time half, run while HOLDING the exclusive launch-exclusion lock."""
    from mozyo_bridge.core.state.lane_lifecycle import (
        LaneLifecycleError,
        LaneLifecycleKey,
        LaneLifecycleStore,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        list_herdr_agent_rows,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
        WorkflowProviderUnresolved,
        resolve_gateway_provider,
        resolve_worker_provider,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
        HerdrSessionStartError,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        derive_lane_workspace_token,
        is_lane_workspace_token,
    )

    # Owner attestation FIRST: the lane label is caller-supplied, and under the shared
    # project-workspace model a wrong label names a DIFFERENT live lane's slots. Requiring
    # the durable row to exist AND own this exact issue is what keeps a typo from closing a
    # working lane's panes (the #13754 R2-F1 foreign-close lesson, applied to this rail).
    try:
        key = LaneLifecycleKey(workspace_id, lane_label)
        record = LaneLifecycleStore(home=home).get(key)
    except ValueError:
        return _blocked(
            RESIDUE_WORKSPACE_UNRESOLVED,
            detail="the lane unit cannot be keyed (empty workspace / lane)",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    except (LaneLifecycleError, OSError) as exc:
        return _blocked(
            RESIDUE_LIFECYCLE_UNREADABLE,
            detail=(
                f"the lifecycle store is unreadable ({type(exc).__name__}); the lane's owner "
                "binding cannot be verified, so no pane is closed"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    if record is None or (record.issue_id or "").strip() != issue:
        return _blocked(
            RESIDUE_LANE_OWNER_UNVERIFIED,
            detail=(
                f"no durable lifecycle row binds lane {lane_label!r} to issue #{issue} "
                f"(found: {'no row' if record is None else 'issue ' + (record.issue_id or '<none>')}). "
                "The lane label is caller-supplied, so an unverified owner binding could aim "
                "this close at a different lane's live pair"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )

    try:
        rows = list_herdr_agent_rows(os.environ)
    except HerdrSessionStartError:
        return _blocked(
            RESIDUE_INVENTORY_UNREADABLE,
            detail="live herdr inventory unreadable; residue cannot be classified",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    try:
        managed_roles = (
            resolve_gateway_provider(str(repo_root)),
            resolve_worker_provider(str(repo_root)),
        )
    except WorkflowProviderUnresolved:
        return _blocked(
            RESIDUE_PROVIDER_UNRESOLVED,
            detail=(
                "workflow provider binding unresolved; the lane's expected slot "
                "names cannot be minted, so nothing can be matched exactly"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )

    legacy_token = ""
    worktree = getattr(args, "worktree", None)
    if worktree:
        try:
            candidate = derive_lane_workspace_token(
                str(Path(worktree).expanduser().resolve())
            )
            legacy_token = candidate if is_lane_workspace_token(candidate) else ""
        except (OSError, ValueError):
            legacy_token = ""

    plan = plan_residue_close(
        rows,
        workspace_id=workspace_id,
        lane_id=lane_label,
        legacy_workspace_id=legacy_token,
        managed_roles=managed_roles,
    )
    if plan.pair_fence_tripped:
        return _blocked(
            RESIDUE_LIVE_PAIR_PRESENT,
            detail=(
                "a live agent occupies one of this lane's own slots, so the lane is not in "
                "the residue shape; closing its partner could break a working pair. Drain "
                "the lane through the ordinary guarded close instead"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            plan=plan,
        )
    if not plan.has_targets:
        return ResidueCloseVerdict(
            state=RESIDUE_NONE,
            detail=(
                "the lane owns no shell residue: every one of its canonical slots is either "
                "absent, live, or reporting activity. A duplicate replay lands here"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            plan=plan,
        )
    if _residue_current_pins(
        plan, rows, home=home, workspace_id=workspace_id, lane_id=lane_label,
        legacy_workspace_id=legacy_token, managed_roles=managed_roles,
    ) is None:
        return _blocked(
            RESIDUE_GENERATION_UNVERIFIED,
            detail="one or more residue targets lack exact v4/completed-v2 current "
            "generation authority; nothing would be closed",
            workspace_id=workspace_id,
            lane_id=lane_label,
            plan=plan,
        )
    if not execute:
        return ResidueCloseVerdict(
            state=RESIDUE_PREFLIGHT,
            detail=(
                f"{len(plan.close_targets)} residue slot(s) would be closed; "
                f"{len(plan.preserved)} of the lane's own slot(s) preserved and "
                f"{len(plan.untouched_names)} other managed row(s) never evaluated. "
                "Re-run with --execute to close them"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            plan=plan,
        )
    closed, failed, skipped = _execute_closes(
        plan,
        workspace_id=workspace_id,
        lane_id=lane_label,
        legacy_workspace_id=legacy_token,
        managed_roles=managed_roles,
        home=home,
    )
    if failed:
        return ResidueCloseVerdict(
            state=RESIDUE_BLOCKED,
            reason=RESIDUE_CLOSE_FAILED,
            detail=(
                f"{len(failed)} residue slot(s) failed to close; the lane still holds "
                "residue, so a terminal retire's live-zero read would still be blocked"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            plan=plan,
            closed=closed,
            failed=failed,
            skipped=skipped,
        )
    if skipped and not closed:
        # Every target was dropped by the pre-close re-verification. Nothing was closed and
        # nothing failed, but this is not the verified "no residue" state either: the lane
        # still holds slots that looked like residue moments ago. Reporting it as a success
        # would be the #13748-class misread this whole surface exists to avoid.
        return ResidueCloseVerdict(
            state=RESIDUE_BLOCKED,
            reason=RESIDUE_IDENTITY_MOVED,
            detail=(
                f"all {len(skipped)} planned target(s) stopped qualifying between the plan "
                "and the close, so nothing was closed. Re-run to re-plan against the "
                "current inventory"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            plan=plan,
            skipped=skipped,
        )
    return ResidueCloseVerdict(
        state=RESIDUE_CLOSED,
        detail=(
            f"closed {len(closed)} shell-residue slot(s) of this lane"
            + (
                f"; {len(skipped)} target(s) were skipped because their identity moved "
                "between the plan and the close"
                if skipped
                else ""
            )
            + ". No worktree, branch or commit was touched and the lifecycle row is "
            "unchanged — terminalization remains a separate, explicitly-fenced step"
        ),
        workspace_id=workspace_id,
        lane_id=lane_label,
        plan=plan,
        closed=closed,
        skipped=skipped,
    )


def _execute_closes(
    plan: ResidueClosePlan,
    *,
    workspace_id: str,
    lane_id: str,
    legacy_workspace_id: str,
    managed_roles,
    home: Path,
):
    """Close each planned pane, re-verifying its identity immediately first (#14499).

    ``herdr pane close`` takes a **locator** and nothing else — it does not check which
    assigned name currently occupies that pane (``herdr_pane_lifecycle._close_base_pane`` →
    ``herdr pane close <pane_id>``). So a plan built from one inventory read and executed
    later is closing an address, not an identity: if a residue shell exits between the two
    and herdr reassigns that pane id, the close lands on whatever is there now (Redmine
    #14499 review j#89191 finding 3).

    Before each close this therefore re-reads the live inventory and re-runs the **whole**
    planner against it, requiring the exact ``(assigned_name, locator)`` pair to still be a
    target. Re-running the planner rather than spot-checking the row means every original
    condition is re-applied — byte-exact name, live locator, stale classification, no
    recognised activity, and the pair fence — so a lane that acquired a live half in the
    interval closes nothing at all. Anything that no longer qualifies is skipped and
    recorded, never closed. An inventory that cannot be re-read skips too (fail-closed).

    **Residual, not claimed solved.** This narrows the window to between the re-check and
    the ``pane close`` call; it cannot eliminate it. Closing an address atomically with a
    condition on its occupant would require a conditional-close primitive that herdr does
    not expose, and no amount of re-reading here substitutes for one.

    Per-target and non-fatal, mirroring the #13330 base pane reclaim / #13331 guarded close
    contract: one stuck pane does not hide the outcome of the others.
    """
    import subprocess

    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        list_herdr_agent_rows,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
        _close_base_pane,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
        HerdrSessionStartError,
        _resolve_binary_or_die,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
        COMMAND_TIMEOUT_SECONDS,
    )
    from .herdr_destructive_close_identity import (
        current_generation_release_pin,
        pinned_generations_absent,
    )

    binary = _resolve_binary_or_die(os.environ)
    closed: list[tuple[str, str]] = []
    failed: list[tuple[str, str, str]] = []
    skipped: list[tuple[str, str, str]] = []
    for name, locator in plan.close_targets:
        try:
            fresh_rows = list_herdr_agent_rows(os.environ)
        except HerdrSessionStartError:
            skipped.append(
                (
                    name,
                    locator,
                    "the live inventory could not be re-read before closing; the "
                    "target's identity could not be re-verified, so it was left alone",
                )
            )
            continue
        recheck = plan_residue_close(
            fresh_rows,
            workspace_id=workspace_id,
            lane_id=lane_id,
            legacy_workspace_id=legacy_workspace_id,
            managed_roles=managed_roles,
        )
        if (name, locator) not in recheck.close_targets:
            skipped.append(
                (
                    name,
                    locator,
                    "this exact (assigned name, locator) pair is no longer a residue target "
                    "in a freshly read inventory"
                    + (
                        " (a live agent appeared in one of the lane's slots)"
                        if recheck.pair_fence_tripped
                        else ""
                    )
                    + "; closing the locator now could land on a different occupant",
                )
            )
            continue
        target_identity = _residue_target_identity(
            name, workspace_id=workspace_id, lane_id=lane_id,
            legacy_workspace_id=legacy_workspace_id, managed_roles=managed_roles,
        )
        target_workspace, target_lane, role = target_identity or ("", "", "")
        pin = current_generation_release_pin(
            tuple(fresh_rows), home=home, workspace_id=target_workspace,
            lane_id=target_lane, role=role, assigned_name=name, locator=locator,
        ) if role else None
        if pin is None:
            skipped.append((
                name, locator,
                "the residue lacks exact v4/completed-v2 current-generation authority",
            ))
            continue
        ok, _detail = _close_base_pane(
            binary, locator, subprocess.run, COMMAND_TIMEOUT_SECONDS, os.environ
        )
        if ok:
            try:
                post_rows = tuple(list_herdr_agent_rows(os.environ))
                absent = pinned_generations_absent(
                    (pin,), post_rows, home=home, workspace_id=target_workspace,
                    lane_id=target_lane,
                )
            except Exception:  # noqa: BLE001 - a close report is not absence proof
                absent = False
            if absent:
                closed.append((name, locator))
            else:
                failed.append((name, locator, "terminal-bound close absence unproven"))
        else:
            failed.append((name, locator, "provider close failed"))
    return tuple(closed), tuple(failed), tuple(skipped)


def _residue_target_identity(
    name: str, *, workspace_id: str, lane_id: str,
    legacy_workspace_id: str, managed_roles,
):
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        DEFAULT_LANE, encode_assigned_name,
    )
    for role in managed_roles:
        if lane_id != DEFAULT_LANE and name == encode_assigned_name(
            workspace_id, role, lane_id
        ):
            return workspace_id, lane_id, role
        if legacy_workspace_id and name == encode_assigned_name(
            legacy_workspace_id, role, DEFAULT_LANE
        ):
            return legacy_workspace_id, DEFAULT_LANE, role
    return None


def _residue_current_pins(
    plan, rows, *, home: Path, workspace_id: str, lane_id: str,
    legacy_workspace_id: str, managed_roles,
):
    from .herdr_destructive_close_identity import current_generation_release_pin
    pins = []
    snapshot = tuple(rows)
    for name, locator in plan.close_targets:
        identity = _residue_target_identity(
            name, workspace_id=workspace_id, lane_id=lane_id,
            legacy_workspace_id=legacy_workspace_id, managed_roles=managed_roles,
        )
        target_workspace, target_lane, role = identity or ("", "", "")
        pin = current_generation_release_pin(
            snapshot, home=home, workspace_id=target_workspace, lane_id=target_lane,
            role=role, assigned_name=name, locator=locator,
        ) if role else None
        if pin is None:
            return None
        pins.append(pin)
    return tuple(pins)


def format_residue_close_text(verdict: ResidueCloseVerdict) -> str:
    lines = [
        f"sublane close-residue: {verdict.state}",
        f"  workspace: {verdict.workspace_id or '-'}",
        f"  lane: {verdict.lane_id or '-'}",
    ]
    if verdict.reason:
        lines.append(f"  reason: {verdict.reason}")
    if verdict.detail:
        lines.append(f"  detail: {verdict.detail}")
    plan = verdict.plan
    if plan is not None:
        if plan.close_targets:
            lines.append("  targets:")
            for name, locator in plan.close_targets:
                lines.append(f"    {name} @ {locator}")
        for candidate in plan.preserved:
            lines.append(
                f"  preserved: {candidate.assigned_name} "
                f"({candidate.preserved_reason}, status={candidate.runtime_status})"
            )
        if plan.untouched_names:
            lines.append(
                f"  never evaluated (not this lane's slots): {len(plan.untouched_names)}"
            )
    for name, locator in verdict.closed:
        lines.append(f"  closed: {name} @ {locator}")
    for name, locator, detail in verdict.skipped:
        lines.append(f"  skipped (identity moved): {name} @ {locator}: {detail}")
    for name, locator, detail in verdict.failed:
        lines.append(f"  FAILED: {name} @ {locator}: {detail}")
    return "\n".join(lines)


def cmd_sublane_close_residue(args: argparse.Namespace) -> int:
    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    verdict = run_residue_close(
        args, repo_root, execute=bool(getattr(args, "execute", False))
    )
    if bool(getattr(args, "json", False)):
        print(json.dumps(verdict.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_residue_close_text(verdict), file=sys.stdout)
    return 0 if verdict.ok else 1


def register_sublane_close_residue_parser(sublane_sub: Any) -> None:
    parser = sublane_sub.add_parser(
        "close-residue",
        help=(
            "Redmine #14499: close ONLY this lane's own post-reboot shell-residue panes — "
            "assigned-name rows that survive with no managed agent behind them (#13518), "
            "which keep the lane's units looking occupied and so block every terminal retire. "
            "A pane is closed only when its name is byte-exactly one of the lane's canonical "
            "slot names, it carries a locator, the shared liveness classifier reads it stale, "
            "AND it reports no recognised runtime activity; a live half in the lane collapses "
            "the whole plan to zero. Foreign occupants, other lanes, other workspaces and the "
            "project's default-lane coordinator pair are structurally out of reach. Requires "
            "the durable lifecycle row to bind --lane-label to --issue. Default is a read-only "
            "preflight. Launches / resumes NO process; removes no worktree, branch or commit, "
            "and never writes the lifecycle row (terminalization stays a separate step)."
        ),
    )
    parser.add_argument("--issue", required=True, help="Redmine issue id owning the lane")
    parser.add_argument(
        "--lane-label",
        dest="lane_label",
        required=True,
        help="Exact lane label whose residue is closed (e.g. issue_<id>_<slug>)",
    )
    parser.add_argument(
        "--worktree",
        default=None,
        help=(
            "Optional. Used ONLY to derive a pre-#13377 legacy twin token so the residue "
            "scan also covers that unit. Never attested, and not required — the recorded "
            "worktree is typically gone in exactly the situation this rail exists for."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Close the planned residue panes; otherwise read-only preflight only",
    )
    from mozyo_bridge.application.cli_common import add_repo_option

    add_repo_option(parser)
    parser.add_argument("--json", action="store_true", help="Emit structured JSON")
    parser.set_defaults(func=cmd_sublane_close_residue)


__all__ = (
    "RESIDUE_BLOCKED",
    "RESIDUE_CLOSED",
    "RESIDUE_CLOSE_FAILED",
    "RESIDUE_EXCLUSION_UNAVAILABLE",
    "RESIDUE_IDENTITY_MOVED",
    "RESIDUE_GENERATION_UNVERIFIED",
    "RESIDUE_INVENTORY_UNREADABLE",
    "RESIDUE_LANE_OWNER_UNVERIFIED",
    "RESIDUE_LAUNCH_IN_FLIGHT",
    "RESIDUE_LIFECYCLE_UNREADABLE",
    "RESIDUE_LIVE_PAIR_PRESENT",
    "RESIDUE_NONE",
    "RESIDUE_NOT_HERDR_BACKEND",
    "RESIDUE_PREFLIGHT",
    "RESIDUE_PROVIDER_UNRESOLVED",
    "RESIDUE_WORKSPACE_UNRESOLVED",
    "ResidueCloseVerdict",
    "cmd_sublane_close_residue",
    "format_residue_close_text",
    "register_sublane_close_residue_parser",
    "run_residue_close",
)
