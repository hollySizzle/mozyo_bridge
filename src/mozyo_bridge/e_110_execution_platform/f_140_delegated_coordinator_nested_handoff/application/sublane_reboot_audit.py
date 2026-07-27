"""``sublane reboot-audit``: the single action-time convergence snapshot (Redmine #14499).

Required behavior 2 asks for one thing that did not exist: a **single action snapshot** that
joins the four authorities a post-reboot lane's disposition depends on —

- **Redmine** — is the lane's issue open or closed?
- **git** — does the recorded worktree still exist, does the branch still resolve, and is the
  lane's head reachable from the integration branch?
- **the durable lifecycle row** — disposition, owner binding, worktree binding, generation,
  revision, release state;
- **the live herdr inventory** — which assigned-name slots exist, which carry a locator, and
  which are backed by a real agent versus a #13518 shell residue.

Before this, reading them meant four commands with four failure modes, and the join was done
in an operator's head. Live audit #13490 j#89060 shows why that does not scale: 23 assigned
panes, 15 of them residue, 8 lanes, every recorded worktree gone — and every ``sublane list``
row reading ``detached`` while every lifecycle row read ``active``. The two are not in
conflict; they describe different axes. Only the join says what to do.

The classification itself is the pure
:func:`...domain.reboot_residue_convergence.plan_lane_convergence`; this module is its live
fact-gathering and rendering. It is **read-only**: no pane is closed, no lifecycle row is
written, no worktree or branch is touched. Its output names the exact next rail per lane, and
deliberately offers no all-lanes action (Required behavior 5) — the roll-up is a count, not a
button.

Every probe here degrades to *unknown* rather than to a value. An unreadable Redmine yields
``issue_closed=None``, which the pure planner turns into :data:`CONVERGE_UNKNOWN`; it never
becomes "open" or "closed" by default.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reboot_residue_convergence import (  # noqa: E501
    RebootLaneFacts,
    RebootLanePlan,
    plan_lane_convergence,
    slot_fact_from_row,
    summarize_convergences,
)

#: The default integration branch when the caller names none. Read from the repo's committed
#: sublane config where available; this constant is only the last-resort literal.
_DEFAULT_INTEGRATION_BRANCH = "main"


class RebootAuditUnavailable(RuntimeError):
    """A snapshot could not be produced because an AUTHORITY could not be read (#14499).

    Distinct from "this repo owns no lanes", which is a legitimate empty result. The
    lifecycle store being unreadable, or the repo's workspace identity being unresolvable,
    means the audit does not know what exists — and an audit that cannot see is not an audit
    that found nothing. The command surfaces this as a non-zero exit, unlike a lane-level
    ``unknown`` / ``blocked`` finding, which is a normal result of a successful snapshot.
    """


def read_issue_closed_states(
    issue_ids: Sequence[str],
    *,
    fetch: Optional[Callable[..., Mapping[str, object]]] = None,
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
) -> dict[str, Optional[bool]]:
    """Read each issue's durable open/closed state, or ``None`` where it cannot be read.

    Uses the same credential-gated, redirect-refusing read transport the callback intake
    uses (:func:`...live_redmine_journal_source.urllib_issue_detail_fetch`), so the API key
    only ever reaches the trusted base URL and never appears in a message. Credentials come
    from env / the home-scoped credential file only — never a repo-local file.

    **Unconfigured or unreachable is ``None``, never ``False``.** An issue whose state could
    not be read must not be treated as open (which would suppress a legitimate terminal
    convergence) nor as closed (which would propose terminalizing live work); the pure
    planner refuses to plan at all for it.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
        LiveRedmineJournalError,
        urllib_issue_detail_fetch,
    )
    from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_context import (  # noqa: E501
        normalize_base_url,
    )
    from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_credentials import (  # noqa: E501
        resolve_redmine_credentials,
    )

    wanted = tuple(sorted({(i or "").strip() for i in issue_ids if (i or "").strip()}))
    states: dict[str, Optional[bool]] = {issue: None for issue in wanted}
    if not wanted:
        return states
    transport = fetch or urllib_issue_detail_fetch
    try:
        credentials = resolve_redmine_credentials(home, environ=environ)
        base_url = normalize_base_url(credentials.base_url)
    except Exception:  # noqa: BLE001 - an unresolvable credential store leaves every state unknown
        return states
    if not credentials.api_key or not base_url:
        return states
    for issue in wanted:
        try:
            payload = transport(
                base_url=base_url,
                api_key=credentials.api_key,
                issue_id=issue,
                since=None,
            )
        except (LiveRedmineJournalError, OSError, ValueError):
            continue
        if not isinstance(payload, Mapping):
            continue
        detail = payload.get("issue")
        if not isinstance(detail, Mapping):
            continue
        status = detail.get("status")
        if not isinstance(status, Mapping) or "is_closed" not in status:
            # A payload shape that does not positively carry the flag stays unknown rather
            # than defaulting: Redmine always sends it, so its absence means we did not read
            # what we think we read.
            continue
        states[issue] = bool(status.get("is_closed"))
    return states


def _worktree_present(path: str) -> Optional[bool]:
    """Is ``path`` still a live git worktree root? ``None`` when it cannot be determined."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        is_git_worktree_root,
    )

    if not path:
        return None
    try:
        if not Path(path).expanduser().is_dir():
            # The characteristic post-reboot state: the recorded /private/tmp path is gone.
            return False
    except OSError:
        return None
    return bool(is_git_worktree_root(Path(path).expanduser()))


def _lane_slot_facts(
    rows: Sequence[Mapping[str, object]],
    *,
    workspace_id: str,
    lane_id: str,
    legacy_workspace_id: str,
    managed_roles: Sequence[str],
):
    """The lane's own slot facts plus its foreign occupants, from the live inventory.

    Reuses :func:`...sublane_herdr_retire.plan_herdr_retire_close` for the unit scoping and
    the foreign-occupant determination, so the audit reports exactly the units and exactly
    the foreign set the retire rails act on — a snapshot that scoped differently would
    describe a lane the actuation does not target.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
        expected_slot_rows,
        plan_herdr_retire_close,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        AGENT_KEY_NAME,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E501
        classify_named_slot,
    )

    plan = plan_herdr_retire_close(
        rows,
        workspace_id=workspace_id,
        lane_id=lane_id,
        legacy_workspace_id=legacy_workspace_id,
        managed_roles=managed_roles,
    )
    facts = [
        slot_fact_from_row(
            found.row,
            role=found.role,
            assigned_name=str(found.row.get(AGENT_KEY_NAME) or ""),
            locator=found.locator,
            liveness=classify_named_slot(found.row),
            foreign=False,
        )
        for found in expected_slot_rows(rows, plan, managed_roles=managed_roles)
    ]
    for name in plan.foreign_names:
        facts.append(
            slot_fact_from_row(
                {},
                role="",
                assigned_name=name,
                locator="",
                liveness="",
                foreign=True,
            )
        )
    return tuple(facts)


def gather_reboot_facts(
    repo_root: Path,
    *,
    integration_branch: str = "",
    issue_states: Optional[Mapping[str, Optional[bool]]] = None,
    rows: Optional[Sequence[Mapping[str, object]]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> tuple[RebootLaneFacts, ...]:
    """Join all four authorities into one :class:`RebootLaneFacts` per lane (#14499 RB2).

    Scoped to the lanes this repo's workspace owns: the lifecycle store is host-global, and
    reporting another project's lanes would invite acting on them. ``issue_states`` and
    ``rows`` are injectable so the join can be exercised without a network or a live herdr.

    Every axis fails to *unknown* independently: an unreadable lifecycle store yields no
    lanes at all (there is nothing to describe), while an unreadable inventory yields lanes
    whose ``slots`` is ``None``, and an unread issue yields ``issue_closed=None``. None of
    these degrade into a confident value.
    """
    from mozyo_bridge.core.state.lane_lifecycle_readonly import (
        load_lane_lifecycle_readonly,
    )
    from mozyo_bridge.core.state.lane_metadata import (
        lane_records_by_unit,
        load_lane_records,
    )
    from mozyo_bridge.core.state.lane_lifecycle_model import DISPOSITION_ACTIVE
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        list_herdr_agent_rows,
        repo_scope_workspace_id,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_integration import (  # noqa: E501
        LiveSublaneGitOperations,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (  # noqa: E501
        LiveSublaneLifecycleOps,
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

    environ = environ if environ is not None else os.environ
    workspace_id = repo_scope_workspace_id(repo_root)
    if not workspace_id:
        # Redmine #14499 review j#89191 finding 4 (adjacent instance): an unresolvable
        # workspace identity used to make the `mine` filter match nothing, which rendered as
        # "no lane rows for this repo workspace" — indistinguishable from a repo that
        # genuinely owns none. That is the fail-open this whole surface exists to avoid.
        raise RebootAuditUnavailable(
            "the repo's workspace identity could not be resolved, so the lanes this repo "
            "owns cannot be determined. This is an unreadable authority, not an empty one"
        )
    records = load_lane_lifecycle_readonly()
    if records is None:
        # `load_lane_lifecycle_readonly` returns None for its fail-closed cases (an
        # unreadable / newer / malformed / partial component schema) and () only for a
        # genuinely absent store. Folding the two together with `or ()` reported an
        # unreadable lifecycle authority as "nothing to converge" (review j#89191 finding 4).
        raise RebootAuditUnavailable(
            "the lane lifecycle store could not be read (unreadable, or a newer / malformed "
            "component schema). A snapshot cannot be produced; this is NOT the same as the "
            "store having no rows"
        )
    mine = tuple(r for r in records if r.repo_workspace_id == workspace_id)
    if not mine:
        return ()

    metadata = lane_records_by_unit(load_lane_records())
    try:
        managed_roles = (
            resolve_gateway_provider(str(repo_root)),
            resolve_worker_provider(str(repo_root)),
        )
    except WorkflowProviderUnresolved:
        managed_roles = ()
    if rows is None:
        try:
            rows = list_herdr_agent_rows(environ)
        except HerdrSessionStartError:
            rows = None

    if issue_states is None:
        issue_states = read_issue_closed_states(
            [r.issue_id for r in mine], environ=environ
        )

    ops = LiveSublaneLifecycleOps(repo_root=repo_root)
    git = LiveSublaneGitOperations(repo_root=repo_root)
    target_branch = (integration_branch or "").strip() or _DEFAULT_INTEGRATION_BRANCH
    # Peer active owners per issue — the ``supersede`` discriminant. Computed once over the
    # whole repo's rows rather than per lane, so the two sides of a duplicate ownership see
    # each other symmetrically.
    active_by_issue: dict[str, list[str]] = {}
    for record in mine:
        if record.lane_disposition == DISPOSITION_ACTIVE and record.issue_id:
            active_by_issue.setdefault(record.issue_id, []).append(record.lane_id)

    facts: list[RebootLaneFacts] = []
    for record in mine:
        meta = metadata.get((record.repo_workspace_id, record.lane_id))
        recorded_worktree = (meta.worktree_path if meta else "") or ""
        branch = (meta.branch if meta else "") or ""
        legacy_token = ""
        if recorded_worktree:
            try:
                candidate = derive_lane_workspace_token(
                    str(Path(recorded_worktree).expanduser().resolve())
                )
                legacy_token = candidate if is_lane_workspace_token(candidate) else ""
            except (OSError, ValueError):
                legacy_token = ""
        slots = (
            None
            if rows is None or not managed_roles
            else _lane_slot_facts(
                rows,
                workspace_id=record.repo_workspace_id,
                lane_id=record.lane_id,
                legacy_workspace_id=legacy_token,
                managed_roles=managed_roles,
            )
        )
        peers = tuple(
            sorted(
                lane
                for lane in active_by_issue.get(record.issue_id, ())
                if lane != record.lane_id
            )
        )
        facts.append(
            RebootLaneFacts(
                workspace_id=record.repo_workspace_id,
                lane_id=record.lane_id,
                issue_id=record.issue_id,
                lane_disposition=record.lane_disposition,
                process_release=record.process_release,
                binding_kind=record.binding_kind,
                worktree_identity=record.worktree_identity,
                lane_generation=record.lane_generation,
                revision=record.revision,
                recorded_worktree=recorded_worktree,
                worktree_present=_worktree_present(recorded_worktree),
                branch=branch,
                branch_exists=(
                    git.integration_branch_resolved(branch) if branch else None
                ),
                head_integrated=(
                    ops.branch_integrated(branch, target_branch) if branch else None
                ),
                issue_closed=issue_states.get(record.issue_id),
                slots=slots,
                peer_active_lanes=peers,
            )
        )
    return tuple(facts)


def audit_payload(
    facts: Sequence[RebootLaneFacts], plans: Sequence[RebootLanePlan]
) -> dict:
    """The structured snapshot: per-lane facts + plan, plus the count-only roll-up."""
    return {
        "lanes": [
            {"facts": f.as_payload(), "plan": p.as_payload()}
            for f, p in zip(facts, plans)
        ],
        "summary": summarize_convergences(plans),
        "lane_count": len(plans),
    }


def format_audit_text(
    facts: Sequence[RebootLaneFacts], plans: Sequence[RebootLanePlan]
) -> str:
    if not plans:
        return "sublane reboot-audit: no lane rows for this repo workspace"
    lines = [f"sublane reboot-audit: {len(plans)} lane(s)"]
    for fact, plan in zip(facts, plans):
        lines.append(
            f"  {plan.lane_id} issue={plan.issue_id or '-'} -> {plan.convergence}"
            + (f" ({plan.reason})" if plan.reason else "")
        )
        lines.append(
            f"    lifecycle: {fact.lane_disposition}/{fact.process_release} "
            f"gen={fact.lane_generation} rev={fact.revision} "
            f"bound={'yes' if fact.is_bound else 'no'}"
        )
        lines.append(
            f"    git: branch={fact.branch or '-'} "
            f"exists={_tri(fact.branch_exists)} integrated={_tri(fact.head_integrated)} "
            f"worktree_present={_tri(fact.worktree_present)}"
        )
        lines.append(
            f"    redmine: issue_closed={_tri(fact.issue_closed)}    "
            f"live={','.join(plan.live_slots) or '-'} "
            f"residue={len(plan.residue_slots)} foreign={len(plan.foreign_slots)}"
        )
        if plan.detail:
            lines.append(f"    detail: {plan.detail}")
        if plan.alternatives:
            lines.append(f"    alternatives: {', '.join(plan.alternatives)}")
        for step in plan.steps:
            lines.append(f"    $ {step}")
    lines.append("  summary: " + json.dumps(summarize_convergences(plans), sort_keys=True))
    lines.append(
        "  note: each lane names its own rail. There is deliberately no all-lanes action — "
        "a reboot leaves lanes in different states that need different answers."
    )
    return "\n".join(lines)


def _tri(value: Optional[bool]) -> str:
    """Render a three-valued fact so ``unknown`` never reads as ``false``."""
    return "unknown" if value is None else ("yes" if value else "no")


def cmd_sublane_reboot_audit(args: argparse.Namespace) -> int:
    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    try:
        facts = gather_reboot_facts(
            repo_root,
            integration_branch=getattr(args, "integration_branch", "") or "",
        )
    except RebootAuditUnavailable as exc:
        # An unreadable AUTHORITY, not an empty result (#14499 review j#89191 finding 4).
        # Non-zero, because no snapshot was produced at all — a caller that treats exit 0 as
        # "audited, nothing to do" must never see that for a store it could not read.
        payload = {"state": "unavailable", "detail": str(exc), "lanes": [], "lane_count": 0}
        if bool(getattr(args, "json", False)):
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(f"sublane reboot-audit: unavailable\n  detail: {exc}", file=sys.stderr)
        return 1
    only = (getattr(args, "lane_label", "") or "").strip()
    if only:
        facts = tuple(f for f in facts if f.lane_id == only)
    plans = tuple(plan_lane_convergence(f) for f in facts)
    if bool(getattr(args, "json", False)):
        print(
            json.dumps(
                audit_payload(facts, plans), ensure_ascii=False, indent=2, sort_keys=True
            )
        )
    else:
        print(format_audit_text(facts, plans), file=sys.stdout)
    # Read-only: exit 0 whenever the snapshot itself was produced. A lane needing work is a
    # finding, not a command failure — a non-zero exit here would make the audit unusable in
    # the loop that is supposed to consume it.
    return 0


def register_sublane_reboot_audit_parser(sublane_sub: Any) -> None:
    parser = sublane_sub.add_parser(
        "reboot-audit",
        help=(
            "Redmine #14499: READ-ONLY single-snapshot convergence audit. Joins the four "
            "authorities a post-reboot lane's disposition depends on — Redmine open/closed, "
            "git worktree presence / branch / origin reachability, the durable lifecycle "
            "binding + generation + revision, and the live assigned-name + process inventory "
            "— and returns the typed next rail for EACH lane (restore_worktree / "
            "terminalize_bound_metadata / terminalize_unbound_metadata / close_shell_residue "
            "/ guarded_close / resume / hibernate / supersede / already_terminal, or "
            "unknown|blocked with the axis that failed). Any unreadable authority yields "
            "`unknown` for that lane rather than a guess. Closes no pane, writes no lifecycle "
            "row, touches no worktree or branch. The roll-up is a count: there is no "
            "all-lanes action, because a reboot leaves lanes needing different answers."
        ),
    )
    parser.add_argument(
        "--lane-label",
        dest="lane_label",
        default="",
        help="Restrict the snapshot to one lane label (default: every lane of this repo)",
    )
    parser.add_argument(
        "--integration-branch",
        dest="integration_branch",
        default="",
        help=(
            "Branch the lanes' heads are checked for reachability against "
            f"(default: {_DEFAULT_INTEGRATION_BRANCH})"
        ),
    )
    from mozyo_bridge.application.cli_common import add_repo_option

    add_repo_option(parser)
    parser.add_argument("--json", action="store_true", help="Emit structured JSON")
    parser.set_defaults(func=cmd_sublane_reboot_audit)


__all__ = (
    "RebootAuditUnavailable",
    "audit_payload",
    "cmd_sublane_reboot_audit",
    "format_audit_text",
    "gather_reboot_facts",
    "read_issue_closed_states",
    "register_sublane_reboot_audit_parser",
)
