"""Fake-port / pure-policy specifications for the doctor health boundary (#12833).

These exercise the ``doctor_health`` verdict authority and section-orchestration
port directly, without monkeypatching the ``doctor.doctor_*_section`` collectors
or any ``commands.*`` doctor helper. They are the monkeypatch -> fake-port /
fake-policy migration for the ``run_doctor`` verdict slice.
"""

from __future__ import annotations

import argparse
import unittest
from typing import Any

from mozyo_bridge.application import doctor
from mozyo_bridge.application.doctor_health import (
    DoctorHealthVerdict,
    DoctorSectionReads,
    LiveDoctorSections,
    RunDoctorUseCase,
    UNHEALTHY_SECTION_STATUSES,
    evaluate_doctor_health,
)


def _section(status: str) -> dict[str, Any]:
    return {"status": status, "next_action": []}


class FakeDoctorSectionReads:
    """In-memory fake of the ``DoctorSectionReads`` port."""

    def __init__(self, sections: dict[str, dict[str, Any]]) -> None:
        self._sections = sections
        self.calls = 0

    def collect_sections(self) -> dict[str, dict[str, Any]]:
        self.calls += 1
        return self._sections


class EvaluateDoctorHealthPolicyTest(unittest.TestCase):
    def test_all_ok_sections_yield_ok_verdict(self) -> None:
        verdict = evaluate_doctor_health(
            {"cli": _section("ok"), "rules": _section("ok")}
        )
        self.assertEqual(DoctorHealthVerdict(ok=True, unhealthy_sections=()), verdict)

    def test_each_hard_bad_status_drags_verdict_down(self) -> None:
        for status in sorted(UNHEALTHY_SECTION_STATUSES):
            with self.subTest(status=status):
                verdict = evaluate_doctor_health(
                    {"cli": _section("ok"), "rules": _section(status)}
                )
                self.assertFalse(verdict.ok)
                self.assertEqual(("rules",), verdict.unhealthy_sections)

    def test_warning_status_is_not_ok(self) -> None:
        verdict = evaluate_doctor_health(
            {"cli": _section("ok"), "tmux": _section("warning")}
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(("tmux",), verdict.unhealthy_sections)

    def test_unhealthy_sections_preserve_collection_order(self) -> None:
        verdict = evaluate_doctor_health(
            {
                "cli": _section("ok"),
                "rules": _section("missing"),
                "scaffold": _section("ok"),
                "tmux": _section("warning"),
            }
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(("rules", "tmux"), verdict.unhealthy_sections)

    def test_missing_status_key_is_treated_as_healthy(self) -> None:
        # A section without a "status" key (e.g. a collector that only emits
        # informational fields) must not flip the verdict — mirrors the legacy
        # ``section.get("status")`` aggregation returning ``None``.
        verdict = evaluate_doctor_health({"cli": {"next_action": []}})
        self.assertTrue(verdict.ok)
        self.assertEqual((), verdict.unhealthy_sections)


class RunDoctorUseCaseTest(unittest.TestCase):
    def test_use_case_returns_legacy_result_shape(self) -> None:
        sections = {"cli": _section("ok"), "rules": _section("ok")}
        reads = FakeDoctorSectionReads(sections)

        result = RunDoctorUseCase(reads).execute()

        self.assertEqual({"ok": True, "sections": sections}, result)
        self.assertIs(sections, result["sections"])
        self.assertEqual(1, reads.calls)

    def test_use_case_reports_not_ok_when_a_section_is_unhealthy(self) -> None:
        sections = {"cli": _section("ok"), "scaffold": _section("drifted")}
        result = RunDoctorUseCase(FakeDoctorSectionReads(sections)).execute()

        self.assertFalse(result["ok"])
        self.assertEqual(sections, result["sections"])

    def test_live_adapter_satisfies_the_port_protocol(self) -> None:
        adapter = LiveDoctorSections(argparse.Namespace(repo="/repo"))
        self.assertIsInstance(adapter, DoctorSectionReads)


class RunDoctorThinHandlerTest(unittest.TestCase):
    """``run_doctor`` is now a thin handler over the use case; the live adapter
    still routes through the ``doctor`` module section collectors at call time."""

    def test_run_doctor_aggregates_collector_statuses(self) -> None:
        ok_stub = _section("ok")
        bad_stub = _section("missing")
        collectors = {
            "doctor_tmux_section": ok_stub,
            "doctor_tmux_ui_artifact_info": {"status": "ok"},
            "doctor_cli_section": ok_stub,
            "doctor_rules_section": bad_stub,
            "doctor_codex_skill_section": ok_stub,
            "doctor_claude_skill_section": ok_stub,
            "doctor_scaffold_section": ok_stub,
            "doctor_workspace_registry_section": ok_stub,
            "doctor_state_store_section": ok_stub,
            "doctor_claude_nagger_section": ok_stub,
            "doctor_claude_launch_policy_section": ok_stub,
            "doctor_otel_section": ok_stub,
        }
        originals = {name: getattr(doctor, name) for name in collectors}
        try:
            for name, value in collectors.items():
                setattr(doctor, name, lambda *a, _v=value, **k: dict(_v))
            # doctor_target / doctor_home are real and harmless on a Namespace.
            result = doctor.run_doctor(argparse.Namespace(repo="/repo", home=None))
        finally:
            for name, value in originals.items():
                setattr(doctor, name, value)

        self.assertFalse(result["ok"])
        self.assertEqual("missing", result["sections"]["rules"]["status"])
        self.assertIn("artifact", result["sections"]["tmux"])


if __name__ == "__main__":
    unittest.main()
