"""Deterministic late-finding review escalation tests (Redmine #13967 item 3).

Pins the pure trigger (:func:`evaluate_subsystem_escalation` / :func:`project_review_escalation`)
and the CLI derivation that back ``workflow review-escalation``:

- **only late AND authority-bearing findings count**, and a round counts once;
- **the trigger is deterministic**: escalate iff distinct late-authority rounds >= threshold;
- **fail toward more review**: an unreadable subsystem history escalates (never a bypass);
- escalation is never fabricated from no qualifying findings.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_review_escalation import (
    cmd_workflow_review_escalation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_escalation import (
    DEFAULT_ESCALATION_THRESHOLD,
    MODE_FULL_SURFACE_ADVERSARIAL,
    MODE_PER_FINDING_REREVIEW,
    REASON_BELOW_THRESHOLD,
    REASON_HISTORY_UNREADABLE,
    REASON_NO_LATE_AUTHORITY_FINDING,
    REASON_REPEATED_LATE_AUTHORITY,
    SubsystemFinding,
    evaluate_subsystem_escalation,
    project_review_escalation,
)


def _f(subsystem, round_index, *, authority=True, late=True):
    return SubsystemFinding(
        subsystem=subsystem,
        round_index=round_index,
        authority_bearing=authority,
        late=late,
    )


class EvaluateTests(unittest.TestCase):
    def test_repeated_late_authority_escalates(self):
        v = evaluate_subsystem_escalation(
            "callback_supervisor", [_f("callback_supervisor", 1), _f("callback_supervisor", 2)]
        )
        self.assertTrue(v.escalate)
        self.assertEqual(v.next_round_mode, MODE_FULL_SURFACE_ADVERSARIAL)
        self.assertEqual(v.reason, REASON_REPEATED_LATE_AUTHORITY)
        self.assertEqual(v.late_authority_rounds, (1, 2))

    def test_single_round_is_below_threshold(self):
        v = evaluate_subsystem_escalation("x", [_f("x", 1)])
        self.assertFalse(v.escalate)
        self.assertEqual(v.next_round_mode, MODE_PER_FINDING_REREVIEW)
        self.assertEqual(v.reason, REASON_BELOW_THRESHOLD)

    def test_multiple_findings_same_round_count_once(self):
        v = evaluate_subsystem_escalation(
            "x", [_f("x", 1, ), _f("x", 1), _f("x", 1)]
        )
        # three findings but all in round 1 -> a single late-authority round -> below threshold
        self.assertEqual(v.late_authority_round_count, 1)
        self.assertFalse(v.escalate)

    def test_non_authority_or_non_late_do_not_count(self):
        v = evaluate_subsystem_escalation(
            "x",
            [
                _f("x", 1, authority=False),  # not authority-bearing
                _f("x", 2, late=False),  # caught in-round, not late
            ],
        )
        self.assertEqual(v.late_authority_round_count, 0)
        self.assertFalse(v.escalate)
        self.assertEqual(v.reason, REASON_NO_LATE_AUTHORITY_FINDING)

    def test_unreadable_history_escalates(self):
        v = evaluate_subsystem_escalation("x", [], history_readable=False)
        self.assertTrue(v.escalate)
        self.assertEqual(v.reason, REASON_HISTORY_UNREADABLE)
        self.assertEqual(v.next_round_mode, MODE_FULL_SURFACE_ADVERSARIAL)

    def test_threshold_is_configurable(self):
        v = evaluate_subsystem_escalation(
            "x", [_f("x", 1), _f("x", 2)], threshold=3
        )
        self.assertFalse(v.escalate)
        v3 = evaluate_subsystem_escalation(
            "x", [_f("x", 1), _f("x", 2), _f("x", 3)], threshold=3
        )
        self.assertTrue(v3.escalate)


class ProjectionTests(unittest.TestCase):
    def test_projection_groups_by_subsystem_and_orders(self):
        proj = project_review_escalation(
            [
                _f("b_sub", 1),
                _f("b_sub", 2),
                _f("a_sub", 1),
            ],
            unreadable_subsystems=["c_sub"],
        )
        subs = [v.subsystem for v in proj.verdicts]
        self.assertEqual(subs, ["a_sub", "b_sub", "c_sub"])  # ordered
        self.assertEqual(proj.escalating_subsystems, ("b_sub", "c_sub"))  # b repeated, c unreadable
        self.assertTrue(proj.any_escalation)


class CliTests(unittest.TestCase):
    def _run(self, payload_obj, **kwargs):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "hist.json"
            path.write_text(json.dumps(payload_obj), encoding="utf-8")
            args = argparse.Namespace(
                snapshot_json=str(path),
                threshold=kwargs.get("threshold"),
                unreadable_subsystem=kwargs.get("unreadable_subsystem"),
                as_json=True,
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cmd_workflow_review_escalation(args)
            self.assertEqual(rc, 0)
            return json.loads(buf.getvalue())

    def test_cli_escalates_repeated(self):
        payload = self._run(
            {
                "findings": [
                    {"subsystem": "supervisor", "round_index": 1, "authority_bearing": True, "late": True},
                    {"subsystem": "supervisor", "round_index": 2, "authority_bearing": True, "late": True},
                ]
            }
        )
        self.assertTrue(payload["any_escalation"])
        self.assertIn("supervisor", payload["escalating_subsystems"])

    def test_cli_threshold_override(self):
        payload = self._run(
            {
                "findings": [
                    {"subsystem": "s", "round_index": 1, "authority_bearing": True, "late": True},
                    {"subsystem": "s", "round_index": 2, "authority_bearing": True, "late": True},
                ]
            },
            threshold=3,
        )
        self.assertFalse(payload["any_escalation"])


if __name__ == "__main__":
    unittest.main()
