"""Fake-port / pure-policy specifications for the doctor rules boundary (#12844).

These exercise the ``doctor_rules`` verdict authority and rules-preset read port
directly, with a synthetic rules read-view — without installing real presets and
without seeding ``MOZYO_BRIDGE_HOME``. They are the install-seed / env-seed ->
fake-port / fake-policy migration for the ``rules`` section slice.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from mozyo_bridge.application import doctor
from mozyo_bridge.application.doctor_rules import (
    RULES_INSTALL_COMMAND,
    LiveRulesReads,
    RulesReads,
    RulesSectionUseCase,
    RulesSectionVerdict,
    evaluate_rules_section,
)


def _row(preset: str, status: str) -> dict[str, str]:
    return {
        "preset": preset,
        "status": status,
        "installed": "1.0.0" if status == "ok" else "-",
        "packaged": "1.0.0",
        "path": f"/home/op/.mozyo_bridge/rules/presets/{preset}/agent-workflow.md",
    }


def _rules_view(
    *,
    presets: list[dict[str, str]],
    home: str = "/home/op/.mozyo_bridge",
    install_command: str = RULES_INSTALL_COMMAND,
) -> dict[str, Any]:
    return {
        "presets": presets,
        "home": home,
        "install_command": install_command,
    }


class FakeRulesReads:
    """In-memory fake of the ``RulesReads`` port."""

    def __init__(self, view: dict[str, Any]) -> None:
        self._view = view
        self.calls = 0

    def describe(self) -> dict[str, Any]:
        self.calls += 1
        return self._view


class EvaluateRulesSectionPolicyTest(unittest.TestCase):
    def test_all_ok_presets_is_ok_with_no_next_action(self) -> None:
        verdict = evaluate_rules_section(
            _rules_view(presets=[_row("redmine", "ok"), _row("asana", "ok")])
        )
        self.assertEqual(RulesSectionVerdict(status="ok"), verdict)

    def test_any_missing_preset_is_missing_or_outdated_with_command(self) -> None:
        verdict = evaluate_rules_section(
            _rules_view(presets=[_row("redmine", "ok"), _row("asana", "missing")])
        )
        self.assertEqual(
            RulesSectionVerdict(
                status="missing-or-outdated",
                next_action=(RULES_INSTALL_COMMAND,),
            ),
            verdict,
        )

    def test_outdated_preset_is_missing_or_outdated(self) -> None:
        verdict = evaluate_rules_section(
            _rules_view(presets=[_row("redmine", "outdated")])
        )
        self.assertEqual("missing-or-outdated", verdict.status)
        self.assertEqual((RULES_INSTALL_COMMAND,), verdict.next_action)

    def test_install_command_is_sourced_from_the_read_view(self) -> None:
        # The install guidance is threaded through the read view (so the
        # adapter can qualify it with --home), not hard-coded in the policy.
        custom = "mozyo-bridge rules install --home /tmp/mb"
        verdict = evaluate_rules_section(
            _rules_view(
                presets=[_row("redmine", "missing")], install_command=custom
            )
        )
        self.assertEqual((custom,), verdict.next_action)


class RulesSectionUseCaseTest(unittest.TestCase):
    def test_use_case_assembles_legacy_ok_section_dict(self) -> None:
        rows = [_row("redmine", "ok"), _row("asana", "ok")]
        view = _rules_view(presets=rows)
        reads = FakeRulesReads(view)

        section = RulesSectionUseCase(reads).execute()

        self.assertEqual(
            {
                "status": "ok",
                "home": view["home"],
                "presets": rows,
                "next_action": [],
            },
            section,
        )
        self.assertEqual(1, reads.calls)

    def test_use_case_propagates_missing_install_next_action(self) -> None:
        custom = "mozyo-bridge rules install --home /tmp/mb"
        view = _rules_view(
            presets=[_row("redmine", "missing")], install_command=custom
        )
        section = RulesSectionUseCase(FakeRulesReads(view)).execute()

        self.assertEqual("missing-or-outdated", section["status"])
        self.assertEqual([custom], section["next_action"])
        self.assertIsInstance(section["next_action"], list)

    def test_live_adapter_satisfies_the_port_protocol(self) -> None:
        self.assertIsInstance(LiveRulesReads(None), RulesReads)


class LiveRulesReadsTest(unittest.TestCase):
    """The live adapter resolves ``rules_status`` at call time and builds the
    ``--home``-aware install command from the diagnosed home."""

    def test_custom_home_qualifies_the_install_command(self) -> None:
        home = Path("/tmp/mb-home")
        captured: dict[str, Any] = {}

        def fake_rules_status(arg: Path | None) -> list[dict[str, str]]:
            captured["arg"] = arg
            return [_row("redmine", "missing")]

        original = doctor.rules_status
        doctor.rules_status = fake_rules_status
        try:
            view = LiveRulesReads(home).describe()
        finally:
            doctor.rules_status = original

        self.assertEqual(home, captured["arg"])
        self.assertEqual(str(home), view["home"])
        self.assertEqual(
            f"{RULES_INSTALL_COMMAND} --home {home}", view["install_command"]
        )

    def test_default_home_uses_bare_install_command(self) -> None:
        def fake_rules_status(arg: Path | None) -> list[dict[str, str]]:
            return [_row("redmine", "ok")]

        original = doctor.rules_status
        doctor.rules_status = fake_rules_status
        try:
            view = LiveRulesReads(None).describe()
        finally:
            doctor.rules_status = original

        self.assertEqual(RULES_INSTALL_COMMAND, view["install_command"])


class DoctorCollectorDelegationTest(unittest.TestCase):
    """``doctor_rules_section`` is now a thin handler over the use case; it still
    routes through the live rules read."""

    def test_collector_matches_use_case_over_live_read(self) -> None:
        home = Path("/tmp/mb-home-delegation")
        section = doctor.doctor_rules_section(home)
        expected = RulesSectionUseCase(LiveRulesReads(home)).execute()
        self.assertEqual(expected, section)
        self.assertIn(section["status"], {"missing-or-outdated", "ok"})


if __name__ == "__main__":
    unittest.main()
