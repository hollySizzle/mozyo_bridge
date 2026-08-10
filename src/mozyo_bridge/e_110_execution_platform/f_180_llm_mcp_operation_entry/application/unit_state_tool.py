"""The read-only Unit-state tool: ports, composer, live adapter (Redmine #15162).

Shape of this module, and why:

- :class:`UnitFacts` is what a source produces — **raw facts plus readability**,
  not finished axes. If each adapter built its own axes, each adapter would own the
  unknown-preservation and blocked-admission rules, and the rules would drift.
- :func:`compose_unit_state` is the single place those rules run. It stamps the
  observation envelope on every field, applies ``admit_blocked``, withholds a
  ``blocked`` classification that no admissible claim supports, and derives the
  health axis from the fields the other three actually reported.
- :class:`LiveUnitStateSource` is the only adapter that touches the environment,
  and it reuses the **shared** glance pipeline (roster → ``active_lane_snapshots``
  → ``fold_glance_rows``) rather than re-deriving a workflow classification. The
  Unit tool and ``workflow glance`` therefore cannot disagree about the same lane.

The composer's two refusals, which are the whole point of #15162:

1. A workflow state token in :data:`UNDERIVABLE_STATES` is only reported when the
   evidence for it exists. ``blocked`` without an admissible
   :class:`~...domain.unit_state.BlockedClaim` becomes ``unknown`` with a note
   naming what is missing. ``idle`` survives only because the fold derives it from
   a *readable, gate-free durable record* — never from silence; if the durable
   facts were unavailable the fold already reports ``unknown`` itself.
2. Nothing is derived from an absence. An unread source yields ``unknown`` with
   ``readability=unreadable``; a dispatch whose landing was never observed yields
   ``unconfirmed``. Neither ever becomes a state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence, Tuple

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.runtime_observation import (  # noqa: E501
    FRESHNESS_FRESH,
    FRESHNESS_UNKNOWN,
    READABILITY_READABLE,
    READABILITY_UNREADABLE,
    SOURCE_HERDR,
    SOURCE_REDMINE,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.read_plan_tools import (  # noqa: E501
    ReadPlanContext,
    ToolOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.unit_selector import (  # noqa: E501
    UnitRecord,
    UnitSelector,
    UnitSelectorError,
    parse_unit_selector,
    resolve_unit,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.unit_state import (  # noqa: E501
    AXES,
    AXIS_DELIVERY,
    AXIS_HEALTH,
    AXIS_RUNTIME,
    AXIS_WORKFLOW,
    DELIVERY_BLOCKER_SOURCES,
    SOURCE_UNOBSERVED,
    BlockedClaim,
    DeliveryAxis,
    HealthAxis,
    ObservedField,
    RuntimeAxis,
    UnitStateReport,
    VALUE_UNCONFIRMED,
    VALUE_UNKNOWN,
    WORKFLOW_BLOCKER_SOURCES,
    WorkflowAxis,
    admit_blocked,
    derive_health,
)

#: Delivery anomaly token meaning "no anomaly". Kept as a local literal so this
#: module reads the glance vocabulary without depending on its import order.
_ANOMALY_NONE = "none"

#: Runtime observation tokens that are *observations*, not workflow claims. They
#: are reported verbatim on the runtime axis and never lifted anywhere else.
_RUNTIME_UNKNOWN = "unknown"

#: The folded workflow state token that a blocker claim must agree with.
_STATE_BLOCKED = "blocked"

#: The delivery outcome reported when a landing was positively observed. Only the
#: shared injection-stage authority may produce it (review j#102599 r3f4).
VALUE_LANDED = "landed"


def landing_from_ledger_record(record: object) -> str:
    """Classify one delivery-ledger record's landing, from a POSITIVE signal only.

    The defect this replaces (review j#102599 r3f4): the outcome used to be
    derived as ``anomaly == none -> landed``. But
    ``anomaly_from_ledger_record`` is documented as deliberately conservative —
    "an uninterpretable row is healthy, not unknown, so the ledger join never
    raises a false alarm" — so ``none`` means *no recognized anomaly*, which an
    unreadable or unknown row also satisfies. Reading that as "the payload
    landed" turns an absence into a positive observation: precisely the
    derivation this whole read model exists to prevent, committed inside the
    module whose docstring forbids it.

    So landing is asked of the **shared injection-stage authority**
    (``injection_stage_for_outcome``), the same one
    ``handoff_application_service`` uses to decide ``delivered`` — no second
    delivery verdict is invented here. Only
    :data:`~...injection_stage.STAGE_SUBMITTED_CONFIRMED` ("submitted, and the
    submission was confirmed") is a landing. Every other stage, and every record
    the authority cannot classify, is :data:`VALUE_UNCONFIRMED`: we looked and did
    not see it land, which is a different fact from not having looked.
    """
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (  # noqa: E501
        STAGE_SUBMITTED_CONFIRMED,
        injection_stage_for_outcome,
    )

    if record is None:
        return VALUE_UNCONFIRMED
    try:
        stage = injection_stage_for_outcome(record)
    except Exception:  # noqa: BLE001 - an unclassifiable record is unconfirmed
        return VALUE_UNCONFIRMED
    return VALUE_LANDED if stage == STAGE_SUBMITTED_CONFIRMED else VALUE_UNCONFIRMED


def _utc_now_iso() -> str:
    """The current UTC instant, in the ISO8601 shape the snapshot envelope parses."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class _RuntimeObservation:
    """The per-role runtime read for one Unit.

    ``backend`` is the transport the Unit was observed on (``herdr`` / ``tmux``) —
    a backend name, not a runtime state. ``roles`` is ``(role, state)`` per role,
    with ``unknown`` for any role the fold did not cover.
    """

    backend: str
    roles: Tuple[Tuple[str, str], ...]
    readable: bool


@dataclass(frozen=True)
class UnitFacts:
    """Raw facts about one Unit, as read from the sources.

    Every group carries its own readability, because the sources fail
    independently: Redmine can be unreachable while the delivery ledger is
    perfectly readable, and collapsing that into one flag would make a readable
    delivery fact look unreliable (or, worse, an unreadable Redmine look fine).
    """

    #: Durable-record facts (all strings; ``""`` means the fold produced nothing).
    issue_id: str = ""
    issue_status: str = ""
    workflow_state: str = ""
    latest_gate: str = ""
    latest_journal: str = ""
    next_owner: str = ""
    next_action: str = ""
    work_unit: str = ""
    workflow_readable: bool = False
    workflow_observed_at: Optional[str] = None
    workflow_freshness: str = FRESHNESS_UNKNOWN
    workflow_blocked: Optional[BlockedClaim] = None

    #: Terminal-runtime observation.
    runtime_backend: str = ""
    runtime_roles: Tuple[Tuple[str, str], ...] = ()
    receive_method: str = ""
    runtime_readable: bool = False
    runtime_observed_at: Optional[str] = None
    runtime_freshness: str = FRESHNESS_UNKNOWN
    runtime_source: str = SOURCE_HERDR

    #: Delivery / dispatch observation.
    delivery_outcome: str = ""
    delivery_anomaly: str = ""
    delivery_anomaly_stale: Optional[bool] = None
    delivery_readable: bool = False
    delivery_observed_at: Optional[str] = None
    delivery_freshness: str = FRESHNESS_UNKNOWN
    delivery_source: str = SOURCE_HERDR
    delivery_blocked: Optional[BlockedClaim] = None

    #: Source-health notes gathered while reading (never pane text or paths).
    notes: Tuple[str, ...] = ()


class UnitStateSource(Protocol):
    """Port: everything :func:`run_unit_state` needs from the environment."""

    def unit_index(self) -> Sequence[UnitRecord]:
        """Every Unit this server can see, in no particular order."""

    def authorized_workspace_ids(self) -> Optional[Sequence[str]]:
        """The workspaces this server may report on, or ``None`` when unresolved.

        ``None`` is a refusal, not a wildcard: :func:`resolve_unit` treats an
        unresolved scope as authorizing nothing, so a server that cannot work out
        which workspace it belongs to reports ``foreign`` instead of answering for
        a Unit it has no basis to claim.
        """

    def unit_facts(self, unit: UnitRecord) -> UnitFacts:
        """Read the raw facts for one resolved Unit. Never raises."""


def _field(
    value: str,
    *,
    source: str,
    readable: bool,
    observed_at: Optional[str],
    freshness: str,
    note: Optional[str] = None,
) -> ObservedField:
    """Stamp one raw value with its observation envelope, fail-closed twice.

    - An unreadable source yields ``unknown`` with ``freshness=unknown`` regardless
      of what the caller passed. Age cannot be asserted about a value that was
      never read, and letting a stale timestamp ride along on an unread field is
      how a fabricated "fresh unknown" appears.
    - A field with **no ``observed_at``** is ``freshness=unknown`` even when the
      source was readable. Freshness is an age class, and there is no age without
      an observation time; ``derive_freshness`` in the snapshot contract classifies
      a missing timestamp exactly the same way. Reporting ``fresh`` beside a null
      timestamp would be an unbacked currency claim — the same defect shape as an
      unbacked ``blocked``, one field over.
    """
    if not readable:
        return ObservedField(
            value=VALUE_UNKNOWN,
            source=source,
            observed_at=None,
            freshness=FRESHNESS_UNKNOWN,
            readability=READABILITY_UNREADABLE,
            note=note,
        )
    return ObservedField.observed(
        value,
        source=source,
        observed_at=observed_at,
        freshness=freshness if observed_at else FRESHNESS_UNKNOWN,
        readability=READABILITY_READABLE,
        note=note,
    )


def _withhold_underivable(
    state: ObservedField, claim: Optional[BlockedClaim]
) -> ObservedField:
    """Degrade an unsupported ``blocked`` classification to ``unknown``.

    Only ``blocked`` is withheld here. ``idle`` and ``completed`` are handled at
    the source: the shared fold derives ``idle`` from a readable, gate-free durable
    record (never from silence) and never derives ``completed`` at all, and an
    unreadable record already folds to ``unknown``. Withholding ``idle`` again here
    would suppress a state the durable record does support.
    """
    if state.value != "blocked" or claim is not None:
        return state
    return ObservedField(
        value=VALUE_UNKNOWN,
        source=state.source,
        observed_at=state.observed_at,
        freshness=state.freshness,
        readability=state.readability,
        note=(
            "a blocked classification was withheld: no durable blocker record with "
            "a blocker source, reason and resume condition was found. Read the "
            "issue's latest gate journal to determine the actual state."
        ),
    )


def compose_unit_state(
    unit: UnitRecord, facts: UnitFacts, *, axes: Sequence[str] = AXES
) -> UnitStateReport:
    """Fold raw facts into the four-axis report. Pure; the only rules live here."""
    wanted = set(axes) if axes else set(AXES)

    def workflow_field(value: str, note: Optional[str] = None) -> ObservedField:
        return _field(
            value,
            source=SOURCE_REDMINE,
            readable=facts.workflow_readable,
            observed_at=facts.workflow_observed_at,
            freshness=facts.workflow_freshness,
            note=note,
        )

    blocked = admit_blocked(
        facts.workflow_blocked, authoritative_sources=WORKFLOW_BLOCKER_SOURCES
    )
    # The durable fold decides whether the block is STILL in force (review j#102186
    # finding_5). A claim read from an older journal is evidence that a block was
    # declared, not that it persists; if the current gate fold says anything other
    # than `blocked` — including `unknown`, where we cannot confirm it — the claim
    # is dropped rather than reported next to a contradicting state. Reporting a
    # resolved block is the same failure this Unit read model exists to prevent,
    # pointed the other way.
    superseded_note: Optional[str] = None
    if blocked is not None and facts.workflow_state != _STATE_BLOCKED:
        superseded_note = (
            "a blocker declaration exists in the durable record but the current "
            "gate no longer reports blocked; the claim is not reported as current"
        )
        blocked = None
    workflow = WorkflowAxis(
        state=_withhold_underivable(
            workflow_field(facts.workflow_state, superseded_note), blocked
        ),
        issue_status=workflow_field(facts.issue_status),
        issue_id=workflow_field(facts.issue_id),
        latest_gate=workflow_field(facts.latest_gate),
        latest_journal=workflow_field(facts.latest_journal),
        next_owner=workflow_field(facts.next_owner),
        next_action=workflow_field(facts.next_action),
        work_unit=workflow_field(facts.work_unit),
        blocked=blocked,
    )

    def runtime_field(value: str) -> ObservedField:
        return _field(
            value,
            source=facts.runtime_source,
            readable=facts.runtime_readable,
            observed_at=facts.runtime_observed_at,
            freshness=facts.runtime_freshness,
        )

    runtime = RuntimeAxis(
        backend=runtime_field(facts.runtime_backend),
        roles=tuple(
            (role, runtime_field(state)) for role, state in facts.runtime_roles
        ),
        receive_method=runtime_field(facts.receive_method),
    )

    delivery_blocked = admit_blocked(
        facts.delivery_blocked, authoritative_sources=DELIVERY_BLOCKER_SOURCES
    )
    # ``unconfirmed`` rather than ``unknown`` when the source WAS readable and
    # carried no landing: "we looked and did not see it land" is a different fact
    # from "we did not look". Routed through ``_field`` like every other value so
    # the missing-timestamp guard applies here too.
    outcome = _field(
        facts.delivery_outcome or VALUE_UNCONFIRMED,
        source=facts.delivery_source,
        readable=facts.delivery_readable,
        observed_at=facts.delivery_observed_at,
        freshness=facts.delivery_freshness,
        note=(
            None
            if facts.delivery_outcome or not facts.delivery_readable
            else "a dispatch was recorded but its landing was never observed"
        ),
    )
    stale = facts.delivery_anomaly_stale
    delivery = DeliveryAxis(
        outcome=outcome,
        anomaly=_field(
            facts.delivery_anomaly,
            source=facts.delivery_source,
            readable=facts.delivery_readable,
            observed_at=facts.delivery_observed_at,
            freshness=facts.delivery_freshness,
        ),
        anomaly_stale=_field(
            "" if stale is None else str(bool(stale)).lower(),
            source=facts.delivery_source,
            readable=facts.delivery_readable and stale is not None,
            observed_at=facts.delivery_observed_at,
            freshness=facts.delivery_freshness,
        ),
        blocked=delivery_blocked,
    )

    reported = (
        workflow.state,
        workflow.issue_status,
        workflow.latest_gate,
        runtime.backend,
        runtime.receive_method,
        delivery.outcome,
        delivery.anomaly,
    )
    # The health axis reports the delivery anomaly verbatim. It is not re-derived
    # or re-classified here: the anomaly's own envelope already says how well it
    # was observed, and a second classification would be a second authority.
    health = derive_health(reported, anomaly=delivery.anomaly, notes=facts.notes)

    return UnitStateReport(
        unit=unit.as_payload(),
        workflow=workflow if AXIS_WORKFLOW in wanted else WorkflowAxis(),
        runtime=runtime if AXIS_RUNTIME in wanted else RuntimeAxis(),
        delivery=delivery if AXIS_DELIVERY in wanted else DeliveryAxis(),
        health=health if AXIS_HEALTH in wanted else HealthAxis(),
    )


def run_unit_state(
    arguments: Mapping[str, Any],
    context: ReadPlanContext,
    *,
    source: Optional[UnitStateSource] = None,
) -> ToolOutcome:
    """The ``unit_state`` tool handler.

    Refusals come back as structured tool-execution errors carrying the closed
    selector ``reason`` token, not as prose the caller must interpret.
    """
    resolved_source = source or LiveUnitStateSource(context)
    try:
        selector = parse_unit_selector(arguments)
        unit = resolve_unit(
            selector,
            resolved_source.unit_index(),
            authorized_workspace_ids=resolved_source.authorized_workspace_ids(),
        )
    except UnitSelectorError as exc:
        return ToolOutcome(
            payload=exc.as_payload(),
            is_error=True,
            summary=f"the Unit selector was refused ({exc.reason})",
        )

    axes = [str(a) for a in (arguments.get("axes") or AXES)]
    facts = resolved_source.unit_facts(unit)
    report = compose_unit_state(unit, facts, axes=axes)
    payload = report.as_payload()
    return ToolOutcome(
        payload=payload,
        summary=(
            f"{unit.unit_id()}: workflow={report.workflow.state.value}, "
            f"runtime={report.runtime.backend.value}, "
            f"delivery={report.delivery.outcome.value}, "
            f"degraded={report.health.degraded}"
        ),
    )


# --------------------------------------------------------------------------- #
# Live adapter
# --------------------------------------------------------------------------- #


@dataclass
class LiveUnitStateSource:
    """The live :class:`UnitStateSource`.

    Builds the Unit index from the durable lane lifecycle records scoped to this
    repo's workspace, and reads the per-Unit facts through the **same** shared
    glance pipeline ``workflow glance`` uses. Nothing here re-derives a workflow
    classification, and nothing reads pane text.

    Every read is fail-open into an *unreadable* fact rather than an exception, so
    a source outage degrades the report (visibly, via ``health``) instead of
    turning a read-only query into an error.
    """

    context: ReadPlanContext
    _units: Optional[Tuple[UnitRecord, ...]] = field(default=None, init=False)
    _notes: list = field(default_factory=list, init=False)

    # -- index ------------------------------------------------------------- #

    def authorized_workspace_ids(self) -> Optional[Sequence[str]]:
        workspace_id = self._repo_workspace_id()
        return None if workspace_id is None else (workspace_id,)

    def _repo_workspace_id(self) -> Optional[str]:
        try:
            from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
                herdr_workspace_segment,
            )

            segment = herdr_workspace_segment(Path(self.context.repo_root))
        except Exception:  # noqa: BLE001 - an unresolved scope is a refusal, not a crash
            return None
        return segment or None

    def _project_id(self) -> str:
        try:
            from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_context import (  # noqa: E501
                read_redmine_project,
            )

            identifier, _ = read_redmine_project(self.context.repo_root)
        except Exception:  # noqa: BLE001 - an unresolvable project context stays empty
            return ""
        return str(identifier or "")

    def unit_index(self) -> Sequence[UnitRecord]:
        if self._units is not None:
            return self._units
        workspace_id = self._repo_workspace_id()
        if workspace_id is None:
            self._notes.append("the repo's workspace scope could not be resolved")
            self._units = ()
            return self._units
        project_id = self._project_id()
        metadata = self._lane_metadata()
        records: list[UnitRecord] = []
        for row in self._lifecycle_rows():
            if str(getattr(row, "repo_workspace_id", "") or "") != workspace_id:
                continue
            lane_id = str(getattr(row, "lane_id", "") or "").strip()
            if not lane_id:
                continue
            meta = metadata.get((workspace_id, lane_id))
            records.append(
                UnitRecord(
                    workspace_id=workspace_id,
                    lane_id=lane_id,
                    project_id=project_id,
                    repo_label=getattr(meta, "lane_label", None) or None,
                    branch=getattr(meta, "branch", None) or None,
                    ticket_system="redmine" if project_id else None,
                    roles=self._roles_for(row),
                )
            )
        self._units = tuple(records)
        return self._units

    def _lifecycle_rows(self) -> Sequence:
        try:
            from mozyo_bridge.core.state.lane_lifecycle import (
                load_lane_lifecycle_readonly,
            )

            rows = load_lane_lifecycle_readonly()
        except Exception:  # noqa: BLE001 - an unreadable store degrades to no Units
            self._notes.append("the lane lifecycle store could not be read")
            return ()
        if rows is None:
            self._notes.append(
                "the lane lifecycle store is unreadable at this schema version"
            )
            return ()
        return rows

    def _lane_metadata(self) -> Mapping[tuple, Any]:
        """Display-join metadata indexed by ``(workspace_id, lane_id)`` (fail-open)."""
        try:
            from mozyo_bridge.core.state.lane_metadata import (
                lane_records_by_unit,
                load_lane_records,
            )

            return lane_records_by_unit(load_lane_records())
        except Exception:  # noqa: BLE001 - metadata is a display join; absence is fine
            return {}

    @staticmethod
    def _roles_for(row) -> Tuple[str, ...]:
        """The Unit's role set, from the lane's durable kind. Never guessed."""
        kind = str(getattr(row, "lane_kind", "") or "").strip()
        if kind == "implementation":
            return ("gateway", "worker")
        if kind in ("coordinator", "delegated_coordinator"):
            return ("gateway",)
        return ()

    # -- facts -------------------------------------------------------------- #

    def unit_facts(self, unit: UnitRecord) -> UnitFacts:
        issue_id = self._issue_for(unit)
        if not issue_id:
            return UnitFacts(
                notes=tuple(self._notes)
                + ("the Unit's lane owns no issue, so it has no durable record",)
            )
        return self._facts_for_issue(unit, issue_id)

    def _issue_for(self, unit: UnitRecord) -> str:
        for row in self._lifecycle_rows():
            if (
                str(getattr(row, "repo_workspace_id", "") or "") == unit.workspace_id
                and str(getattr(row, "lane_id", "") or "") == unit.lane_id
            ):
                return str(getattr(row, "issue_id", "") or "").strip()
        return ""

    def _facts_for_issue(self, unit: UnitRecord, issue_id: str) -> UnitFacts:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_snapshot_source import (  # noqa: E501
            active_lane_snapshots,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_source_wiring import (  # noqa: E501
            build_glance_sources,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_glance import (  # noqa: E501
            fold_glance_rows,
        )
        from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.blocker_claim import (  # noqa: E501
            latest_blocker_claim,
        )

        notes = list(self._notes)
        try:
            sources = build_glance_sources(
                store_path=self.context.store_paths.get("store"),
                reconcile_store_path=self.context.store_paths.get("reconcile_store"),
                ledger_path=self.context.store_paths.get("ledger"),
                redmine_fixture_path=self.context.redmine_fixture_path,
                redmine_live=self.context.redmine_live,
            )
        except Exception as exc:  # noqa: BLE001
            return UnitFacts(
                issue_id=issue_id,
                notes=tuple(notes) + (f"glance sources unavailable ({type(exc).__name__})",),
            )

        collection = active_lane_snapshots(
            ((issue_id, unit.lane_id),),
            redmine_source=sources.redmine_source,
            store=sources.store,
            ledger=sources.ledger,
            reconcile_store=sources.reconcile_store,
            authority_index=sources.index(),
        )
        notes.extend(str(n) for n in collection.notes)
        snapshots = list(collection.snapshots)
        if not snapshots:
            return UnitFacts(
                issue_id=issue_id,
                notes=tuple(notes) + ("no snapshot could be folded for this Unit",),
            )
        snapshot = snapshots[0]
        row = fold_glance_rows((snapshot,))[0]
        # The read time IS the observation time: every source above was read live in
        # this call. Stamping it is what lets `freshness` mean something — a readable
        # field with no timestamp is reported `unknown` by the composer, because an
        # age class with no age is not a claim anyone can check.
        observed_at = _utc_now_iso()

        workflow_readable = bool(snapshot.durable_facts_available)
        claim = None
        if workflow_readable and sources.redmine_source is not None:
            # The claim carries the SAME observation envelope as the other durable
            # fields (review j#102186 finding_5): it was read in this call, from the
            # same live fetch, so it gets this read's timestamp and freshness rather
            # than the resting `None` / `unknown` the earlier version always returned.
            claim = self._blocker_claim(
                sources.redmine_source,
                issue_id,
                latest_blocker_claim,
                observed_at=observed_at,
                freshness=FRESHNESS_FRESH,
            )

        delivery_source_token = str(row.delivery_source or "")
        delivery_readable = delivery_source_token not in ("", "none")
        # The landing verdict comes from the ledger record itself through the
        # shared injection-stage authority, never from "the fold reported no
        # anomaly" (review j#102599 r3f4).
        landing = (
            landing_from_ledger_record(self._latest_ledger_record(sources.ledger, issue_id))
            if delivery_readable
            else ""
        )
        runtime = self._runtime_observation(unit)

        return UnitFacts(
            issue_id=issue_id,
            issue_status="open" if snapshot.signal.issue_open else "closed",
            workflow_state=row.workflow_state,
            latest_gate=row.latest_gate,
            latest_journal=row.latest_journal,
            next_owner=row.next_owner,
            next_action=row.next_action,
            work_unit=row.work_unit,
            workflow_readable=workflow_readable,
            workflow_observed_at=observed_at if workflow_readable else None,
            workflow_freshness=FRESHNESS_FRESH if workflow_readable else FRESHNESS_UNKNOWN,
            workflow_blocked=claim,
            runtime_backend=runtime.backend,
            runtime_roles=runtime.roles,
            receive_method=row.receive_method,
            runtime_readable=runtime.readable,
            runtime_observed_at=observed_at if runtime.readable else None,
            runtime_freshness=FRESHNESS_FRESH if runtime.readable else FRESHNESS_UNKNOWN,
            runtime_source=SOURCE_HERDR,
            delivery_outcome=landing,
            delivery_anomaly=row.delivery_anomaly,
            delivery_anomaly_stale=(
                bool(row.delivery_anomaly_stale) if delivery_readable else None
            ),
            delivery_readable=delivery_readable,
            delivery_observed_at=observed_at if delivery_readable else None,
            delivery_freshness=FRESHNESS_FRESH if delivery_readable else FRESHNESS_UNKNOWN,
            delivery_source=delivery_source_token or SOURCE_UNOBSERVED,
            notes=tuple(notes),
        )

    @staticmethod
    def _latest_ledger_record(ledger, issue_id: str):
        """The issue's most recent delivery-ledger record, or ``None`` (fail-open)."""
        if ledger is None:
            return None
        try:
            records = ledger.records_for_issue(issue_id)
        except Exception:  # noqa: BLE001 - a ledger read never breaks a read-only query
            return None
        return records[-1] if records else None

    def _runtime_observation(self, unit: UnitRecord) -> "_RuntimeObservation":
        """Per-role runtime observation for this Unit, or ``unknown`` per role.

        The acceptance asks for the *gateway / worker* observed states. The
        previous version copied one lane-level value onto every role and put that
        same value in ``backend`` (review j#102599 r3f5), so the payload had the
        shape of per-role observation without the substance — a reader could not
        tell the two roles apart, and would reasonably assume it could.

        The per-role states come from the live herdr ``agent list`` fold the
        cockpit read model already performs; each Unit row carries
        ``role_runtime_states`` as ``(role, state)`` pairs. When that fold does not
        cover this Unit, each role is reported ``unknown`` rather than filled with
        a lane-level substitute: not knowing per role is the honest answer, and it
        is the answer this Feature's own rules require.
        """
        roles = tuple(unit.roles)
        observed = ()
        try:
            from mozyo_bridge.e_120_operations_cockpit.f_120_cockpit_web_ui.application.cockpit_payload import (  # noqa: E501
                herdr_observed_units,
            )

            observed, _diagnostics = herdr_observed_units(
                repo_root=Path(self.context.repo_root), now=datetime.now(timezone.utc)
            )
        except Exception:  # noqa: BLE001 - no live fold available: report unknown
            observed = ()

        states: dict = {}
        backend = ""
        for row in observed or ():
            if (
                str(getattr(row, "workspace_id", "") or "") != unit.workspace_id
                or str(getattr(row, "lane_id", "") or "") != unit.lane_id
            ):
                continue
            backend = str(getattr(row, "backend", "") or "")
            states = {
                str(role): str(state)
                for role, state in (getattr(row, "role_runtime_states", ()) or ())
            }
            break

        pairs = tuple((role, states.get(role, VALUE_UNKNOWN)) for role in roles)
        readable = any(state != VALUE_UNKNOWN for _, state in pairs)
        return _RuntimeObservation(backend=backend, roles=pairs, readable=readable)

    @staticmethod
    def _blocker_claim(
        redmine_source,
        issue_id: str,
        reader,
        *,
        observed_at: Optional[str] = None,
        freshness: str = FRESHNESS_UNKNOWN,
    ):
        """Read the blocker claim still in force, with this read's envelope."""
        try:
            record = redmine_source.read_issue(issue_id)
        except Exception:  # noqa: BLE001 - an unreadable issue yields no claim
            return None
        return reader(
            getattr(record, "journals", ()) or (),
            issue_id=issue_id,
            observed_at=observed_at,
            freshness=freshness,
        )


__all__ = (
    "LiveUnitStateSource",
    "UnitFacts",
    "UnitSelector",
    "UnitStateSource",
    "compose_unit_state",
    "run_unit_state",
)
