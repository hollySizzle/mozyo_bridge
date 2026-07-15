"""onboarding CLI wiring + handler behaviour (Redmine #13498 / #13501).

In-process ``build_parser()`` + ``ns.func(ns)`` harness across inspect / plan /
apply / resume, with the trusted gate secret set in the environment.
"""

from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application.commands_onboarding import (
    GATE_SECRET_ENV,
    _load_json_arg,
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


class OnboardingJsonArgDiscriminatorTests(unittest.TestCase):
    """``--plan`` / ``--intent`` JSON-vs-path discrimination (Redmine #13691).

    The argument is "an inline JSON string or a path to a JSON file", an
    existing file taking precedence. An argument we cannot stat is not a file:
    ``Path.exists()`` raises ``ENAMETOOLONG`` on a long inline plan under some
    interpreters / platforms and silently reports "missing" under others, which
    is what made the ``apply`` security gate's error semantics depend on the
    runtime rather than on the plan.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_unstatable_inline_json_still_parses(self) -> None:
        # Interpreter-independent form of the reported failure: whatever the
        # filesystem raises about the argument, inline JSON reaches the gate.
        raw = json.dumps({"plan_id": "x" * 400, "steps": []})
        boom = OSError(errno.ENAMETOOLONG, "File name too long")
        with mock.patch.object(Path, "exists", autospec=True, side_effect=boom):
            self.assertEqual(_load_json_arg(raw), {"plan_id": "x" * 400, "steps": []})

    def test_unstatable_path_argument_fails_closed_as_json(self) -> None:
        # Not JSON and unusable as a path: the error must be the JSON error on
        # every interpreter, never a leaked OSError from the discriminator.
        with self.assertRaises(ValueError):
            _load_json_arg("/" + "n" * 4000)

    def test_json_file_path_input_remains_supported(self) -> None:
        plan_file = self.tmp / "plan.json"
        plan_file.write_text(json.dumps(_INTENT), encoding="utf-8")
        self.assertEqual(_load_json_arg(str(plan_file)), _INTENT)

    def test_json_prefix_file_path_input_remains_supported(self) -> None:
        # A file path may itself start with `{` / `[`, so the discrimination
        # cannot be made on the argument's leading character (R1-F1). Only a
        # relative argument exercises this: an absolute one starts with `/`.
        for name in ("{plan}.json", "[draft].json"):
            with self.subTest(name=name):
                (self.tmp / name).write_text(json.dumps({"from_file": name}), encoding="utf-8")
                cwd = os.getcwd()
                os.chdir(self.tmp)
                try:
                    self.assertEqual(_load_json_arg(name), {"from_file": name})
                finally:
                    os.chdir(cwd)


class OnboardingCliLongInlinePlanTests(unittest.TestCase):
    """The reported reproduction: a real plan long enough to exceed ``PATH_MAX``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # A deep root makes the emitted plan JSON longer than PATH_MAX, which is
        # what turned the inline plan into `invalid_plan_json` before #13691.
        self.root = Path(self._tmp.name).joinpath("d" * 180, "e" * 180, "proj")
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _plan_json(self) -> str:
        rc, out = _run(
            ["onboarding", "plan", "--root", str(self.root),
             "--intent", json.dumps(_INTENT), "--json"]
        )
        self.assertEqual(rc, 0, msg=out)
        self.assertGreater(len(out), 1024, msg="reproduction needs a plan longer than PATH_MAX")
        return out

    def test_long_inline_plan_reaches_the_confirm_gate(self) -> None:
        with _gate_secret("cli-secret"):
            plan_json = self._plan_json()
            rc, out = _run(["onboarding", "apply", "--plan", plan_json, "--json"])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["error"], "plan_not_confirmed")

    def test_long_forged_inline_plan_reaches_the_authority_gate(self) -> None:
        with _gate_secret("cli-secret"):
            forged = json.loads(self._plan_json())
            forged["scaffold_preset"] = "redmine-rails-governed"  # tamper, keep plan_id
            rc, out = _run(
                ["onboarding", "apply", "--plan", json.dumps(forged), "--confirm", "--json"]
            )
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["error"], "plan_unauthorized")

    def test_non_object_inline_plan_stays_fail_closed(self) -> None:
        with _gate_secret("cli-secret"):
            rc, out = _run(["onboarding", "apply", "--plan", "[1, 2]", "--confirm", "--json"])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["error"], "invalid_plan_json")


if __name__ == "__main__":
    unittest.main()
