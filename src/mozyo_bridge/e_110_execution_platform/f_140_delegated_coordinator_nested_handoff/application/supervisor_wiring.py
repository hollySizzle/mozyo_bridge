"""Production default wiring for the workspace callback supervisor (Redmine #13683; extracted R2).

The concrete adapters the supervisor composition root binds in production — enumerated lazily so the
class in :mod:`...workspace_callback_supervisor` stays deterministically testable with injected fakes.
This is a move-only leaf split (the composition root imports and re-exports these, so every caller's
import surface is preserved): it exists so the composition root stays a cohesive under-threshold unit
after the #13683 R2 lane_gateway route landed on the same feature (module-health j#82367). Nothing about
behaviour changes here — the ``default_*`` factories, the ``SupervisedWorkspace`` projection, and the
scrubbed background-service env are byte-identical to their former inline definitions.

Deliberately free of any dependency on the composition root, so the two modules do not import each other
in a cycle: the root imports these; these import only siblings + core.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Optional

from mozyo_bridge.core.state.callback_outbox import CallbackOutboxRow  # noqa: F401 - re-export type surface
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_review_return import (
    owning_lane_generation_reader,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    RedmineJournalSource,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    authoritative_workspace_by_issue,
)


@dataclasses.dataclass(frozen=True)
class SupervisedWorkspace:
    """The minimal workspace facts the supervisor needs (id + canonical checkout path).

    A thin projection of :class:`...core.state.workspace_registry.WorkspaceRecord` so the roster
    resolver / source factory receive exactly what they need and nothing runtime-adjacent.
    """

    workspace_id: str
    canonical_path: str


#: The launch-time lane-identity env a background supervisor must NOT inherit (Redmine #13683
#: R2-F3). A supervisor is not a lane agent, so carrying another lane's role / lane / workspace id
#: into a target workspace's send would misroute on a foreign identity (or, in a login service,
#: present a stale identity). These are scrubbed from the send env and only ``MOZYO_WORKSPACE_ID``
#: is re-set to the target workspace — so a herdr send that needs an attested lane-sender identity
#: fails **closed** (``missing_sender_env``) rather than misrouting on a stale ambient identity. The
#: sanctioned background system-actor sender-identity contract (a supervisor is not a claude/codex
#: lane provider, so :func:`...herdr_target_resolution.resolve_sender_identity` has no slot for it)
#: is a design-consultation seam, not resolved by ambient env.
_SCRUBBED_LANE_IDENTITY_ENV = ("MOZYO_AGENT_ROLE", "MOZYO_LANE_ID", "MOZYO_WORKSPACE_ID")


def default_workspaces(*, home: Optional[Path] = None) -> list[SupervisedWorkspace]:
    """Enumerate the home workspace registry into supervised-workspace projections."""
    from mozyo_bridge.core.state.workspace_registry import list_workspaces

    return [
        SupervisedWorkspace(
            workspace_id=str(rec.workspace_id), canonical_path=str(rec.canonical_path)
        )
        for rec in list_workspaces(home=home)
        if str(rec.workspace_id or "").strip()
    ]


def default_roster(ws: SupervisedWorkspace) -> tuple[tuple[str, ...], str]:
    """Resolve THIS workspace's active-lane issue set, partitioned to it (``(issues, error)``).

    Uses the workspace-partitioned enumeration (Redmine #13968) so the supervisor supervises each
    active issue under exactly ONE authoritative registry workspace. The host's live lane
    inventory is enumerated host-global (the herdr ``agent list`` is host-wide by the #13331
    contract), then filtered to lanes whose durable ``workspace_id`` equals this workspace's
    registry id: a foreign / stale registry workspace that owns none of the host's live lanes gets
    an empty roster and therefore zero-ingest/zero-deliver (acceptance 1). The partition key is the
    registry identity stamped into each managed lane slot, never the project name or a shared issue
    list (acceptance 2). Without this filter every registry workspace received the same host-global
    roster and re-ingested + re-delivered every active issue into its own outbox partition,
    amplifying pending / dead-letter on each run.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_snapshot_source import (
        enumerate_active_lanes_for_workspace,
    )

    roster, error = enumerate_active_lanes_for_workspace(
        Path(ws.canonical_path), workspace_id=ws.workspace_id
    )
    issues = tuple(
        dict.fromkeys(str(issue).strip() for issue, _lane in roster if str(issue).strip())
    )
    return issues, (error or "")


def default_authoritative_map(lifecycle_store: object) -> dict[str, str]:
    """The home-global ``{issue -> sole actively-owning workspace}`` map (Redmine #13968 F1).

    The durable owning-lane authority (registry-identity source of truth) that selects the one
    authoritative workspace per issue, so a foreign / stale registry workspace (or a duplicate live
    lane) never double-delivers a shared issue. Reads every lifecycle row via the NON-migrating
    reader, keeps the ACTIVE-disposition + bound-issue rows as ``(workspace_id, issue)`` pairs, and
    resolves each issue's unique owner (:func:`...authoritative_workspace_by_issue`): zero /
    two-or-more owners is omitted (fail-closed). An unreadable store yields ``{}`` (never a crash).
    """
    from mozyo_bridge.core.state.lane_lifecycle import DISPOSITION_ACTIVE

    try:
        records = lifecycle_store.records()
    except Exception:  # noqa: BLE001 - an owner read never breaks the sweep
        return {}
    active_owners = [
        (rec.repo_workspace_id, rec.issue_id)
        for rec in records
        if str(getattr(rec, "lane_disposition", "") or "").strip() == DISPOSITION_ACTIVE
        and str(getattr(rec, "issue_id", "") or "").strip()
    ]
    return authoritative_workspace_by_issue(active_owners)


def default_redmine_source(
    ws: SupervisedWorkspace, *, home: Optional[Path] = None
) -> Optional[RedmineJournalSource]:
    """Build the live credential-gated Redmine journal source, or ``None`` when unconfigured.

    ``home`` scopes the credential root exactly like the registry / store / lease, so the launchd
    daemon (started with the ``--home`` the installer pinned) reads its Redmine credentials from the
    same mozyo home the install preflight validated — not whatever ``mozyo_bridge_home()`` a
    launchd process with no ``MOZYO_BRIDGE_HOME`` would re-derive (Redmine #13683 review j#79092
    R2-F1).
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (
        LiveRedmineJournalError,
        LiveRedmineJournalSource,
    )

    try:
        return LiveRedmineJournalSource.from_environment(home=home)
    except LiveRedmineJournalError:
        return None


def background_transport_env(workspace_id: str) -> dict:
    """The deterministic env for a background-service delivery subprocess (design answer j#77216).

    Model A' delivers as a ``background_service`` origin, NOT an agent: the inherited lane identity
    (``MOZYO_AGENT_ROLE`` / ``MOZYO_LANE_ID`` / ``MOZYO_WORKSPACE_ID``) is scrubbed so no foreign
    lane identity carries over (boundary 1), the target workspace id is pinned, and the delivery
    origin is stamped ``MOZYO_DELIVERY_ORIGIN=background_service`` so the transport is separated from
    an agent send (boundary 5). The lease + claim authority (not this env) gates the delivery.
    """
    import os

    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.background_service_delivery import (
        BACKGROUND_SERVICE_ORIGIN,
    )

    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_LANE_IDENTITY_ENV}
    env["MOZYO_WORKSPACE_ID"] = str(workspace_id or "")
    env["MOZYO_DELIVERY_ORIGIN"] = BACKGROUND_SERVICE_ORIGIN
    return env


def workspace_live_inventory(ws: SupervisedWorkspace) -> "tuple[list, str]":
    """Best-effort ``(raw_inventory, backend)`` for this workspace (the live-inventory seam, R5-F1).

    Returns the workspace's **raw** backend inventory + its backend token so the resolver delegates
    the stable-key match to the one backend-neutral route authority (``resolve_route_neutral``),
    which normalizes and matches it. Herdr yields the live ``agent list`` rows + ``"herdr"``; an
    unresolved / unsupported backend yields ``([], "")`` so the resolver fail-closes (never a
    partial-key match on an unadapted inventory). Live running agents are the Phase B dogfood surface
    (#13490 / #13492); tests inject fixed ``(rows, backend)``.
    """
    try:
        import os

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (
            list_herdr_agent_rows,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_entrypoint_preflight import (
            herdr_backend_active,
        )

        if herdr_backend_active(str(Path(ws.canonical_path))):
            return list(list_herdr_agent_rows(os.environ)), "herdr"
        # tmux / other backend live-inventory adaptation is the Phase B dogfood surface: the
        # resolver's unsupported-backend branch fail-closes on the empty backend token.
        return [], ""
    except Exception:  # noqa: BLE001 - inventory unavailable -> fail-closed empty
        return [], ""


def default_lifecycle_store(*, home: Optional[Path] = None):
    """The home-scoped owning-lane binding authority reader (#13681/#13689 owner, #13844 read-only).

    The callback supervisor only READS the lifecycle authority (``resolve_owner`` / ``get`` to
    route a review_result / callback return to the current owning lane); it never mutates it.
    It therefore reads through the NON-MIGRATING, version-compatible
    :class:`LaneLifecycleReader` (Redmine #13844): a supervisor running a newer-schema source
    CLI must not forward-migrate the shared home store while resolving an owner, which would
    fail-close every concurrent older-schema reader lane's transport. The reader mirrors the
    store's read surface (``resolve_owner`` / ``get``) with the same fail-closed contract.
    """
    from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleReader

    return LaneLifecycleReader(home=home)


def default_target_resolver(ws: SupervisedWorkspace, *, lifecycle_store: object = None):
    """Build the production backend-neutral route target resolver for a workspace (R5-F1 / #13684).

    Delegates the stable-key match (``(workspace_id, lane_id, role, pane_name)``) to the ledger's
    :func:`...domain.backend_neutral_resolver.resolve_route_neutral` authority over the workspace's
    live ``(rows, backend)`` inventory (:func:`workspace_live_inventory`) — never a cached locator or
    a partial hand-rolled filter. The live running-agent surface is the Phase B dogfood (#13490).

    ``lifecycle_store`` (when supplied) wires the independent live-generation authority
    (:func:`owning_lane_generation_reader`) so the correlated review_result return route delivers under
    the generation fence; without it the resolver supplies no live generation (unchanged Phase A
    fail-closed-disabled delivery).
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.background_service_sender import (
        BackendNeutralTargetResolver,
    )

    live_generation_fn = None
    if lifecycle_store is not None:
        live_generation_fn = owning_lane_generation_reader(
            ws.workspace_id, lifecycle_store=lifecycle_store
        )
    return BackendNeutralTargetResolver(
        workspace_id=ws.workspace_id,
        inventory=lambda: workspace_live_inventory(ws),
        live_generation_fn=live_generation_fn,
    )


def default_background_transport(ws: SupervisedWorkspace):
    """Build the production background-service delivery transport for a workspace (boundary 5).

    Shares the handoff rail's outcome vocabulary but under a **separated origin class**: it fires
    ``mozyo-bridge handoff send`` to the **re-resolved explicit target** (never a role label) from
    the target workspace's canonical root, with the scrubbed background-service env
    (:func:`background_transport_env`). Delivery safety is the lease + claim authority (verified by
    the sender before this transport is ever called) + the outbox one-send fence, not this env. The
    subprocess runner is injectable (tests inject a fake; the live wire is the Phase B dogfood).
    """
    import subprocess

    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_send_port import (
        _parse_outcome,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.handoff_callback_sender import (
        HandoffDeliveryResult,
    )

    canonical = str(ws.canonical_path)
    env = background_transport_env(ws.workspace_id)

    class _HandoffBackgroundTransport:
        def deliver(self, row, target) -> "HandoffDeliveryResult":  # noqa: F821
            # Redmine #14082: pin the exact stable target slot on the herdr rail. On the herdr backend
            # an explicit `--target <locator>` is NOT the route authority (only a `%N` tmux pane is), so
            # the pre-authorized locator alone is dropped and `--to <receiver>` re-derives the lane from
            # the (scrubbed / default) sender lane — the coordinator/default misroute. Passing the
            # re-resolved target's OWN lane as `--target-lane` makes the route resolve the exact
            # `(workspace_id, lane, receiver)` slot (tier-1 explicit), never a sender-lane re-derivation.
            # The `background_service` origin env (background_transport_env) admits this as a sanctioned
            # system actor without a fake agent identity. The live locator is passed only as the
            # like-for-like target, never promoted to sole authority.
            target_lane = str(getattr(target, "lane", "") or "").strip()
            argv = [
                "mozyo-bridge", "handoff", "send",
                "--to", str(target.receiver or "codex"),
                "--target", str(target.locator),  # the re-resolved explicit locator, never a label
                "--target-repo", canonical,
            ]
            if target_lane:
                argv += ["--target-lane", target_lane]
            argv += [
                "--source", "redmine",
                "--issue", str(target.issue),
                "--journal", str(target.journal),
                "--kind", "reply",
                "--mode", "standard",
                "--record-format", "json",
            ]
            try:
                proc = subprocess.run(  # noqa: S603 - fixed argv, no shell; sanctioned handoff CLI
                    argv, capture_output=True, text=True, check=False, cwd=canonical or None, env=env
                )
            except Exception:  # noqa: BLE001 - a runner blow-up is fail-safe uncertain
                return HandoffDeliveryResult("blocked", "inject_failed")
            parsed = _parse_outcome(proc.stdout or "")
            if parsed is not None:
                return HandoffDeliveryResult(parsed[0], parsed[1])
            return HandoffDeliveryResult("blocked", "turn_start_unconfirmed")

    return _HandoffBackgroundTransport()


def default_binding(ws: SupervisedWorkspace) -> object:
    """Resolve the repo-local role->provider binding for the event-intake fold (best-effort)."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_binding_source import (
        load_workflow_binding,
    )

    try:
        binding, _warnings = load_workflow_binding(ws.canonical_path)
        return binding
    except Exception:  # noqa: BLE001 - a broken binding config folds the compatibility default
        return None


__all__ = (
    "SupervisedWorkspace",
    "default_workspaces",
    "default_roster",
    "default_authoritative_map",
    "default_redmine_source",
    "background_transport_env",
    "workspace_live_inventory",
    "default_lifecycle_store",
    "default_target_resolver",
    "default_background_transport",
    "default_binding",
)
