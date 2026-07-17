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
        # Late authority findings in rounds 2 and 3 (round 1 can never be late).
        v = evaluate_subsystem_escalation(
            "callback_supervisor", [_f("callback_supervisor", 2), _f("callback_supervisor", 3)]
        )
        self.assertTrue(v.escalate)
        self.assertEqual(v.next_round_mode, MODE_FULL_SURFACE_ADVERSARIAL)
        self.assertEqual(v.reason, REASON_REPEATED_LATE_AUTHORITY)
        self.assertEqual(v.late_authority_rounds, (2, 3))

    def test_round_1_is_never_late(self):
        # Redmine #13967 F4: a finding in the first round can never be "late" even if the
        # caller flags it late — nothing preceded it.
        v = evaluate_subsystem_escalation("x", [_f("x", 1)])
        self.assertEqual(v.late_authority_round_count, 0)
        self.assertFalse(v.escalate)
        self.assertEqual(v.reason, REASON_NO_LATE_AUTHORITY_FINDING)

    def test_single_late_round_is_below_threshold(self):
        v = evaluate_subsystem_escalation("x", [_f("x", 2)])
        self.assertFalse(v.escalate)
        self.assertEqual(v.next_round_mode, MODE_PER_FINDING_REREVIEW)
        self.assertEqual(v.reason, REASON_BELOW_THRESHOLD)

    def test_multiple_findings_same_round_count_once(self):
        v = evaluate_subsystem_escalation(
            "x", [_f("x", 2), _f("x", 2), _f("x", 2)]
        )
        # three findings but all in round 2 -> a single late-authority round -> below threshold
        self.assertEqual(v.late_authority_round_count, 1)
        self.assertFalse(v.escalate)

    def test_non_authority_or_non_late_do_not_count(self):
        v = evaluate_subsystem_escalation(
            "x",
            [
                _f("x", 2, authority=False),  # not authority-bearing
                _f("x", 3, late=False),  # caught in-round, not late
            ],
        )
        self.assertEqual(v.late_authority_round_count, 0)
        self.assertFalse(v.escalate)
        self.assertEqual(v.reason, REASON_NO_LATE_AUTHORITY_FINDING)

    def test_domain_enforces_exact_bool(self):
        # R3-F3: a domain-direct caller passing truthy non-bool strings must not count —
        # counts_toward_escalation requires `is True`, not truthiness.
        v = evaluate_subsystem_escalation(
            "s",
            [SubsystemFinding(subsystem="s", round_index=2, authority_bearing="true", late="true")],
        )
        self.assertEqual(v.late_authority_round_count, 0)
        self.assertFalse(v.escalate)

    def test_unreadable_history_escalates(self):
        v = evaluate_subsystem_escalation("x", [], history_readable=False)
        self.assertTrue(v.escalate)
        self.assertEqual(v.reason, REASON_HISTORY_UNREADABLE)
        self.assertEqual(v.next_round_mode, MODE_FULL_SURFACE_ADVERSARIAL)

    def test_threshold_is_configurable(self):
        v = evaluate_subsystem_escalation(
            "x", [_f("x", 2), _f("x", 3)], threshold=3
        )
        self.assertFalse(v.escalate)
        v3 = evaluate_subsystem_escalation(
            "x", [_f("x", 2), _f("x", 3), _f("x", 4)], threshold=3
        )
        self.assertTrue(v3.escalate)


class ProjectionTests(unittest.TestCase):
    def test_projection_groups_by_subsystem_and_orders(self):
        proj = project_review_escalation(
            [
                _f("b_sub", 2),
                _f("b_sub", 3),
                _f("a_sub", 2),
            ],
            unreadable_subsystems=["c_sub"],
        )
        subs = [v.subsystem for v in proj.verdicts]
        self.assertEqual(subs, ["a_sub", "b_sub", "c_sub"])  # ordered
        self.assertEqual(proj.escalating_subsystems, ("b_sub", "c_sub"))  # b repeated, c unreadable
        self.assertTrue(proj.any_escalation)


class CliTests(unittest.TestCase):
    def _run(self, payload_obj, *, add_provenance=True, **kwargs):
        if add_provenance and isinstance(payload_obj, dict) and "provenance" not in payload_obj:
            payload_obj = {**payload_obj, "provenance": "redmine:13967:j#test"}
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
                    {"subsystem": "supervisor", "round_index": 2, "authority_bearing": True, "late": True},
                    {"subsystem": "supervisor", "round_index": 3, "authority_bearing": True, "late": True},
                ]
            }
        )
        self.assertEqual(payload["escalation_decision"], "escalate")
        self.assertTrue(payload["evaluated"])
        self.assertIn("supervisor", payload["escalating_subsystems"])

    def test_cli_threshold_override(self):
        payload = self._run(
            {
                "findings": [
                    {"subsystem": "s", "round_index": 2, "authority_bearing": True, "late": True},
                    {"subsystem": "s", "round_index": 3, "authority_bearing": True, "late": True},
                ]
            },
            threshold=3,
        )
        self.assertEqual(payload["escalation_decision"], "no_escalation")

    def test_cli_malformed_bool_fails_closed_to_escalation(self):
        # Redmine #13967 R2-F3: a JSON string "false" (which bool(...) would coerce to True)
        # is not an exact bool -> the entry is malformed -> its subsystem fails closed to
        # escalation, never silently dropped.
        payload = self._run(
            {
                "findings": [
                    {"subsystem": "sup", "round_index": 2, "authority_bearing": "false", "late": True},
                ]
            }
        )
        self.assertEqual(payload["escalation_decision"], "escalate")
        self.assertIn("sup", payload["escalating_subsystems"])

    def test_cli_missing_bool_is_malformed_not_default_false(self):
        # Redmine #13967 R2-F3: a finding missing `late` is not a valid `false` — it is
        # malformed and its subsystem fails closed to escalation.
        payload = self._run(
            {
                "findings": [
                    {"subsystem": "sup", "round_index": 2, "authority_bearing": True},
                ]
            }
        )
        self.assertEqual(payload["escalation_decision"], "escalate")
        self.assertIn("sup", payload["escalating_subsystems"])

    def test_cli_no_provenance_is_indeterminate(self):
        # Redmine #13967 R2-F3: without a declared provenance the authority verdict is
        # indeterminate, never a confident no_escalation.
        payload = self._run(
            {
                "findings": [
                    {"subsystem": "s", "round_index": 2, "authority_bearing": True, "late": False},
                ]
            },
            add_provenance=False,
        )
        self.assertEqual(payload["escalation_decision"], "indeterminate")
        self.assertFalse(payload["evaluated"])
        self.assertIn("no_or_invalid_provenance", payload["indeterminate_reasons"])
        # R3-F3a: any_escalation must fail closed to True when indeterminate.
        self.assertTrue(payload["any_escalation"])

    def test_cli_no_history_is_indeterminate_not_no_escalation(self):
        args = argparse.Namespace(
            snapshot_json=None, threshold=None, unreadable_subsystem=None, as_json=True
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_workflow_review_escalation(args)
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload["history_provided"])
        self.assertFalse(payload["evaluated"])
        self.assertEqual(payload["escalation_decision"], "indeterminate")
        # R3-F3a: a legacy consumer keying on any_escalation must NOT read a confident False.
        self.assertTrue(payload["any_escalation"])

    def test_cli_freeform_and_nonstring_provenance_are_indeterminate(self):
        # R3-F3b: a free-form ("x") or non-string (["x"]) provenance is not a verified
        # durable anchor -> indeterminate (never no_escalation).
        for prov in ("x", ["x"], 5):
            payload = self._run(
                {
                    "provenance": prov,
                    "findings": [
                        {"subsystem": "s", "round_index": 2, "authority_bearing": True, "late": False},
                    ],
                },
                add_provenance=False,
            )
            self.assertEqual(payload["escalation_decision"], "indeterminate", prov)

    def test_cli_round_index_zero_is_malformed(self):
        # R3-F3c: round_index must be >= 1 (1-based); 0 is malformed -> subsystem escalates.
        payload = self._run(
            {
                "findings": [
                    {"subsystem": "s", "round_index": 0, "authority_bearing": True, "late": True},
                ]
            }
        )
        self.assertEqual(payload["escalation_decision"], "escalate")
        self.assertIn("s", payload["escalating_subsystems"])

    def test_cli_string_threshold_is_indeterminate(self):
        # R3-F3c: a snapshot threshold that is not an exact int is not coerced.
        payload = self._run(
            {
                "threshold": "100",
                "findings": [
                    {"subsystem": "s", "round_index": 2, "authority_bearing": True, "late": True},
                ],
            }
        )
        self.assertEqual(payload["escalation_decision"], "indeterminate")

    def test_cli_non_list_unreadable_subsystems_does_not_crash(self):
        # R3-F3d: a non-list unreadable_subsystems returns a safe indeterminate envelope,
        # not a TypeError crash.
        payload = self._run({"unreadable_subsystems": 1, "findings": []})
        self.assertEqual(payload["escalation_decision"], "indeterminate")
        self.assertIn("unreadable_subsystems_not_a_list", payload["indeterminate_reasons"])

    def test_cli_bare_colon_digit_provenance_is_indeterminate(self):
        # Redmine #13967 R4-F3: a free-form "x:1" is not a recognized durable anchor.
        payload = self._run(
            {
                "provenance": "x:1",
                "findings": [
                    {"subsystem": "s", "round_index": 2, "authority_bearing": True, "late": False},
                ],
            },
            add_provenance=False,
        )
        self.assertEqual(payload["escalation_decision"], "indeterminate")

    def test_cli_structured_subsystem_is_unattributable_indeterminate(self):
        # Redmine #13967 R4-F3: a non-string subsystem is not str-coerced into an invented
        # name; it is unattributable -> indeterminate (cannot split rounds to evade threshold).
        payload = self._run(
            {
                "provenance": "redmine:13967:j#81330",
                "findings": [
                    {"subsystem": {"x": 1}, "round_index": 2, "authority_bearing": True, "late": True},
                ],
            },
            add_provenance=False,
        )
        self.assertEqual(payload["escalation_decision"], "indeterminate")

    def test_cli_non_list_findings_is_indeterminate(self):
        payload = self._run({"provenance": "redmine:1:j#1", "findings": 1}, add_provenance=False)
        self.assertEqual(payload["escalation_decision"], "indeterminate")

    def test_cli_malformed_unreadable_member_is_indeterminate(self):
        # Redmine #13967 R5-F2: an empty / whitespace / non-string unreadable_subsystems
        # member is an explicit unreadable marker we cannot attribute -> indeterminate. It
        # is never silently dropped nor str-coerced (a null must not become subsystem "None").
        for member in ("", "   ", None, 5, {"x": 1}):
            payload = self._run(
                {"provenance": "redmine:13967:j#81338", "findings": [], "unreadable_subsystems": [member]},
                add_provenance=False,
            )
            self.assertEqual(payload["escalation_decision"], "indeterminate", member)

    def test_cli_valid_unreadable_member_escalates_that_subsystem(self):
        payload = self._run(
            {"provenance": "redmine:13967:j#81338", "findings": [], "unreadable_subsystems": ["supervisor"]},
            add_provenance=False,
        )
        self.assertEqual(payload["escalation_decision"], "escalate")
        self.assertIn("supervisor", payload["escalating_subsystems"])

    def test_cli_valid_provenance_evaluates(self):
        payload = self._run(
            {
                "provenance": "redmine:13967:j#81322",
                "findings": [
                    {"subsystem": "s", "round_index": 2, "authority_bearing": True, "late": False},
                ],
            },
            add_provenance=False,
        )
        self.assertEqual(payload["escalation_decision"], "no_escalation")
        self.assertTrue(payload["evaluated"])
        self.assertFalse(payload["any_escalation"])


if __name__ == "__main__":
    unittest.main()
