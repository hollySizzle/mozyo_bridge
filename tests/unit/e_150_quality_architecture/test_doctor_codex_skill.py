"""Fake-port / pure-policy specifications for the doctor codex-skill boundary (#12836).

These exercise the ``doctor_codex_skill`` verdict authority and Codex skill read
port directly, with a synthetic skill read-view — without patching ``CODEX_HOME``
and without seeding a real temp-dir skill install. They are the env-patch /
filesystem-seed -> fake-port / fake-policy migration for the ``codex_skill``
section slice.
"""

from __future__ import annotations

import unittest
from typing import Any

from mozyo_bridge.application import doctor
from mozyo_bridge.application.doctor_codex_skill import (
    SYNC_REFERENCES_HINT,
    CodexSkillReads,
    CodexSkillSectionUseCase,
    CodexSkillSectionVerdict,
    LiveCodexSkillReads,
    evaluate_codex_skill_section,
)


_INSTALL_HINT = "curl -fsSL https://example.invalid/install_codex_skill.sh | sh"


def _skill_view(
    *,
    present: bool,
    references_missing: list[str],
    home: str = "/home/op/.codex",
    skill_dir: str = "/home/op/.codex/skills/mozyo-bridge-agent",
    skill_md: str = "/home/op/.codex/skills/mozyo-bridge-agent/SKILL.md",
    install_hint: str = _INSTALL_HINT,
) -> dict[str, Any]:
    return {
        "home": home,
        "skill_dir": skill_dir,
        "skill_md": skill_md,
        "present": present,
        "references_missing": references_missing,
        "install_hint": install_hint,
    }


class FakeCodexSkillReads:
    """In-memory fake of the ``CodexSkillReads`` port."""

    def __init__(self, view: dict[str, Any]) -> None:
        self._view = view
        self.calls = 0

    def describe(self) -> dict[str, Any]:
        self.calls += 1
        return self._view


class EvaluateCodexSkillSectionPolicyTest(unittest.TestCase):
    def test_missing_skill_is_missing_with_install_hint(self) -> None:
        verdict = evaluate_codex_skill_section(
            _skill_view(present=False, references_missing=["workflow.md"])
        )
        self.assertEqual(
            CodexSkillSectionVerdict(status="missing", next_action=(_INSTALL_HINT,)),
            verdict,
        )

    def test_missing_references_is_incomplete_with_sync_hint(self) -> None:
        verdict = evaluate_codex_skill_section(
            _skill_view(present=True, references_missing=["safety.md"])
        )
        self.assertEqual("incomplete", verdict.status)
        self.assertEqual((SYNC_REFERENCES_HINT,), verdict.next_action)

    def test_complete_skill_is_ok_with_no_next_action(self) -> None:
        verdict = evaluate_codex_skill_section(
            _skill_view(present=True, references_missing=[])
        )
        self.assertEqual(CodexSkillSectionVerdict(status="ok"), verdict)

    def test_install_hint_is_sourced_from_the_read_view(self) -> None:
        # The missing-skill guidance is threaded through the read view, not
        # hard-coded in the policy, so a different install hint flows through.
        other = "OTHER-HINT"
        verdict = evaluate_codex_skill_section(
            _skill_view(present=False, references_missing=[], install_hint=other)
        )
        self.assertEqual((other,), verdict.next_action)


class CodexSkillSectionUseCaseTest(unittest.TestCase):
    def test_use_case_assembles_legacy_ok_section_dict(self) -> None:
        view = _skill_view(present=True, references_missing=[])
        reads = FakeCodexSkillReads(view)

        section = CodexSkillSectionUseCase(reads).execute()

        self.assertEqual(
            {
                "status": "ok",
                "home": view["home"],
                "skill_dir": view["skill_dir"],
                "skill_md": view["skill_md"],
                "present": True,
                "references_missing": [],
                "next_action": [],
            },
            section,
        )
        self.assertEqual(1, reads.calls)

    def test_use_case_propagates_missing_install_next_action(self) -> None:
        view = _skill_view(present=False, references_missing=["workflow.md"])
        section = CodexSkillSectionUseCase(FakeCodexSkillReads(view)).execute()

        self.assertEqual("missing", section["status"])
        self.assertEqual([_INSTALL_HINT], section["next_action"])
        self.assertIsInstance(section["next_action"], list)
        self.assertFalse(section["present"])

    def test_use_case_propagates_incomplete_reference_list(self) -> None:
        view = _skill_view(present=True, references_missing=["release.md"])
        section = CodexSkillSectionUseCase(FakeCodexSkillReads(view)).execute()

        self.assertEqual("incomplete", section["status"])
        self.assertEqual(["release.md"], section["references_missing"])
        self.assertEqual([SYNC_REFERENCES_HINT], section["next_action"])

    def test_live_adapter_satisfies_the_port_protocol(self) -> None:
        self.assertIsInstance(LiveCodexSkillReads(), CodexSkillReads)


class DoctorCollectorDelegationTest(unittest.TestCase):
    """``doctor_codex_skill_section`` is now a thin handler over the use case;
    it still routes through the live Codex skill read."""

    def test_collector_matches_use_case_over_live_read(self) -> None:
        section = doctor.doctor_codex_skill_section()
        expected = CodexSkillSectionUseCase(LiveCodexSkillReads()).execute()
        self.assertEqual(expected, section)
        self.assertIn(
            section["status"], {"missing", "incomplete", "ok"}
        )


if __name__ == "__main__":
    unittest.main()
