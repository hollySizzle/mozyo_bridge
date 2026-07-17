"""Public guarded retirement of a session-start scratch pair (Redmine #13892).

The inverse of ``herdr session-start``. ``session-start`` mints an exact Claude/Codex pair
whose only durable identity is its herdr assigned name; it writes no lane lifecycle record,
so every recorded-lane retirement surface refuses it structurally and the pair leaks
capacity forever (live evidence: #13882 j#80060 / j#80066 — a preserved ``dogfood13882``
pair no public rail could retire, which blocked that ticket's F4 retry).

This module is the impure half of that surface: it resolves the exact pair from durable
identity, observes the unit at action time, asks the pure
:func:`...domain.scratch_pair_retire.decide_scratch_pair_retire` for a verdict, and — only
on a green verdict — closes the resolved locators and records the durable outcome.

It **composes reviewed parts** rather than re-deriving them (the #13847 pattern):

- ``plan_herdr_retire_close`` purely as a **unit scoper** (never for its own close targets):
  it is what fixes the targeted unit to this lane and structurally excludes the project
  workspace's default-lane coordinator pair and every other lane (#13377);
- ``expected_slot_rows`` — the RAW scan, read alongside the plan. The aggregated
  ``expected_live_slots`` role-set is deliberately NOT the authority here: it drops
  unexpected occupants, duplicate multiplicity and locator-less rows, so an empty aggregate
  means "no expected role is live", never "the unit is empty" (#13845 review j#80148);
- ``execute_herdr_retire_close`` — the reviewed, per-target non-fatal ``herdr pane close``,
  driven with the **verdict's** pin-matched targets (the #13842 ``pin_matched_close_plan``
  shape), so nothing outside the decided set can ever be closed.

Boundaries this surface keeps: no lifecycle row is ever created (acceptance 4 — a row minted
to pass a retire is fabrication, refused by #13882 j#80066); no store is mutated; no
worktree / branch is removed; no process is launched or resumed; no raw herdr / tmux is
driven by the caller. The only mutation is the pin-matched close of this pair's own slots.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping, Optional, Protocol, Sequence

from mozyo_bridge.core.state.scratch_retirement_fence import (  # noqa: E501
    RetirementUnit,
    ScratchRetirementBusy,
    ScratchRetirementFenceError,
    slot_digest,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
    RUNTIME_AWAITING_INPUT,
    RUNTIME_TURN_ENDED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    DEFAULT_LANE,
    _agent_locator,
    _norm,
    _norm_lane,
    decode_assigned_name,
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E501
    SLOT_STALE,
    classify_named_slot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.scratch_pair_retire import (  # noqa: E501
    STATE_ABSENT,
    STATE_BLOCKED,
    STATE_GREEN,
    ScratchPairObservation,
    ScratchPairRetireVerdict,
    ScratchSlotObservation,
    decide_scratch_pair_retire,
)

#: The herdr runtime receiver-states a settled, drivable agent may be in. ``busy`` /
#: ``blocked`` / ``unknown`` are NOT settled — never close over an in-flight or unreadable
#: turn (the #13842 ``_SETTLED_RUNTIME_STATES`` contract).
_SETTLED_RUNTIME_STATES = frozenset({RUNTIME_AWAITING_INPUT, RUNTIME_TURN_ENDED})

#: Reasons the command itself refuses before any observation (zero-read, zero-write).
REASON_NO_REPO_ANCHOR = "no_repo_anchor"
REASON_WORKSPACE_UNRESOLVED = "workspace_unresolved"
REASON_LANE_REQUIRED = "lane_required"
REASON_LANE_IS_DEFAULT = "lane_is_default"
REASON_PROVIDER_UNRESOLVED = "provider_unresolved"
REASON_IDENTITY_UNENCODABLE = "identity_unencodable"
REASON_CLOSE_FAILED = "close_failed"
#: A durable obligation is owed to one of this pair's slots (Redmine #13892 review j#80506 F4).
#: A reserved / uncertain dispatch-outbox row means a send took the write lock against that
#: assigned name and its fate is unresolved. A runtime ``idle`` / ``turn_ended`` reading cannot
#: rule this out: receiver state and durable obligation are different axes, and the workflow
#: contract forbids promoting a runtime signal into a gate verdict.
REASON_WORK_OBLIGATION_PRESENT = "work_obligation_present"
#: The durable obligation store could not be read. Not observing an obligation is not the same
#: as there being none, so this fails closed rather than closing over unknown owed work.
REASON_OBLIGATION_UNREADABLE = "obligation_unreadable"
#: The post-close re-measure found the targeted unit still occupied (Redmine #13892 review
#: j#80506 F3). A close command's return code is not proof the unit is empty; capacity is
#: recovered only when the panes are actually gone.
REASON_POST_CLOSE_RESIDUE = "post_close_residue"
#: The post-close re-measure could not read a fresh inventory, so the unit's emptiness — the
#: whole point of the retire — is unproven. The closes that committed are still reported.
REASON_POST_CLOSE_UNREADABLE = "post_close_unreadable"
#: Zero expected slots are live, but nothing proves THIS command retired them (Redmine #13892
#: review j#80506 F1). A mistyped ``--lane`` and a never-launched pair are indistinguishable
#: from a completed retire by absence alone, so absence is refused rather than celebrated.
REASON_RETIRE_EVIDENCE_ABSENT = "retire_evidence_absent"
#: Another retirement transaction holds this unit. Zero-close; never wait, never steal.
REASON_RETIREMENT_BUSY = "retirement_busy"
#: The retirement authority is unavailable / unprovable (damaged artifacts, unknown schema,
#: seal mismatch, or a zero-slot run over an absent store). Fail closed (j#80526).
REASON_RETIREMENT_AUTHORITY_UNAVAILABLE = "retirement_authority_unavailable"
#: The close committed but the fence completion write failed. NOT a success — but the closes
#: that committed are reported truthfully, and the next run repairs from the pending attempt.
REASON_COMPLETION_UNPROVEN = "completion_unproven"
#: The surface's own signature (the unit has no lifecycle record) was lost mid-flight
#: (Redmine #13892 review j#80523 R2-F3). Re-verified before the completion, never assumed.
REASON_SIGNATURE_LOST = "signature_lost"

#: The idempotent replay: this exact unit was proven retired by a prior run and no expected
#: slot is live now. Exit 0 (design j#80526 rejects an effect-only exit 1).
STATE_ALREADY_RETIRED = "already_retired"

#: The managed-event kinds this surface appends as its durable retirement outcome. It is
#: an audit record, NOT lifecycle authority — capacity is recovered by the panes ceasing to
#: exist (``enumerate_active_lanes`` folds live panes; a record-less unit has disposition
#: ``None`` and stays in the roster until its panes are gone), so this record explains a
#: retirement rather than causing one.
EVENT_COMMAND = "herdr session-retire"
EVENT_KIND_RETIRED = "scratch_pair_retired"


@dataclass(frozen=True)
class SessionRetireVerdict:
    """The fail-closed outcome of a scratch-pair retire.

    ``closed`` / ``durable_retirement`` carry what **actually happened** even when
    ``state`` is blocked: the close precedes the (best-effort) audit append, so a verdict
    must never claim "nothing was closed" once a close committed (the #13842 review j#79363
    R7 factuality rule).
    """

    state: str
    reason: str = ""
    detail: str = ""
    workspace_id: str = ""
    lane_id: str = ""
    expected_names: tuple[str, ...] = field(default_factory=tuple)
    foreign_names: tuple[str, ...] = field(default_factory=tuple)
    closed: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    failed: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)
    durable_retirement: str = ""
    audit_record: str = ""
    executed: bool = False

    @property
    def ok(self) -> bool:
        # `already_retired` is a proven idempotent replay: exact prior completion + a
        # live-zero measured now. Design j#80526 rejects an effect-only exit 1 for it.
        return self.state in (STATE_GREEN, STATE_ALREADY_RETIRED)

    def as_payload(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "expected_names": list(self.expected_names),
            "foreign_names": list(self.foreign_names),
            "closed": [{"role": r, "locator": loc} for r, loc in self.closed],
            "failed": [
                {"role": r, "locator": loc, "detail": d} for r, loc, d in self.failed
            ],
            "durable_retirement": self.durable_retirement,
            "audit_record": self.audit_record,
            "executed": self.executed,
            "retire_ok": self.ok,
        }


def _blocked(reason: str, detail: str = "", **kw) -> SessionRetireVerdict:
    return SessionRetireVerdict(state=STATE_BLOCKED, reason=reason, detail=detail, **kw)


class SessionRetireOps(Protocol):
    """The impure seam: live inventory, runtime, lifecycle read, close, audit append."""

    def agent_rows(self) -> Sequence[Mapping[str, object]]:
        """The live herdr inventory. Raises on an unreadable inventory (fail-closed)."""

    def runtime_state(self, locator: str) -> str:
        """The herdr runtime receiver-state, fail-soft to ``unknown``."""

    def observe_composer(self, locator: str) -> tuple[bool, Optional[bool]]:
        """Content-free ``(readable, has_pending)``; ``None`` pending = unreadable."""

    def lifecycle_record_absent(self, workspace_id: str, lane_id: str) -> Optional[bool]:
        """``True`` = no record, ``False`` = a record exists, ``None`` = unreadable."""

    def open_obligations(self, workspace_id: str, assigned_names: Sequence[str]):
        """Obligations owed to these slots (with causal identity); ``None`` = unreadable."""

    def retirement_transaction(self, unit, *, live_pair_present: bool):
        """The held, exclusive retirement transaction for the unit (context manager)."""

    def close(self, workspace_id: str, lane_id: str, targets):
        """Close exactly ``targets`` (``(role, locator)``); returns the close result."""

    def record_retirement(self, *, workspace_id: str, lane_id: str, intent: dict) -> str:
        """Append the durable audit record; returns an outcome token."""


class LiveSessionRetireOps:
    """The live composition root (herdr CLI + state stores)."""

    def __init__(self, *, repo_root: Path, env: Optional[Mapping[str, str]] = None):
        self._repo_root = repo_root
        self._env = env

    def _environ(self) -> Mapping[str, str]:
        return self._env if self._env is not None else os.environ

    def agent_rows(self) -> Sequence[Mapping[str, object]]:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            list_herdr_agent_rows,
        )

        return list_herdr_agent_rows(self._environ())

    def _binary(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            _resolve_binary_or_die,
        )

        return _resolve_binary_or_die(self._environ())

    def runtime_state(self, locator: str) -> str:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_state import (  # noqa: E501
            HerdrCliAgentStateReader,
        )

        try:
            state = HerdrCliAgentStateReader(self._binary()).read_agent_state(locator)
            return state.state if state.ok else "unknown"
        except Exception:  # noqa: BLE001 - a failed runtime read is fail-soft to unknown
            return "unknown"

    def observe_composer(self, locator: str) -> tuple[bool, Optional[bool]]:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
            observe_composer_text,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
            HerdrCliTransport,
        )

        try:
            read = HerdrCliTransport(self._binary()).read_pane(locator, lines=80)
            if not read.ok:
                return (False, None)
            observation = observe_composer_text(read.content)
            return (observation.readable, observation.has_pending)
        except Exception:  # noqa: BLE001 - a failed composer read is fail-soft to unreadable
            return (False, None)

    def lifecycle_record_absent(self, workspace_id: str, lane_id: str) -> Optional[bool]:
        from mozyo_bridge.core.state.lane_lifecycle import (
            LaneLifecycleKey,
            LaneLifecycleStore,
        )

        try:
            record = LaneLifecycleStore().get(LaneLifecycleKey(workspace_id, lane_id))
        except Exception:  # noqa: BLE001 - unreadable is NOT absent; fail closed
            return None
        return record is None

    def open_obligations(self, workspace_id: str, assigned_names):
        from mozyo_bridge.core.state.dispatch_outbox_fence import (
            DispatchOutboxFence,
            DispatchOutboxFenceError,
        )

        try:
            return DispatchOutboxFence().obligations_for_targets(
                workspace_id=workspace_id, target_assigned_names=tuple(assigned_names)
            )
        except (DispatchOutboxFenceError, OSError):
            # Unreadable / identity-mismatched store: an obligation we cannot see is not an
            # obligation that is absent. `None` routes the caller to a fail-closed refusal.
            return None

    def retirement_transaction(self, unit, *, live_pair_present: bool):
        from mozyo_bridge.core.state.scratch_retirement_fence import (
            ScratchRetirementFence,
        )

        return ScratchRetirementFence().transaction(
            unit, live_pair_present=live_pair_present
        )

    def close(self, workspace_id: str, lane_id: str, targets):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
            HerdrRetireClosePlan,
            execute_herdr_retire_close,
        )

        # A pin-matched plan built from the VERDICT's targets: the reviewed executor can
        # then only ever touch the set the decision proved (#13842 pin_matched_close_plan).
        plan = HerdrRetireClosePlan(
            workspace_id=workspace_id,
            lane_id=lane_id,
            close_targets=tuple(targets),
            foreign_names=(),
        )
        return execute_herdr_retire_close(plan, env=self._environ())

    def record_retirement(self, *, workspace_id: str, lane_id: str, intent: dict) -> str:
        from mozyo_bridge.core.state.managed_events import record_managed_event

        event = record_managed_event(
            command=EVENT_COMMAND,
            event_kind=EVENT_KIND_RETIRED,
            workspace_id=workspace_id,
            repo_root=str(self._repo_root),
            intent=intent,
        )
        # Best-effort by contract (a telemetry-shaped append must never undo a committed
        # close), so the token is surfaced rather than raised — an operator must be able to
        # see that the close happened but the audit row did not.
        return "recorded" if event is not None else "not_recorded:append_failed"


def observe_scratch_pair(
    ops: SessionRetireOps,
    *,
    workspace_id: str,
    lane_id: str,
    expected_roles: Sequence[str],
) -> ScratchPairObservation:
    """Observe the targeted unit at action time (impure; every fact positive).

    A slot is resolved by **exact assigned-name match**, the pair's only durable identity.
    Liveness policy follows the #13845 discipline: progress requires a *positive proof of
    deadness*, never the absence of a liveness proof. A row ``classify_named_slot``
    positively calls ``SLOT_STALE`` is shell residue — it has no agent, hence no in-flight
    turn and no composer to lose, so its settle-facts are true by construction. Every other
    present row must positively prove idle + settled composer through the runtime.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
        expected_slot_rows,
        plan_herdr_retire_close,
    )

    try:
        rows = list(ops.agent_rows())
    except Exception:  # noqa: BLE001 - an unreadable inventory is never an empty one
        return ScratchPairObservation(inventory_readable=False)

    record_absent = ops.lifecycle_record_absent(workspace_id, lane_id)

    # The plan is used ONLY as a unit scoper: it fixes the targeted unit to this lane and
    # structurally excludes the coordinator's default-lane pair and every other lane. Its
    # own close_targets are deliberately ignored — this surface decides its own.
    plan = plan_herdr_retire_close(
        rows,
        workspace_id=workspace_id,
        lane_id=lane_id,
        legacy_workspace_id="",
        managed_roles=tuple(expected_roles),
    )
    candidates = expected_slot_rows(rows, plan, managed_roles=tuple(expected_roles))

    # Duplicate multiplicity, keyed on the CANONICAL slot `(workspace_id, lane_id, role)` —
    # never on `role` alone, which would misread the legitimate shared/legacy-twin shape as
    # a uniqueness violation (#13845 review j#80187 R3-F1).
    seen: dict[tuple[str, str, str], int] = {}
    for found in candidates:
        seen[found.slot_key] = seen.get(found.slot_key, 0) + 1
    duplicates = tuple(sorted(key for key, count in seen.items() if count > 1))

    # Present-but-unlocatable rows that the liveness contract does NOT positively call
    # stale residue: they can neither be closed nor read as gone.
    unresolved = tuple(
        sorted(
            {
                found.role
                for found in candidates
                if not found.locator and classify_named_slot(found.row) != SLOT_STALE
            }
        )
    )

    slots: list[ScratchSlotObservation] = []
    for role in expected_roles:
        assigned_name = encode_assigned_name(workspace_id, role, lane_id)
        matches = [
            row
            for row in rows
            if isinstance(row, Mapping)
            and _norm(row.get(AGENT_KEY_NAME)) == _norm(assigned_name)
        ]
        if len(matches) != 1:
            # 0 = positively absent (a prior run closed it, or it never launched) — that is
            # what makes a partial close replayable, not a block. >1 = ambiguous.
            slots.append(
                ScratchSlotObservation(
                    role=role,
                    assigned_name=assigned_name,
                    candidate_count=len(matches),
                )
            )
            continue
        row = matches[0]
        locator = _norm(_agent_locator(row))
        decode = decode_assigned_name(row.get(AGENT_KEY_NAME))
        belongs = bool(
            decode.ok
            and decode.identity is not None
            and decode.identity.workspace_id == _norm(workspace_id)
            and _norm_lane(decode.identity.lane_id) == _norm_lane(lane_id)
            and decode.identity.role == _norm(role)
        )
        if classify_named_slot(row) == SLOT_STALE:
            # Positively dead shell residue: no agent, so no turn and no composer to lose.
            agent_idle = True
            composer_settled = True
        elif locator:
            agent_idle = ops.runtime_state(locator) in _SETTLED_RUNTIME_STATES
            readable, has_pending = ops.observe_composer(locator)
            composer_settled = bool(readable) and has_pending is False
        else:
            agent_idle = False
            composer_settled = False
        slots.append(
            ScratchSlotObservation(
                role=role,
                assigned_name=assigned_name,
                candidate_count=1,
                locator=locator,
                belongs_to_pair=belongs,
                agent_idle=agent_idle,
                composer_settled=composer_settled,
            )
        )

    return ScratchPairObservation(
        inventory_readable=True,
        # `None` (unreadable) must never be read as "absent" — a lifecycle store that
        # cannot be read cannot prove this unit is record-less.
        lifecycle_record_absent=record_absent is True,
        slots=tuple(slots),
        duplicate_slot_keys=duplicates,
        foreign_names=tuple(plan.foreign_names),
        unresolved_roles=unresolved,
    )


def run_session_retire(
    args: argparse.Namespace,
    repo_root: Path,
    *,
    ops: Optional[SessionRetireOps] = None,
) -> SessionRetireVerdict:
    """Resolve, observe, decide and (only on ``--execute`` + green) close. Fail-closed.

    Read-only by default: without ``--execute`` this reports the verdict and closes
    nothing, so an operator can see what a retire *would* do before doing it.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
        resolve_gateway_provider,
        resolve_worker_provider,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
        herdr_workspace_segment,
    )

    lane_id = _norm_lane(getattr(args, "lane", "") or "")
    if not _norm(getattr(args, "lane", "") or ""):
        return _blocked(
            REASON_LANE_REQUIRED,
            "a scratch pair is named by its lane; --lane is required so the retire can "
            "never resolve a unit the request did not name",
        )
    if lane_id == DEFAULT_LANE:
        return _blocked(
            REASON_LANE_IS_DEFAULT,
            "the default lane hosts the coordinator pair and is never a retire target",
        )

    try:
        resolved_root = Path(repo_root).expanduser().resolve()
    except (OSError, ValueError):
        return _blocked(REASON_NO_REPO_ANCHOR, "the repo root could not be resolved")

    try:
        workspace_id = herdr_workspace_segment(resolved_root)
    except Exception:  # noqa: BLE001
        workspace_id = ""
    if not workspace_id:
        return _blocked(
            REASON_WORKSPACE_UNRESOLVED,
            "the workspace segment could not be resolved from the repo root; the pair's "
            "identity cannot be built, so nothing is closed",
        )

    try:
        gateway = resolve_gateway_provider(str(resolved_root))
        worker = resolve_worker_provider(str(resolved_root))
    except Exception as exc:  # noqa: BLE001
        return _blocked(
            REASON_PROVIDER_UNRESOLVED,
            f"the provider binding could not be resolved ({type(exc).__name__})",
            workspace_id=workspace_id,
            lane_id=lane_id,
        )
    if not gateway or not worker:
        return _blocked(
            REASON_PROVIDER_UNRESOLVED,
            "the provider binding did not resolve both a gateway and a worker provider",
            workspace_id=workspace_id,
            lane_id=lane_id,
        )
    expected_roles = (gateway, worker)

    try:
        expected_names = tuple(
            encode_assigned_name(workspace_id, role, lane_id) for role in expected_roles
        )
    except Exception as exc:  # noqa: BLE001
        return _blocked(
            REASON_IDENTITY_UNENCODABLE,
            f"the pair's assigned names could not be encoded ({type(exc).__name__})",
            workspace_id=workspace_id,
            lane_id=lane_id,
        )

    live_ops = ops or LiveSessionRetireOps(repo_root=resolved_root)
    observation = observe_scratch_pair(
        live_ops,
        workspace_id=workspace_id,
        lane_id=lane_id,
        expected_roles=expected_roles,
    )
    verdict: ScratchPairRetireVerdict = decide_scratch_pair_retire(
        observation, expected_roles=expected_roles
    )

    base = dict(
        workspace_id=workspace_id,
        lane_id=lane_id,
        expected_names=expected_names,
        foreign_names=tuple(observation.foreign_names),
    )
    base = dict(
        workspace_id=workspace_id,
        lane_id=lane_id,
        expected_names=expected_names,
        foreign_names=tuple(observation.foreign_names),
    )
    if verdict.state == STATE_BLOCKED:
        return _blocked(verdict.reason, verdict.detail, **base)

    live_targets = tuple(verdict.close_targets)
    pair_is_live = bool(live_targets)

    # The retirement transaction (design j#80526). Everything from the authority read through
    # the close, the fresh re-measure and the completion happens under ONE exclusive,
    # non-blocking advisory lock: `BEGIN IMMEDIATE` cannot span the close, which is an external
    # process operation, so it alone would let a second caller enter the unit mid-flight.
    try:
        unit = RetirementUnit(
            workspace_id=workspace_id,
            lane_id=lane_id,
            slot_digest=slot_digest(expected_names),
        )
    except ValueError as exc:
        return _blocked(REASON_IDENTITY_UNENCODABLE, str(exc), **base)

    try:
        with live_ops.retirement_transaction(unit, live_pair_present=pair_is_live) as txn:
            prior = txn.current()

            if not pair_is_live:
                # Zero expected slots live. Only a proven prior completion of THIS exact unit
                # makes it a success; absence alone never does (review j#80506 F1).
                if prior is not None and prior.completed:
                    return SessionRetireVerdict(
                        state=STATE_ALREADY_RETIRED,
                        detail=(
                            "this exact pair was proven retired by a prior run and no slot is "
                            "live now"
                        ),
                        closed=prior.closed,
                        durable_retirement="already_completed",
                        executed=bool(getattr(args, "execute", False)),
                        **base,
                    )
                if prior is not None and prior.pending:
                    # A crash after the close but before the completion. Repair it: re-measure
                    # the whole unit and, if provably empty, finish the attempt (F2 repair).
                    if not getattr(args, "execute", False):
                        return SessionRetireVerdict(
                            state=STATE_GREEN,
                            detail=(
                                "a pending retirement attempt is repairable; re-run with "
                                "--execute to re-measure and complete it"
                            ),
                            executed=False,
                            **base,
                        )
                    return _finish_retirement(
                        live_ops,
                        txn,
                        args=args,
                        attempt=prior,
                        closed=prior.closed,
                        workspace_id=workspace_id,
                        lane_id=lane_id,
                        expected_roles=expected_roles,
                        expected_names=expected_names,
                        base=base,
                        repaired=True,
                    )
                return _blocked(
                    REASON_RETIRE_EVIDENCE_ABSENT,
                    "no slot of this pair is live, and no durable attempt proves this command "
                    "retired it; refusing to report a retirement it cannot prove (a mistyped "
                    "--lane looks exactly like this). Nothing was closed or written",
                    **base,
                )

            # A live pair. Read the durable obligations owed to these exact slots AFTER taking
            # the transaction (j#80526 ordering): holding the authority first is what closes
            # the window in which a dispatch could reserve against a slot we are about to
            # close. Runtime idle / turn-ended is receiver state and proves nothing here.
            blocked = _obligation_refusal(live_ops, workspace_id, expected_names, base)
            if blocked is not None:
                return blocked

            if not getattr(args, "execute", False):
                return SessionRetireVerdict(
                    state=STATE_GREEN,
                    detail=(
                        "the pair is retirable; re-run with --execute to close "
                        f"{len(live_targets)} slot(s)"
                    ),
                    executed=False,
                    **base,
                )

            # Resume an in-flight attempt, or open a new one. A `completed` attempt followed by
            # newly live slots is a RELAUNCH at the same deterministic names, so it opens a new
            # attempt rather than being mistaken for the old completion (j#80526).
            already_closed: tuple[tuple[str, str], ...] = ()
            if prior is not None and prior.pending:
                attempt = prior
                already_closed = prior.closed
                remaining = tuple(
                    t for t in live_targets if t not in set(already_closed)
                )
            else:
                attempt = txn.reserve(pinned=live_targets)
                remaining = live_targets

            result = live_ops.close(workspace_id, lane_id, remaining)
            closed = tuple(already_closed) + tuple(getattr(result, "closed", ()) or ())
            failed = tuple(getattr(result, "failed", ()) or ())
            if closed:
                # Persist progress so a crash here resumes from what actually committed.
                txn.record_progress(attempt_id=attempt.attempt_id, closed=closed)
            if failed:
                return SessionRetireVerdict(
                    state=STATE_BLOCKED,
                    reason=REASON_CLOSE_FAILED,
                    detail=(
                        "one or more slots did not close; the attempt stays pending and a "
                        "re-run resumes the remainder"
                    ),
                    closed=closed,
                    failed=failed,
                    executed=True,
                    **base,
                )
            return _finish_retirement(
                live_ops,
                txn,
                args=args,
                attempt=attempt,
                closed=closed,
                workspace_id=workspace_id,
                lane_id=lane_id,
                expected_roles=expected_roles,
                expected_names=expected_names,
                base=base,
                repaired=False,
            )
    except ScratchRetirementBusy as exc:
        return _blocked(REASON_RETIREMENT_BUSY, str(exc), **base)
    except ScratchRetirementFenceError as exc:
        return _blocked(REASON_RETIREMENT_AUTHORITY_UNAVAILABLE, str(exc), **base)


def _obligation_refusal(live_ops, workspace_id, expected_names, base):
    """The durable-obligation gate, or ``None`` when nothing is owed. (review j#80506 F4)"""
    obligations = live_ops.open_obligations(workspace_id, expected_names)
    if obligations is None:
        return _blocked(
            REASON_OBLIGATION_UNREADABLE,
            "the durable dispatch-obligation store could not be read; an obligation that "
            "cannot be observed is not an obligation that is absent, so nothing is closed",
            **base,
        )
    owed = [o for o in obligations if o.non_terminal]
    if owed:
        shown = ", ".join(
            f"{o.target_assigned_name} ({o.state}, issue {o.issue or '<none>'})" for o in owed
        )
        return _blocked(
            REASON_WORK_OBLIGATION_PRESENT,
            f"durable work is owed to this pair ({shown}); a reserved / uncertain dispatch "
            "means a send's fate is unresolved, which no idle runtime reading can rule out",
            **base,
        )
    # `delivered` is a delivery ACK, not task completion (the ACK / completion separation), so
    # it cannot be waved through on its own: whether the handed-off work is discharged lives in
    # the Redmine gate its issue/journal names. This surface has no read of that gate, so an
    # uncorrelatable delivered obligation fails closed rather than being assumed finished.
    delivered = [o for o in obligations if o.needs_gate_correlation]
    if delivered:
        shown = ", ".join(
            f"{o.target_assigned_name} (delivered, issue {o.issue or '<none>'} "
            f"j#{o.journal or '<none>'})"
            for o in delivered
        )
        return _blocked(
            REASON_WORK_OBLIGATION_PRESENT,
            f"work was delivered to this pair and its completion cannot be correlated "
            f"({shown}); a delivery ACK is not task completion, so this surface will not "
            "assume the work is discharged",
            **base,
        )
    return None


def _finish_retirement(
    live_ops,
    txn,
    *,
    args,
    attempt,
    closed,
    workspace_id,
    lane_id,
    expected_roles,
    expected_names,
    base,
    repaired,
):
    """Fresh whole-unit re-measure -> fence completion -> best-effort audit -> green.

    The re-measure re-checks the SURFACE SIGNATURE too, not only the inventory (review j#80523
    R2-F3): this rail exists only for a record-less unit, so if a lifecycle record appeared
    mid-flight the unit stopped being this surface's to retire and no completion may be claimed.
    """
    after = observe_scratch_pair(
        live_ops,
        workspace_id=workspace_id,
        lane_id=lane_id,
        expected_roles=expected_roles,
    )
    if not after.inventory_readable:
        return SessionRetireVerdict(
            state=STATE_BLOCKED,
            reason=REASON_POST_CLOSE_UNREADABLE,
            detail=(
                "the close committed but a fresh inventory could not be read, so the unit's "
                "emptiness is unproven; the attempt stays pending and a re-run repairs it"
            ),
            closed=closed,
            executed=True,
            **base,
        )
    if not after.lifecycle_record_absent:
        return SessionRetireVerdict(
            state=STATE_BLOCKED,
            reason=REASON_SIGNATURE_LOST,
            detail=(
                "the unit gained a durable lifecycle record during the retire, so it is no "
                "longer this surface's to retire (this rail is only for record-less pairs); "
                "no completion is claimed"
            ),
            closed=closed,
            executed=True,
            **base,
        )
    residue = [slot.assigned_name for slot in after.slots if not slot.absent]
    if residue or after.foreign_names or after.duplicate_slot_keys:
        bits = []
        if residue:
            bits.append(f"expected slot(s) still live: {', '.join(residue)}")
        if after.foreign_names:
            bits.append(f"foreign occupant(s): {', '.join(after.foreign_names)}")
        if after.duplicate_slot_keys:
            bits.append("duplicate canonical slot(s) appeared")
        return SessionRetireVerdict(
            state=STATE_BLOCKED,
            reason=REASON_POST_CLOSE_RESIDUE,
            detail=(
                "the close committed but the unit is not empty, so this is not a retirement ("
                + "; ".join(bits)
                + ")"
            ),
            closed=closed,
            foreign_names=tuple(after.foreign_names),
            executed=True,
            **{k: v for k, v in base.items() if k != "foreign_names"},
        )
    # An obligation reserved DURING the close would be invisible to the pre-close read alone
    # (review j#80523 R2-F2), so re-read it here, still under the held transaction.
    blocked = _obligation_refusal(live_ops, workspace_id, expected_names, base)
    if blocked is not None:
        return replace(blocked, closed=closed, executed=True)

    try:
        txn.mark_completed(
            attempt_id=attempt.attempt_id,
            closed=closed,
            detail=f"{len(closed)} slot(s) closed; unit re-measured empty",
        )
    except ScratchRetirementFenceError as exc:
        # The load-bearing durable outcome failed. NOT a success — but the closes that
        # committed are reported truthfully, and the pending attempt lets the next run repair.
        return SessionRetireVerdict(
            state=STATE_BLOCKED,
            reason=REASON_COMPLETION_UNPROVEN,
            detail=(
                f"the panes closed and the unit re-measured empty, but the retirement could "
                f"not be recorded ({exc}); re-run to repair the pending attempt"
            ),
            closed=closed,
            executed=True,
            **base,
        )

    # The fence row IS the durable outcome (acceptance 4). `managed_events` is appended only
    # AFTER it, purely as lossy narrative audit: its failure is reported but never invalidates
    # a proven retirement, because the load-bearing authority is the fence, not the audit log.
    audit = live_ops.record_retirement(
        workspace_id=workspace_id,
        lane_id=lane_id,
        intent={
            "lane_id": lane_id,
            "expected_names": list(expected_names),
            "closed": [{"role": r, "locator": loc} for r, loc in closed],
            "surface": EVENT_COMMAND,
        },
    )
    detail = f"closed {len(closed)} slot(s); the unit re-measured empty on a fresh inventory"
    if repaired:
        detail = (
            f"repaired a pending attempt: {len(closed)} slot(s) were already closed and the "
            "unit re-measured empty"
        )
    return SessionRetireVerdict(
        state=STATE_GREEN,
        detail=detail,
        closed=closed,
        durable_retirement="fence_completed",
        audit_record=audit,
        executed=True,
        **base,
    )



def format_session_retire_text(result: SessionRetireVerdict) -> str:
    lines = [f"scratch pair retire: {result.state}"]
    if result.reason:
        lines.append(f"  reason: {result.reason}")
    if result.detail:
        lines.append(f"  detail: {result.detail}")
    lines.append(f"  workspace: {result.workspace_id or '<unresolved>'}")
    lines.append(f"  lane: {result.lane_id or '<unresolved>'}")
    for name in result.expected_names:
        lines.append(f"    expected slot: {name}")
    for name in result.foreign_names:
        lines.append(f"    foreign (never closed): {name}")
    for role, locator in result.closed:
        lines.append(f"    closed: {role} @ {locator}")
    for role, locator, detail in result.failed:
        lines.append(f"    close FAILED: {role} @ {locator}: {detail}")
    if result.durable_retirement:
        lines.append(f"  durable retirement: {result.durable_retirement}")
    if result.audit_record and result.audit_record != "recorded":
        # The fence proves the retirement; the audit log is narrative only, so a failed
        # append is reported without invalidating a proven close (design j#80526).
        lines.append(f"  audit record: {result.audit_record} (narrative only; the "
                     f"retirement itself is proven by the fence)")
    if not result.executed and result.state == STATE_GREEN:
        lines.append("  (read-only preflight; nothing was closed)")
    return "\n".join(lines)


__all__ = (
    "EVENT_COMMAND",
    "EVENT_KIND_RETIRED",
    "REASON_NO_REPO_ANCHOR",
    "REASON_WORKSPACE_UNRESOLVED",
    "REASON_LANE_REQUIRED",
    "REASON_LANE_IS_DEFAULT",
    "REASON_PROVIDER_UNRESOLVED",
    "REASON_IDENTITY_UNENCODABLE",
    "REASON_CLOSE_FAILED",
    "REASON_WORK_OBLIGATION_PRESENT",
    "REASON_OBLIGATION_UNREADABLE",
    "REASON_POST_CLOSE_RESIDUE",
    "REASON_POST_CLOSE_UNREADABLE",
    "REASON_RETIRE_EVIDENCE_ABSENT",
    "REASON_RETIREMENT_BUSY",
    "REASON_RETIREMENT_AUTHORITY_UNAVAILABLE",
    "REASON_COMPLETION_UNPROVEN",
    "REASON_SIGNATURE_LOST",
    "STATE_ALREADY_RETIRED",
    "SessionRetireVerdict",
    "SessionRetireOps",
    "LiveSessionRetireOps",
    "observe_scratch_pair",
    "run_session_retire",
    "format_session_retire_text",
)
