"""onboarding CLI wiring + handler behaviour (Redmine #13498 / #13501).

In-process ``build_parser()`` + ``ns.func(ns)`` harness across inspect / plan /
apply / resume, with the trusted gate secret set in the environment.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application.commands_onboarding import (
    GATE_SECRET_ENV,
)

_INTENT = {
    "schema_version": 1,
    "action": "confirm_plan",
    "preset": "none",
    "backend": "herdr",
    "git_mode": "none",
    "rules_store": "central",
    "free_text_summary": "",
}


def _run(argv):
    parser = build_parser()
    ns = parser.parse_args(argv)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = ns.func(ns)
    return rc, out.getvalue()


@contextlib.contextmanager
def _gate_secret(value):
    prev = os.environ.get(GATE_SECRET_ENV)
    if value is None:
        os.environ.pop(GATE_SECRET_ENV, None)
    else:
        os.environ[GATE_SECRET_ENV] = value
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop(GATE_SECRET_ENV, None)
        else:
            os.environ[GATE_SECRET_ENV] = prev


class OnboardingCliRegistrationTests(unittest.TestCase):
    def test_subcommands_registered(self) -> None:
        parser = build_parser()
        for cmd, fn, extra in [
            ("inspect", "cmd_onboarding_inspect", []),
            ("plan", "cmd_onboarding_plan", ["--intent", "{}"]),
            ("apply", "cmd_onboarding_apply", ["--plan", "{}"]),
            ("resume", "cmd_onboarding_resume", []),
        ]:
            ns = parser.parse_args(["onboarding", cmd, *extra])
            self.assertEqual(ns.func.__name__, fn)


class OnboardingCliBehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "proj"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_inspect_json_reports_unadopted(self) -> None:
        with _gate_secret("cli-secret"):
            rc, out = _run(["onboarding", "inspect", "--root", str(self.root), "--json"])
        record = json.loads(out)
        self.assertEqual(record["state"], "unadopted")
        self.assertEqual(rc, 0)

    def test_plan_rejects_invalid_intent(self) -> None:
        bad = dict(_INTENT, preset="rails")
        with _gate_secret("cli-secret"):
            rc, out = _run(
                ["onboarding", "plan", "--root", str(self.root),
                 "--intent", json.dumps(bad), "--json"]
            )
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["error"], "unknown_enum")

    def test_plan_without_gate_secret_fails_closed(self) -> None:
        with _gate_secret(None):
            rc, out = _run(
                ["onboarding", "plan", "--root", str(self.root),
                 "--intent", json.dumps(_INTENT), "--json"]
            )
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["error"], "gate_secret_required")

    def test_apply_requires_confirm_flag(self) -> None:
        with _gate_secret("cli-secret"):
            rc, out = _run(
                ["onboarding", "plan", "--root", str(self.root),
                 "--intent", json.dumps(_INTENT), "--json"]
            )
            self.assertEqual(rc, 0, msg=out)
            rc, out = _run(["onboarding", "apply", "--plan", out, "--json"])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["error"], "plan_not_confirmed")

    def test_apply_rejects_forged_plan(self) -> None:
        with _gate_secret("cli-secret"):
            rc, plan_json = _run(
                ["onboarding", "plan", "--root", str(self.root),
                 "--intent", json.dumps(_INTENT), "--json"]
            )
            self.assertEqual(rc, 0, msg=plan_json)
            forged = json.loads(plan_json)
            forged["scaffold_preset"] = "redmine-rails-governed"  # tamper, keep plan_id
            rc, out = _run(
                ["onboarding", "apply", "--plan", json.dumps(forged), "--confirm", "--json"]
            )
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["error"], "plan_unauthorized")

    def test_resume_without_receipt(self) -> None:
        with _gate_secret("cli-secret"):
            rc, out = _run(["onboarding", "resume", "--root", str(self.root), "--json"])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["error"], "nothing_to_resume")


if __name__ == "__main__":
    unittest.main()
