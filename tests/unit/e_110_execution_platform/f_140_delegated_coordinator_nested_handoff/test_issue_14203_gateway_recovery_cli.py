"""Unit: ``sublane recover-gateway`` CLI staged seam (Redmine #14203).

The live observation / actuation ops are a follow-up; until they land EVERY invocation —
preflight and execute alike — must return the fail-closed typed seam refusal with ZERO
process effect and a non-zero exit, and the honest ``turn_unobservable`` classification
(never a fabricated preflight).
"""

from __future__ import annotations

import argparse
import io
import json
import unittest
from contextlib import redirect_stdout

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_gateway_recovery_cli import (  # noqa: E501
    SEAM_UNAVAILABLE_VERDICT,
    cmd_sublane_recover_gateway,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E501
    TURN_CLASS_UNOBSERVABLE,
    TURN_REASON_UNKNOWN,
)


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        issue="14203", lane="issue_x_lane", role="codex", provider="codex",
        assigned_name="gw", locator="w:3", journal="", action_id="",
        action_generation=0, gateway_revision="", lane_revision="",
        lane_generation="", resume_anchor_journal="", resume_gate="",
        reason_token="", execute=False, json=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class StagedSeamTests(unittest.TestCase):
    def _run(self, **overrides):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cmd_sublane_recover_gateway(_args(**overrides))
        return code, json.loads(out.getvalue())

    def test_a_preflight_is_a_typed_seam_refusal_nonzero_exit(self):
        code, payload = self._run(execute=False)
        self.assertEqual(code, 1)
        self.assertEqual(payload["verdict"], SEAM_UNAVAILABLE_VERDICT)
        self.assertEqual(payload["turn_class"], TURN_CLASS_UNOBSERVABLE)
        self.assertEqual(payload["turn_reason"], TURN_REASON_UNKNOWN)
        self.assertIn("zero process effect", payload["detail"])

    def test_an_execute_is_equally_refused_with_zero_effect(self):
        code, payload = self._run(execute=True)
        self.assertEqual(code, 1)
        self.assertEqual(payload["verdict"], SEAM_UNAVAILABLE_VERDICT)
        self.assertEqual(payload["status"], "refused")
        self.assertTrue(payload["executed"])
        self.assertFalse(payload["closed_old_gateway"])
        self.assertFalse(payload["fresh_slot_attested"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
