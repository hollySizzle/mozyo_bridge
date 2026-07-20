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


class _NullSource:
    """A source yielding no journal entries — used when no Redmine source is configured.

    The callback drain (recover / deliver-once / sweep) still runs against it, so an unconfigured
    Redmine degrades to "drain the existing outbox" rather than skipping the workspace entirely.
    """

    def read_entries(self, issue_id: object = None):
        return []


#: The shared no-op source singleton (used by the drain path and the unconfigured-Redmine degrade).
_NULL_SOURCE = _NullSource()


class _ProviderCallCounter:
    """A mutable ticket-provider read counter SHARED across every source a workspace pass builds.

    Redmine #14150 review F1: ``read_entries`` is one fresh provider fetch (``LiveRedmineJournalSource``
    issues a new HTTP request per call). A workspace pass reads the provider through MORE than the
    reconcile source — the send-edge review-round fence builds its OWN source and re-reads the journal
    at delivery time. Sharing ONE counter across all those sources makes ``provider_calls`` the ACTUAL
    whole-pass provider call count (supply + discovery + dispatch-anchor + review-identity + review_return
    / lane_gateway discovery + send-edge round-fence + own-workspace backlog), not just the reconcile
    source's reads.
    """

    __slots__ = ("n",)

    def __init__(self) -> None:
        self.n = 0


class _CountingSource:
    """Wrap a Redmine source and increment a SHARED counter on every ``read_entries`` (Redmine #14150).

    All sources a workspace pass constructs (the reconcile source AND the send-edge round-fence source)
    wrap the SAME :class:`_ProviderCallCounter`, so the count reflects every real transport invocation
    across the whole pass, not the per-issue boolean (which under-counted 1/issue) nor the reconcile
    source alone (which missed the send-edge round-fence reads — review F1). ``count`` mirrors the
    shared counter for callers that read a single wrapper directly.
    """

    __slots__ = ("_inner", "_counter")

    def __init__(self, inner: object, counter: Optional["_ProviderCallCounter"] = None) -> None:
        self._inner = inner
        self._counter = counter if counter is not None else _ProviderCallCounter()

    @property
    def count(self) -> int:
        return self._counter.n

    def read_entries(self, issue_id: object = None):
        self._counter.n += 1
        return self._inner.read_entries(issue_id)

    def __getattr__(self, name: str):  # delegate any other source attribute unchanged
        return getattr(self._inner, name)


@dataclasses.dataclass(frozen=True)
class SupervisedWorkspace:
    """The minimal workspace facts the supervisor needs (id + canonical checkout path).

    A thin projection of :class:`...core.state.workspace_registry.WorkspaceRecord` so the roster
    resolver / source factory receive exactly what they need and nothing runtime-adjacent.
    """

    workspace_id: str
    canonical_path: str


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
    """Build the production background-service delivery transport for a workspace (Redmine #14082 R2).

    A **dedicated** ``background_service`` delivery seam (design constraint j#82553 / Model A' #13683
    j#77216): it delivers to the **already-resolved explicit live locator** the sender re-matched from
    the row's stable target tuple, by driving the sanctioned turn-start rail directly — it does NOT go
    through the agent ``mozyo-bridge handoff send`` entry, ``resolve_sender_identity``, or any target
    re-derivation, and never presents a fake agent identity or an env-only authorization. It shares
    only the handoff **outcome vocabulary** (via :func:`...turn_start_observation.project_herdr_turn_start`),
    under a separated origin class + entry seam. The delivery authority is the supervisor lease + the
    same-workspace outbox claim + the persisted stable tuple + the action-time live route/generation
    re-check — all enforced by :class:`...background_service_sender.BackgroundServiceCallbackSender`
    BEFORE this transport is ever called; the stable lane pin is the resolver matching the exact
    ``(workspace_id, lane, receiver)`` slot, so the locator is never promoted to sole authority.

    The rail is resolved once from the target workspace's repo-local ``terminal_transport`` config; an
    unreadable config / unsupported backend is a fail-closed ``target_unavailable`` (bounded retry),
    never a raw-Herdr fallback. Immediately before driving the rail — mirroring the ``handoff send``
    boundary — it runs the #13760 pre-send startup admission (a trust / first-run / login screen is a
    zero-send ``receiver_startup_interaction_required``, an unreadable / unprofiled receiver a zero-send
    ``target_unavailable``), so a blind Enter never lands on a startup screen. The rail is injectable
    for tests via ``resolve_turn_start_rail``.
    """
    from pathlib import Path as _Path

    from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config
    from mozyo_bridge.application.turn_start_observation import project_herdr_turn_start
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
        AnchorError,
        build_marker,
        build_notification_body,
        normalize_anchor,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.handoff_callback_sender import (
        HandoffDeliveryResult,
    )
    from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
        RepoLocalConfigError,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_admission import (
        ADMISSION_BLOCKED,
        evaluate_startup_admission,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_turn_start import (
        resolve_turn_start_rail,
    )

    canonical = str(ws.canonical_path)

    def _resolve_rail():
        # The turn-start rail for the target workspace's terminal backend, or None (fail-closed).
        try:
            config = load_repo_local_config(_Path(canonical)).terminal_transport
        except RepoLocalConfigError:
            return None
        return resolve_turn_start_rail(config)

    class _BackgroundServiceRailTransport:
        def deliver(self, row, target) -> "HandoffDeliveryResult":  # noqa: F821
            # Build the reply landing marker + body from the row's durable anchor (the SAME domain
            # builders `handoff send` uses), then drive the turn-start rail to the resolved explicit
            # locator — no target re-derivation, no agent sender identity.
            try:
                anchor = normalize_anchor(
                    "redmine", issue=str(target.issue), journal=str(target.journal)
                )
                receiver = str(target.receiver or "codex")
                marker = build_marker(anchor, "reply", receiver)
                body = build_notification_body(anchor, "reply", None, receiver)
            except AnchorError:
                return HandoffDeliveryResult("blocked", "invalid_anchor")
            except Exception:  # noqa: BLE001 - a body-build failure is a deterministic pre-send refusal
                return HandoffDeliveryResult("blocked", "invalid_args")
            try:
                rail = _resolve_rail()
            except Exception:  # noqa: BLE001 - an unresolvable rail is fail-closed (bounded retry)
                return HandoffDeliveryResult("blocked", "target_unavailable")
            if rail is None:
                return HandoffDeliveryResult("blocked", "target_unavailable")
            locator = str(target.locator or "").strip()
            if not locator:
                return HandoffDeliveryResult("blocked", "target_unavailable")
            # Redmine #14082 R2 (review j#82572): run the #13760 pre-send startup admission — the SAME
            # action-time hard gate the `handoff send` boundary runs before its first keystroke — so
            # this dedicated seam closes the bypass inside itself rather than inheriting it. The rail's
            # own precondition gate rejects a BUSY pane, but a trust / first-run / login startup screen
            # snapshots as idle-LOOKING yet has no composer, so a blind Enter would accept its default
            # and destroy an existing request. Classify the receiver's VISIBLE pane against the
            # provider's declared startup screens through the rail's read-only borrow; a match →
            # ``receiver_startup_interaction_required`` and an unreadable / unprofiled provider →
            # ``target_unavailable`` — every non-admit is a ZERO-send refusal (the rail is NEVER driven),
            # distinct from a busy precondition. Both reasons are deterministic pre-injection, so the
            # bounded retry can never duplicate (re-refuse, or deliver once after an operator clears it).
            admission = evaluate_startup_admission(
                provider_id=receiver, read_visible=lambda: rail.read_visible_pane(locator)
            )
            if not admission.admitted:
                reason = (
                    "receiver_startup_interaction_required"
                    if admission.outcome == ADMISSION_BLOCKED
                    else "target_unavailable"
                )
                return HandoffDeliveryResult("blocked", reason)
            try:
                result = rail.drive_turn_start(locator, f"{marker} {body}")
            except Exception:  # noqa: BLE001 - a rail blow-up mid-drive is fail-safe uncertain
                return HandoffDeliveryResult("blocked", "inject_failed")
            status, reason = project_herdr_turn_start(result)
            return HandoffDeliveryResult(status, reason)

    return _BackgroundServiceRailTransport()


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
    "workspace_live_inventory",
    "default_lifecycle_store",
    "default_target_resolver",
    "default_background_transport",
    "default_binding",
)
