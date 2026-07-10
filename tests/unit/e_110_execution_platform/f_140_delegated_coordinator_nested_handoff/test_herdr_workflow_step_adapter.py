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


def _record(**kw):
    base = dict(repo_workspace_id=WS, lane_id="issue_1", issue_id="13489", retired=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


class ResolveLaneAnchorTest(unittest.TestCase):
    """The lane-metadata issue-anchor join (F3)."""

    def _patch(self, records):
        from mozyo_bridge.core.state import lane_metadata
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            sublane_herdr_projection,
        )

        return [
            patch.object(sublane_herdr_projection, "repo_scope_workspace_id", return_value=WS),
            patch.object(lane_metadata, "load_lane_records", return_value=records),
        ]

    def _run(self, records):
        patches = self._patch(records)
        for p in patches:
            p.start()
        try:
            return adapter._resolve_lane_anchor(Path("/repo"), "issue_1")
        finally:
            for p in patches:
                p.stop()

    def test_single_record_verifies_issue_anchor(self):
        status, ptr = self._run({"t1": _record()})
        self.assertEqual(status, ANCHOR_VERIFIED)
        self.assertEqual(ptr, PTR)

    def test_two_distinct_issues_is_ambiguous(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_herdr import (
            ANCHOR_AMBIGUOUS,
        )

        status, _ = self._run({"t1": _record(issue_id="13489"), "t2": _record(issue_id="13490")})
        self.assertEqual(status, ANCHOR_AMBIGUOUS)

    def test_only_retired_is_retired(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_herdr import (
            ANCHOR_RETIRED,
        )

        status, _ = self._run({"t1": _record(retired=True)})
        self.assertEqual(status, ANCHOR_RETIRED)

    def test_no_matching_record_is_missing(self):
        status, _ = self._run({"t1": _record(lane_id="other_lane")})
        self.assertEqual(status, ANCHOR_MISSING)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
