"""Project-gateway create/adopt canonical declaration tests (Redmine #13811 T2).

Covers the pure declaration function :func:`declare_project_gateway_owner_row` (dry-run
zero-write, idempotent adopt, and the fail-closed negative matrix) and the CLI use case
:class:`ProjectGatewayDeclareUseCase` (unreadable inventory / unresolved provider zero-write,
dry-run vs execute). Drives a real :class:`LaneLifecycleStore` / :class:`LaneDeclarationStore`
over a temp home and a real attestation store so the fail-closed gate is exercised end to end.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.herdr_identity_attestation import (
    VERDICT_CONFLICT,
    VERDICT_PRESENT,
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
)
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore
from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_project_gateway_declare import (  # noqa: E501
    PG_DECL_UNREADABLE,
    PG_DECL_UNRESOLVED_PROVIDER,
    ProjectGatewayDeclareRequest,
    ProjectGatewayDeclareUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.project_gateway_declaration import (  # noqa: E501
    PG_DECL_DRY_RUN,
    PG_DECL_NO_SCOPE,
    declare_project_gateway_owner_row,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E501
    ADOPT_DECL_AMBIGUOUS_LOCATORS,
    ADOPT_DECL_BAD_ANCHOR,
    ADOPT_DECL_DECLARED,
    ADOPT_DECL_DUPLICATE_CANDIDATES,
    ADOPT_DECL_INCOMPLETE_PAIR,
    ADOPT_DECL_NO_ANCHOR,
    ADOPT_DECL_OWNER_CONFLICT,
    ADOPT_DECL_STALE_SLOT,
    ADOPT_DECL_UNATTESTED,
    ADOPT_DECL_UNRESOLVED_UNIT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (  # noqa: E501
    project_gateway_lane_id,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

WS = "wProj"
SCOPE = "giken/cloud-drive/project"
ISSUE = "13581"
JOURNAL = "78405"
LANE = project_gateway_lane_id(SCOPE)
GW_NAME = encode_assigned_name(WS, "codex", LANE)
WK_NAME = encode_assigned_name(WS, "claude", LANE)
GW_LOC = "wProj:p2"
WK_LOC = "wProj:p3"


def _row(name: str, locator: str, **extra) -> dict:
    # SLOT_LIVE requires the liveness classifier to see a live pane; include the fields it
    # reads. `classify_named_slot` treats a locator-bearing row with an agent as live.
    row = {"name": name, "pane_id": locator, "agent": "codex", "status": "idle"}
    row.update(extra)
    return row


def _pair_rows(gw_loc=GW_LOC, wk_loc=WK_LOC) -> list:
    return [_row(GW_NAME, gw_loc, agent="codex"), _row(WK_NAME, wk_loc, agent="claude")]


def _seed_attestations(home, gw_loc=GW_LOC, wk_loc=WK_LOC) -> None:
    store = HerdrIdentityAttestationStore(home=home)
    store.upsert(IdentityAttestationRecord(
        assigned_name=GW_NAME, workspace_id=WS, role="codex", lane_id=LANE,
        locator=gw_loc, verdict=VERDICT_PRESENT))
    store.upsert(IdentityAttestationRecord(
        assigned_name=WK_NAME, workspace_id=WS, role="claude", lane_id=LANE,
        locator=wk_loc, verdict=VERDICT_PRESENT))


def _declare(tmp, *, rows=None, scope=SCOPE, lane_id=LANE, dry_run, seed_att=True,
             providers=("codex", "claude"), issue=ISSUE, journal=JOURNAL,
             gw_loc=GW_LOC, wk_loc=WK_LOC):
    home = Path(tmp)
    if seed_att:
        _seed_attestations(home, gw_loc=gw_loc, wk_loc=wk_loc)
    return declare_project_gateway_owner_row(
        journal=journal,
        issue=issue,
        project_scope=scope,
        lane_id=lane_id,
        workspace_id=WS,
        providers=providers,
        rows=_pair_rows() if rows is None else rows,
        dry_run=dry_run,
        store_factory=lambda: LaneDeclarationStore(home=home),
        attestation_store_factory=lambda: HerdrIdentityAttestationStore(home=home),
    )


class DeclareProjectGatewayOwnerRowTest(unittest.TestCase):
    def _row_at(self, tmp):
        return LaneLifecycleStore(home=Path(tmp)).get(LaneLifecycleKey(WS, LANE))

    # --- dry-run: zero-write ----------------------------------------------------
    def test_dry_run_plans_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            o = _declare(tmp, dry_run=True)
            self.assertEqual(o.status, PG_DECL_DRY_RUN)
            self.assertFalse(o.applied)
            self.assertTrue(o.would_declare)
            self.assertEqual(len(o.planned_slots), 2)
            self.assertIsNone(self._row_at(tmp))  # nothing written

    # --- execute: canonical project_gateway row ---------------------------------
    def test_execute_declares_canonical_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            o = _declare(tmp, dry_run=False)
            self.assertEqual(o.status, ADOPT_DECL_DECLARED)
            self.assertTrue(o.applied)
            self.assertEqual(o.revision, 1)
            rec = self._row_at(tmp)
            self.assertEqual(rec.binding_kind, "project_gateway")
            self.assertEqual(rec.project_scope, SCOPE)
            self.assertEqual(rec.issue_id, "")  # a project lane owns a scope, not an issue
            self.assertEqual(rec.lane_generation, 1)
            self.assertEqual(rec.lane_disposition, DISPOSITION_ACTIVE)
            self.assertEqual(len(rec.declared_pins), 2)
            # The decision anchor's real issue is preserved on the row.
            self.assertEqual(rec.decision_issue_id, ISSUE)
            self.assertEqual(rec.decision_journal, JOURNAL)

    def test_execute_is_idempotent_adopt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_declare(tmp, dry_run=False, seed_att=True).status, ADOPT_DECL_DECLARED)
            again = _declare(tmp, dry_run=False, seed_att=False)
            self.assertEqual(again.status, ADOPT_DECL_DECLARED)
            self.assertTrue(again.applied)
            self.assertEqual(self._row_at(tmp).revision, 1)  # no new generation minted

    def test_declared_slots_carry_canonical_roles_and_providers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _declare(tmp, dry_run=False)
            roles = {p.role for p in self._row_at(tmp).declared_pins}
            providers = {p.provider for p in self._row_at(tmp).declared_pins}
            self.assertEqual(roles, {"gateway", "worker"})
            self.assertEqual(providers, {"codex", "claude"})

    # --- fail-closed anchor / scope / unit --------------------------------------
    def test_missing_anchor_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_declare(tmp, dry_run=False, journal="").status, ADOPT_DECL_NO_ANCHOR)
            self.assertEqual(_declare(tmp, dry_run=False, issue="").status, ADOPT_DECL_NO_ANCHOR)
            self.assertIsNone(self._row_at(tmp))

    def test_missing_scope_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            o = _declare(tmp, dry_run=False, scope="")
            self.assertEqual(o.status, PG_DECL_NO_SCOPE)
            self.assertIsNone(self._row_at(tmp))

    def test_unresolved_unit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            o = declare_project_gateway_owner_row(
                journal=JOURNAL, issue=ISSUE, project_scope=SCOPE, lane_id="",
                workspace_id=WS, providers=("codex", "claude"), rows=_pair_rows(),
                dry_run=False, store_factory=lambda: LaneDeclarationStore(home=Path(tmp)),
                attestation_store_factory=lambda: HerdrIdentityAttestationStore(home=Path(tmp)))
            self.assertEqual(o.status, ADOPT_DECL_UNRESOLVED_UNIT)

    def test_malformed_anchor_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # a non-redmine-shaped journal id can make the DecisionPointer invalid
            o = _declare(tmp, dry_run=False, journal="not a journal!!")
            self.assertIn(o.status, (ADOPT_DECL_BAD_ANCHOR, ADOPT_DECL_NO_ANCHOR))
            self.assertIsNone(self._row_at(tmp))

    # --- fail-closed live-pair gate ---------------------------------------------
    def test_unattested_slot_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # No attestations seeded -> both slots unattested.
            o = _declare(tmp, dry_run=False, seed_att=False)
            self.assertEqual(o.status, ADOPT_DECL_UNATTESTED)
            self.assertIsNone(self._row_at(tmp))

    def test_conflict_attestation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            store = HerdrIdentityAttestationStore(home=home)
            store.upsert(IdentityAttestationRecord(
                assigned_name=GW_NAME, workspace_id=WS, role="codex", lane_id=LANE,
                locator=GW_LOC, verdict=VERDICT_CONFLICT))
            store.upsert(IdentityAttestationRecord(
                assigned_name=WK_NAME, workspace_id=WS, role="claude", lane_id=LANE,
                locator=WK_LOC, verdict=VERDICT_PRESENT))
            o = declare_project_gateway_owner_row(
                journal=JOURNAL, issue=ISSUE, project_scope=SCOPE, lane_id=LANE,
                workspace_id=WS, providers=("codex", "claude"), rows=_pair_rows(),
                dry_run=False, store_factory=lambda: LaneDeclarationStore(home=home),
                attestation_store_factory=lambda: HerdrIdentityAttestationStore(home=home))
            self.assertEqual(o.status, ADOPT_DECL_UNATTESTED)

    def test_duplicate_candidate_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rows = _pair_rows() + [_row(GW_NAME, "wProj:p99", agent="codex")]  # gateway name twice
            o = _declare(tmp, dry_run=False, rows=rows)
            self.assertEqual(o.status, ADOPT_DECL_DUPLICATE_CANDIDATES)
            self.assertIsNone(self._row_at(tmp))

    def test_incomplete_pair_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            o = _declare(tmp, dry_run=False, rows=[_row(GW_NAME, GW_LOC, agent="codex")])
            self.assertEqual(o.status, ADOPT_DECL_INCOMPLETE_PAIR)
            self.assertIsNone(self._row_at(tmp))

    def test_zero_live_slots_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            o = _declare(tmp, dry_run=False, rows=[])
            self.assertEqual(o.status, ADOPT_DECL_INCOMPLETE_PAIR)
            self.assertIsNone(self._row_at(tmp))

    def test_wrong_provider_rows_not_adopted(self) -> None:
        # The live rows carry a provider token that is NOT the declared pair (a foreign
        # provider under this lane): no candidate resolves for either slot -> incomplete.
        with tempfile.TemporaryDirectory() as tmp:
            foreign = [
                _row(encode_assigned_name(WS, "gemini", LANE), GW_LOC, agent="gemini"),
                _row(encode_assigned_name(WS, "mistral", LANE), WK_LOC, agent="mistral"),
            ]
            o = _declare(tmp, dry_run=False, rows=foreign)
            self.assertEqual(o.status, ADOPT_DECL_INCOMPLETE_PAIR)
            self.assertIsNone(self._row_at(tmp))

    def test_stale_slot_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # A locator-bearing gateway row whose detected-agent field is present but BLANK is
            # a positive shell-residue signal (SLOT_STALE) — never adopted.
            stale = {"name": GW_NAME, "pane_id": GW_LOC, "agent": ""}
            rows = [stale, _row(WK_NAME, WK_LOC, agent="claude")]
            o = _declare(tmp, dry_run=False, rows=rows)
            self.assertEqual(o.status, ADOPT_DECL_STALE_SLOT)
            self.assertIsNone(self._row_at(tmp))

    def test_ambiguous_locators_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Both slots resolve to the SAME locator -> ambiguous / recycled target.
            _seed_attestations(Path(tmp), gw_loc=GW_LOC, wk_loc=GW_LOC)
            rows = [_row(GW_NAME, GW_LOC, agent="codex"), _row(WK_NAME, GW_LOC, agent="claude")]
            o = declare_project_gateway_owner_row(
                journal=JOURNAL, issue=ISSUE, project_scope=SCOPE, lane_id=LANE,
                workspace_id=WS, providers=("codex", "claude"), rows=rows,
                dry_run=False, store_factory=lambda: LaneDeclarationStore(home=Path(tmp)),
                attestation_store_factory=lambda: HerdrIdentityAttestationStore(home=Path(tmp)))
            self.assertEqual(o.status, ADOPT_DECL_AMBIGUOUS_LOCATORS)
            self.assertIsNone(self._row_at(tmp))

    def test_owner_conflict_when_another_lane_owns_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _seed_attestations(home)
            # A DIFFERENT lane already owns this scope.
            LaneDeclarationStore(home=home).declare_lane(
                LaneLifecycleKey(WS, "pgwv1_other-lane"),
                decision=_import_decision(),
                binding_kind="project_gateway",
                project_scope=SCOPE,
                declared_slots=(_a_pin(),),
            )
            o = declare_project_gateway_owner_row(
                journal=JOURNAL, issue=ISSUE, project_scope=SCOPE, lane_id=LANE,
                workspace_id=WS, providers=("codex", "claude"), rows=_pair_rows(),
                dry_run=False, store_factory=lambda: LaneDeclarationStore(home=home),
                attestation_store_factory=lambda: HerdrIdentityAttestationStore(home=home))
            self.assertEqual(o.status, ADOPT_DECL_OWNER_CONFLICT)
            self.assertIsNone(self._row_at(tmp))

    def test_dry_run_zero_write_even_on_conflict_free_pair(self) -> None:
        # A clean dry-run followed by NO execute leaves the store empty.
        with tempfile.TemporaryDirectory() as tmp:
            _declare(tmp, dry_run=True)
            _declare(tmp, dry_run=True)  # repeated dry-runs never write
            self.assertIsNone(self._row_at(tmp))


def _import_decision():
    from mozyo_bridge.core.state.lane_lifecycle import DecisionPointer
    return DecisionPointer(source="redmine", issue_id=ISSUE, journal_id=JOURNAL)


def _a_pin():
    from mozyo_bridge.core.state.lane_lifecycle import ProcessGenerationPin
    return ProcessGenerationPin(
        role="gateway", provider="codex",
        assigned_name=encode_assigned_name(WS, "codex", "pgwv1_other-lane"),
        locator="wProj:p8")


class _FakeOps:
    def __init__(self, *, rows=None, readable=True, providers=("codex", "claude")):
        self._rows = _pair_rows() if rows is None else rows
        self._readable = readable
        self._providers = providers

    def workspace_id(self):
        return WS

    def read_inventory(self):
        return list(self._rows), self._readable

    def providers(self):
        return self._providers


class ProjectGatewayDeclareUseCaseTest(unittest.TestCase):
    def _request(self):
        return ProjectGatewayDeclareRequest(issue=ISSUE, journal=JOURNAL, project_scope=SCOPE)

    def _use_case(self, tmp, ops):
        home = Path(tmp)
        _seed_attestations(home)
        return ProjectGatewayDeclareUseCase(
            ops=ops,
            store_factory=lambda: LaneDeclarationStore(home=home),
            attestation_store_factory=lambda: HerdrIdentityAttestationStore(home=home),
        )

    def test_unreadable_inventory_is_zero_write(self) -> None:
        # An unreadable inventory short-circuits before any store touch.
        o = ProjectGatewayDeclareUseCase(ops=_FakeOps(readable=False)).run(
            self._request(), execute=True
        )
        self.assertEqual(o.status, PG_DECL_UNREADABLE)
        self.assertFalse(o.applied)
        self.assertFalse(o.would_declare)

    def test_unresolved_provider_is_zero_write(self) -> None:
        o = ProjectGatewayDeclareUseCase(ops=_FakeOps(providers=("", ""))).run(
            self._request(), execute=True
        )
        self.assertEqual(o.status, PG_DECL_UNRESOLVED_PROVIDER)
        self.assertFalse(o.applied)

    def test_use_case_derives_lane_id_and_dry_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            o = self._use_case(tmp, _FakeOps()).run(self._request(), execute=False)
            self.assertEqual(o.lane_id, LANE)
            self.assertEqual(o.status, PG_DECL_DRY_RUN)
            self.assertIsNone(LaneLifecycleStore(home=Path(tmp)).get(LaneLifecycleKey(WS, LANE)))

    def test_use_case_execute_declares(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            o = self._use_case(tmp, _FakeOps()).run(self._request(), execute=True)
            self.assertEqual(o.status, ADOPT_DECL_DECLARED)
            self.assertTrue(o.applied)
            rec = LaneLifecycleStore(home=Path(tmp)).get(LaneLifecycleKey(WS, LANE))
            self.assertEqual(rec.binding_kind, "project_gateway")


if __name__ == "__main__":
    unittest.main()
