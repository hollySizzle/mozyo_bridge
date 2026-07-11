"""Bare-entry routing + launch-exactly-once tests (Redmine #13497 j#74933 / R1 / R3).

Every leg is exercised for the two invariants the acceptance audit fixed: one
bare `mozyo` reaches adoption **and** the backend launch (exactly one launch on a
complete outcome), and no launch on any cancelled / failed / blocked / broken /
in-progress-incomplete outcome. The #13498 deterministic tools are faked so the
routing + launch wiring is isolated.
"""

from __future__ import annotations

import json
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application import (
    bare_entry as be,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application.adoption_probe import (
    ADOPTION_BROKEN,
    ADOPTION_COMPLETE,
    ADOPTION_IN_PROGRESS,
    AdoptionStatus,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.conversation_port import (
    Explain,
    IntentCandidate,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.receipt import (
    RECEIPT_STATE_COMPLETE,
)

_ROOT = Path("/tmp/target-root")
_READY_INTENT = {
    "schema_version": 1,
    "action": "confirm_plan",
    "preset": "none",
    "backend": "herdr",
    "git_mode": "none",
    "rules_store": "central",
    "free_text_summary": "fresh setup",
}


class FakeIO:
    def __init__(self, *, prompts=(), confirms=()):
        self._prompts = list(prompts)
        self._confirms = list(confirms)
        self.shown = []

    def show(self, text):
        self.shown.append(text)

    def prompt(self):
        return self._prompts.pop(0) if self._prompts else None

    def confirm(self, text):
        return self._confirms.pop(0) if self._confirms else False


class FakeProvider:
    def __init__(self, turns):
        self._turns = list(turns)

    def converse(self, context):
        return self._turns.pop(0)


class Launch:
    def __init__(self, code=0):
        self.calls = 0
        self._code = code

    def __call__(self):
        self.calls += 1
        return self._code


def _preflight(state, *, path_risk="normal", reasons=()):
    return types.SimpleNamespace(
        state=state, path_risk=path_risk, hard_block_reasons=reasons,
        herdr_binary=types.SimpleNamespace(state="resolved"),
        root_kind="non_git", adoption_marker="absent",
    )


def _facts():
    return types.SimpleNamespace(canonical_root=str(_ROOT), path_risk="normal")


def _inspection(state, **kw):
    return types.SimpleNamespace(preflight=_preflight(state, **kw), facts=_facts())


def _plan():
    return types.SimpleNamespace(
        scaffold_preset="none", rules_store="central",
        ordered_steps=[types.SimpleNamespace(summary="step one")],
        warnings=[], as_record=lambda: {"plan_id": "plan.v2.x"},
    )


def _apply_result(*, complete=True, failed=None):
    return types.SimpleNamespace(
        state=RECEIPT_STATE_COMPLETE if complete else "adoption_in_progress",
        failed_step=failed, applied_steps=("rules_install",), next_action=None,
    )


class AdoptedEntryTest(unittest.TestCase):
    def test_valid_complete_launches_once_no_conversation(self):
        launch = Launch()
        provider = FakeProvider([])  # must never be consulted
        with mock.patch.object(
            be, "classify_adoption",
            return_value=AdoptionStatus(ADOPTION_COMPLETE, _ROOT),
        ):
            rc = be.run_bare_entry(
                target_root=_ROOT, launch_adopted=launch, provider=provider,
                gate_secret="s", io=FakeIO(),
            )
        self.assertEqual(rc, 0)
        self.assertEqual(launch.calls, 1)

    def test_broken_never_launches(self):
        launch = Launch()
        with mock.patch.object(
            be, "classify_adoption",
            return_value=AdoptionStatus(ADOPTION_BROKEN, _ROOT, reason="bad config"),
        ):
            rc = be.run_bare_entry(
                target_root=_ROOT, launch_adopted=launch,
                provider=FakeProvider([]), gate_secret="s", io=FakeIO(),
            )
        self.assertEqual(rc, 1)
        self.assertEqual(launch.calls, 0)


class ResumeTest(unittest.TestCase):
    def _status(self):
        return AdoptionStatus(ADOPTION_IN_PROGRESS, _ROOT)

    def test_resume_to_complete_launches_once(self):
        launch = Launch()
        with mock.patch.object(be, "classify_adoption", return_value=self._status()), \
             mock.patch.object(be, "resume_onboarding",
                               return_value=_apply_result(complete=True)):
            rc = be.run_bare_entry(
                target_root=_ROOT, launch_adopted=launch, provider=FakeProvider([]),
                gate_secret="s", io=FakeIO(confirms=[True]),
            )
        self.assertEqual(rc, 0)
        self.assertEqual(launch.calls, 1)

    def test_resume_declined_never_launches(self):
        launch = Launch()
        with mock.patch.object(be, "classify_adoption", return_value=self._status()):
            rc = be.run_bare_entry(
                target_root=_ROOT, launch_adopted=launch, provider=FakeProvider([]),
                gate_secret="s", io=FakeIO(confirms=[False]),
            )
        self.assertEqual(rc, 1)
        self.assertEqual(launch.calls, 0)

    def test_resume_failure_never_launches(self):
        launch = Launch()
        with mock.patch.object(be, "classify_adoption", return_value=self._status()), \
             mock.patch.object(be, "resume_onboarding",
                               return_value=_apply_result(complete=False, failed="scaffold_apply")):
            rc = be.run_bare_entry(
                target_root=_ROOT, launch_adopted=launch, provider=FakeProvider([]),
                gate_secret="s", io=FakeIO(confirms=[True]),
            )
        self.assertEqual(rc, 1)
        self.assertEqual(launch.calls, 0)


class FreshOnboardingTest(unittest.TestCase):
    def _absent(self):
        return AdoptionStatus("absent", _ROOT)

    def test_fresh_complete_launches_exactly_once(self):
        launch = Launch()
        provider = FakeProvider([IntentCandidate(_READY_INTENT)])
        with mock.patch.object(be, "classify_adoption", return_value=self._absent()), \
             mock.patch.object(be, "inspect_onboarding",
                               return_value=_inspection("unadopted")), \
             mock.patch.object(be, "build_plan", return_value=_plan()), \
             mock.patch.object(be, "apply_plan", return_value=_apply_result(complete=True)):
            rc = be.run_bare_entry(
                target_root=_ROOT, launch_adopted=launch, provider=provider,
                gate_secret="s", io=FakeIO(prompts=["set up here"], confirms=[True]),
            )
        self.assertEqual(rc, 0)
        self.assertEqual(launch.calls, 1)

    def test_fresh_apply_failure_never_launches(self):
        launch = Launch()
        provider = FakeProvider([IntentCandidate(_READY_INTENT)])
        with mock.patch.object(be, "classify_adoption", return_value=self._absent()), \
             mock.patch.object(be, "inspect_onboarding",
                               return_value=_inspection("unadopted")), \
             mock.patch.object(be, "build_plan", return_value=_plan()), \
             mock.patch.object(be, "apply_plan",
                               return_value=_apply_result(complete=False, failed="rules_install")):
            rc = be.run_bare_entry(
                target_root=_ROOT, launch_adopted=launch, provider=provider,
                gate_secret="s", io=FakeIO(prompts=["set up here"], confirms=[True]),
            )
        self.assertEqual(rc, 1)
        self.assertEqual(launch.calls, 0)

    def test_plan_declined_never_launches_or_mutates(self):
        launch = Launch()
        provider = FakeProvider([IntentCandidate(_READY_INTENT)])
        apply_mock = mock.Mock()
        with mock.patch.object(be, "classify_adoption", return_value=self._absent()), \
             mock.patch.object(be, "inspect_onboarding",
                               return_value=_inspection("unadopted")), \
             mock.patch.object(be, "build_plan", return_value=_plan()), \
             mock.patch.object(be, "apply_plan", apply_mock):
            rc = be.run_bare_entry(
                target_root=_ROOT, launch_adopted=launch, provider=provider,
                gate_secret="s", io=FakeIO(prompts=["set up here"], confirms=[False]),
            )
        self.assertEqual(rc, 1)
        self.assertEqual(launch.calls, 0)
        apply_mock.assert_not_called()

    def test_conversation_cancelled_never_launches(self):
        launch = Launch()
        # Provider asks a question; the human EOFs (prompt returns None) -> cancel.
        provider = FakeProvider([Explain("what is this?")])
        with mock.patch.object(be, "classify_adoption", return_value=self._absent()), \
             mock.patch.object(be, "inspect_onboarding",
                               return_value=_inspection("unadopted")):
            rc = be.run_bare_entry(
                target_root=_ROOT, launch_adopted=launch, provider=provider,
                gate_secret="s", io=FakeIO(prompts=["hi"]),
            )
        self.assertEqual(rc, 1)
        self.assertEqual(launch.calls, 0)

    def test_blocked_never_launches(self):
        launch = Launch()
        with mock.patch.object(be, "classify_adoption", return_value=self._absent()), \
             mock.patch.object(be, "inspect_onboarding",
                               return_value=_inspection("blocked", reasons=("home root",))):
            rc = be.run_bare_entry(
                target_root=_ROOT, launch_adopted=launch, provider=FakeProvider([]),
                gate_secret="s", io=FakeIO(),
            )
        self.assertEqual(rc, 1)
        self.assertEqual(launch.calls, 0)

    def test_caution_declined_never_launches_or_converses(self):
        launch = Launch()
        provider = FakeProvider([])  # never consulted when caution declined
        with mock.patch.object(be, "classify_adoption", return_value=self._absent()), \
             mock.patch.object(be, "inspect_onboarding",
                               return_value=_inspection("caution_requires_ack",
                                                        path_risk="sync_or_cloud")):
            rc = be.run_bare_entry(
                target_root=_ROOT, launch_adopted=launch, provider=provider,
                gate_secret="s", io=FakeIO(confirms=[False]),
            )
        self.assertEqual(rc, 1)
        self.assertEqual(launch.calls, 0)


class StrictNoIO:
    """An IO that fails the test if any interactive method is touched."""

    def show(self, text):
        raise AssertionError("show() must not be called under --json")

    def prompt(self):
        raise AssertionError("stdin must not be read under --json")

    def confirm(self, text):
        raise AssertionError("confirm() must not be called under --json")


class JsonModeTest(unittest.TestCase):
    """--json machine-readable contract: no prose/prompt mixing (j#74970 F3)."""

    def _run_json(self, **patches):
        launch = Launch()
        apply_mock = mock.Mock()
        resume_mock = mock.Mock()
        buf = StringIO()
        ctxs = [
            mock.patch.object(be, "apply_plan", apply_mock),
            mock.patch.object(be, "resume_onboarding", resume_mock),
        ]
        for name, val in patches.items():
            ctxs.append(mock.patch.object(be, name, val))
        with redirect_stdout(buf):
            for c in ctxs:
                c.__enter__()
            try:
                rc = be.run_bare_entry(
                    target_root=_ROOT, launch_adopted=launch,
                    provider=FakeProvider([]), gate_secret="s",
                    io=StrictNoIO(), json_output=True,
                )
            finally:
                for c in reversed(ctxs):
                    c.__exit__(None, None, None)
        return rc, buf.getvalue(), launch, apply_mock, resume_mock

    def test_adopted_complete_launches_under_json(self):
        launch = Launch()
        with mock.patch.object(
            be, "classify_adoption",
            return_value=AdoptionStatus(ADOPTION_COMPLETE, _ROOT),
        ):
            rc = be.run_bare_entry(
                target_root=_ROOT, launch_adopted=launch, provider=FakeProvider([]),
                gate_secret="s", io=StrictNoIO(), json_output=True,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(launch.calls, 1)

    def test_fresh_unadopted_emits_single_json_no_launch_no_mutation(self):
        rc, out, launch, apply_mock, resume_mock = self._run_json(
            classify_adoption=mock.Mock(return_value=AdoptionStatus("absent", _ROOT)),
            inspect_onboarding=mock.Mock(return_value=_inspection("unadopted")),
        )
        self.assertEqual(rc, 1)
        obj = json.loads(out)  # exactly one JSON object, parseable
        self.assertEqual(obj["error"], "interactive_onboarding_required")
        self.assertEqual(launch.calls, 0)
        apply_mock.assert_not_called()

    def test_in_progress_emits_json_no_resume(self):
        rc, out, launch, apply_mock, resume_mock = self._run_json(
            classify_adoption=mock.Mock(
                return_value=AdoptionStatus(ADOPTION_IN_PROGRESS, _ROOT)),
        )
        self.assertEqual(rc, 1)
        obj = json.loads(out)
        self.assertEqual(obj["error"], "interactive_onboarding_required")
        self.assertEqual(obj["state"], "adoption_in_progress")
        resume_mock.assert_not_called()
        self.assertEqual(launch.calls, 0)

    def test_broken_emits_json(self):
        rc, out, launch, apply_mock, resume_mock = self._run_json(
            classify_adoption=mock.Mock(
                return_value=AdoptionStatus(ADOPTION_BROKEN, _ROOT, reason="bad")),
        )
        self.assertEqual(rc, 1)
        obj = json.loads(out)
        self.assertEqual(obj["error"], "onboarding_blocked")
        self.assertEqual(launch.calls, 0)

    def test_blocked_emits_json(self):
        rc, out, launch, apply_mock, resume_mock = self._run_json(
            classify_adoption=mock.Mock(return_value=AdoptionStatus("absent", _ROOT)),
            inspect_onboarding=mock.Mock(
                return_value=_inspection("blocked", reasons=("home root",))),
        )
        self.assertEqual(rc, 1)
        obj = json.loads(out)
        self.assertEqual(obj["state"], "blocked")
        self.assertEqual(obj["error"], "onboarding_blocked")


if __name__ == "__main__":
    unittest.main()
