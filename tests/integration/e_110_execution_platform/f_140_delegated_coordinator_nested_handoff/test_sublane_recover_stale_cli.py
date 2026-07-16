"""`sublane recover-stale` CLI wiring tests (Redmine #13806 tranche D j#79485).

The owner-facing surface of the stale-worker recovery contract, so its defaults are part of
the safety boundary:

- the subcommand registers under the ``sublane`` family (alongside quarantine / hibernate);
- the exact-target fields (issue / lane / role / provider / assigned-name / locator) are
  **required** — a partially identified worker can never be named on the command line;
- the default is read-only preflight (``--execute`` is opt-in);
- this tranche ships only the pure semantic surface, so the live seam is fail-closed: the
  command reports a typed ``live_seam_unavailable`` refusal with ZERO process effect and a
  non-zero exit (never mistaken for a completed recovery).
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser  # noqa: E402
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery import (  # noqa: E402,E501
    SEAM_UNAVAILABLE_VERDICT,
)

BASE = [
    "sublane", "recover-stale",
    "--issue", "13806", "--lane", "l", "--role", "worker",
    "--provider", "claude", "--assigned-name", "wk", "--locator", "w:2",
]


def _run(argv):
    parser = build_parser(None)
    ns = parser.parse_args(argv)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = ns.func(ns)
    return rc, out.getvalue()


class RecoverStaleCliTests(unittest.TestCase):
    def test_registers_under_sublane_family(self):
        parser = build_parser(None)
        ns = parser.parse_args(BASE)
        self.assertEqual(ns.func.__name__, "cmd_sublane_recover_stale")

    def test_exact_target_fields_are_required(self):
        parser = build_parser(None)
        for drop in ("--lane", "--role", "--provider", "--assigned-name", "--locator"):
            argv = list(BASE)
            idx = argv.index(drop)
            del argv[idx : idx + 2]
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    parser.parse_args(argv)

    def test_preflight_default_is_staged_seam_zero_effect(self):
        rc, text = _run(BASE)
        self.assertEqual(rc, 1)  # staged seam refusal is non-zero
        self.assertIn("recover-stale", text)

    def test_json_payload_shape_and_seam_verdict(self):
        rc, text = _run(BASE + ["--json"])
        payload = json.loads(text)
        self.assertEqual(payload["verdict"], SEAM_UNAVAILABLE_VERDICT)
        self.assertFalse(payload["executed"])  # no --execute
        self.assertEqual(payload["status"], "refused")
        self.assertIn("issue", payload)

    def test_execute_flag_is_opt_in_and_still_zero_effect(self):
        rc, text = _run(BASE + ["--execute", "--json"])
        payload = json.loads(text)
        self.assertTrue(payload["executed"])
        self.assertEqual(payload["verdict"], SEAM_UNAVAILABLE_VERDICT)
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
