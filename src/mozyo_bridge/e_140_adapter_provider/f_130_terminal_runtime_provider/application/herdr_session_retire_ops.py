"""The impure seam for the scratch-pair retire: live herdr / store observation.

Split from :mod:`...herdr_session_retire` (Redmine #13892) along the boundary the module
already had: this half talks to herdr and the durable stores, that half decides. Keeping the
observation here also keeps the decision module readable as one ordered contract.
"""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - fixed-argv, read-only git probes
from pathlib import Path
from typing import Mapping, Optional, Protocol, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
    RUNTIME_AWAITING_INPUT,
    RUNTIME_TURN_ENDED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
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
    ScratchPairObservation,
    ScratchSlotObservation,
)

#: The herdr runtime receiver-states a settled, drivable agent may be in. ``busy`` /
#: ``blocked`` / ``unknown`` are NOT settled — never close over an in-flight or unreadable
#: turn (the #13842 ``_SETTLED_RUNTIME_STATES`` contract).
_SETTLED_RUNTIME_STATES = frozenset({RUNTIME_AWAITING_INPUT, RUNTIME_TURN_ENDED})


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

    def worktree_facts(self) -> tuple[bool, bool, str]:
        """Action-time ``(readable, clean, branch)`` for this repo root."""

    def open_obligations(self, workspace_id: str, assigned_names: Sequence[str]):
        """EVERY covered source's blocking obligations; ``None`` = unreadable (fail closed).

        Covered = dispatch outbox (owed TO) + callback outbox (owed TO) + forward fence
        (owed BY). The set is fixed by the obligation source matrix test.
        """

    def retirement_transaction(self, unit, *, live_pair_present: bool):
        """The held, exclusive retirement transaction for the unit (context manager)."""

    def peek_retirement(self, unit):
        """Strict read-only observation of the unit's attempt; writes NO artifact."""

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

    def worktree_facts(self) -> tuple[bool, bool, str]:
        """Read the historical lane worktree without mutating Git state (#13918)."""
        try:
            status = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["git", "status", "--porcelain=v1"],
                cwd=self._repo_root,
                text=True,
                capture_output=True,
                check=False,
            )
            branch = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["git", "branch", "--show-current"],
                cwd=self._repo_root,
                text=True,
                capture_output=True,
                check=False,
            )
        except (OSError, ValueError):
            return (False, False, "")
        readable = status.returncode == 0 and branch.returncode == 0
        if not readable:
            return (False, False, "")
        return (True, not bool(status.stdout.strip()), branch.stdout.strip())

    def open_obligations(self, workspace_id: str, assigned_names):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.scratch_pair_obligations import (  # noqa: E501
            ObligationStoreUnreadable,
            all_pair_obligations,
        )

        lane = ""
        roles: list[str] = []
        for name in assigned_names:
            decode = decode_assigned_name(name)
            if decode.ok and decode.identity is not None:
                lane = lane or _norm_lane(decode.identity.lane_id)
                roles.append(decode.identity.role)
        try:
            return all_pair_obligations(
                workspace_id=workspace_id,
                lane_id=lane,
                assigned_names=tuple(assigned_names),
                roles=tuple(roles),
                correlate=self._durable_disposition,
            )
        except ObligationStoreUnreadable:
            # An obligation we cannot see is not an obligation that is absent. `None` routes
            # the caller to a fail-closed refusal.
            return None

    def _durable_disposition(self, row):
        """Was the work a delivered send handed over positively discharged? (design j#80629)

        ``row`` is the ACTUAL :class:`TargetObligation` from the dispatch outbox, carrying the
        full causal identity the store recorded. It is passed through **unchanged** (review
        j#80644 R6-F2): the earlier cut received only ``(issue, journal)`` and rebuilt the
        workspace / lane / target / action_id **from the AUTHORIZE**, so the correlator's
        identity check compared that AUTHORIZE with itself — a tautology that discharged every
        delivered row, including ones whose real ``action_id`` named a foreign action.

        Reads the **source of truth** — the Redmine issue's journals — and requires the full
        three-way correspondence: the row's own AUTHORIZE, a later canonical `review_request`,
        and a later `dispatch-disposition` naming that exact action and terminal journal.

        The earlier cut asked the CallbackOutbox instead, which was rejected (j#80620): a
        callback `delivered` is a *callback's* delivery ACK and `dead_letter` is "unclassified /
        retries exhausted" — neither owns the completion of the work the dispatch handed over.
        That the Redmine gate is hard to read here is not a licence to believe a different
        store: unreadable means `None`, and the caller blocks.

        `True` = discharged, `False` = still owed, `None` = unknown (the caller blocks).
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_disposition import (  # noqa: E501
            CORRELATION_DISCHARGED,
            CORRELATION_OWED,
            DispatchRowIdentity,
            correlate_dispatch_disposition,
        )

        source = self._redmine_source()
        if source is None:
            return None  # no credentialed source -> unknown -> block
        try:
            entries = list(source.read_entries(_norm(row.issue)))
        except Exception:  # noqa: BLE001 - unreadable / credential failure -> unknown -> block
            return None
        auths, reviews = self._authorize_and_review_index(entries)
        identity = DispatchRowIdentity(
            issue=_norm(row.issue),
            journal=_norm(row.journal),
            workspace_id=_norm(row.workspace_id),
            lane_id=_norm(row.lane_id),
            target_assigned_name=_norm(row.target_assigned_name),
            action_id=_norm(row.action_id),
        )
        verdict = correlate_dispatch_disposition(
            identity, entries, authorize_journals=auths, review_request_journals=reviews
        )
        if verdict.state == CORRELATION_DISCHARGED:
            return True
        if verdict.state == CORRELATION_OWED:
            return False
        return None  # ambiguous -> block

    def _redmine_source(self):
        """The credential-gated live journal source, or ``None`` when unavailable."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
            LiveRedmineJournalSource,
        )

        try:
            return LiveRedmineJournalSource.from_environment()
        except Exception:  # noqa: BLE001 - no credentials / transport -> unknown -> block
            return None

    @staticmethod
    def _authorize_and_review_index(entries):
        """`{journal: (DispatchAuthorization, ...)}` and the canonical review_request journals.

        The AUTHORIZE index keeps **every** valid marker at a journal, preserving cardinality
        (review j#80644 R6-F2). The earlier `{journal: auth}` dict comprehension collapsed two
        valid AUTHORIZE markers at one journal by last-write-wins, silently converting a
        genuine ambiguity into a confident discharge. 0 / 1 / 2+ are three different answers
        and the correlator must be able to tell them apart.

        Both come from the repo's existing parsers — this adds no second interpretation of
        marker text.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (  # noqa: E501
            parse_dispatch_authorizations,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            extract_markers,
        )

        auths: dict[str, list] = {}
        for a in parse_dispatch_authorizations(entries):
            if a.valid:
                auths.setdefault(_norm(a.journal), []).append(a)
        reviews = [
            _norm(m.journal)
            for m in extract_markers(entries)
            if _norm(m.gate) == "review_request"
        ]
        return auths, reviews

    def retirement_transaction(self, unit, *, live_pair_present: bool):
        """Open the exclusive retirement transaction for the exact pair unit."""
        from mozyo_bridge.core.state.scratch_retirement_fence import (
            ScratchRetirementFence,
        )

        return ScratchRetirementFence().transaction(
            unit, live_pair_present=live_pair_present
        )

    def peek_retirement(self, unit):
        """Read the retirement fence without creating or migrating its store."""
        from mozyo_bridge.core.state.scratch_retirement_fence import (
            ScratchRetirementFence,
        )

        return ScratchRetirementFence().peek(unit)

    def close(self, workspace_id: str, lane_id: str, targets):
        """Close only the exact role/locator pins admitted by the retire verdict."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
            HerdrRetireClosePlan,
            execute_herdr_retire_close,
        )

        plan = HerdrRetireClosePlan(
            workspace_id=workspace_id,
            lane_id=lane_id,
            close_targets=tuple(targets),
            foreign_names=(),
        )
        return execute_herdr_retire_close(plan, env=self._environ())

    def record_retirement(self, *, workspace_id: str, lane_id: str, intent: dict) -> str:
        """Append the best-effort narrative audit after the fence proves completion."""
        from mozyo_bridge.core.state.managed_events import record_managed_event
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire import (  # noqa: E501
            EVENT_COMMAND,
            EVENT_KIND_RETIRED,
        )

        event = record_managed_event(
            command=EVENT_COMMAND,
            event_kind=EVENT_KIND_RETIRED,
            workspace_id=workspace_id,
            repo_root=str(self._repo_root),
            intent=intent,
        )
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




__all__ = (
    "SessionRetireOps",
    "LiveSessionRetireOps",
    "observe_scratch_pair",
)
