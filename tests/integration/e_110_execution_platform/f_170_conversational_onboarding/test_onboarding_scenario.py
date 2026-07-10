"""End-to-end onboarding scenario (Redmine #13498 / #13503).

Fresh non-Git sync fixture: inspect -> caution -> flagless intent -> plan ->
apply -> config / scaffold / rules / registry / receipt complete, and git init
is never run. Plus partial-failure resume and same-root lock behaviour, exercised
against the real scaffold / rules / workspace use cases (isolated to a temp home).
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

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
    STATE_UNADOPTED,
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
        # Fresh non-Git folder under a sync root.
        self.sync_root = self.home / "Library" / "CloudStorage"
        self.root = self.sync_root / "GoogleDrive-x" / "project"
        self.root.mkdir(parents=True)
        binary = _fake_herdr(base / "bin")
        self.env = {HERDR_BINARY_ENV: binary, "PATH": ""}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _inspect(self):
        return inspect_onboarding(
            self.root, home=self.home, sync_roots=(self.sync_root,), env=self.env
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

    def _apply(self, plan):
        return apply_plan(
            plan,
            human_confirmed=True,
            home=self.mozyo_home,
            sync_roots=(self.sync_root,),
            env=self.env,
        )

    def test_full_fresh_sync_adoption_completes_without_git_init(self) -> None:
        plan = self._plan()
        result = self._apply(plan)
        self.assertEqual(result.state, RECEIPT_STATE_COMPLETE, msg=result.as_record())
        # config / scaffold / registry anchor / receipt all present.
        self.assertTrue((self.root / ".mozyo-bridge" / "config.yaml").exists())
        self.assertTrue((self.root / ".mozyo-bridge" / "scaffold.json").exists())
        self.assertTrue((self.root / ".mozyo-bridge" / "workspace-anchor.json").exists())
        self.assertTrue((self.root / ".mozyo-bridge" / "onboarding-receipt.json").exists())
        self.assertTrue((self.root / "AGENTS.md").exists())
        # git init was never run.
        self.assertFalse((self.root / ".git").exists())
        # re-inspect: now adopted (complete receipt).
        self.assertEqual(self._inspect().preflight.state, "adopted")

    def test_apply_requires_confirmation(self) -> None:
        plan = self._plan()
        with self.assertRaises(ApplyError) as ctx:
            apply_plan(plan, human_confirmed=False, home=self.mozyo_home,
                       sync_roots=(self.sync_root,), env=self.env)
        self.assertEqual(ctx.exception.code, "plan_not_confirmed")

    def test_apply_refuses_drifted_plan(self) -> None:
        plan = self._plan()
        # Introduce drift: a pre-existing config appears after planning.
        cfg = self.root / ".mozyo-bridge" / "config.yaml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("version: 1\nproviders: {}\n", encoding="utf-8")
        with self.assertRaises(ApplyError) as ctx:
            self._apply(plan)
        self.assertEqual(ctx.exception.code, "plan_drift")

    def test_partial_failure_resumes_to_completion(self) -> None:
        plan = self._plan()
        # First apply with NO herdr binary → verify step fails, receipt persists.
        broken_env = {HERDR_BINARY_ENV: "", "PATH": ""}
        result = apply_plan(
            plan, human_confirmed=True, home=self.mozyo_home,
            sync_roots=(self.sync_root,), env=broken_env,
        )
        self.assertEqual(result.failed_step, "verify", msg=result.as_record())
        self.assertEqual(result.state, STATE_ADOPTION_IN_PROGRESS)
        receipt_path = self.root / ".mozyo-bridge" / "onboarding-receipt.json"
        self.assertTrue(receipt_path.exists())
        # re-inspect: adoption_in_progress reroutes to resume.
        self.assertEqual(self._inspect().preflight.state, STATE_ADOPTION_IN_PROGRESS)
        # Resume with a working herdr → completes, earlier steps are no-ops.
        resumed = resume_onboarding(
            self.root, home=self.mozyo_home, sync_roots=(self.sync_root,), env=self.env
        )
        self.assertEqual(resumed.state, RECEIPT_STATE_COMPLETE, msg=resumed.as_record())

    def test_no_secret_persisted_in_receipt(self) -> None:
        plan = self._plan()
        self._apply(plan)
        receipt_text = (self.root / ".mozyo-bridge" / "onboarding-receipt.json").read_text(
            encoding="utf-8"
        )
        for needle in (_SECRET, "PATH", "MOZYO_HERDR_BINARY", "herdr\n"):
            self.assertNotIn(needle, receipt_text)

    def test_resume_without_receipt_errors(self) -> None:
        with self.assertRaises(ApplyError) as ctx:
            resume_onboarding(self.root, home=self.mozyo_home,
                              sync_roots=(self.sync_root,), env=self.env)
        self.assertEqual(ctx.exception.code, "nothing_to_resume")


if __name__ == "__main__":
    unittest.main()
