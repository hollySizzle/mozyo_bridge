"""Fake-port / pure-policy specifications for the doctor Claude-Nagger boundary
(#12859).

These exercise the ``doctor_claude_nagger`` verdict authority and nagger read
port directly, with a synthetic read-view — without a real scaffolded checkout,
without writing ``.claude-nagger/`` skeleton files, and without parsing a real
``.mozyo-bridge`` manifest. They are the checkout-topology -> fake-port /
fake-policy migration for the ``claude_nagger`` section slice.
"""

from __future__ import annotations

import argparse
import unittest
from pathlib import Path
from typing import Any

from mozyo_bridge.application import doctor
from mozyo_bridge.application.doctor_claude_nagger import (
    ClaudeNaggerReads,
    ClaudeNaggerSectionUseCase,
    ClaudeNaggerSectionVerdict,
    LiveClaudeNaggerReads,
    evaluate_claude_nagger_section,
)

_EXAMPLE_NAMES = (
    "config.yaml.example",
    "command_conventions.yaml.example",
    "mcp_conventions.yaml.example",
)


def _examples(*, missing: tuple[str, ...] = (), root: str = "/checkout/.claude-nagger") -> dict[str, Any]:
    return {
        name: {
            "path": f"{root}/{name}",
            "present": name not in missing,
        }
        for name in _EXAMPLE_NAMES
    }


def _view(
    *,
    manifest_tracks_nagger: bool,
    examples: dict[str, Any] | None = None,
    config_present: bool = False,
    target: str = "/checkout",
    root: str = "/checkout/.claude-nagger",
) -> dict[str, Any]:
    return {
        "target": target,
        "nagger_dir": root,
        "manifest_tracks_nagger": manifest_tracks_nagger,
        "examples": examples if examples is not None else _examples(root=root),
        "config_yaml": {
            "path": f"{root}/config.yaml",
            "present": config_present,
        },
    }


class FakeClaudeNaggerReads:
    """In-memory fake of the ``ClaudeNaggerReads`` port."""

    def __init__(self, view: dict[str, Any]) -> None:
        self._view = view
        self.calls = 0

    def describe(self) -> dict[str, Any]:
        self.calls += 1
        return self._view


class EvaluateClaudeNaggerSectionPolicyTest(unittest.TestCase):
    def test_untracked_manifest_is_skipped_with_opt_in_guidance(self) -> None:
        verdict = evaluate_claude_nagger_section(
            _view(manifest_tracks_nagger=False)
        )
        self.assertEqual("skipped", verdict.status)
        self.assertEqual(
            (
                "Claude Nagger is opt-out (manifest does not track .claude-nagger/); "
                "rerun `mozyo-bridge scaffold apply <preset> --target /checkout` "
                "without --skip-nagger to install the skeleton",
            ),
            verdict.next_action,
        )

    def test_untracked_ignores_present_examples_on_disk(self) -> None:
        # Even with every example file present on disk (e.g. leftover after a
        # `--skip-nagger --backup` opt-out), the manifest source-of-truth wins:
        # the section stays `skipped`, never `ok` / `skeleton-only`.
        verdict = evaluate_claude_nagger_section(
            _view(manifest_tracks_nagger=False, config_present=True)
        )
        self.assertEqual("skipped", verdict.status)

    def test_tracked_missing_examples_is_incomplete_with_per_file_restore(self) -> None:
        verdict = evaluate_claude_nagger_section(
            _view(
                manifest_tracks_nagger=True,
                examples=_examples(
                    missing=("command_conventions.yaml.example",),
                ),
            )
        )
        self.assertEqual("incomplete", verdict.status)
        self.assertEqual(
            (
                "missing /checkout/.claude-nagger/command_conventions.yaml.example; "
                "rerun scaffold apply --backup to restore",
            ),
            verdict.next_action,
        )

    def test_tracked_multiple_missing_examples_preserve_skeleton_order(self) -> None:
        verdict = evaluate_claude_nagger_section(
            _view(
                manifest_tracks_nagger=True,
                examples=_examples(
                    missing=(
                        "mcp_conventions.yaml.example",
                        "config.yaml.example",
                    ),
                ),
            )
        )
        self.assertEqual("incomplete", verdict.status)
        # next_action follows the skeleton iteration order, not the `missing` arg.
        self.assertEqual(
            (
                "missing /checkout/.claude-nagger/config.yaml.example; "
                "rerun scaffold apply --backup to restore",
                "missing /checkout/.claude-nagger/mcp_conventions.yaml.example; "
                "rerun scaffold apply --backup to restore",
            ),
            verdict.next_action,
        )

    def test_tracked_all_present_and_config_active_is_ok(self) -> None:
        verdict = evaluate_claude_nagger_section(
            _view(manifest_tracks_nagger=True, config_present=True)
        )
        self.assertEqual(
            ClaudeNaggerSectionVerdict(status="ok"), verdict
        )

    def test_tracked_all_present_without_config_is_skeleton_only(self) -> None:
        verdict = evaluate_claude_nagger_section(
            _view(manifest_tracks_nagger=True, config_present=False)
        )
        self.assertEqual("skeleton-only", verdict.status)
        self.assertEqual(
            (
                "copy /checkout/.claude-nagger/config.yaml.example to "
                "/checkout/.claude-nagger/config.yaml to activate Claude Nagger",
            ),
            verdict.next_action,
        )

    def test_incomplete_takes_precedence_over_missing_config(self) -> None:
        # A missing example with no config still resolves to `incomplete`, not
        # `skeleton-only`: the example-presence branch is checked first.
        verdict = evaluate_claude_nagger_section(
            _view(
                manifest_tracks_nagger=True,
                examples=_examples(missing=("config.yaml.example",)),
                config_present=False,
            )
        )
        self.assertEqual("incomplete", verdict.status)


class ClaudeNaggerSectionUseCaseTest(unittest.TestCase):
    def test_use_case_assembles_legacy_section_dict(self) -> None:
        view = _view(manifest_tracks_nagger=True, config_present=True)
        reads = FakeClaudeNaggerReads(view)

        section = ClaudeNaggerSectionUseCase(reads).execute()

        self.assertEqual(
            {
                "status": "ok",
                "target": "/checkout",
                "nagger_dir": "/checkout/.claude-nagger",
                "manifest_tracks_nagger": True,
                "examples": view["examples"],
                "config_yaml": view["config_yaml"],
                "next_action": [],
            },
            section,
        )
        # The legacy collector key insertion order.
        self.assertEqual(
            [
                "status",
                "target",
                "nagger_dir",
                "manifest_tracks_nagger",
                "examples",
                "config_yaml",
                "next_action",
            ],
            list(section.keys()),
        )
        self.assertIs(view["examples"], section["examples"])
        self.assertIs(view["config_yaml"], section["config_yaml"])
        self.assertIsInstance(section["next_action"], list)
        self.assertEqual(1, reads.calls)

    def test_use_case_carries_verdict_next_action_for_skipped(self) -> None:
        section = ClaudeNaggerSectionUseCase(
            FakeClaudeNaggerReads(_view(manifest_tracks_nagger=False))
        ).execute()
        self.assertEqual("skipped", section["status"])
        self.assertEqual(1, len(section["next_action"]))

    def test_live_adapter_satisfies_the_port_protocol(self) -> None:
        self.assertIsInstance(LiveClaudeNaggerReads(object()), ClaudeNaggerReads)


class LiveClaudeNaggerReadsTest(unittest.TestCase):
    """The live adapter resolves ``doctor_target`` / the ``CLAUDE_NAGGER_*``
    constants / ``_scaffold_manifest_files`` through the ``doctor`` module at
    call time."""

    def test_describe_routes_through_doctor_helpers_at_call_time(self) -> None:
        captured: dict[str, Any] = {}
        target = Path("/checkout/repo")
        args = object()

        def fake_target(passed_args: Any) -> Path:
            captured["target_args"] = passed_args
            return target

        def fake_manifest_files(passed_target: Path) -> set[str]:
            captured["manifest_target"] = passed_target
            return {".claude-nagger/config.yaml.example", "AGENTS.md"}

        original_target = doctor.doctor_target
        original_manifest = doctor._scaffold_manifest_files
        doctor.doctor_target = fake_target
        doctor._scaffold_manifest_files = fake_manifest_files
        try:
            view = LiveClaudeNaggerReads(args).describe()
        finally:
            doctor.doctor_target = original_target
            doctor._scaffold_manifest_files = original_manifest

        self.assertIs(args, captured["target_args"])
        self.assertEqual(target, captured["manifest_target"])
        self.assertEqual(str(target), view["target"])
        self.assertEqual(
            str(target / doctor.CLAUDE_NAGGER_DIRNAME), view["nagger_dir"]
        )
        self.assertTrue(view["manifest_tracks_nagger"])
        # The example set mirrors the doctor constant, in order, with disk
        # presence flags resolved at call time (none of these exist on disk).
        self.assertEqual(
            list(doctor.CLAUDE_NAGGER_EXAMPLES), list(view["examples"].keys())
        )
        for name in doctor.CLAUDE_NAGGER_EXAMPLES:
            self.assertEqual(
                str(target / doctor.CLAUDE_NAGGER_DIRNAME / name),
                view["examples"][name]["path"],
            )
            self.assertFalse(view["examples"][name]["present"])
        self.assertEqual(
            str(target / doctor.CLAUDE_NAGGER_DIRNAME / "config.yaml"),
            view["config_yaml"]["path"],
        )
        self.assertFalse(view["config_yaml"]["present"])

    def test_describe_untracked_when_manifest_omits_nagger_prefix(self) -> None:
        target = Path("/checkout/repo")

        original_target = doctor.doctor_target
        original_manifest = doctor._scaffold_manifest_files
        doctor.doctor_target = lambda _args: target
        doctor._scaffold_manifest_files = lambda _target: {"AGENTS.md", "CLAUDE.md"}
        try:
            view = LiveClaudeNaggerReads(object()).describe()
        finally:
            doctor.doctor_target = original_target
            doctor._scaffold_manifest_files = original_manifest

        self.assertFalse(view["manifest_tracks_nagger"])


class DoctorCollectorDelegationTest(unittest.TestCase):
    """``doctor_claude_nagger_section`` is now a thin handler over the use case;
    it still routes through the live nagger read."""

    def test_collector_matches_use_case_over_live_read(self) -> None:
        args = argparse.Namespace(
            repo="/tmp/doctor-nagger-delegation-no-checkout", home=None
        )
        section = doctor.doctor_claude_nagger_section(args)
        expected = ClaudeNaggerSectionUseCase(LiveClaudeNaggerReads(args)).execute()
        self.assertEqual(expected, section)


if __name__ == "__main__":
    unittest.main()
