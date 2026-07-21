"""Redmine journal -> pending workflow action intake (Redmine #12672).

The spine roadmap US #12672
(``vibes/docs/logics/coordinator-sublane-development-flow.md`` ``### ロードマップUS`` step 3)
adds the **event watcher** half of the workflow runtime: Redmine journal / issue updates
are read as the durable event source and turned into *pending workflow actions* in the
mozyo DB, instead of an agent re-deriving "what is owed next" from free text. The #12857
runtime / #12671 next_action (``...domain.workflow_runtime`` / ``...domain.workflow_next_action``)
already fold a durable event log into ``workflow.state`` + an enriched ``workflow.next_action``;
this module is the **intake front-end** that produces those events from Redmine journals
and classifies the resulting next action as a pending action — with duplicate suppression,
ambiguity handling, and a fail-closed posture fixed first (the issue's design intent:
"duplicate suppression / retry / fail-closed / ambiguity handling を先に固定する").

Design boundaries the policy holds (mirroring the #12857 first-slice boundary):

- **structured markers, never free-text parse.** A :class:`JournalMarker` is the structured
  gate / marker a journal sweep already yields (issue, journal id, gate kind, and the
  #12856 :class:`LaneSignal` facts), *not* natural-language inference over the note body.
  The gate is validated against the literal :data:`GATE_KINDS` vocabulary and fails closed
  on anything unknown — the watcher never guesses a gate from prose.
- **the event id is the durable journal anchor.** :func:`redmine_event_id` is exactly
  ``redmine:<issue>:<journal>`` so re-observing the same journal is suppressed (the issue's
  ``event_id は redmine:<issue>:<journal> のように重複排除できる``). Duplicate suppression is
  observable at intake (:func:`classify_intake`: a marker whose anchor is already recorded
  is suppressed) *and* again at replay (the #12857 fold dedups by the same ``event_id``).
- **missing / ambiguous route is a pending *failed* state, never an auto-send.** This module
  performs no delivery at all — it discovers nothing live and sends nothing (the issue:
  "自動配送は慎重に扱い、まず pending action 作成と明示実行を優先"). When the owed action targets a
  lane whose route is missing, mismatched, or **ambiguous** (more than one distinct
  provider-matching route), the pending action is :data:`PENDING_FAILED` with a fail-closed
  reason; it is recorded for explicit handling, not delivered.
- **it persists nothing and opens no DB.** The application layer (``cli_workflow_watch``)
  reads the persisted store, supplies the already-known event anchors / route candidates /
  advisory inputs, persists the new events, and renders the result. This module is pure.

Relationship to the existing route selection: :func:`...workflow_next_action.derive_workflow_next_action`
resolves a routing action's route by deterministic last-write-wins among provider-matching
candidates (correct for ``workflow resume``, which reconstructs a single best decision). The
watcher is intentionally *stricter*: when several distinct routes match the owner's expected
provider it refuses to pick one and fails closed ``route_ambiguous`` — the watcher must not
silently choose a delivery target. It reuses the same role->provider binding via
:func:`...workflow_next_action.expected_provider_for` so the two never drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    CALLBACK_NONE,
    CALLBACK_STATES,
    GATE_KINDS,
    GATE_OWNER_CLOSE_APPROVAL,
    GATE_REVIEW,
    REVIEW_APPROVED,
    REVIEW_CHANGES_REQUESTED,
    REVIEW_CONCLUSIONS,
    REVIEW_PENDING,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (
    RoleProviderBinding,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_next_action import (
    BLOCKED_NONE,
    RISK_HIGH,
    RouteCandidate,
    WorkflowCommandResult,
    WorkflowNextAction,
    _escalate,
    derive_workflow_next_action,
    expected_provider_for,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (
    LaneEvent,
    evaluate_workflow_runtime,
)

# ---------------------------------------------------------------------------
# Durable event-source identity. The watcher reads Redmine journals; the durable
# anchor is the journal pointer, and the event id encodes it verbatim so the same
# journal is the same durable fact across runs.
# ---------------------------------------------------------------------------

#: The event source this watcher reads (literal; the event id namespace prefix).
SOURCE_REDMINE = "redmine"


def redmine_event_id(issue: str, journal: str) -> str:
    """Build the durable ``redmine:<issue>:<journal>`` event id (pure).

    This is the duplicate-suppression key: two observations of the same Redmine journal
    yield the same id, so re-running the watcher over a journal already recorded suppresses
    it (intake) and the replay fold dedups it again. Both ``issue`` and ``journal`` must be
    non-empty — an anchor that cannot name a specific journal is not a durable anchor.
    """
    issue_s = str(issue).strip()
    journal_s = str(journal).strip()
    if not issue_s or not journal_s:
        raise ValueError(
            "redmine event id requires a non-empty issue and journal "
            f"(got issue={issue!r} journal={journal!r})"
        )
    return f"{SOURCE_REDMINE}:{issue_s}:{journal_s}"


# ---------------------------------------------------------------------------
# Structured marker gate aliases. A journal records a durable gate by its spine name;
# ``review_result`` is the journal-facing name for a recorded review outcome, which the
# runtime models as the ``review`` gate plus a conclusion. The alias maps the marker name
# onto the runtime gate; everything else passes through and is validated literally.
# ---------------------------------------------------------------------------

#: Marker-facing gate name -> runtime gate kind. Only the names that differ are listed;
#: an unlisted name is used as-is (and rejected if not in :data:`GATE_KINDS`).
#: ``owner_close_approval_waiting`` is the callback-facing state name (a child is waiting for
#: owner close approval — workflow.md ``### coordinator callback を要する state``); it maps onto
#: the runtime ``owner_close_approval`` gate (#13520 review F5).
MARKER_GATE_ALIASES: dict[str, str] = {
    "review_result": GATE_REVIEW,
    "owner_close_approval_waiting": GATE_OWNER_CLOSE_APPROVAL,
}


# ---------------------------------------------------------------------------
# Intake disposition (per marker) — observable duplicate suppression.
# ---------------------------------------------------------------------------

#: A marker whose durable anchor was not previously recorded — folded into the runtime.
INTAKE_ACCEPTED = "accepted"
#: A marker whose durable anchor is already recorded (or repeated within the batch) —
#: suppressed so the same journal is never folded twice.
INTAKE_SUPPRESSED = "suppressed"


# ---------------------------------------------------------------------------
# Pending action status — the watcher's classification of the resulting next action.
# ---------------------------------------------------------------------------

#: The owed action resolved cleanly (route known / no confirmation gate); it is recorded
#: as pending and surfaced for explicit execution. The watcher still never auto-sends.
PENDING_READY = "ready"
#: The owed action resolved but is gated behind an explicit confirmation (a mutating /
#: owner / release / destructive action under the #12671 risk policy).
PENDING_NEEDS_CONFIRMATION = "needs_confirmation"
#: The owed action could not be safely recommended — an unknown action, or a routing
#: action whose route is missing / mismatched / ambiguous. Recorded, never auto-sent.
PENDING_FAILED = "failed"

# ---------------------------------------------------------------------------
# Fail-closed reasons specific to the watcher's stricter route selection. (The
# #12671 ``blocked_reason`` tokens — ``unknown_action`` / ``route_identity_unresolved``
# — are carried through verbatim; ``route_ambiguous`` is the watcher addition.)
# ---------------------------------------------------------------------------

#: More than one distinct route matched the owner's expected provider — the watcher refuses
#: to pick a delivery target silently (the issue: "ambiguous route は pending failed state").
FAILED_ROUTE_AMBIGUOUS = "route_ambiguous"


@dataclass(frozen=True)
class JournalMarker:
    """One structured workflow marker observed on a Redmine journal (never free text).

    ``issue`` / ``journal`` are the durable anchor; ``gate`` is the runtime gate kind (an
    alias such as ``review_result`` is already mapped). The remaining fields are exactly the
    #12856 :class:`LaneSignal` facts the marker carries — read from the structured gate
    record, not inferred from prose. Build instances through :func:`build_marker`, which
    validates the vocabulary and applies the gate alias; the dataclass itself stays a plain
    value object.
    """

    issue: str
    journal: str
    gate: str
    review_conclusion: str = REVIEW_PENDING
    callback_state: str = CALLBACK_NONE
    commit_bearing: bool = False
    integration_recorded: bool = False
    issue_open: bool = True
    blocker_recorded: bool = False
    #: Redmine #13974 (additive review-gate contract): the exact full commit head this review gate
    #: reviewed / requested (``target_head``), and — on a ``review_result`` — the exact
    #: ``review_request_journal`` it answers. Blank on non-review gates and on legacy markers written
    #: before the contract. The callback generation fence conjoins them (with lane identity + lifecycle
    #: generation + the provider-authoritative review_result journal id as source sequence) and fails
    #: closed when a review row's head / round drifts or is missing — never parsed from prose.
    target_head: str = ""
    review_request_journal: str = ""

    @property
    def event_id(self) -> str:
        """The durable ``redmine:<issue>:<journal>`` anchor / duplicate-suppression key."""
        return redmine_event_id(self.issue, self.journal)

    def to_lane_event(self) -> LaneEvent:
        """Project this marker onto the #12857 :class:`LaneEvent` it folds in as."""
        return LaneEvent(
            event_id=self.event_id,
            issue=str(self.issue).strip(),
            gate=self.gate,
            review_conclusion=self.review_conclusion,
            callback_state=self.callback_state,
            commit_bearing=self.commit_bearing,
            integration_recorded=self.integration_recorded,
            issue_open=self.issue_open,
            blocker_recorded=self.blocker_recorded,
        )


class JournalMarkerError(ValueError):
    """A structured marker carried a value outside the literal workflow vocabulary.

    Raised by :func:`build_marker` for an unknown gate / conclusion / callback state. The
    watcher fails closed at the boundary rather than silently classifying a typo to
    ``blocked`` — a marker the watcher cannot read structurally is rejected, not guessed.
    """


def build_marker(
    issue: str,
    journal: str,
    gate: str,
    *,
    review_conclusion: str = REVIEW_PENDING,
    callback_state: str = CALLBACK_NONE,
    commit_bearing: bool = False,
    integration_recorded: bool = False,
    issue_open: bool = True,
    blocker_recorded: bool = False,
    target_head: str = "",
    review_request_journal: str = "",
) -> JournalMarker:
    """Validate + normalize a structured marker into a :class:`JournalMarker` (pure).

    ``gate`` is resolved through :data:`MARKER_GATE_ALIASES` (so ``review_result`` becomes
    the ``review`` gate) and then validated against :data:`GATE_KINDS`; ``review_conclusion``
    against :data:`REVIEW_CONCLUSIONS`; ``callback_state`` against :data:`CALLBACK_STATES`.
    A non-empty issue + journal is required (:func:`redmine_event_id`). Any value outside the
    literal vocabulary raises :class:`JournalMarkerError` — fail-closed at the boundary.
    """
    # Validate the durable anchor up front (raises ValueError on empties).
    redmine_event_id(issue, journal)

    raw_gate = str(gate).strip()
    resolved_gate = MARKER_GATE_ALIASES.get(raw_gate, raw_gate)
    if resolved_gate not in GATE_KINDS:
        raise JournalMarkerError(
            f"marker gate must be a known gate (one of {sorted(GATE_KINDS)} or alias "
            f"{sorted(MARKER_GATE_ALIASES)}), got {gate!r}"
        )
    conclusion = str(review_conclusion).strip() or REVIEW_PENDING
    if conclusion not in REVIEW_CONCLUSIONS:
        raise JournalMarkerError(
            f"marker review_conclusion must be one of {sorted(REVIEW_CONCLUSIONS)}, "
            f"got {review_conclusion!r}"
        )
    callback = str(callback_state).strip() or CALLBACK_NONE
    if callback not in CALLBACK_STATES:
        raise JournalMarkerError(
            f"marker callback_state must be one of {sorted(CALLBACK_STATES)}, "
            f"got {callback_state!r}"
        )
    return JournalMarker(
        issue=str(issue).strip(),
        journal=str(journal).strip(),
        gate=resolved_gate,
        review_conclusion=conclusion,
        callback_state=callback,
        commit_bearing=bool(commit_bearing),
        integration_recorded=bool(integration_recorded),
        issue_open=bool(issue_open),
        blocker_recorded=bool(blocker_recorded),
        target_head=str(target_head or "").strip(),
        review_request_journal=str(review_request_journal or "").strip(),
    )


@dataclass(frozen=True)
class IntakeRecord:
    """One observed marker plus its intake disposition (accepted / suppressed)."""

    marker: JournalMarker
    disposition: str

    @property
    def event_id(self) -> str:
        return self.marker.event_id

    def as_payload(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "issue": self.marker.issue,
            "journal": self.marker.journal,
            "gate": self.marker.gate,
            "disposition": self.disposition,
        }


def classify_intake(
    markers: Iterable[JournalMarker], known_event_ids: Iterable[str]
) -> tuple[IntakeRecord, ...]:
    """Partition markers into accepted (new) vs suppressed (duplicate) by durable anchor.

    A marker is :data:`INTAKE_SUPPRESSED` when its ``event_id`` was already recorded
    (``known_event_ids``, the persisted store) **or** repeated earlier in this same batch;
    otherwise it is :data:`INTAKE_ACCEPTED` and its anchor is remembered so a later repeat
    in the batch is also suppressed. Order is preserved so the result is replay-stable.
    """
    seen = {str(eid).strip() for eid in known_event_ids if str(eid).strip()}
    records: list[IntakeRecord] = []
    for marker in markers:
        anchor = marker.event_id
        if anchor in seen:
            records.append(IntakeRecord(marker=marker, disposition=INTAKE_SUPPRESSED))
            continue
        seen.add(anchor)
        records.append(IntakeRecord(marker=marker, disposition=INTAKE_ACCEPTED))
    return tuple(records)


def select_route(
    owner_role: str,
    candidates: Sequence[RouteCandidate],
    *,
    binding: RoleProviderBinding | None = None,
) -> tuple[str, str]:
    """Watcher route selection: ambiguity-aware + fail-closed (pure).

    Returns ``(pointer, failed_reason)``:

    - ``("", "")`` — the owner has no expected provider binding (a non-routing owner such as
      ``owner`` for a release gate, or ``none``); the caller does not treat this as a route
      failure for a non-routing action.
    - ``("", route_missing-equivalent)`` — no candidate matches the owner's expected
      provider. Signalled with an empty pointer + empty reason here; the caller maps an
      unresolved *routing* action to the #12671 ``route_identity_unresolved`` it already
      raises, so this function does not re-name that case.
    - ``("", FAILED_ROUTE_AMBIGUOUS)`` — more than one *distinct* provider-matching pointer.
      The watcher refuses to choose a delivery target silently.
    - ``(pointer, "")`` — exactly one distinct provider-matching pointer.

    Distinctness is by pointer string: the same route recorded twice is not ambiguous; two
    different routes for the same provider is. Uses the shared #12671 role->provider binding
    (:func:`...workflow_next_action.expected_provider_for`) so the watcher and ``resume``
    never drift. ``binding`` is the #12673 role->provider binding (the #13157 config-driven
    override, or the compatibility default when ``None``); it must be the SAME binding the
    matching :func:`derive_workflow_next_action` resolved through, so the watcher's stricter
    ambiguity check and the enrichment's route selection agree on the expected provider.
    """
    expected = expected_provider_for(owner_role, binding=binding)
    if expected is None:
        return "", ""
    matching = [c.pointer for c in candidates if c.provider_role == expected]
    distinct = list(dict.fromkeys(matching))  # de-dup, order-preserving
    if not distinct:
        return "", ""
    if len(distinct) > 1:
        return "", FAILED_ROUTE_AMBIGUOUS
    return distinct[0], ""


@dataclass(frozen=True)
class PendingWorkflowAction:
    """The watcher's pending-action classification of the enriched next action.

    Wraps the #12671 :class:`WorkflowNextAction` (action / owner_role / target_issue /
    route_identity / anchor / risk / confirmation / blocked_reason) and adds the watcher's
    :attr:`status` (:data:`PENDING_READY` / :data:`PENDING_NEEDS_CONFIRMATION` /
    :data:`PENDING_FAILED`) plus a single :attr:`failed_reason` when failed. A pending action
    is a *record*, never a delivery — the watcher sends nothing.
    """

    status: str
    failed_reason: str
    next_action: WorkflowNextAction

    @property
    def is_failed(self) -> bool:
        return self.status == PENDING_FAILED

    @property
    def action(self) -> str:
        return self.next_action.action

    @property
    def anchor(self) -> str:
        return self.next_action.anchor

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "failed_reason": self.failed_reason or "",
            "next_action": self.next_action.as_payload(),
        }


@dataclass(frozen=True)
class EventIntakeOutcome:
    """The full intake result: per-marker dispositions + the resulting pending action.

    ``intake`` is the observable duplicate-suppression partition; ``command_result`` is the
    #12671 ``workflow.{state,next_action}`` envelope re-folded over the full (already
    recorded + newly accepted) event log; ``pending_action`` is the watcher classification.
    The whole thing is reproducible from the durable anchors, so it is safe to record.
    """

    intake: tuple[IntakeRecord, ...]
    command_result: WorkflowCommandResult
    pending_action: PendingWorkflowAction

    @property
    def accepted(self) -> tuple[IntakeRecord, ...]:
        return tuple(r for r in self.intake if r.disposition == INTAKE_ACCEPTED)

    @property
    def suppressed(self) -> tuple[IntakeRecord, ...]:
        return tuple(r for r in self.intake if r.disposition == INTAKE_SUPPRESSED)

    @property
    def accepted_events(self) -> tuple[LaneEvent, ...]:
        """The newly accepted markers as #12857 lane events (what to persist)."""
        return tuple(r.marker.to_lane_event() for r in self.accepted)

    def as_payload(self) -> dict[str, object]:
        return {
            "intake": [r.as_payload() for r in self.intake],
            "pending_action": self.pending_action.as_payload(),
            "workflow": self.command_result.as_payload()["workflow"],
        }


def classify_pending_action(
    next_action: WorkflowNextAction,
    *,
    issue_routes: Mapping[str, Sequence[RouteCandidate]] | None = None,
    binding: RoleProviderBinding | None = None,
) -> PendingWorkflowAction:
    """Classify an enriched next action into a pending action (pure).

    Precedence (fail-closed first):

    1. an existing #12671 ``blocked_reason`` (unknown_action / route_identity_unresolved)
       -> :data:`PENDING_FAILED` carrying that reason verbatim;
    2. otherwise, if the action targets a lane and that lane's provider-matching routes are
       **ambiguous** (:func:`select_route` -> :data:`FAILED_ROUTE_AMBIGUOUS`)
       -> :data:`PENDING_FAILED` / ``route_ambiguous`` (the watcher's stricter check on top
       of ``resume``'s last-write-wins selection);
    3. otherwise, if the action requires explicit confirmation
       -> :data:`PENDING_NEEDS_CONFIRMATION`;
    4. otherwise -> :data:`PENDING_READY`.
    """
    routes = issue_routes or {}

    if next_action.blocked_reason:
        return PendingWorkflowAction(
            status=PENDING_FAILED,
            failed_reason=next_action.blocked_reason,
            next_action=next_action,
        )

    target = next_action.target_issue
    if target and next_action.route_identity:
        # The next action resolved a route via last-write-wins; the watcher additionally
        # refuses an ambiguous target. Only routing actions reach here (route_identity is
        # only set for them), so a non-routing owner never trips this.
        _pointer, reason = select_route(
            next_action.owner_role, routes.get(target, ()), binding=binding
        )
        if reason == FAILED_ROUTE_AMBIGUOUS:
            escalated = _escalate(next_action.risk_level, RISK_HIGH)
            failed_next = WorkflowNextAction(
                action=next_action.action,
                owner_role=next_action.owner_role,
                target_issue=next_action.target_issue,
                route_identity=next_action.route_identity,
                anchor=next_action.anchor,
                suggested_command=next_action.suggested_command,
                risk_level=escalated,
                requires_confirmation=True,
                blocked_reason=FAILED_ROUTE_AMBIGUOUS,
                reason=next_action.reason,
                # Keep the enrichment's resolved provider (#12673 binding surface): the
                # fail-closed rebuild must not drop the rebound provider (#13157 j#71977).
                provider=next_action.provider,
            )
            return PendingWorkflowAction(
                status=PENDING_FAILED,
                failed_reason=FAILED_ROUTE_AMBIGUOUS,
                next_action=failed_next,
            )

    if next_action.requires_confirmation:
        return PendingWorkflowAction(
            status=PENDING_NEEDS_CONFIRMATION,
            failed_reason="",
            next_action=next_action,
        )
    return PendingWorkflowAction(
        status=PENDING_READY, failed_reason=BLOCKED_NONE, next_action=next_action
    )


def evaluate_event_intake(
    markers: Iterable[JournalMarker],
    *,
    recorded_events: Iterable[LaneEvent] = (),
    known_event_ids: Iterable[str] = (),
    issue_routes: Mapping[str, Sequence[RouteCandidate]] | None = None,
    ready_independent_work: int = 0,
    ready_overlapping_work: int = 0,
    capacity_remaining: int = 0,
    owner_or_release_gate_active: bool = False,
    binding: RoleProviderBinding | None = None,
) -> EventIntakeOutcome:
    """Fold newly observed markers into a pending workflow action (pure given inputs).

    1. :func:`classify_intake` partitions the markers into accepted (new anchor) vs
       suppressed (anchor already recorded / repeated) — observable duplicate suppression;
    2. the **already recorded** events plus the accepted markers replay (with the #12857
       fold's own ``event_id`` dedup) into ``workflow.state`` + the enriched next action,
       with each lane's anchor being its latest event id and routes selected by the #12671
       enrichment;
    3. :func:`classify_pending_action` classifies the result, applying the watcher's
       stricter ambiguity check.

    ``recorded_events`` / ``known_event_ids`` come from the persisted store; passing the
    recorded events keeps a lane's prior state in the fold even when this batch only adds a
    later journal. Routes / advisory inputs come from the store too. ``binding`` is the
    #12673 role->provider binding (the #13157 config override, or the compatibility default
    when ``None``); it is threaded into both the enrichment (route selection / provider
    display) and the watcher's stricter ambiguity check so both resolve the owner's expected
    provider through the same binding. This function performs no I/O — persistence and
    rendering are the caller's job.
    """
    recorded = tuple(recorded_events)
    known = list(known_event_ids) or [e.event_id for e in recorded]
    intake = classify_intake(markers, known)

    accepted_events = tuple(
        r.marker.to_lane_event() for r in intake if r.disposition == INTAKE_ACCEPTED
    )
    # Recorded events first (apply order), then the newly accepted journals.
    events = recorded + accepted_events

    issue_anchors: dict[str, str] = {}
    for event in events:
        issue_anchors[event.issue] = event.event_id

    state = evaluate_workflow_runtime(
        events,
        ready_independent_work=ready_independent_work,
        ready_overlapping_work=ready_overlapping_work,
        capacity_remaining=capacity_remaining,
        owner_or_release_gate_active=owner_or_release_gate_active,
    )
    next_action = derive_workflow_next_action(
        state, issue_routes=issue_routes, issue_anchors=issue_anchors, binding=binding
    )
    command_result = WorkflowCommandResult(state=state, next_action=next_action)
    pending = classify_pending_action(
        next_action, issue_routes=issue_routes, binding=binding
    )
    return EventIntakeOutcome(
        intake=intake, command_result=command_result, pending_action=pending
    )


def render_intake_text(outcome: EventIntakeOutcome) -> str:
    """Render the intake outcome as a public-safe human summary (pure; no pane id)."""
    pending = outcome.pending_action
    na = pending.next_action
    lines = [
        f"pending_status: {pending.status}",
        f"failed_reason: {pending.failed_reason or '<none>'}",
        f"action: {na.action}",
        f"owner_role: {na.owner_role}",
        f"target_issue: {na.target_issue or '<none>'}",
        f"route_identity: {na.route_identity or '<unresolved>'}",
        f"anchor: {na.anchor or '<none>'}",
        f"risk_level: {na.risk_level}",
        f"requires_confirmation: {str(na.requires_confirmation).lower()}",
        f"accepted: {len(outcome.accepted)} suppressed: {len(outcome.suppressed)}",
    ]
    if outcome.intake:
        lines.extend(
            f"intake: {r.event_id} {r.marker.gate} -> {r.disposition}"
            for r in outcome.intake
        )
    else:
        lines.append("intake: <none>")
    lines.append(f"reason: {na.reason}")
    return "\n".join(lines)


def render_intake_journal(outcome: EventIntakeOutcome) -> str:
    """Render the intake outcome as a public-safe durable record (pure; no pane id).

    Reuses the #12671 command-result journal (Bandwidth Record Template + runtime read model
    + enriched next action) and prefixes the watcher intake summary — the pending status, the
    fail-closed reason, and the per-marker accepted / suppressed partition — so the recorded
    pending action is reproducible from the durable anchors.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_next_action import (
        render_command_result_journal,
    )

    pending = outcome.pending_action
    lines = [
        "## Redmine event intake pending action",
        "",
        f"- pending_status: {pending.status}",
        f"- failed_reason: {pending.failed_reason or 'none'}",
        f"- accepted: {len(outcome.accepted)}",
        f"- suppressed: {len(outcome.suppressed)}",
        "- intake:",
    ]
    if outcome.intake:
        lines.extend(
            f"  - {r.event_id}: {r.marker.gate} -> {r.disposition}"
            for r in outcome.intake
        )
    else:
        lines.append("  - none")
    lines.extend(["", render_command_result_journal(outcome.command_result)])
    return "\n".join(lines)


__all__ = (
    "SOURCE_REDMINE",
    "redmine_event_id",
    "MARKER_GATE_ALIASES",
    "INTAKE_ACCEPTED",
    "INTAKE_SUPPRESSED",
    "PENDING_READY",
    "PENDING_NEEDS_CONFIRMATION",
    "PENDING_FAILED",
    "FAILED_ROUTE_AMBIGUOUS",
    "JournalMarker",
    "JournalMarkerError",
    "build_marker",
    "IntakeRecord",
    "classify_intake",
    "select_route",
    "PendingWorkflowAction",
    "EventIntakeOutcome",
    "classify_pending_action",
    "evaluate_event_intake",
    "render_intake_text",
    "render_intake_journal",
)
