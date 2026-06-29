"""Fake-port / pure-policy specifications for the doctor claude-skill boundary (#12843).

These exercise the ``doctor_claude_skill`` verdict authority and Claude skill
read port directly, with a synthetic skill read-view — without patching
``MOZYO_BRIDGE_CLAUDE_HOME`` / ``MOZYO_BRIDGE_CLAUDE_PROJECT_DIR`` and without
seeding a real temp-dir skill install. They are the env-patch / filesystem-seed
-> fake-port / fake-policy migration for the ``claude_skill`` section slice.
"""

from __future__ import annotations

import argparse
import unittest
from typing import Any

from mozyo_bridge.application import doctor
from mozyo_bridge.application.doctor_claude_skill import (
    GLOBAL_SYNC_REFERENCES_HINT,
    PROJECT_SYNC_REFERENCES_HINT,
    ClaudeSkillReads,
    ClaudeSkillSectionUseCase,
    ClaudeSkillSectionVerdict,
    LiveClaudeSkillReads,
    evaluate_claude_skill_section,
    precedence_warning,
)


_INSTALL_HINT = "curl -fsSL https://example.invalid/install_claude_skill.sh | sh"


def _skill_dir_info(
    *,
    present: bool,
    references_missing: list[str] | None = None,
    path: str = "/home/op/.claude/skills/mozyo-bridge-agent",
) -> dict[str, Any]:
    return {
        "present": present,
        "path": path,
        "skill_md": path + "/SKILL.md",
        "references_missing": references_missing or [],
    }


def _plugin_info(
    *, present: bool, versions: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "present": present,
        "root": "/home/op/.claude/plugins/cache/mozyo-bridge/mozyo-bridge-agent",
        "versions": versions or [],
    }


def _skill_view(
    *,
    global_info: dict[str, Any],
    project_info: dict[str, Any],
    plugin_info: dict[str, Any],
    global_home: str = "/home/op/.claude",
    project_dir: str = "/work/project",
    install_hint: str = _INSTALL_HINT,
) -> dict[str, Any]:
    return {
        "global_home": global_home,
        "global": global_info,
        "project_dir": project_dir,
        "project": project_info,
        "plugin": plugin_info,
        "install_hint": install_hint,
    }


class FakeClaudeSkillReads:
    """In-memory fake of the ``ClaudeSkillReads`` port."""

    def __init__(self, view: dict[str, Any]) -> None:
        self._view = view
        self.calls = 0

    def describe(self) -> dict[str, Any]:
        self.calls += 1
        return self._view


class EvaluateClaudeSkillSectionPolicyTest(unittest.TestCase):
    def test_nothing_installed_is_missing_with_install_hint(self) -> None:
        verdict = evaluate_claude_skill_section(
            _skill_view(
                global_info=_skill_dir_info(present=False),
                project_info=_skill_dir_info(present=False),
                plugin_info=_plugin_info(present=False),
            )
        )
        self.assertEqual(
            ClaudeSkillSectionVerdict(
                status="missing", next_action=(_INSTALL_HINT,)
            ),
            verdict,
        )

    def test_plugin_only_is_plugin_managed_with_no_next_action(self) -> None:
        verdict = evaluate_claude_skill_section(
            _skill_view(
                global_info=_skill_dir_info(present=False),
                project_info=_skill_dir_info(present=False),
                plugin_info=_plugin_info(
                    present=True, versions=[{"version": "abc12345"}]
                ),
            )
        )
        self.assertEqual(
            ClaudeSkillSectionVerdict(status="plugin-managed"), verdict
        )

    def test_install_hint_is_sourced_from_the_read_view(self) -> None:
        # The missing guidance is threaded through the read view, not hard-coded
        # in the policy, so a different install hint flows through.
        other = "OTHER-HINT"
        verdict = evaluate_claude_skill_section(
            _skill_view(
                global_info=_skill_dir_info(present=False),
                project_info=_skill_dir_info(present=False),
                plugin_info=_plugin_info(present=False),
                install_hint=other,
            )
        )
        self.assertEqual((other,), verdict.next_action)

    def test_global_missing_references_is_incomplete_with_global_hint(self) -> None:
        verdict = evaluate_claude_skill_section(
            _skill_view(
                global_info=_skill_dir_info(
                    present=True, references_missing=["workflow.md"]
                ),
                project_info=_skill_dir_info(present=False),
                plugin_info=_plugin_info(present=False),
            )
        )
        self.assertEqual("incomplete", verdict.status)
        self.assertEqual((GLOBAL_SYNC_REFERENCES_HINT,), verdict.next_action)

    def test_project_only_missing_references_is_incomplete_with_project_hint(
        self,
    ) -> None:
        verdict = evaluate_claude_skill_section(
            _skill_view(
                global_info=_skill_dir_info(present=False),
                project_info=_skill_dir_info(
                    present=True, references_missing=["safety.md"]
                ),
                plugin_info=_plugin_info(present=False),
            )
        )
        self.assertEqual("incomplete", verdict.status)
        self.assertEqual((PROJECT_SYNC_REFERENCES_HINT,), verdict.next_action)

    def test_global_and_project_present_collision_is_warning(self) -> None:
        verdict = evaluate_claude_skill_section(
            _skill_view(
                global_info=_skill_dir_info(
                    present=True, path="/home/op/.claude/skills/mozyo-bridge-agent"
                ),
                project_info=_skill_dir_info(
                    present=True, path="/work/project/.claude/skills/mozyo-bridge-agent"
                ),
                plugin_info=_plugin_info(present=False),
            )
        )
        self.assertEqual("warning", verdict.status)
        self.assertEqual(
            (
                precedence_warning(
                    "/home/op/.claude/skills/mozyo-bridge-agent",
                    "/work/project/.claude/skills/mozyo-bridge-agent",
                ),
            ),
            verdict.warnings,
        )
        # The precedence collision is a warning, not an actionable next step.
        self.assertEqual((), verdict.next_action)

    def test_global_incomplete_takes_precedence_over_collision_warning(self) -> None:
        # When the global skill is present-but-thin AND the project skill is
        # present, the incomplete branch wins the status while the precedence
        # warning is still surfaced (legacy order: warnings computed first).
        verdict = evaluate_claude_skill_section(
            _skill_view(
                global_info=_skill_dir_info(
                    present=True, references_missing=["release.md"]
                ),
                project_info=_skill_dir_info(present=True),
                plugin_info=_plugin_info(present=False),
            )
        )
        self.assertEqual("incomplete", verdict.status)
        self.assertEqual((GLOBAL_SYNC_REFERENCES_HINT,), verdict.next_action)
        self.assertEqual(1, len(verdict.warnings))

    def test_complete_global_skill_is_ok(self) -> None:
        verdict = evaluate_claude_skill_section(
            _skill_view(
                global_info=_skill_dir_info(present=True),
                project_info=_skill_dir_info(present=False),
                plugin_info=_plugin_info(present=False),
            )
        )
        self.assertEqual(ClaudeSkillSectionVerdict(status="ok"), verdict)


class ClaudeSkillSectionUseCaseTest(unittest.TestCase):
    def test_use_case_assembles_legacy_ok_section_dict(self) -> None:
        view = _skill_view(
            global_info=_skill_dir_info(present=True),
            project_info=_skill_dir_info(present=False),
            plugin_info=_plugin_info(present=False),
        )
        reads = FakeClaudeSkillReads(view)

        section = ClaudeSkillSectionUseCase(reads).execute()

        self.assertEqual(
            {
                "status": "ok",
                "global_home": view["global_home"],
                "global": view["global"],
                "project_dir": view["project_dir"],
                "project": view["project"],
                "plugin": view["plugin"],
                "warnings": [],
                "next_action": [],
            },
            section,
        )
        self.assertEqual(1, reads.calls)

    def test_use_case_propagates_missing_install_next_action(self) -> None:
        view = _skill_view(
            global_info=_skill_dir_info(present=False),
            project_info=_skill_dir_info(present=False),
            plugin_info=_plugin_info(present=False),
        )
        section = ClaudeSkillSectionUseCase(FakeClaudeSkillReads(view)).execute()

        self.assertEqual("missing", section["status"])
        self.assertEqual([_INSTALL_HINT], section["next_action"])
        self.assertIsInstance(section["next_action"], list)
        self.assertIsInstance(section["warnings"], list)

    def test_use_case_propagates_plugin_managed_versions(self) -> None:
        view = _skill_view(
            global_info=_skill_dir_info(present=False),
            project_info=_skill_dir_info(present=False),
            plugin_info=_plugin_info(
                present=True, versions=[{"version": "abc12345"}]
            ),
        )
        section = ClaudeSkillSectionUseCase(FakeClaudeSkillReads(view)).execute()

        self.assertEqual("plugin-managed", section["status"])
        self.assertEqual([], section["next_action"])
        self.assertTrue(section["plugin"]["present"])

    def test_use_case_surfaces_collision_warning_list(self) -> None:
        view = _skill_view(
            global_info=_skill_dir_info(
                present=True, path="/g/skills/mozyo-bridge-agent"
            ),
            project_info=_skill_dir_info(
                present=True, path="/p/skills/mozyo-bridge-agent"
            ),
            plugin_info=_plugin_info(present=False),
        )
        section = ClaudeSkillSectionUseCase(FakeClaudeSkillReads(view)).execute()

        self.assertEqual("warning", section["status"])
        self.assertEqual(
            [
                precedence_warning(
                    "/g/skills/mozyo-bridge-agent", "/p/skills/mozyo-bridge-agent"
                )
            ],
            section["warnings"],
        )

    def test_live_adapter_satisfies_the_port_protocol(self) -> None:
        self.assertIsInstance(
            LiveClaudeSkillReads(argparse.Namespace(repo=None)), ClaudeSkillReads
        )


class DoctorCollectorDelegationTest(unittest.TestCase):
    """``doctor_claude_skill_section`` is now a thin handler over the use case;
    it still routes through the live Claude skill reads."""

    def test_collector_matches_use_case_over_live_read(self) -> None:
        args = argparse.Namespace(repo=None)
        section = doctor.doctor_claude_skill_section(args)
        expected = ClaudeSkillSectionUseCase(LiveClaudeSkillReads(args)).execute()
        self.assertEqual(expected, section)
        self.assertIn(
            section["status"],
            {"missing", "incomplete", "warning", "plugin-managed", "ok"},
        )


if __name__ == "__main__":
    unittest.main()
