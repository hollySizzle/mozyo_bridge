"""herdr-native `workflow step` application adapter tests (Redmine #13489).

Hermetic: the terminal-runtime seams (repo root, sender identity, lane-metadata anchor, live
inventory) are patched so no test depends on a repo-local config, the workspace registry, or a
live herdr binary. Pins the mid-review corrections (j#74748 / j#74749 / j#74750): the adapter
verifies the Redmine issue anchor from the lane metadata store (F3), reads the worker liveness
only when the gateway lane reaches the worker gate, folds the inventory into a 0 / 1 / 2+
cardinality (F2/D), and no longer consults registry project_name (F1).
"""

from __future__ import annotations

import argparse
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    herdr_workflow_step as adapter,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_herdr import (
    ANCHOR_MISSING,
    ANCHOR_VERIFIED,
    REASON_HERDR_ANCHOR_UNRESOLVED,
    REASON_HERDR_DEFAULT_COORDINATOR_UNRESOLVED,
    REASON_HERDR_SENDER_IDENTITY_UNRESOLVED,
    REASON_HERDR_WORKER_AMBIGUOUS,
    REASON_HERDR_WORKER_DISPATCH_READY,
    REASON_HERDR_WORKER_STEP_READY,
    WORKER_ABSENT,
    WORKER_AMBIGUOUS,
    WORKER_LIVE,
    WORKER_LOCATOR_MISSING,
    WORKER_UNAVAILABLE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain import (
    herdr_target_resolution as htr,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_LOCATOR,
    AGENT_KEY_NAME,
    encode_assigned_name,
)

WS = "e1487dcb1f2d4412b28e825fdeccf9e8"
PTR = "redmine:issue=13489"


def _sender_ok(role, lane):
    return htr.SenderIdentityResolution.success(
        htr.SenderIdentity(workspace_id=WS, role=role, lane_id=lane)
    )


class ResolveHerdrStepOutcomeTest(unittest.TestCase):
    def setUp(self):
        from mozyo_bridge.application import commands_common

        self._patches = [
            patch.object(commands_common, "repo_root_from_args", return_value=Path("/repo")),
            patch.object(adapter, "_anchor_workspace_id", return_value=WS),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _run(self):
        return adapter.resolve_herdr_step_outcome(argparse.Namespace(repo=None))

    def test_missing_env_fails_closed(self):
        with patch.object(
            htr,
            "resolve_sender_identity",
            return_value=htr.SenderIdentityResolution.failure(
                htr.REASON_MISSING_SENDER_ENV, "unset"
            ),
        ):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_SENDER_IDENTITY_UNRESOLVED)
        self.assertEqual(out.execution, "blocked")

    def test_default_lane_blocks_without_anchor_or_inventory_read(self):
        with patch.object(
            htr, "resolve_sender_identity", return_value=_sender_ok("codex", "default")
        ), patch.object(
            adapter, "_resolve_lane_anchor", side_effect=AssertionError("anchor read for default")
        ), patch.object(
            adapter, "_same_lane_worker_liveness", side_effect=AssertionError("inventory read")
        ):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_DEFAULT_COORDINATOR_UNRESOLVED)

    def test_worker_verified_anchor_resolves_without_inventory_read(self):
        with patch.object(
            htr, "resolve_sender_identity", return_value=_sender_ok("claude", "issue_1")
        ), patch.object(
            adapter, "_resolve_lane_anchor", return_value=(ANCHOR_VERIFIED, PTR)
        ), patch.object(
            adapter, "_same_lane_worker_liveness", side_effect=AssertionError("inventory for worker")
        ):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_WORKER_STEP_READY)
        self.assertEqual(out.durable_anchor, PTR)

    def test_worker_missing_anchor_fails_closed(self):
        with patch.object(
            htr, "resolve_sender_identity", return_value=_sender_ok("claude", "issue_1")
        ), patch.object(adapter, "_resolve_lane_anchor", return_value=(ANCHOR_MISSING, "")):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_ANCHOR_UNRESOLVED)

    def test_gateway_verified_anchor_reads_worker_liveness(self):
        seen = {}

        def _liveness(ws, lane, *, env):
            seen["args"] = (ws, lane)
            return WORKER_LIVE

        with patch.object(
            htr, "resolve_sender_identity", return_value=_sender_ok("codex", "issue_1")
        ), patch.object(
            adapter, "_resolve_lane_anchor", return_value=(ANCHOR_VERIFIED, PTR)
        ), patch.object(adapter, "_same_lane_worker_liveness", side_effect=_liveness):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_WORKER_DISPATCH_READY)
        self.assertEqual(seen["args"], (WS, "issue_1"))

    def test_gateway_duplicate_worker_is_ambiguous(self):
        with patch.object(
            htr, "resolve_sender_identity", return_value=_sender_ok("codex", "issue_1")
        ), patch.object(
            adapter, "_resolve_lane_anchor", return_value=(ANCHOR_VERIFIED, PTR)
        ), patch.object(adapter, "_same_lane_worker_liveness", return_value=WORKER_AMBIGUOUS):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_WORKER_AMBIGUOUS)

    def test_gateway_missing_anchor_skips_inventory(self):
        with patch.object(
            htr, "resolve_sender_identity", return_value=_sender_ok("codex", "issue_1")
        ), patch.object(
            adapter, "_resolve_lane_anchor", return_value=(ANCHOR_MISSING, "")
        ), patch.object(
            adapter, "_same_lane_worker_liveness", side_effect=AssertionError("inventory read")
        ):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_ANCHOR_UNRESOLVED)


class SameLaneWorkerLivenessTest(unittest.TestCase):
    """The inventory fold cardinality (real assigned-name decode)."""

    def _rows(self, *specs):
        rows = []
        for role, lane, loc in specs:
            row = {AGENT_KEY_NAME: encode_assigned_name(WS, role, lane)}
            if loc:
                row[AGENT_KEY_LOCATOR] = loc
            rows.append(row)
        return rows

    def _patch_rows(self, rows=None, error=None):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            sublane_herdr_projection,
        )

        if error is not None:
            return patch.object(
                sublane_herdr_projection, "list_herdr_agent_rows", side_effect=error
            )
        return patch.object(
            sublane_herdr_projection, "list_herdr_agent_rows", return_value=rows
        )

    def test_single_worker_with_locator_is_live(self):
        with self._patch_rows(self._rows(("claude", "issue_1", "p1"), ("codex", "issue_1", "p2"))):
            self.assertEqual(adapter._same_lane_worker_liveness(WS, "issue_1", env={}), WORKER_LIVE)

    def test_no_worker_is_absent(self):
        with self._patch_rows(self._rows(("claude", "other", "p1"), ("codex", "issue_1", "p2"))):
            self.assertEqual(
                adapter._same_lane_worker_liveness(WS, "issue_1", env={}), WORKER_ABSENT
            )

    def test_duplicate_workers_is_ambiguous(self):
        with self._patch_rows(self._rows(("claude", "issue_1", "p1"), ("claude", "issue_1", "p9"))):
            self.assertEqual(
                adapter._same_lane_worker_liveness(WS, "issue_1", env={}), WORKER_AMBIGUOUS
            )

    def test_single_worker_without_locator_is_locator_missing(self):
        with self._patch_rows(self._rows(("claude", "issue_1", ""))):
            self.assertEqual(
                adapter._same_lane_worker_liveness(WS, "issue_1", env={}), WORKER_LOCATOR_MISSING
            )

    def test_inventory_error_is_unavailable(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
            HerdrSessionStartError,
        )

        with self._patch_rows(error=HerdrSessionStartError("down")):
            self.assertEqual(
                adapter._same_lane_worker_liveness(WS, "issue_1", env={}), WORKER_UNAVAILABLE
            )


from mozyo_bridge.core.state.workflow_runtime_store import (
    WorkflowEventRow,
    WorkflowRouteRow,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_herdr import (
    ANCHOR_AMBIGUOUS,
    ANCHOR_MISMATCH,
    ANCHOR_RETIRED,
)

VERIFIED_PTR = "redmine:issue=13489:journal=74766"


def _route(issue="13489", ws=WS, lane="issue_1"):
    return WorkflowRouteRow(
        route_id="r", issue=issue, workspace_id=ws, lane_id=lane, role="codex",
        pane_name="p", last_seen_pane_id="", observed_at="t",
    )


def _event(event_id, issue="13489"):
    return WorkflowEventRow(
        event_id=event_id, issue=issue, gate="review_request", review_conclusion="",
        callback_state="", commit_bearing=False, integration_recorded=False,
        issue_open=True, blocker_recorded=False,
    )


def _fake_store(routes=(), events=(), exists=True):
    return types.SimpleNamespace(
        path=types.SimpleNamespace(exists=lambda: exists),
        read_route_identities=lambda: tuple(routes),
        read_events=lambda: tuple(events),
    )


class ResolveLaneAnchorTest(unittest.TestCase):
    """The durable-workflow-gate issue+journal verification (F3, store-authoritative)."""

    def _run(self, *, store, candidate=(frozenset(), False)):
        with patch.object(adapter, "_load_workflow_store", return_value=store), patch.object(
            adapter, "_lane_metadata_candidate_issues", return_value=candidate
        ):
            return adapter._resolve_lane_anchor(
                argparse.Namespace(store_path=None), WS, Path("/repo"), "issue_1"
            )

    def test_verified_issue_plus_journal_from_store(self):
        status, ptr = self._run(
            store=_fake_store(routes=[_route()], events=[_event("13489:74766")])
        )
        self.assertEqual(status, ANCHOR_VERIFIED)
        self.assertEqual(ptr, VERIFIED_PTR)  # issue + journal, not issue-only

    def test_display_record_only_without_store_route_is_missing(self):
        # THE R1 regression: a display lane-metadata candidate alone is NOT proof.
        status, _ = self._run(
            store=_fake_store(routes=[], events=[]), candidate=({"13489"}, False)
        )
        self.assertEqual(status, ANCHOR_MISSING)

    def test_absent_store_fails_closed(self):
        status, _ = self._run(store=_fake_store(exists=False))
        self.assertEqual(status, ANCHOR_MISSING)

    def test_unavailable_store_fails_closed(self):
        status, _ = self._run(store=None)
        self.assertEqual(status, ANCHOR_MISSING)

    def test_unreadable_store_fails_closed(self):
        def _boom():
            raise RuntimeError("db error")

        store = types.SimpleNamespace(
            path=types.SimpleNamespace(exists=lambda: True),
            read_route_identities=_boom,
            read_events=lambda: (),
        )
        status, _ = self._run(store=store)
        self.assertEqual(status, ANCHOR_MISSING)

    def test_two_distinct_route_issues_is_ambiguous(self):
        status, _ = self._run(
            store=_fake_store(
                routes=[_route(issue="13489"), _route(issue="13490")],
                events=[_event("13489:1"), _event("13490:2", issue="13490")],
            )
        )
        self.assertEqual(status, ANCHOR_AMBIGUOUS)

    def test_duplicate_route_same_issue_is_verified_not_ambiguous(self):
        # Gateway + worker route rows share the lane's issue — one distinct issue, not drift.
        status, ptr = self._run(
            store=_fake_store(
                routes=[_route(issue="13489"), _route(issue="13489")],
                events=[_event("13489:74766")],
            )
        )
        self.assertEqual(status, ANCHOR_VERIFIED)
        self.assertEqual(ptr, VERIFIED_PTR)

    def test_no_gate_journal_is_missing(self):
        status, _ = self._run(store=_fake_store(routes=[_route()], events=[]))
        self.assertEqual(status, ANCHOR_MISSING)

    def test_latest_gate_event_journal_wins(self):
        status, ptr = self._run(
            store=_fake_store(
                routes=[_route()], events=[_event("13489:100"), _event("13489:200")]
            )
        )
        self.assertEqual(ptr, "redmine:issue=13489:journal=200")

    def test_candidate_mismatch_fails_closed(self):
        status, _ = self._run(
            store=_fake_store(routes=[_route()], events=[_event("13489:74766")]),
            candidate=({"99999"}, False),
        )
        self.assertEqual(status, ANCHOR_MISMATCH)

    def test_all_retired_candidate_fails_closed(self):
        status, _ = self._run(
            store=_fake_store(routes=[_route()], events=[_event("13489:74766")]),
            candidate=(set(), True),
        )
        self.assertEqual(status, ANCHOR_RETIRED)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
