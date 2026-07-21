"""Regressions for the #14222 US-level R2 review findings (Redmine j#85125 F1-F3).

F1 — startup execution events must be attributed to a PARTICIPANT: one provider's
``provider_exec_rejected`` must never collapse its sibling's inventory join to
``not_applicable`` (the independent reproduction in the review). F2 — the post-launch
evidence gate must fire through the REAL health composition (`probe_session_health`
with a bound evidence reader), not only when a unit test calls the pure classifier
directly. F3 — `config status` must classify operator-relevant LEAF key paths so a
partial block declaration cannot bury an undeclared nested default, and a legacy v1
declaration must be machine-readably `compatibility`, not `declared`.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mozyo_bridge.core.state.startup_execution_events import (
    JOIN_NOT_APPLICABLE,
    JOIN_PROVIDER_LIVE_CONFIRMED,
    REASON_STARTUP_EVIDENCE_UNATTRIBUTED,
    STAGE_NO_EVIDENCE,
    STAGE_PROVIDER_EXEC_CALL_REACHED,
    STAGE_PROVIDER_EXEC_REJECTED,
    STAGE_WRAPPER_ENTERED,
    append_execution_event,
    ensure_execution_events_table,
    read_execution_events,
)
from mozyo_bridge.core.state.startup_transaction_fence import (
    Participant,
    StartupTransactionFence,
    StartupUnit,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501
    RepoLocalConfig,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config_status import (  # noqa: E501
    ACTION_CONFIG_MIGRATE,
    SOURCE_COMPATIBILITY,
    SOURCE_DECLARED,
    SOURCE_DEFAULT,
    classify_config_sources,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_health import (  # noqa: E501
    StartupProbe,
    live_evidence_reader,
    probe_session_health,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_status import (  # noqa: E501
    build_startup_status,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_health import (  # noqa: E501
    HEALTH_HEALTHY,
    HEALTH_STARTUP_EVIDENCE_UNAVAILABLE,
)

WS = "wR2"
LANE = "issue_14222_r2_lane"
CLAUDE = "mzb1_wR2_claude_lane"
CODEX = "mzb1_wR2_codex_lane"


def _reserved_action(home: Path, *, participants: int = 2) -> tuple[StartupTransactionFence, str]:
    """A real reserved action with launched participants in a temp store."""
    fence = StartupTransactionFence(home=home)
    providers = ("claude", "codex")[:participants]
    action = fence.reserve(
        StartupUnit(workspace_id=WS, lane_id=LANE, providers=providers), "nonce-r2"
    )
    ensure_execution_events_table(fence, action.action_id)
    names = [(CLAUDE, f"{WS}:p1", "claude"), (CODEX, f"{WS}:p2", "codex")][:participants]
    for name, locator, role in names:
        fence.record_participant(
            action.action_id,
            Participant(role=role, assigned_name=name, locator=locator, receipt="r"),
        )
    return fence, action.action_id


class ParticipantAttributionTest(unittest.TestCase):
    """F1: the review's independent reproduction, now asserted per participant."""

    def test_sibling_reject_does_not_poison_the_live_participant(self) -> None:
        # Claude's wrapper was rejected pre-exec; Codex reached exec and is live.
        # Before the fix BOTH participants read last_stage=provider_exec_call_reached
        # (whichever row happened to be last) and join=not_applicable.
        with tempfile.TemporaryDirectory() as tmp:
            fence, action_id = _reserved_action(Path(tmp))
            append_execution_event(
                fence, action_id, STAGE_WRAPPER_ENTERED, participant=CLAUDE
            )
            append_execution_event(
                fence, action_id, STAGE_PROVIDER_EXEC_REJECTED, participant=CLAUDE
            )
            append_execution_event(
                fence, action_id, STAGE_WRAPPER_ENTERED, participant=CODEX
            )
            append_execution_event(
                fence, action_id, STAGE_PROVIDER_EXEC_CALL_REACHED, participant=CODEX
            )
            report = build_startup_status(
                action_id=action_id, fence=fence, live_locators=[f"{WS}:p2"]
            )
        by_name = {p.assigned_name: p for p in report.participants}
        self.assertEqual(by_name[CLAUDE].last_stage, STAGE_PROVIDER_EXEC_REJECTED)
        self.assertEqual(by_name[CLAUDE].inventory_join, JOIN_NOT_APPLICABLE)
        self.assertEqual(
            by_name[CODEX].last_stage, STAGE_PROVIDER_EXEC_CALL_REACHED
        )
        self.assertEqual(
            by_name[CODEX].inventory_join, JOIN_PROVIDER_LIVE_CONFIRMED
        )

    def test_mixed_success_and_vanish_stay_per_participant(self) -> None:
        # Both reached exec; only Codex is still live. Claude must read
        # post_exec_locator_absent while Codex reads live-confirmed.
        with tempfile.TemporaryDirectory() as tmp:
            fence, action_id = _reserved_action(Path(tmp))
            for name in (CLAUDE, CODEX):
                append_execution_event(
                    fence, action_id, STAGE_PROVIDER_EXEC_CALL_REACHED, participant=name
                )
            report = build_startup_status(
                action_id=action_id, fence=fence, live_locators=[f"{WS}:p2"]
            )
        by_name = {p.assigned_name: p for p in report.participants}
        self.assertEqual(
            by_name[CODEX].inventory_join, JOIN_PROVIDER_LIVE_CONFIRMED
        )
        self.assertEqual(by_name[CLAUDE].inventory_join, "post_exec_locator_absent")

    def test_legacy_unattributed_rows_are_an_honest_gap_on_a_pair(self) -> None:
        # v1 rows (no participant) on a TWO-participant action: never guessed onto
        # either participant, surfaced as the unattributed gap — not "no evidence".
        with tempfile.TemporaryDirectory() as tmp:
            fence, action_id = _reserved_action(Path(tmp))
            append_execution_event(
                fence, action_id, STAGE_PROVIDER_EXEC_CALL_REACHED, participant=""
            )
            report = build_startup_status(
                action_id=action_id, fence=fence, live_locators=[]
            )
        for participant in report.participants:
            self.assertEqual(participant.last_stage, STAGE_NO_EVIDENCE)
            self.assertTrue(participant.evidence_gap)
            self.assertEqual(
                participant.bounded_reason, REASON_STARTUP_EVIDENCE_UNATTRIBUTED
            )

    def test_legacy_rows_attribute_to_a_sole_participant(self) -> None:
        # A single-participant action's unattributed rows are unambiguous — keep them.
        with tempfile.TemporaryDirectory() as tmp:
            fence, action_id = _reserved_action(Path(tmp), participants=1)
            append_execution_event(
                fence, action_id, STAGE_PROVIDER_EXEC_CALL_REACHED, participant=""
            )
            report = build_startup_status(
                action_id=action_id, fence=fence, live_locators=[f"{WS}:p1"]
            )
        (participant,) = report.participants
        self.assertEqual(participant.last_stage, STAGE_PROVIDER_EXEC_CALL_REACHED)
        self.assertEqual(participant.inventory_join, JOIN_PROVIDER_LIVE_CONFIRMED)


class _HealthySlot:
    """A launched slot whose inventory/screen/attestation facts all read positive."""

    def __init__(self, name, locator, provider):
        self.assigned_name = name
        self.locator = locator
        self.provider = provider
        self.outcome = "launched"
        self.detail = ""


class EvidenceGateCompositionTest(unittest.TestCase):
    """F2: the gate fires through the REAL probe composition, not only the pure fn."""

    def _probe(self, home: Path, action_id: str, *, seed_evidence: bool):
        slot = _HealthySlot(CLAUDE, f"{WS}:p1", "claude")
        if seed_evidence:
            fence = StartupTransactionFence(home=home)
            append_execution_event(
                fence, action_id, STAGE_PROVIDER_EXEC_CALL_REACHED, participant=CLAUDE
            )
        rows = [{"name": CLAUDE, "pane_id": f"{WS}:p1", "agent": "claude",
                 "agent_status": "idle"}]
        attestation = mock.MagicMock()
        attestation.locator = f"{WS}:p1"
        attestation.verdict = "present"
        with mock.patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider"
            ".application.herdr_startup_health._screen_of",
            return_value=("clear", ""),
        ), mock.patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider"
            ".application.herdr_startup_health._attestation_of",
            return_value="ok",
        ):
            return probe_session_health(
                slots=[slot],
                workspace_id=WS,
                lane=LANE,
                list_rows=lambda: rows,
                read_attestation=lambda name: attestation,
                read_visible=lambda locator: "$ ready",
                attested_launch=True,
                probe=StartupProbe(polls=2, interval=0.0, sleeper=lambda _s: None),
                evidence_reader=live_evidence_reader(action_id),
            )

    def test_missing_evidence_downgrades_a_would_be_green_launch(self) -> None:
        # Everything else positive, NO attributed evidence row -> the real composition
        # must not report healthy. Pre-fix this returned HEALTH_HEALTHY because the
        # composition never consulted the projection at all.
        with tempfile.TemporaryDirectory() as tmp:
            fence, action_id = _reserved_action(Path(tmp), participants=1)
            del fence
            with mock.patch.dict(
                "os.environ", {"MOZYO_BRIDGE_HOME": tmp}, clear=False
            ):
                (slot,) = self._probe(Path(tmp), action_id, seed_evidence=False)
        self.assertEqual(slot.health, HEALTH_STARTUP_EVIDENCE_UNAVAILABLE)
        # j#84724: the evidence gap owes NO rollback — the pane is demonstrably live.
        self.assertEqual(slot.compensation, "not_needed")

    def test_attributed_evidence_keeps_the_same_launch_green(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fence, action_id = _reserved_action(Path(tmp), participants=1)
            del fence
            with mock.patch.dict(
                "os.environ", {"MOZYO_BRIDGE_HOME": tmp}, clear=False
            ):
                (slot,) = self._probe(Path(tmp), action_id, seed_evidence=True)
        self.assertEqual(slot.health, HEALTH_HEALTHY)

    def test_sibling_only_evidence_is_not_this_participants_evidence(self) -> None:
        # Rows attributed ONLY to the codex sibling must not green the claude slot.
        with tempfile.TemporaryDirectory() as tmp:
            fence, action_id = _reserved_action(Path(tmp))
            append_execution_event(
                fence, action_id, STAGE_PROVIDER_EXEC_CALL_REACHED, participant=CODEX
            )
            with mock.patch.dict(
                "os.environ", {"MOZYO_BRIDGE_HOME": tmp}, clear=False
            ):
                (slot,) = self._probe(Path(tmp), action_id, seed_evidence=False)
        self.assertEqual(slot.health, HEALTH_STARTUP_EVIDENCE_UNAVAILABLE)

    def test_no_action_id_composes_the_prior_pipeline(self) -> None:
        # An unmanaged run (no reserved action) must stay byte-invariant green.
        self.assertIsNone(live_evidence_reader(""))
        with tempfile.TemporaryDirectory() as tmp:
            fence, action_id = _reserved_action(Path(tmp), participants=1)
            del fence, action_id
            with mock.patch.dict(
                "os.environ", {"MOZYO_BRIDGE_HOME": tmp}, clear=False
            ):
                slot = _HealthySlot(CLAUDE, f"{WS}:p1", "claude")
                rows = [{"name": CLAUDE, "pane_id": f"{WS}:p1", "agent": "claude",
                         "agent_status": "idle"}]
                attestation = mock.MagicMock()
                attestation.locator = f"{WS}:p1"
                attestation.verdict = "present"
                with mock.patch(
                    "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider"
                    ".application.herdr_startup_health._screen_of",
                    return_value=("clear", ""),
                ), mock.patch(
                    "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider"
                    ".application.herdr_startup_health._attestation_of",
                    return_value="ok",
                ):
                    (probed,) = probe_session_health(
                        slots=[slot],
                        workspace_id=WS,
                        lane=LANE,
                        list_rows=lambda: rows,
                        read_attestation=lambda name: attestation,
                        read_visible=lambda locator: "$ ready",
                        attested_launch=True,
                        probe=StartupProbe(
                            polls=2, interval=0.0, sleeper=lambda _s: None
                        ),
                        evidence_reader=None,
                    )
        self.assertEqual(probed.health, HEALTH_HEALTHY)


class LeafConfigStatusTest(unittest.TestCase):
    """F3: partial block declarations and legacy compatibility, machine-readable."""

    def test_partial_presentation_block_does_not_bury_undeclared_leaves(self) -> None:
        # The review's runtime reproduction: `presentation` declares grouping rules but
        # NOT delegation_window_policy. The block reads declared; the leaf must not.
        raw = {
            "presentation": {
                "project_group_presentation": "project_group_tmux_window",
                "grouping": {"membership_rules": []},
            }
        }
        statuses = classify_config_sources(
            raw_record=raw,
            config=RepoLocalConfig.from_record(raw),
            schema_version=2,
            legacy_migratable=False,
        )
        by_key = {s.key: s for s in statuses}
        self.assertEqual(by_key["presentation"].source, SOURCE_DECLARED)
        self.assertEqual(
            by_key["presentation.grouping.project_group_presentation"].source,
            SOURCE_DECLARED,
        )
        self.assertEqual(
            by_key["presentation.grouping.delegation_window_policy"].source,
            SOURCE_DEFAULT,
        )
        self.assertEqual(
            by_key["presentation.grouping.delegation_window_policy"].effective_value,
            "shared",
        )

    def test_partially_declared_sublane_integration_separates_leaves(self) -> None:
        raw = {"sublane_integration": {"integration_branch": "main-next"}}
        statuses = classify_config_sources(
            raw_record=raw,
            config=RepoLocalConfig.from_record(raw),
            schema_version=2,
            legacy_migratable=False,
        )
        by_key = {s.key: s for s in statuses}
        self.assertEqual(
            by_key["sublane_integration.integration_branch"].source, SOURCE_DECLARED
        )
        self.assertEqual(
            by_key["sublane_integration.merge_on_retire"].source, SOURCE_DEFAULT
        )
        self.assertIs(
            by_key["sublane_integration.merge_on_retire"].effective_value, True
        )

    def test_legacy_v1_declaration_is_compatibility_with_migrate_action(self) -> None:
        raw = {"agent_launch": {"launch_argv": {"claude": {"sublane": ["--model", "m"]}}}}
        statuses = classify_config_sources(
            raw_record=raw,
            config=RepoLocalConfig.from_record(raw),
            schema_version=1,
            legacy_migratable=True,
        )
        by_key = {s.key: s for s in statuses}
        self.assertEqual(by_key["agent_launch"].source, SOURCE_COMPATIBILITY)
        self.assertEqual(by_key["agent_launch"].action, ACTION_CONFIG_MIGRATE)
        self.assertEqual(by_key["agents"].source, SOURCE_COMPATIBILITY)
        self.assertEqual(by_key["agents"].action, ACTION_CONFIG_MIGRATE)

    def test_every_row_carries_a_machine_readable_action_field(self) -> None:
        statuses = classify_config_sources(
            raw_record=None,
            config=RepoLocalConfig.from_record(None),
            schema_version=2,
            legacy_migratable=False,
        )
        for status in statuses:
            self.assertIn("action", status.as_payload())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
