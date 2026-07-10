"""onboarding CLI wiring + handler behaviour (Redmine #13498).

Exercises the in-process ``build_parser()`` + ``ns.func(ns)`` harness (the repo
convention) across inspect / plan / apply / resume, and the bare-`mozyo`
adoption_in_progress reroute hook.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application.commands_onboarding import (
    GATE_SECRET_ENV,
    maybe_resume_bare_mozyo,
)


def _run(argv):
    parser = build_parser()
    ns = parser.parse_args(argv)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = ns.func(ns)
    return rc, out.getvalue()


class OnboardingCliRegistrationTests(unittest.TestCase):
    def test_subcommands_registered(self) -> None:
        parser = build_parser()
        for cmd, fn in [
            ("inspect", "cmd_onboarding_inspect"),
            ("plan", "cmd_onboarding_plan"),
            ("apply", "cmd_onboarding_apply"),
            ("resume", "cmd_onboarding_resume"),
        ]:
            ns = parser.parse_args(["onboarding", cmd, *(["--intent", "{}"] if cmd == "plan" else []), *(["--plan", "{}"] if cmd == "apply" else [])])
            self.assertEqual(ns.func.__name__, fn)


class OnboardingCliBehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "proj"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_inspect_json_reports_unadopted(self) -> None:
        rc, out = _run(["onboarding", "inspect", "--root", str(self.root), "--json"])
        record = json.loads(out)
        self.assertEqual(record["state"], "unadopted")
        self.assertEqual(rc, 0)

    def test_plan_rejects_invalid_intent(self) -> None:
        rc, out = _run(
            [
                "onboarding",
                "plan",
                "--root",
                str(self.root),
                "--intent",
                json.dumps({"schema_version": 1, "action": "propose", "preset": "rails",
                            "backend": "herdr", "git_mode": "none", "rules_store": "central",
                            "free_text_summary": ""}),
                "--json",
            ]
        )
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["error"], "unknown_enum")

    def test_apply_requires_confirm_flag(self) -> None:
        # Build a valid plan first.
        rc, out = _run(
            [
                "onboarding",
                "plan",
                "--root",
                str(self.root),
                "--intent",
                json.dumps({"schema_version": 1, "action": "confirm_plan", "preset": "none",
                            "backend": "herdr", "git_mode": "none", "rules_store": "central",
                            "free_text_summary": ""}),
                "--json",
            ]
        )
        self.assertEqual(rc, 0, msg=out)
        plan_json = out
        rc, out = _run(["onboarding", "apply", "--plan", plan_json, "--json"])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["error"], "plan_not_confirmed")

    def test_apply_rejects_tampered_plan(self) -> None:
        tampered = json.dumps(
            {
                "plan_id": "deadbeef",
                "root_fingerprint": "x",
                "canonical_root": str(self.root),
                "scaffold_preset": "none",
                "rules_store": "central",
                "ordered_steps": [{"step_id": "finalize", "summary": ""}],
            }
        )
        rc, out = _run(["onboarding", "apply", "--plan", tampered, "--confirm", "--json"])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["error"], "tampered_plan")

    def test_resume_without_receipt(self) -> None:
        rc, out = _run(["onboarding", "resume", "--root", str(self.root), "--json"])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["error"], "nothing_to_resume")

    def test_bare_mozyo_reroute_returns_none_when_not_in_progress(self) -> None:
        # An unadopted cwd is not adoption_in_progress → hook lets launch proceed.
        cwd = os.getcwd()
        try:
            os.chdir(self.root)
            parser = build_parser()
            ns = parser.parse_args([])
            self.assertIsNone(maybe_resume_bare_mozyo(ns))
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
