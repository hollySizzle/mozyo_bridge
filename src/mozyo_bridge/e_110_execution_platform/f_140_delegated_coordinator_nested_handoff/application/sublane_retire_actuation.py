"""Guarded retire close actuation + its fail-closed verdict (Redmine #13754).

The actuation half of ``sublane retire --execute``, extracted from
``sublane_lifecycle_command`` so the command boundary stays a thin composition site (the
same split ``sublane_process_release`` made for the supersede / hibernate release path).

The command's preflight (``may_retire``) says a retire is *permitted*. This module says
whether one *happened* — a distinction the pre-#13754 code did not draw, and whose
absence let a mis-aimed target root report a successful retire of a still-live pair
(#13748 j#77473: ``retire_ok`` + ``workspace_id: ""`` + ``closed: []`` + exit 0).

Three concerns live here:

- :func:`run_guarded_retire_close` — resolve the lane's unit from the ``--worktree``
  anchor, plan + execute the managed-slot close, and return a
  :class:`~...sublane_herdr_retire.RetireActuation` verdict. Every failure to resolve the
  lane (no anchor, no workspace identity, an unreadable inventory, an unresolved /
  unlaunchable provider binding) is **blocked with a reason**, never a silent empty
  close result.
- :func:`record_lane_retired_disposition` — after a real close, CAS the lane's durable
  disposition to the #13689 terminal ``retired``. It already gates the send path
  (``gateway_route_enforcement``: ``BLOCKED_LANE_RETIRED``) but had no writer, so a
  retired lane still read as active and a re-run had no durable fact to prove its no-op
  against.
- :func:`lane_retired_durably` — the fail-closed read of that fact, the proof half of a
  verified idempotent no-op.

Boundary (unchanged): never removes a worktree, deletes a branch, closes a foreign /
another lane's agent, or touches the project workspace or its default-lane coordinator
pair. What changed is only that a retire which proved nothing no longer exits 0.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import replace
from pathlib import Path

def run_guarded_retire_close(args: argparse.Namespace, repo_root: Path):
    """Guarded herdr retire close, or ``None`` when not on the herdr backend (Redmine #13377).

    Resolves the lane's unit from the ``--worktree`` anchor — the shared project
    workspace segment + the requested ``--lane-label`` (design j#73613), plus the legacy
    pre-#13377 per-lane ``wt_<hash>`` twin — plans the managed-slot close from the live
    herdr inventory, and executes it. The close never touches the project workspace, the
    default-lane coordinator pair, or another lane's slots.

    Returns a :class:`RetireActuation` verdict (Redmine #13754). Every way the lane can
    fail to resolve — a missing ``--worktree``, a root that carries no workspace anchor
    (the #13748 j#77473 mis-aimed integration worktree), an unreadable inventory, an
    unresolved / unlaunchable provider binding — is **blocked with a reason**, never a
    silent empty close result. Fail-safe is preserved (no crash, no foreign close, no
    guessed pane); what changes is that a failure to resolve is no longer reported as a
    successful retire.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        list_herdr_agent_rows,
        repo_backend_is_herdr,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
        ACTUATION_CLOSED,
        REASON_INVENTORY_UNREADABLE,
        REASON_NO_WORKTREE_ANCHOR,
        REASON_PROVIDER_NOT_LAUNCHABLE,
        REASON_PROVIDER_UNRESOLVED,
        REASON_WORKSPACE_UNRESOLVED,
        blocked_actuation,
        decide_retire_actuation,
        execute_herdr_retire_close,
        expected_live_slots,
        plan_herdr_retire_close,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
        WorkflowProviderUnresolved,
        resolve_gateway_provider,
        resolve_worker_provider,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
        HerdrSessionStartError,
        herdr_workspace_segment,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        derive_directory_lane_token,
        derive_lane_workspace_token,
    )

    if not repo_backend_is_herdr(repo_root):
        return None
    worktree = getattr(args, "worktree", None)
    lane_label = (getattr(args, "lane_label", "") or "").strip()
    if not worktree:
        return blocked_actuation(
            REASON_NO_WORKTREE_ANCHOR,
            detail=(
                "--execute needs the lane's --worktree anchor to resolve the managed "
                "target; without it no lane identity can be established"
            ),
            lane_id=lane_label,
        )
    # Resolve the lane's unit through the shared resolver: the worktree inherits the
    # project workspace identity (#13377), and its stable path token names the legacy
    # pre-#13377 per-lane workspace (compatibility close) plus the metadata tombstone key.
    try:
        resolved_worktree = Path(worktree).expanduser().resolve()
        workspace_id = herdr_workspace_segment(resolved_worktree)
    except (OSError, ValueError) as exc:
        return blocked_actuation(
            REASON_WORKSPACE_UNRESOLVED,
            detail=f"--worktree does not resolve ({type(exc).__name__})",
            lane_id=lane_label,
        )
    # #13392: a non-git (directory scaffold) lane runs in the workspace root itself — the
    # ``--worktree`` anchor collapses to the workspace root (== ``repo_root``), exactly as
    # the create site collapsed it. Such a lane has no ``wt_<hash>`` per-lane workspace
    # twin, and its metadata record is keyed on the lane-scoped ``dl_`` token (matching the
    # non-git create site). A Git lane's distinct worktree keeps the path-derived ``wt_``
    # token both as the legacy twin and as the tombstone key.
    try:
        collapsed_to_root = resolved_worktree == repo_root.expanduser().resolve()
    except OSError:
        collapsed_to_root = False
    if collapsed_to_root:
        legacy_token = ""
        metadata_token = derive_directory_lane_token(str(resolved_worktree), lane_label)
    else:
        legacy_token = derive_lane_workspace_token(str(resolved_worktree))
        metadata_token = legacy_token
    # The #13748 j#77473 defect: an integration worktree carries no workspace anchor, so
    # the segment resolved EMPTY and the close matched nothing — yet exited 0. An
    # unresolved target identity is now a blocker, not a retire.
    if not workspace_id and not legacy_token:
        return blocked_actuation(
            REASON_WORKSPACE_UNRESOLVED,
            detail=(
                "the --worktree root carries no herdr workspace anchor and no lane "
                "token; the managed target cannot be identified (point --repo / "
                "--worktree at the lane's own checkout)"
            ),
            lane_id=lane_label,
        )
    # Action-time retire-target attestation (Redmine #13754 F1 j#78475 + R2-F1 j#78528,
    # design j#78572 A+C): before ANY close, prove the requested (issue, lane, --worktree)
    # name ONE durable lane unit, against the fail-closed #13689 lifecycle store. Under the
    # shared project workspace model (#13377) the worktree resolves only the project
    # workspace (not the lane), so both the requested lane_label AND the requested
    # --worktree could belong to a DIFFERENT lane; the lifecycle owner binding (issue) and
    # the recorded worktree binding are the fail-closed authorities that tie them together.
    # This runs for EVERY execute path (shared and legacy alike, design j#78572): a legacy
    # / unbound lane with no recorded binding, or a workspace that cannot be keyed, fails
    # closed here — never a silent close of an unattested pair.
    attested, reason, detail = attest_retire_target(
        workspace_id,
        lane_label,
        issue=getattr(args, "issue", "") or "",
        worktree_identity=metadata_token,
    )
    if not attested:
        return blocked_actuation(
            reason, detail=detail, workspace_id=workspace_id, lane_id=lane_label
        )
    try:
        rows = list_herdr_agent_rows(os.environ)
    except HerdrSessionStartError as exc:
        # An unreadable inventory is NOT an empty one: folding it to "nothing live" is
        # how an unreadable runtime became a successful retire (Redmine #13682 R1-F1
        # pins the same distinction for hibernate).
        return blocked_actuation(
            REASON_INVENTORY_UNREADABLE,
            detail=f"live herdr inventory unreadable ({exc}); liveness cannot be measured",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    # The managed slots to retire are the providers the repo-local binding assigns to the
    # lane's gateway / worker roles (Redmine #13569 Increment 2B): default (codex, claude),
    # byte-identical. A rebound lane retires ITS slots, and a provider the binding does not
    # assign is never a retire target. An unbound role (impossible under the default) fails
    # closed to zero-actuation rather than closing a guessed pane.
    try:
        managed_roles = (
            resolve_gateway_provider(str(repo_root)),
            resolve_worker_provider(str(repo_root)),
        )
    except WorkflowProviderUnresolved as exc:
        return blocked_actuation(
            REASON_PROVIDER_UNRESOLVED,
            detail=f"workflow provider binding unresolved ({exc})",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    # Launchability gate (Redmine #13569 R2-F4b): a managed provider that is unknown or not
    # mechanically launchable is not a lane this retire can trust to identify — a binding to a
    # non-existent provider must never close a guessed pane. Fail closed to zero-actuation
    # before planning. Built-in codex/claude are always launchable, so this is byte-identical.
    from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_runtime import (  # noqa: E501
        BUILTIN_AGENT_PROVIDER_SNAPSHOT,
    )

    if not all(BUILTIN_AGENT_PROVIDER_SNAPSHOT.is_launchable(p) for p in managed_roles):
        return blocked_actuation(
            REASON_PROVIDER_NOT_LAUNCHABLE,
            detail=(
                "the binding assigns a provider that is not mechanically launchable; "
                "the lane's managed pair cannot be trusted to identify"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    plan = plan_herdr_retire_close(
        rows,
        workspace_id=workspace_id,
        lane_id=lane_label,
        legacy_workspace_id=legacy_token,
        managed_roles=managed_roles,
    )
    result = execute_herdr_retire_close(plan)
    # The zero-close fence (Redmine #13754): a close that closed nothing is a retire ONLY
    # when both authorities agree the lane is gone — the durable lifecycle records it
    # retired (read fail-closed) AND the live inventory shows zero expected managed slots.
    actuation = decide_retire_actuation(
        plan,
        result,
        expected_live=expected_live_slots(rows, plan, managed_roles=managed_roles),
        already_retired=lane_retired_durably(workspace_id, lane_label),
    )
    if actuation.state == ACTUATION_CLOSED:
        # A real close is what makes the lane retired; record that fact in the durable
        # lifecycle so the NEXT run can prove the idempotent no-op instead of guessing it
        # from an empty list.
        actuation = replace(
            actuation,
            durable_retirement=record_lane_retired_disposition(
                workspace_id=workspace_id,
                lane_label=lane_label,
                issue=getattr(args, "issue", "") or "",
                journal=getattr(args, "journal", "") or "",
            ),
        )
        # Best-effort lane metadata tombstone (Redmine #13356 j#73386 Q2): the retire
        # command boundary marks the lane's display-metadata record `retired` (kept as
        # a tombstone for late label resolution / residue diagnosis, never deleted
        # here). The record key is the same key the matching create site upsert on
        # (the ``wt_`` path token for a Git lane, the ``dl_`` lane-scoped token for a
        # non-git one). Never raises; an unrecorded lane simply stays unrecorded. This
        # is a DISPLAY join, never the retirement authority (``lane_metadata``: "never
        # routing authority", fail-open reader, no CAS) — the authority is the
        # lifecycle disposition recorded above.
        from mozyo_bridge.core.state.lane_metadata import record_lane_retired

        record_lane_retired(metadata_token)
    return actuation


def attest_retire_target(
    workspace_id: str, lane_label: str, *, issue: str, worktree_identity: str
) -> tuple[bool, str, str]:
    """Attest the requested ``(issue, lane, worktree)`` name ONE durable lane unit (#13754).

    Returns ``(attested, reason, detail)`` — ``attested`` False carries the blocked reason
    and a human detail; both empty when attested. Every axis fails **closed**.

    Two vectors this closes, both proven against the fail-closed #13689 lifecycle store:

    - **issue ↔ lane** (F1, j#78475): under the shared project workspace model (#13377)
      every lane's worktree resolves the SAME project ``workspace_id``, so the requested
      ``--lane-label`` could name a *different* lane. Require the lifecycle OWNER BINDING
      ``((repo_workspace_id, lane_id) -> issue_id)`` to name the requested issue.
    - **worktree ↔ lane** (R2-F1, j#78528, design j#78572 A+C): the ``--worktree`` (used
      for the dirty check and, historically, trusted as the target) must belong to THIS
      lane, or a sibling lane's clean worktree could pass the dirty check while another
      lane's pair closes. Require the lifecycle's recorded canonical worktree binding to
      equal the token the caller's ``--worktree`` resolves to.

    Deliberately NOT the ``lane_metadata`` join for either axis: it is documented
    display-only ("never routing authority"), fails **open**, and carries no CAS, so it
    cannot bear a fail-closed identity fact (design j#78572 forbids promoting it). A
    missing binding, a mismatch, an unkeyable unit, or an unreadable store is NOT attested.
    """
    from mozyo_bridge.core.state.lane_lifecycle import (
        LaneLifecycleError,
        LaneLifecycleKey,
        LaneLifecycleStore,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
        REASON_ISSUE_LANE_MISMATCH,
        REASON_LANE_OWNER_UNVERIFIED,
        REASON_LIFECYCLE_UNREADABLE,
        REASON_WORKTREE_BINDING_MISMATCH,
        REASON_WORKTREE_BINDING_UNVERIFIED,
    )

    want_issue = (issue or "").strip()
    want_worktree = (worktree_identity or "").strip()
    if not want_issue:
        return (
            False,
            REASON_LANE_OWNER_UNVERIFIED,
            "no --issue supplied to attest the lane's durable owner binding against; "
            "the requested lane cannot be confirmed as the close target",
        )
    if not want_worktree:
        return (
            False,
            REASON_WORKTREE_BINDING_UNVERIFIED,
            "the --worktree did not resolve to a canonical lane token; its binding to "
            "the lane cannot be verified before a close",
        )
    try:
        key = LaneLifecycleKey(workspace_id, lane_label)
    except ValueError:
        return (
            False,
            REASON_LANE_OWNER_UNVERIFIED,
            "the lane unit cannot be keyed (empty workspace / lane); its owner binding "
            "cannot be verified before a close",
        )
    try:
        record = LaneLifecycleStore().get(key)
    except (LaneLifecycleError, OSError) as exc:
        return (
            False,
            REASON_LIFECYCLE_UNREADABLE,
            f"the lifecycle store is unreadable ({type(exc).__name__}); the lane's "
            "binding cannot be verified, so the close fails closed",
        )
    if record is None:
        return (
            False,
            REASON_LANE_OWNER_UNVERIFIED,
            "the lane unit has no durable lifecycle owner binding; its identity cannot be "
            "attested before a close (a lane declared with --journal carries one)",
        )
    if (record.issue_id or "").strip() != want_issue:
        owned = record.issue_id or "<none>"
        return (
            False,
            REASON_ISSUE_LANE_MISMATCH,
            f"the lane unit's durable owner binding is issue {owned}, not the requested "
            f"{want_issue}; refusing to close a lane the request does not name",
        )
    bound_worktree = (record.worktree_identity or "").strip()
    if not bound_worktree:
        return (
            False,
            REASON_WORKTREE_BINDING_UNVERIFIED,
            "the lane unit has no recorded worktree binding (a pre-#13754 / unbound "
            "row); its --worktree cannot be attested, so the close fails closed until "
            "the lane is re-declared",
        )
    if bound_worktree != want_worktree:
        return (
            False,
            REASON_WORKTREE_BINDING_MISMATCH,
            "the --worktree does not resolve to the lane's recorded worktree binding; "
            "refusing to close (the --worktree belongs to a different lane)",
        )
    return True, "", ""


def lane_retired_durably(workspace_id: str, lane_label: str) -> bool:
    """Does the durable lifecycle record this lane ``retired``? (fail-closed).

    The proof half of the verified idempotent no-op (Redmine #13754). The authority is
    the #13689 lane lifecycle component, whose readers fail **closed** by contract: an
    absent / unreadable store, an unkeyable lane, or any store error yields False —
    "not proven retired" — so a zero-close blocks rather than inventing a retirement.

    Deliberately NOT the ``lane_metadata`` tombstone: that component is documented
    display-only ("never routing authority"), fails **open** to empty, carries no CAS,
    and its upsert revives a tombstone — so it cannot carry a fail-closed fact.
    """
    from mozyo_bridge.core.state.lane_lifecycle import (
        DISPOSITION_RETIRED,
        LaneLifecycleError,
        LaneLifecycleKey,
        LaneLifecycleStore,
    )

    try:
        record = LaneLifecycleStore().get(LaneLifecycleKey(workspace_id, lane_label))
    except (LaneLifecycleError, ValueError, OSError):
        return False
    return record is not None and record.lane_disposition == DISPOSITION_RETIRED


def record_lane_retired_disposition(
    *, workspace_id: str, lane_label: str, issue: str, journal: str
) -> str:
    """CAS the lane's durable disposition to ``retired`` after a real close (#13754).

    ``retired`` is the #13689 terminal disposition every other disposition may reach
    (``active`` / ``hibernated`` / ``superseded`` -> ``retired``). It already gates the
    send path (``gateway_route_enforcement``: ``BLOCKED_LANE_RETIRED``) but had no
    writer — so a retired lane still read as active, and a re-run of ``retire --execute``
    had no durable fact to prove its no-op against. This is that writer.

    Best-effort like the sibling lifecycle writes at this command boundary: a store error
    never un-does the close that already happened. It returns the outcome token instead,
    so the JSON says plainly whether the retirement was durably recorded:

    - ``recorded`` — the CAS landed;
    - ``already_retired`` — the lane was already durably retired;
    - ``not_recorded:<reason>`` — the lane has no lifecycle row (an owner-unbound lane,
      created without a ``--journal`` anchor), no durable decision anchor to write with,
      or the CAS was refused. The panes ARE closed; only the durable fact is missing, and
      a later zero-close re-run will fail closed rather than silently pass.
    """
    from mozyo_bridge.core.state.lane_lifecycle import (
        DISPOSITION_RETIRED,
        DecisionPointer,
        DecisionPointerError,
        LaneLifecycleError,
        LaneLifecycleKey,
        LaneLifecycleStore,
    )

    try:
        key = LaneLifecycleKey(workspace_id, lane_label)
    except ValueError:
        return "not_recorded:unkeyable_lane"
    try:
        # The row must name the durable record that put it in its current state (#13689
        # R1-F5) — a retirement with no re-readable Redmine anchor is not writable here.
        decision = DecisionPointer(
            source="redmine", issue_id=issue, journal_id=journal
        )
    except DecisionPointerError:
        return "not_recorded:no_durable_decision_anchor"
    store = LaneLifecycleStore()
    try:
        record = store.get(key)
    except (LaneLifecycleError, OSError) as exc:
        return f"not_recorded:store_unreadable({type(exc).__name__})"
    if record is None:
        return "not_recorded:lane_not_declared"
    if record.lane_disposition == DISPOSITION_RETIRED:
        return "already_retired"
    try:
        outcome = store.transition_disposition(
            key,
            expected_disposition=record.lane_disposition,
            expected_revision=record.revision,
            target=DISPOSITION_RETIRED,
            decision=decision,
        )
    except (LaneLifecycleError, DecisionPointerError, ValueError, OSError) as exc:
        return f"not_recorded:store_error({type(exc).__name__})"
    return "recorded" if outcome.applied else f"not_recorded:{outcome.reason}"


def format_retire_close_text(result) -> str:
    """Render the actuation verdict (Redmine #13754).

    The text surface leads with the verdict, so an operator reading the terminal — like
    a coordinator reading the JSON — cannot mistake "closed nothing" for "retired".
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
        ACTUATION_VERIFIED_NOOP,
    )

    unit = result.workspace_id or "<unresolved>"
    if getattr(result, "lane_id", ""):
        unit = f"{unit} lane={result.lane_id}"
    header = f"  herdr retire close: {result.state}"
    if result.reason:
        header += f" ({result.reason})"
    lines = [f"{header} workspace={unit}"]
    if result.detail:
        lines.append(f"    {result.detail}")
    if not result.ok:
        lines.append("    -> fail-closed: lane NOT retired; the pair may still be live")
    for role, locator in result.closed:
        lines.append(f"    - closed {role} {locator}")
    for role, locator, detail in result.failed:
        lines.append(f"    ! close failed {role} {locator}: {detail}")
    if result.state == ACTUATION_VERIFIED_NOOP:
        lines.append("    - verified no-op: no managed lane agent left to close")
    if result.expected_live:
        lines.append(
            "    live expected managed slots: " + ", ".join(result.expected_live)
        )
    if result.durable_retirement:
        lines.append(f"    durable retirement: {result.durable_retirement}")
    if result.foreign_names:
        lines.append(
            "    (lane unit also has non-managed agents, recorded and never closed: "
            + ", ".join(result.foreign_names)
            + ")"
        )
    return "\n".join(lines)



__all__ = (
    "attest_retire_target",
    "format_retire_close_text",
    "lane_retired_durably",
    "record_lane_retired_disposition",
    "run_guarded_retire_close",
)
