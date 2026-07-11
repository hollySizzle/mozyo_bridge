"""Session-transition routable-lane / runtime-fingerprint contract tests
(Redmine #13543, parent US #13535).

The transition bundle harness used to treat a `target lane` label as a live,
routable herdr lane and skipped the installed/source runtime fingerprint check,
so a fresh session mis-attributed a `sublanes: []` (lane-unregistered) lane to a
tmux candidate 空振り (#13535 j#75183). This module pins two things so the fix
cannot silently regress:

1. The session-continuity harness docs now carry the lane-state distinction
   (Git branch/worktree vs registered lane metadata vs live routable runtime)
   and the backend=herdr runtime-fingerprint gate, each in its authority doc.
2. The existing diagnostics the docs point at actually exist and behave as the
   contract claims: `doctor runtime` (#12612) fails closed on an installed/source
   probe drift, and `sublane list --lane` exposes the lane-metadata filter. These
   assertions clarify how the existing helpers are used, so a doc pointer to a
   non-existent / changed helper fails here rather than only in live operation.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))


class RoutableLaneStateContractDocsTest(unittest.TestCase):
    """Pin the lane-state / runtime-fingerprint contract across the harness docs.

    Each assertion names the surface so a regression failure points at one file.
    The contract 正本 is the spec; the logic / onboarding / herdr-ops docs point
    at it (入口の薄さ), so their markers are lighter but must still name the
    vocabulary a fresh session needs to avoid the #13543 mis-attribution.
    """

    def _doc(self, *parts: str) -> str:
        path = ROOT.joinpath(*parts)
        self.assertTrue(path.is_file(), f"missing doc: {path}")
        return path.read_text(encoding="utf-8")

    def test_spec_pins_lane_state_and_fingerprint_contract(self) -> None:
        body = self._doc(
            "vibes", "docs", "specs", "session-continuity-user-harness.md"
        )
        for marker in (
            # Lane-state distinction: three independent states, not one label.
            "### Routable lane state の区別",
            "git_branch_worktree",
            "registered_lane_metadata",
            "live_routable_runtime",
            # Routable verdict vocabulary (fail-closed, layer-named).
            "branch-only",
            "lane-unregistered",
            "runtime-unavailable",
            # Runtime fingerprint gate (backend=herdr) + the helpers it uses.
            "### Runtime fingerprint gate (backend=herdr)",
            "mozyo-bridge doctor runtime",
            "sublane list --lane",
            "agents targets",
        ):
            self.assertIn(
                marker,
                body,
                msg=(
                    f"spec-session-continuity-user-harness is missing lane-state / "
                    f"fingerprint marker {marker!r}; see Redmine #13543."
                ),
            )

    def test_session_boundary_logic_points_at_lane_state_contract(self) -> None:
        body = self._doc("vibes", "docs", "logics", "session-boundary.md")
        for marker in (
            "registered lane metadata",
            "branch-only / lane-unregistered / runtime-unavailable",
            "mozyo-bridge doctor runtime",
            "spec-session-continuity-user-harness",
        ):
            self.assertIn(
                marker,
                body,
                msg=(
                    f"logic-session-boundary §4 is missing lane-state marker "
                    f"{marker!r}; see Redmine #13543."
                ),
            )

    def test_onboarding_receipt_checks_lane_state_before_next_action(self) -> None:
        body = self._doc("vibes", "docs", "tasks", "new-session-onboarding.md")
        for marker in (
            "sublane list --lane",
            "mozyo-bridge doctor runtime",
            "lane-unregistered / branch-only",
            "agents targets",
        ):
            self.assertIn(
                marker,
                body,
                msg=(
                    f"task-new-session-onboarding receipt is missing lane-state "
                    f"marker {marker!r}; see Redmine #13543."
                ),
            )

    def test_herdr_lane_operations_has_fingerprint_runbook(self) -> None:
        body = self._doc("vibes", "docs", "tasks", "herdr-lane-operations.md")
        for marker in (
            "routable lane state の確認と runtime fingerprint",
            "sublane list --lane",
            "mozyo-bridge doctor runtime",
            "PYTHONPATH=src python3 -m mozyo_bridge",
            "lane-unregistered",
        ):
            self.assertIn(
                marker,
                body,
                msg=(
                    f"task-herdr-lane-operations is missing runbook marker "
                    f"{marker!r}; see Redmine #13543."
                ),
            )

    def test_received_stale_bundle_is_deleted(self) -> None:
        # The #13535 transition bundle was received (#13537 j#75168) and its
        # stale cleanup deferred to the next reviewable transition commit. Per
        # spec-session-continuity-user-harness stale-bundle lifecycle, that
        # commit must DELETE the received bundle, not keep a corrected live
        # pointer — the durable routable-lane correction already lives in the
        # permanent spec / herdr-ops runbook and #13535 j#75183. Deletion is the
        # expected end state; a re-appearing #13535 bundle is a lifecycle
        # regression (Redmine #13543 review j#75201 F1).
        path = ROOT / "vibes" / "docs" / "temps" / "session-handoff-13535.md"
        self.assertFalse(
            path.is_file(),
            msg=(
                "received #13535 transition bundle must be deleted in its next "
                "reviewable transition commit, not retained as a live pointer; "
                "the durable correction lives in the permanent spec / runbook and "
                "#13535 j#75183 (see Redmine #13543 j#75201 F1)."
            ),
        )


class ExistingDiagnosticUsageTest(unittest.TestCase):
    """Clarify how the existing diagnostics the docs point at are used.

    These are not new helpers: `doctor runtime` (#12612) and `sublane list
    --lane` already ship. The docs now direct a fresh session to them for the
    fingerprint gate and the registered-lane-metadata probe, so pin the behavior
    the contract relies on.
    """

    def test_doctor_runtime_fails_closed_on_installed_source_probe_drift(self) -> None:
        from mozyo_bridge.application.doctor_runtime import evaluate_fingerprint

        # Installed CLI and source report the same version, but the installed
        # surface lacks a gate-critical behavior the source ships — exactly the
        # herdr-preflight skew of #13543 (installed 0.10.0 without #13446).
        active = {
            "surface": "pipx",
            "version": "0.10.0",
            "package_path": "/opt/pipx/venvs/mozyo-bridge/lib/mozyo_bridge",
            "feature_probes": {"herdr_preflight": False},
        }
        source = {
            "present": True,
            "version": "0.10.0",
            "package_path": "/repo/src/mozyo_bridge",
            "feature_probes": {"herdr_preflight": True},
        }
        verdict = evaluate_fingerprint(active, source)
        self.assertFalse(
            verdict["ok"],
            msg="doctor runtime must fail closed on same-version probe drift",
        )
        self.assertEqual("drifted", verdict["status"])
        self.assertEqual("same-version-probe-drift", verdict["relation"])
        self.assertTrue(
            any(m["probe"] == "herdr_preflight" for m in verdict["probe_mismatch"]),
            msg="probe mismatch must name the missing gate-critical behavior",
        )

    def test_doctor_runtime_next_action_points_at_repo_local_cli(self) -> None:
        from mozyo_bridge.application.doctor_runtime import build_runtime_next_action

        verdict = {"ok": False, "status": "drifted"}
        actions = build_runtime_next_action(
            verdict, "PYTHONPATH=src python3 -m mozyo_bridge"
        )
        self.assertTrue(actions, "a non-ok verdict must yield a next action")
        self.assertIn(
            "PYTHONPATH=src python3 -m mozyo_bridge",
            "\n".join(actions),
            msg="fingerprint mismatch must route to the repo-local source CLI",
        )

    def test_sublane_list_exposes_lane_metadata_filter(self) -> None:
        from mozyo_bridge.application.cli import build_parser

        parser = build_parser()
        action = next(
            (a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"),
            None,
        )
        self.assertIsNotNone(action, "top-level subparsers missing")
        self.assertIn("sublane", action.choices)
        sublane_sub = next(
            (
                a
                for a in action.choices["sublane"]._actions
                if a.__class__.__name__ == "_SubParsersAction"
            ),
            None,
        )
        self.assertIsNotNone(sublane_sub, "sublane subparsers missing")
        self.assertIn("list", sublane_sub.choices)
        options = {
            opt
            for a in sublane_sub.choices["list"]._actions
            for opt in a.option_strings
        }
        self.assertIn(
            "--lane",
            options,
            msg="`sublane list --lane` is the registered-lane-metadata probe the "
            "harness docs point at (#13543)",
        )


if __name__ == "__main__":
    unittest.main()
