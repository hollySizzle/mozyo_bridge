"""Fake-port / pure-policy specifications for the doctor tmux-UI artifact
boundary (#12866).

These exercise the ``doctor_tmux_ui`` verdict authority and tmux-ui read port
directly, with a synthetic read-view — without a real scaffolded checkout,
without writing the ``.mozyo-bridge/tmux/agent-ui.conf`` snippet, without
parsing a real ``.mozyo-bridge`` manifest, and without a real host
``~/.tmux.conf`` wiring topology. They are the checkout-topology -> fake-port /
fake-policy migration for the ``tmux`` section's ``artifact`` sub-record.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from mozyo_bridge.application import doctor
from mozyo_bridge.application import tmux_ui
from mozyo_bridge.application.doctor_tmux_ui import (
    LiveTmuxUiArtifactReads,
    TmuxUiArtifactReads,
    TmuxUiArtifactSectionUseCase,
    TmuxUiArtifactVerdict,
    evaluate_tmux_ui_artifact_section,
)


def _host_wiring(
    *,
    state: str = "installed",
    drift_reason: str | None = None,
    tmux_conf: str = "/home/op/.tmux.conf",
    tmux_conf_exists: bool = True,
    current_source_path: str | None = "/checkout/.mozyo-bridge/tmux/agent-ui.conf",
    expected_snippet: str = "/checkout/.mozyo-bridge/tmux/agent-ui.conf",
) -> dict[str, Any]:
    return {
        "state": state,
        "tmux_conf": tmux_conf,
        "tmux_conf_exists": tmux_conf_exists,
        "current_source_path": current_source_path,
        "expected_snippet": expected_snippet,
        "drift_reason": drift_reason,
    }


def _view(
    *,
    manifest_tracks_tmux_ui: bool,
    present: bool = True,
    target: str = "/checkout",
    snippet_path: str = "/checkout/.mozyo-bridge/tmux/agent-ui.conf",
    host_wiring: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "target": target,
        "snippet_path": snippet_path,
        "present": present,
        "manifest_tracks_tmux_ui": manifest_tracks_tmux_ui,
        "host_wiring": host_wiring if host_wiring is not None else _host_wiring(),
        "state_not_installed": tmux_ui.STATE_NOT_INSTALLED,
        "state_drift": tmux_ui.STATE_DRIFT,
    }


class FakeTmuxUiArtifactReads:
    """In-memory fake of the ``TmuxUiArtifactReads`` port."""

    def __init__(self, view: dict[str, Any]) -> None:
        self._view = view
        self.calls = 0

    def describe(self) -> dict[str, Any]:
        self.calls += 1
        return self._view


class EvaluateTmuxUiArtifactSectionPolicyTest(unittest.TestCase):
    def test_untracked_manifest_is_skipped_with_opt_in_guidance(self) -> None:
        verdict = evaluate_tmux_ui_artifact_section(
            _view(manifest_tracks_tmux_ui=False)
        )
        self.assertEqual("skipped", verdict.status)
        self.assertEqual(
            (
                "tmux UI helper is opt-out (manifest does not track agent-ui.conf); "
                "rerun `mozyo-bridge scaffold apply <preset> --target /checkout` "
                "without --skip-tmux-ui to install the snippet",
            ),
            verdict.next_action,
        )

    def test_untracked_ignores_present_snippet_on_disk(self) -> None:
        # Even with the snippet present on disk (e.g. leftover after a
        # `--skip-tmux-ui --backup` opt-out), the manifest source-of-truth wins:
        # the section stays `skipped`, never `ok` / `incomplete`.
        verdict = evaluate_tmux_ui_artifact_section(
            _view(manifest_tracks_tmux_ui=False, present=True)
        )
        self.assertEqual("skipped", verdict.status)

    def test_untracked_emits_no_host_wiring_action(self) -> None:
        # Host wiring is gated on tracked AND present; an opt-out never nags
        # about wiring even when the host config is not installed.
        verdict = evaluate_tmux_ui_artifact_section(
            _view(
                manifest_tracks_tmux_ui=False,
                host_wiring=_host_wiring(state=tmux_ui.STATE_NOT_INSTALLED),
            )
        )
        self.assertEqual((), verdict.host_wiring_next_action)

    def test_tracked_present_is_ok(self) -> None:
        verdict = evaluate_tmux_ui_artifact_section(
            _view(manifest_tracks_tmux_ui=True, present=True)
        )
        self.assertEqual("ok", verdict.status)
        self.assertEqual((), verdict.next_action)

    def test_tracked_missing_snippet_is_incomplete_with_restore(self) -> None:
        verdict = evaluate_tmux_ui_artifact_section(
            _view(manifest_tracks_tmux_ui=True, present=False)
        )
        self.assertEqual("incomplete", verdict.status)
        self.assertEqual(
            (
                "manifest tracks /checkout/.mozyo-bridge/tmux/agent-ui.conf "
                "but the file is missing; rerun scaffold apply --backup to restore",
            ),
            verdict.next_action,
        )

    def test_tracked_missing_snippet_emits_no_host_wiring_action(self) -> None:
        # `incomplete` (tracked but snippet gone) skips host-wiring guidance:
        # there is nothing on disk to source.
        verdict = evaluate_tmux_ui_artifact_section(
            _view(
                manifest_tracks_tmux_ui=True,
                present=False,
                host_wiring=_host_wiring(state=tmux_ui.STATE_NOT_INSTALLED),
            )
        )
        self.assertEqual((), verdict.host_wiring_next_action)

    def test_tracked_present_not_installed_host_yields_wire_it_guidance(self) -> None:
        verdict = evaluate_tmux_ui_artifact_section(
            _view(
                manifest_tracks_tmux_ui=True,
                present=True,
                host_wiring=_host_wiring(state=tmux_ui.STATE_NOT_INSTALLED),
            )
        )
        self.assertEqual("ok", verdict.status)
        self.assertEqual(
            (
                "host tmux config does not source agent-ui.conf; run "
                "`mozyo-bridge tmux-ui install --target /checkout` to wire it",
            ),
            verdict.host_wiring_next_action,
        )

    def test_tracked_present_drift_host_yields_refresh_guidance(self) -> None:
        verdict = evaluate_tmux_ui_artifact_section(
            _view(
                manifest_tracks_tmux_ui=True,
                present=True,
                host_wiring=_host_wiring(
                    state=tmux_ui.STATE_DRIFT,
                    drift_reason="points to /old/agent-ui.conf",
                ),
            )
        )
        self.assertEqual(
            (
                "host tmux config has a managed block pointing elsewhere "
                "(points to /old/agent-ui.conf); rerun "
                "`mozyo-bridge tmux-ui install --target /checkout --force` to refresh",
            ),
            verdict.host_wiring_next_action,
        )

    def test_tracked_present_installed_host_needs_no_wiring_action(self) -> None:
        verdict = evaluate_tmux_ui_artifact_section(
            _view(
                manifest_tracks_tmux_ui=True,
                present=True,
                host_wiring=_host_wiring(state=tmux_ui.STATE_INSTALLED),
            )
        )
        self.assertEqual((), verdict.host_wiring_next_action)


class TmuxUiArtifactSectionUseCaseTest(unittest.TestCase):
    def test_use_case_assembles_legacy_section_dict(self) -> None:
        host_wiring = _host_wiring(state=tmux_ui.STATE_INSTALLED)
        view = _view(
            manifest_tracks_tmux_ui=True, present=True, host_wiring=host_wiring
        )
        reads = FakeTmuxUiArtifactReads(view)

        section = TmuxUiArtifactSectionUseCase(reads).execute()

        self.assertEqual(
            {
                "status": "ok",
                "path": "/checkout/.mozyo-bridge/tmux/agent-ui.conf",
                "present": True,
                "manifest_tracks_tmux_ui": True,
                "host_wiring": {
                    "state": tmux_ui.STATE_INSTALLED,
                    "tmux_conf": host_wiring["tmux_conf"],
                    "tmux_conf_exists": host_wiring["tmux_conf_exists"],
                    "current_source_path": host_wiring["current_source_path"],
                    "expected_snippet": host_wiring["expected_snippet"],
                    "drift_reason": host_wiring["drift_reason"],
                    "next_action": [],
                },
                "next_action": [],
            },
            section,
        )
        # The legacy collector key insertion order — top level and the
        # host_wiring sub-record both matter for the JSON surface.
        self.assertEqual(
            [
                "status",
                "path",
                "present",
                "manifest_tracks_tmux_ui",
                "host_wiring",
                "next_action",
            ],
            list(section.keys()),
        )
        self.assertEqual(
            [
                "state",
                "tmux_conf",
                "tmux_conf_exists",
                "current_source_path",
                "expected_snippet",
                "drift_reason",
                "next_action",
            ],
            list(section["host_wiring"].keys()),
        )
        self.assertIsInstance(section["next_action"], list)
        self.assertIsInstance(section["host_wiring"]["next_action"], list)
        self.assertEqual(1, reads.calls)

    def test_use_case_carries_verdict_next_action_for_skipped(self) -> None:
        section = TmuxUiArtifactSectionUseCase(
            FakeTmuxUiArtifactReads(_view(manifest_tracks_tmux_ui=False))
        ).execute()
        self.assertEqual("skipped", section["status"])
        self.assertEqual(1, len(section["next_action"]))
        self.assertEqual([], section["host_wiring"]["next_action"])

    def test_use_case_carries_host_wiring_action(self) -> None:
        section = TmuxUiArtifactSectionUseCase(
            FakeTmuxUiArtifactReads(
                _view(
                    manifest_tracks_tmux_ui=True,
                    present=True,
                    host_wiring=_host_wiring(state=tmux_ui.STATE_NOT_INSTALLED),
                )
            )
        ).execute()
        self.assertEqual("ok", section["status"])
        self.assertEqual([], section["next_action"])
        self.assertEqual(1, len(section["host_wiring"]["next_action"]))

    def test_live_adapter_satisfies_the_port_protocol(self) -> None:
        self.assertIsInstance(
            LiveTmuxUiArtifactReads(Path("/checkout")), TmuxUiArtifactReads
        )


class LiveTmuxUiArtifactReadsTest(unittest.TestCase):
    """The live adapter resolves the ``TMUX_UI_*`` constants /
    ``_scaffold_manifest_files`` through the ``doctor`` module and the host
    wiring through ``tmux_ui`` at call time."""

    def test_describe_routes_through_doctor_and_tmux_ui_at_call_time(self) -> None:
        captured: dict[str, Any] = {}
        target = Path("/checkout/repo")
        snippet = target / doctor.TMUX_UI_RELATIVE_PATH
        host_conf = Path("/home/op/.tmux.conf")
        wiring = _host_wiring(
            state=tmux_ui.STATE_DRIFT,
            drift_reason="elsewhere",
            tmux_conf=str(host_conf),
            current_source_path="/old/agent-ui.conf",
            expected_snippet=str(snippet),
        )

        def fake_manifest_files(passed_target: Path) -> set[str]:
            captured["manifest_target"] = passed_target
            return {doctor.TMUX_UI_MANIFEST_PATH, "AGENTS.md"}

        def fake_host_conf() -> Path:
            return host_conf

        def fake_compute_status(repo_root: Path, tmux_conf: Path) -> dict[str, Any]:
            captured["compute_args"] = (repo_root, tmux_conf)
            return wiring

        original_manifest = doctor._scaffold_manifest_files
        original_host_conf = tmux_ui.default_host_tmux_conf
        original_compute = tmux_ui.compute_status
        doctor._scaffold_manifest_files = fake_manifest_files
        tmux_ui.default_host_tmux_conf = fake_host_conf
        tmux_ui.compute_status = fake_compute_status
        try:
            view = LiveTmuxUiArtifactReads(target).describe()
        finally:
            doctor._scaffold_manifest_files = original_manifest
            tmux_ui.default_host_tmux_conf = original_host_conf
            tmux_ui.compute_status = original_compute

        self.assertEqual(target, captured["manifest_target"])
        self.assertEqual((target, host_conf), captured["compute_args"])
        self.assertEqual(str(target), view["target"])
        self.assertEqual(str(snippet), view["snippet_path"])
        self.assertTrue(view["manifest_tracks_tmux_ui"])
        # The snippet does not exist on disk in this synthetic target.
        self.assertFalse(view["present"])
        self.assertEqual(tmux_ui.STATE_DRIFT, view["host_wiring"]["state"])
        self.assertEqual("elsewhere", view["host_wiring"]["drift_reason"])
        # The state literals are threaded through for the pure policy.
        self.assertEqual(tmux_ui.STATE_NOT_INSTALLED, view["state_not_installed"])
        self.assertEqual(tmux_ui.STATE_DRIFT, view["state_drift"])
        # The host_wiring read-view is narrowed to the six legacy keys.
        self.assertEqual(
            [
                "state",
                "tmux_conf",
                "tmux_conf_exists",
                "current_source_path",
                "expected_snippet",
                "drift_reason",
            ],
            list(view["host_wiring"].keys()),
        )

    def test_describe_untracked_when_manifest_omits_tmux_ui_path(self) -> None:
        target = Path("/checkout/repo")
        wiring = _host_wiring(state=tmux_ui.STATE_NOT_INSTALLED)

        original_manifest = doctor._scaffold_manifest_files
        original_host_conf = tmux_ui.default_host_tmux_conf
        original_compute = tmux_ui.compute_status
        doctor._scaffold_manifest_files = lambda _target: {"AGENTS.md", "CLAUDE.md"}
        tmux_ui.default_host_tmux_conf = lambda: Path("/home/op/.tmux.conf")
        tmux_ui.compute_status = lambda _root, _conf: wiring
        try:
            view = LiveTmuxUiArtifactReads(target).describe()
        finally:
            doctor._scaffold_manifest_files = original_manifest
            tmux_ui.default_host_tmux_conf = original_host_conf
            tmux_ui.compute_status = original_compute

        self.assertFalse(view["manifest_tracks_tmux_ui"])


class DoctorCollectorDelegationTest(unittest.TestCase):
    """``doctor_tmux_ui_artifact_info`` is now a thin handler over the use case;
    it still routes through the live tmux-ui read."""

    def test_collector_matches_use_case_over_live_read(self) -> None:
        target = Path("/tmp/doctor-tmux-ui-delegation-no-checkout")
        section = doctor.doctor_tmux_ui_artifact_info(target)
        expected = TmuxUiArtifactSectionUseCase(
            LiveTmuxUiArtifactReads(target)
        ).execute()
        self.assertEqual(expected, section)


if __name__ == "__main__":
    unittest.main()
