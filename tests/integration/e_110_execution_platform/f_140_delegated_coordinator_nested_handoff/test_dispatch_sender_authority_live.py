"""Redmine #15706 — delegated-gateway sender authority over the REAL durable stores.

The unit file (tests/unit/.../test_dispatch_sender_authority.py) pins the decision
through injected facts; this file pins the LIVE composition the decision reads —
a registered temp workspace, the real lane lifecycle store (schema v12), and the
real herdr identity attestation store (schema v4 join) — so "verified against
durable records, never caller env" is proven against the stores themselves:

- the admitted L2 flow: an active ``delegated_coordinator`` lifecycle row + a
  generation-matched startup self-attestation for the lane's gateway slot admit a
  child-implementation create, and the verdict's ``parent_lane_id`` is the verified
  lane;
- caller env alone admits nothing: the same env with no lifecycle row, or with the
  row but no attestation record, refuses typed;
- the child's lifecycle declaration records the verified parent binding
  (``parent_lane_id``, schema v12) and every pre-#15706 declaration stays ``''``;
- the direct-predecessor v11 -> v12 migration lands existing rows on ``''`` (the
  additive default), mirroring the v5 -> v6 direct-predecessor pin (review j#79379).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    VERDICT_PRESENT,
    IdentityAttestationRecord,
    record_identity_attestation,
)
from mozyo_bridge.core.state.lane_kind import (  # noqa: E402
    LANE_KIND_DELEGATED_COORDINATOR,
    LANE_KIND_IMPLEMENTATION,
)
from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore  # noqa: E402
from mozyo_bridge.core.state.lane_lifecycle_model import (  # noqa: E402
    DecisionPointer,
    LaneLifecycleKey,
)
from mozyo_bridge.core.state.lane_lifecycle_schema import (  # noqa: E402
    LANE_LIFECYCLE_COMPONENT,
    LANE_LIFECYCLE_SCHEMA_VERSION,
    lane_lifecycle_path,
)
from mozyo_bridge.core.state.workspace_registry import register_workspace  # noqa: E402
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_preflight import (  # noqa: E402,E501
    SENDER_GATEWAY_UNATTESTED,
    SENDER_KIND_DELEGATED_GATEWAY,
    SENDER_LANE_UNESTABLISHED,
    evaluate_dispatch_sender_authority,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_create_lifecycle_declaration import (  # noqa: E402,E501
    declare_created_lane_lifecycle,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    AGENT_KEY_LOCATOR,
    AGENT_KEY_NAME,
    AGENT_KEY_TERMINAL_ID,
    encode_assigned_name,
)

L2_LANE = "issue_15693_l2_trial"
CHILD_LANE = "issue_15703_l3_child"


def _decision(issue: str) -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=issue, journal_id="108004")


class _Fixture:
    """A registered temp workspace + real stores, torn down with the temp dir."""

    def __init__(self, tmp: str) -> None:
        self.home = Path(tmp) / "home"
        self.home.mkdir()
        self.repo = Path(tmp) / "repo"
        self.repo.mkdir()
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False):
            self.ws = register_workspace(self.repo, home=self.home).record.workspace_id

    def env(self, role: str = "codex", lane: str = L2_LANE) -> dict:
        return {
            "MOZYO_WORKSPACE_ID": self.ws,
            "MOZYO_AGENT_ROLE": role,
            "MOZYO_LANE_ID": lane,
        }

    def declare_l2(self) -> None:
        LaneLifecycleStore(home=self.home).declare_active(
            LaneLifecycleKey(self.ws, L2_LANE),
            decision=_decision("15693"),
            issue_id="15693",
            worktree_identity="wt_l2trial01",
            lane_kind=LANE_KIND_DELEGATED_COORDINATOR,
        )

    def attest_gateway(self, *, locator: str = "w9:p1", terminal: str = "t-l2gw") -> str:
        name = encode_assigned_name(self.ws, "codex", L2_LANE)
        record = record_identity_attestation(
            IdentityAttestationRecord(
                assigned_name=name,
                workspace_id=self.ws,
                role="codex",
                lane_id=L2_LANE,
                locator=locator,
                verdict=VERDICT_PRESENT,
                terminal_id=terminal,
            ),
            home=self.home,
        )
        assert record is not None
        return name

    def rows(self, *, locator: str = "w9:p1", terminal: str = "t-l2gw") -> list:
        return [
            {
                AGENT_KEY_NAME: encode_assigned_name(self.ws, "codex", L2_LANE),
                AGENT_KEY_LOCATOR: locator,
                AGENT_KEY_TERMINAL_ID: terminal,
            },
            {
                AGENT_KEY_NAME: encode_assigned_name(self.ws, "claude", L2_LANE),
                AGENT_KEY_LOCATOR: "w9:p2",
                AGENT_KEY_TERMINAL_ID: "t-l2wk",
            },
        ]

    def decide(self, *, env=None, rows=None):
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False):
            return evaluate_dispatch_sender_authority(
                env if env is not None else self.env(),
                self.repo,
                requested_lane_kind=LANE_KIND_IMPLEMENTATION,
                agent_rows_reader=lambda: (rows if rows is not None else self.rows()),
                inventory_workspace_resolver=lambda: self.ws,
            )


class LiveDelegatedGatewayAuthorityTest(unittest.TestCase):
    """The admitted flow and its env-is-not-authority negatives, over real stores."""

    def test_attested_l2_gateway_admits_and_carries_the_verified_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _Fixture(tmp)
            fx.declare_l2()
            fx.attest_gateway()
            verdict = fx.decide()
        self.assertTrue(verdict.ok, verdict.detail)
        self.assertEqual(verdict.sender_kind, SENDER_KIND_DELEGATED_GATEWAY)
        self.assertEqual(verdict.parent_lane_id, L2_LANE)

    def test_env_claim_without_a_lifecycle_row_refuses_typed(self) -> None:
        # The caller env asserts the L2 lane, but no durable row exists: env is never
        # authority (design constraint 1).
        with tempfile.TemporaryDirectory() as tmp:
            fx = _Fixture(tmp)
            fx.attest_gateway()
            verdict = fx.decide()
        self.assertFalse(verdict.ok)
        self.assertIn(SENDER_LANE_UNESTABLISHED, verdict.detail)

    def test_lifecycle_row_without_a_startup_attestation_refuses_typed(self) -> None:
        # The lane exists durably, but no launch-time self-attestation joins the live
        # slot: the store's ATTEST_ABSENT fails the exactly-one-attested policy closed.
        with tempfile.TemporaryDirectory() as tmp:
            fx = _Fixture(tmp)
            fx.declare_l2()
            verdict = fx.decide()
        self.assertFalse(verdict.ok)
        self.assertIn(SENDER_GATEWAY_UNATTESTED, verdict.detail)

    def test_stale_generation_attestation_refuses_typed(self) -> None:
        # A recorded attestation whose terminal identity no longer matches the live
        # slot is a different process generation — never re-used (v4 join).
        with tempfile.TemporaryDirectory() as tmp:
            fx = _Fixture(tmp)
            fx.declare_l2()
            fx.attest_gateway(terminal="t-old-generation")
            verdict = fx.decide(rows=fx.rows(terminal="t-cold-restore"))
        self.assertFalse(verdict.ok)
        self.assertIn(SENDER_GATEWAY_UNATTESTED, verdict.detail)


class ChildParentBindingDeclarationTest(unittest.TestCase):
    """Design constraint 3 (durable half): the child row records the verified parent."""

    def test_child_declaration_records_the_verified_parent_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _Fixture(tmp)
            with patch.dict(
                os.environ, {"MOZYO_BRIDGE_HOME": str(fx.home)}, clear=False
            ):
                declare_created_lane_lifecycle(
                    repo_workspace_id=fx.ws,
                    lane_label=CHILD_LANE,
                    issue="15703",
                    journal="107980",
                    worktree_identity="wt_l3child01",
                    lane_kind=LANE_KIND_IMPLEMENTATION,
                    parent_lane_id=L2_LANE,
                )
                record = LaneLifecycleStore(home=fx.home).get(
                    LaneLifecycleKey(fx.ws, CHILD_LANE)
                )
        self.assertIsNotNone(record)
        self.assertEqual(record.parent_lane_id, L2_LANE)
        self.assertEqual(record.lane_kind, LANE_KIND_IMPLEMENTATION)

    def test_pre_15706_declaration_stays_parentless(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _Fixture(tmp)
            with patch.dict(
                os.environ, {"MOZYO_BRIDGE_HOME": str(fx.home)}, clear=False
            ):
                declare_created_lane_lifecycle(
                    repo_workspace_id=fx.ws,
                    lane_label="issue_13331_x",
                    issue="13331",
                    journal="70250",
                    worktree_identity="wt_plain01",
                )
                record = LaneLifecycleStore(home=fx.home).get(
                    LaneLifecycleKey(fx.ws, "issue_13331_x")
                )
        self.assertIsNotNone(record)
        self.assertEqual(record.parent_lane_id, "")


class DirectPredecessorMigrationTest(unittest.TestCase):
    """v11 -> v12 migrates additively; existing rows land on '' (never a guess)."""

    def test_v11_store_migrates_and_reads_empty_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _Fixture(tmp)
            store = LaneLifecycleStore(home=fx.home)
            store.declare_active(
                LaneLifecycleKey(fx.ws, L2_LANE),
                decision=_decision("15693"),
                issue_id="15693",
                worktree_identity="wt_l2trial01",
                lane_kind=LANE_KIND_DELEGATED_COORDINATOR,
            )
            path = lane_lifecycle_path(fx.home)
            conn = sqlite3.connect(path)
            try:
                # A faithful direct-predecessor rewind: ONLY the v12 column is absent.
                conn.execute(
                    "ALTER TABLE lane_lifecycle_records DROP COLUMN parent_lane_id"
                )
                conn.execute(
                    "UPDATE state_schema_components SET schema_version = 11 "
                    "WHERE component = ?",
                    (LANE_LIFECYCLE_COMPONENT,),
                )
                conn.commit()
            finally:
                conn.close()
            store.ensure_schema()
            conn = sqlite3.connect(path)
            try:
                recorded = conn.execute(
                    "SELECT schema_version FROM state_schema_components "
                    "WHERE component = ?",
                    (LANE_LIFECYCLE_COMPONENT,),
                ).fetchone()[0]
            finally:
                conn.close()
            record = store.get(LaneLifecycleKey(fx.ws, L2_LANE))
        self.assertEqual(recorded, LANE_LIFECYCLE_SCHEMA_VERSION)
        self.assertIsNotNone(record)
        self.assertEqual(record.parent_lane_id, "")
        self.assertEqual(record.lane_kind, LANE_KIND_DELEGATED_COORDINATOR)


if __name__ == "__main__":
    unittest.main()
