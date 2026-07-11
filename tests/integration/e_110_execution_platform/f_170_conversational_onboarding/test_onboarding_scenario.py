"""End-to-end onboarding scenario (Redmine #13498 / #13503 / #13501).

Fresh non-Git sync fixture: inspect -> caution -> flagless intent -> plan ->
apply -> config / scaffold / rules / registry / receipt complete, and git init
is never run. Plus one-step-per-call resume and no-secret persistence, exercised
against the real scaffold / rules / workspace use cases (isolated to a temp home).
"""

from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application import (
    apply_usecase as _au,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application.apply_usecase import (
    ApplyError,
    apply_plan,
    resume_onboarding,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application.herdr_binary import (
    HERDR_BINARY_ENV,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application.inspect_usecase import (
    inspect_onboarding,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.intent import (
    validate_onboarding_intent,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.plan import (
    build_plan,
    compute_root_fingerprint,
    issue_human_gate_receipt,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.preflight import (
    STATE_ADOPTION_IN_PROGRESS,
    STATE_CAUTION_REQUIRES_ACK,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.receipt import (
    RECEIPT_STATE_COMPLETE,
)

_SECRET = "scenario-gate-secret"


def _fake_herdr(dirpath: Path) -> str:
    dirpath.mkdir(parents=True, exist_ok=True)
    binary = dirpath / "herdr"
    binary.write_text("#!/bin/sh\necho herdr\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(binary)


class OnboardingScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        self.mozyo_home = base / "mozyo_home"
        self.mozyo_home.mkdir()
        self.sync_root = self.home / "Library" / "CloudStorage"
        self.root = self.sync_root / "GoogleDrive-x" / "project"
        self.root.mkdir(parents=True)
        binary = _fake_herdr(base / "bin")
        self.env = {HERDR_BINARY_ENV: binary, "PATH": ""}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _inspect(self):
        return inspect_onboarding(
            self.root, home=self.home, sync_roots=(self.sync_root,),
            env=self.env, gate_secret=_SECRET,
        )

    def _plan(self):
        inspection = self._inspect()
        self.assertEqual(inspection.preflight.state, STATE_CAUTION_REQUIRES_ACK)
        intent = validate_onboarding_intent(
            {
                "schema_version": 1,
                "action": "confirm_plan",
                "preset": "none",
                "backend": "herdr",
                "git_mode": "none",
                "rules_store": "central",
                "free_text_summary": "adopt this synced folder",
            }
        )
        fp = compute_root_fingerprint(inspection.facts)
        receipt = issue_human_gate_receipt(fp, inspection.facts.path_risk, secret=_SECRET)
        return build_plan(
            inspection.facts, intent, human_gate_receipt=receipt, gate_secret=_SECRET
        )

    def _apply(self, plan, env=None):
        return apply_plan(
            plan,
            human_confirmed=True,
            gate_secret=_SECRET,
            home=self.mozyo_home,
            sync_roots=(self.sync_root,),
            env=env or self.env,
        )

    def _resume(self, env=None):
        return resume_onboarding(
            self.root, gate_secret=_SECRET, home=self.mozyo_home,
            sync_roots=(self.sync_root,), env=env or self.env,
        )

    def test_full_fresh_sync_adoption_completes_without_git_init(self) -> None:
        plan = self._plan()
        result = self._apply(plan)
        self.assertEqual(result.state, RECEIPT_STATE_COMPLETE, msg=result.as_record())
        self.assertTrue((self.root / ".mozyo-bridge" / "config.yaml").exists())
        self.assertTrue((self.root / ".mozyo-bridge" / "scaffold.json").exists())
        self.assertTrue((self.root / ".mozyo-bridge" / "workspace-anchor.json").exists())
        self.assertTrue((self.root / ".mozyo-bridge" / "onboarding-receipt.json").exists())
        self.assertTrue((self.root / "AGENTS.md").exists())
        self.assertFalse((self.root / ".git").exists())
        self.assertEqual(self._inspect().preflight.state, "adopted")

    def test_apply_requires_confirmation(self) -> None:
        plan = self._plan()
        with self.assertRaises(ApplyError) as ctx:
            apply_plan(plan, human_confirmed=False, gate_secret=_SECRET,
                       home=self.mozyo_home, sync_roots=(self.sync_root,), env=self.env)
        self.assertEqual(ctx.exception.code, "plan_not_confirmed")

    def test_empty_gate_secret_fails_closed(self) -> None:
        plan = self._plan()
        for bad in ("", "   ", None):
            with self.assertRaises(ApplyError) as ctx:
                apply_plan(plan, human_confirmed=True, gate_secret=bad,
                           home=self.mozyo_home, sync_roots=(self.sync_root,), env=self.env)
            self.assertEqual(ctx.exception.code, "gate_secret_required")

    def test_partial_failure_resumes_one_step_at_a_time(self) -> None:
        plan = self._plan()
        # Inject a transient failure at the verify step (keeping the env — and so
        # the authority-bound facts — unchanged), then resume once the injection
        # is removed.
        failing = lambda ctx: _au._StepOutcome(_au.STEP_STATUS_FAILED, reason="injected")
        with mock.patch.dict(_au._EXECUTORS, {"verify": failing}):
            result = self._apply(plan)
        self.assertEqual(result.failed_step, "verify", msg=result.as_record())
        self.assertEqual(result.state, STATE_ADOPTION_IN_PROGRESS)
        self.assertEqual(self._inspect().preflight.state, STATE_ADOPTION_IN_PROGRESS)

        # Resume advances exactly one pending step per call.
        first = self._resume()
        self.assertNotEqual(first.state, RECEIPT_STATE_COMPLETE)
        self.assertEqual(len(first.applied_steps) + len(first.no_op_steps), 1)

        # Keep resuming (one step each) until complete.
        guard = 0
        result = first
        while result.state != RECEIPT_STATE_COMPLETE and guard < 8:
            result = self._resume()
            guard += 1
        self.assertEqual(result.state, RECEIPT_STATE_COMPLETE, msg=result.as_record())

    def test_no_secret_persisted_in_receipt(self) -> None:
        plan = self._plan()
        self._apply(plan)
        receipt_text = (self.root / ".mozyo-bridge" / "onboarding-receipt.json").read_text(
            encoding="utf-8"
        )
        for needle in (_SECRET, "PATH", "MOZYO_HERDR_BINARY", "echo herdr"):
            self.assertNotIn(needle, receipt_text)

    def test_resume_without_receipt_errors(self) -> None:
        with self.assertRaises(ApplyError) as ctx:
            self._resume()
        self.assertEqual(ctx.exception.code, "nothing_to_resume")

    def test_apply_rejects_already_adopted(self) -> None:
        plan = self._plan()
        self._apply(plan)  # completes
        # A second apply of any plan is refused; the root is adopted.
        with self.assertRaises(ApplyError) as ctx:
            self._apply(plan)
        self.assertIn(ctx.exception.code, {"already_adopted", "adoption_in_progress"})


if __name__ == "__main__":
    unittest.main()
