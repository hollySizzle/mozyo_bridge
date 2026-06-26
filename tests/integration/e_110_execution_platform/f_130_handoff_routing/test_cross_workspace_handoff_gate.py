from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser
import mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver as pane_resolver
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import next_action_for

class CrossWorkspaceHandoffGateTest(unittest.TestCase):
    """Cross-workspace handoff gate (Redmine #10332).

    Origin Codex must not deliver directly to a foreign workspace's Claude
    pane. The gate enforces: cross-session `--to claude` is rejected; the
    sender must route through the target session's Codex window with
    `--to codex`. The optional `--target-repo` flag adds a repo-mismatch
    fail-closed check on top.
    """

    def run_handoff(self, argv, pane, sender_session="local"):
        parser = build_parser()
        args = parser.parse_args(argv)
        sent: list[tuple[str, ...]] = []

        def fake_run_tmux(*tmux_args, check: bool = True):
            sent.append(tmux_args)
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch(
                "mozyo_bridge.application.commands.capture_pane",
                return_value="",
            ), \
            patch(
                "mozyo_bridge.application.commands.run_tmux",
                side_effect=fake_run_tmux,
            ), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch(
                "mozyo_bridge.application.commands.current_session_name",
                return_value=sender_session,
            ), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.validate_target"), \
            patch(
                "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines",
                return_value=[pane],
            ), \
            contextlib.redirect_stdout(io.StringIO()) as stdout, \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as exit_ctx:
                args.func(args)
        return exit_ctx.exception, sent, stdout.getvalue(), stderr.getvalue()

    def test_cross_session_claude_handoff_is_rejected(self) -> None:
        # Origin lives in `local`, target pane lives in `other`; receiver is
        # claude — the gate must fail closed with `cross_session_claude` and
        # no tmux send-keys must be issued.
        pane = {
            "id": "%9",
            "location": "other:1.0",
            "command": "claude",
            "cwd": "/repo",
            "window_name": "claude",
            "pane_active": "1",
        }
        _exc, sent, stdout, stderr = self.run_handoff(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "redmine",
                "--issue",
                "10332",
                "--journal",
                "49623",
                "--kind",
                "implementation_request",
                "--target",
                "%9",
                "--mode",
                "standard",
            ],
            pane=pane,
            sender_session="local",
        )

        # No tmux input was typed into the target pane — fail-closed before
        # any send-keys runs.
        self.assertFalse(
            any(
                call[:2] == ("send-keys", "-t")
                for call in sent
            ),
            f"unexpected send-keys: {sent}",
        )
        # Outcome JSON carries the new reason and no notification_marker
        # (no body was assembled past the gate).
        json_lines = [
            line for line in stdout.splitlines() if line.strip().startswith("{")
        ]
        self.assertTrue(json_lines, f"no JSON outcome: {stdout!r}")
        outcome = json.loads(json_lines[-1])
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("cross_session_claude", outcome["reason"])
        self.assertEqual("claude", outcome["receiver"])
        self.assertIn(
            "cross-session handoff to Claude is not allowed", stderr
        )

    def test_cross_session_codex_handoff_is_allowed_through_the_gate(self) -> None:
        # `--to codex` is the gateway path: routing into a foreign workspace
        # through that workspace's Codex window is what the gate permits.
        # The handoff dies later (no marker observed) under `standard`, but
        # NOT with `cross_session_claude`. Different `reason` proves the
        # cross-session gate let it through.
        pane = {
            "id": "%9",
            "location": "other:1.0",
            "command": "codex",
            "cwd": "/repo",
            "window_name": "codex",
            "pane_active": "1",
        }
        _exc, _sent, stdout, _stderr = self.run_handoff(
            [
                "handoff",
                "send",
                "--to",
                "codex",
                "--source",
                "redmine",
                "--issue",
                "10332",
                "--journal",
                "49623",
                "--kind",
                "review_request",
                "--target",
                "%9",
                "--mode",
                "standard",
                "--landing-timeout",
                "0.01",
                "--submit-delay",
                "0",
            ],
            pane=pane,
            sender_session="local",
        )

        json_lines = [
            line for line in stdout.splitlines() if line.strip().startswith("{")
        ]
        self.assertTrue(json_lines, f"no JSON outcome: {stdout!r}")
        outcome = json.loads(json_lines[-1])
        # The gate did NOT trigger. The handoff dies on marker_timeout
        # (strict mode, no marker observed) instead.
        self.assertEqual("blocked", outcome["status"])
        self.assertNotEqual("cross_session_claude", outcome["reason"])

    def test_same_session_claude_handoff_is_not_blocked_by_the_gate(self) -> None:
        # In-session `--to claude` is the existing window-only resolver path;
        # the cross-workspace gate must not regress it. The handoff itself
        # still dies on marker_timeout under strict standard mode.
        pane = {
            "id": "%9",
            "location": "local:1.0",
            "command": "claude",
            "cwd": "/repo",
            "window_name": "claude",
            "pane_active": "1",
        }
        _exc, _sent, stdout, _stderr = self.run_handoff(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "redmine",
                "--issue",
                "10332",
                "--journal",
                "49623",
                "--kind",
                "implementation_request",
                "--target",
                "%9",
                "--mode",
                "standard",
                "--landing-timeout",
                "0.01",
                "--submit-delay",
                "0",
            ],
            pane=pane,
            sender_session="local",
        )

        json_lines = [
            line for line in stdout.splitlines() if line.strip().startswith("{")
        ]
        self.assertTrue(json_lines, f"no JSON outcome: {stdout!r}")
        outcome = json.loads(json_lines[-1])
        self.assertNotEqual("cross_session_claude", outcome["reason"])

    def test_default_queue_enter_cross_session_codex_is_rejected_as_invalid_args(
        self,
    ) -> None:
        # Regression for Redmine #10332 review #49646, updated for #11301.
        # Cross-session `--to codex` is the documented gateway path. Since
        # #11301 the default `queue-enter` rail admits a cross-session target
        # only under the constrained identity gate (explicit `--target` PLUS a
        # passing `--target-repo`). This send supplies the explicit `--target`
        # but NO `--target-repo`, so the identity gate is not satisfied and the
        # rail still fails closed with `invalid_args` before any typing. This
        # pins that cross-session admission is not granted without the
        # workspace identity assertion.
        pane = {
            "id": "%9",
            "location": "other:1.0",
            "command": "codex",
            "cwd": "/repo",
            "window_name": "codex",
            "pane_active": "1",
        }
        _exc, sent, stdout, stderr = self.run_handoff(
            [
                "handoff",
                "send",
                "--to",
                "codex",
                "--source",
                "redmine",
                "--issue",
                "10332",
                "--journal",
                "49623",
                "--kind",
                "review_request",
                "--target",
                "%9",
                # No `--mode` → default queue-enter (since v0.4).
            ],
            pane=pane,
            sender_session="local",
        )

        # No tmux input typed before the rail rejects the cross-session
        # target.
        self.assertFalse(
            any(call[:2] == ("send-keys", "-t") for call in sent),
            f"unexpected send-keys: {sent}",
        )
        json_lines = [
            line for line in stdout.splitlines() if line.strip().startswith("{")
        ]
        self.assertTrue(json_lines, f"no JSON outcome: {stdout!r}")
        outcome = json.loads(json_lines[-1])
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertIn(
            "queue-enter requires the target pane to live in the sender's tmux session",
            stderr,
        )

    def test_cross_session_claude_outcome_guides_to_codex_gateway(self) -> None:
        # Regression for Redmine #10332 review #49646, updated for #11301.
        # The recovery path from a `cross_session_claude` block must steer the
        # sender to the codex-gateway path with workspace identity:
        # `--to codex --target <target_session>:codex --target-repo <root>`.
        # Since #11301 that gateway send is admitted on the *default*
        # queue-enter rail when --target is explicit and --target-repo passes,
        # so the guidance must NOT present `--mode standard` as required; it is
        # only a fallback. The next_action_for / outcome narrative / die()
        # message must all carry the gateway + --target-repo hint.
        pane = {
            "id": "%9",
            "location": "other:1.0",
            "command": "claude",
            "cwd": "/repo",
            "window_name": "claude",
            "pane_active": "1",
        }
        _exc, _sent, stdout, stderr = self.run_handoff(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "redmine",
                "--issue",
                "10332",
                "--journal",
                "49623",
                "--kind",
                "implementation_request",
                "--target",
                "%9",
                "--mode",
                "standard",
            ],
            pane=pane,
            sender_session="local",
        )

        json_lines = [
            line for line in stdout.splitlines() if line.strip().startswith("{")
        ]
        self.assertTrue(json_lines, f"no JSON outcome: {stdout!r}")
        outcome = json.loads(json_lines[-1])
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("cross_session_claude", outcome["reason"])
        # The structured outcome's next_action must steer to the codex gateway
        # with the workspace identity gate so the sender's next attempt is
        # admitted on the default queue-enter rail. The die() trailer on stderr
        # must carry the same hint.
        self.assertIn("--to codex", outcome["next_action"])
        self.assertIn("--target-repo", outcome["next_action"])
        self.assertIn("--to codex", stderr)
        self.assertIn("--target-repo", stderr)
        # `--mode standard` must read as a fallback, not a requirement.
        self.assertIn("fallback", outcome["next_action"])
        # The durable record (markdown) must repeat the gateway hint so
        # auditors and downstream agents see it even when the structured
        # outcome is consumed and discarded.
        self.assertIn("--target-repo", stdout)

    def test_target_repo_mismatch_is_rejected(self) -> None:
        # `--target-repo` opts the sender into a repo-mismatch fail-closed
        # gate. When the target pane's cwd does not walk up to the named
        # repo root, the handoff is rejected before any send-keys.
        with tempfile.TemporaryDirectory() as tmp_str:
            expected_repo = Path(tmp_str) / "expected"
            other_repo = Path(tmp_str) / "other"
            (expected_repo / "src").mkdir(parents=True)
            (other_repo / "src").mkdir(parents=True)
            (expected_repo / "pyproject.toml").write_text("", encoding="utf-8")
            (other_repo / "pyproject.toml").write_text("", encoding="utf-8")

            pane = {
                "id": "%9",
                "location": "local:1.0",
                "command": "claude",
                "cwd": str(other_repo / "src"),
                "window_name": "claude",
                "pane_active": "1",
            }
            _exc, sent, stdout, stderr = self.run_handoff(
                [
                    "handoff",
                    "send",
                    "--to",
                    "claude",
                    "--source",
                    "redmine",
                    "--issue",
                    "10332",
                    "--journal",
                    "49623",
                    "--kind",
                    "implementation_request",
                    "--target",
                    "%9",
                    "--target-repo",
                    str(expected_repo),
                    "--mode",
                    "standard",
                ],
                pane=pane,
                sender_session="local",
            )

        self.assertFalse(
            any(call[:2] == ("send-keys", "-t") for call in sent),
            f"unexpected send-keys: {sent}",
        )
        json_lines = [
            line for line in stdout.splitlines() if line.strip().startswith("{")
        ]
        self.assertTrue(json_lines, f"no JSON outcome: {stdout!r}")
        outcome = json.loads(json_lines[-1])
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("target_repo_mismatch", outcome["reason"])
        self.assertIn("target pane is not in the expected repo", stderr)

    def test_target_repo_gate_passes_for_scaffolded_non_git_workspace(self) -> None:
        # Redmine #11301: a non-git scaffolded workspace (only
        # `.mozyo-bridge/scaffold.json`) is a first-class identity root, so a
        # pane whose cwd is under it satisfies `--target-repo <workspace>`.
        # The gate must NOT fire; the handoff dies later on marker_timeout.
        with tempfile.TemporaryDirectory() as tmp_str:
            workspace = Path(tmp_str) / "人形使い"
            (workspace / ".mozyo-bridge").mkdir(parents=True)
            (workspace / ".mozyo-bridge" / "scaffold.json").write_text(
                "{}", encoding="utf-8"
            )
            (workspace / "src").mkdir()

            pane = {
                "id": "%8",
                "location": "local:1.0",
                "command": "claude",
                "cwd": str(workspace / "src"),
                "window_name": "claude",
                "pane_active": "1",
            }
            _exc, sent, stdout, _stderr = self.run_handoff(
                [
                    "handoff",
                    "send",
                    "--to",
                    "claude",
                    "--source",
                    "redmine",
                    "--issue",
                    "11301",
                    "--journal",
                    "54071",
                    "--kind",
                    "implementation_request",
                    "--target",
                    "%8",
                    "--target-repo",
                    str(workspace),
                    "--mode",
                    "standard",
                    "--landing-timeout",
                    "0.01",
                    "--submit-delay",
                    "0",
                ],
                pane=pane,
                sender_session="local",
            )

        json_lines = [
            line for line in stdout.splitlines() if line.strip().startswith("{")
        ]
        self.assertTrue(json_lines, f"no JSON outcome: {stdout!r}")
        outcome = json.loads(json_lines[-1])
        # The identity gate let it through. It dies on marker_timeout (strict
        # standard mode, no marker observed), NOT on the repo gate.
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("marker_timeout", outcome["reason"])

    def test_location_form_target_resolves_instead_of_pane_disappeared(self) -> None:
        # Redmine #11666: `--target '<session>:codex'` — the exact form the
        # cross-session guidance tells operators to use — used to die with
        # `pane disappeared after resolve` even though the pane existed.
        # With location→pane-id normalization it must reach the same
        # endpoint as a pane-id target (marker_timeout in standard mode),
        # not the resolver death.
        with tempfile.TemporaryDirectory() as tmp_str:
            workspace = Path(tmp_str) / "other-repo"
            (workspace / "src").mkdir(parents=True)
            (workspace / "pyproject.toml").write_text("", encoding="utf-8")

            pane = {
                "id": "%9",
                "location": "other:1.0",
                "command": "codex",
                "cwd": str(workspace / "src"),
                "window_name": "codex",
                "pane_active": "1",
            }
            with patch(
                "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.resolve_pane_id",
                return_value="%9",
            ):
                _exc, _sent, stdout, _stderr = self.run_handoff(
                    [
                        "handoff",
                        "send",
                        "--to",
                        "codex",
                        "--source",
                        "redmine",
                        "--issue",
                        "11666",
                        "--journal",
                        "56072",
                        "--kind",
                        "implementation_request",
                        "--target",
                        "other:codex",
                        "--target-repo",
                        str(workspace),
                        "--mode",
                        "standard",
                        "--landing-timeout",
                        "0.01",
                        "--submit-delay",
                        "0",
                    ],
                    pane=pane,
                    sender_session="local",
                )

        json_lines = [
            line for line in stdout.splitlines() if line.strip().startswith("{")
        ]
        self.assertTrue(json_lines, f"no JSON outcome: {stdout!r}")
        outcome = json.loads(json_lines[-1])
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("marker_timeout", outcome["reason"])

    def test_target_repo_gate_passes_across_unicode_normal_forms(self) -> None:
        # Redmine #11625: the pane cwd arrives in macOS NFD bytes while the
        # operator's `--target-repo` is typically NFC (copied from docs /
        # Redmine). Same directory, different bytes — the identity gate must
        # compare through Unicode normalization, not raw strings, so the
        # handoff proceeds (and then dies on marker_timeout, NOT the gate).
        import unicodedata as _ud

        nfd_name = _ud.normalize("NFD", "動画ドライブ")
        nfc_name = _ud.normalize("NFC", "動画ドライブ")
        self.assertNotEqual(nfd_name, nfc_name)
        with tempfile.TemporaryDirectory() as tmp_str:
            workspace = Path(tmp_str) / nfd_name
            (workspace / "src").mkdir(parents=True)
            (workspace / "pyproject.toml").write_text("", encoding="utf-8")
            nfc_spelling = str(Path(tmp_str) / nfc_name)

            pane = {
                "id": "%8",
                "location": "local:1.0",
                "command": "claude",
                "cwd": str(workspace / "src"),
                "window_name": "claude",
                "pane_active": "1",
            }
            _exc, _sent, stdout, _stderr = self.run_handoff(
                [
                    "handoff",
                    "send",
                    "--to",
                    "claude",
                    "--source",
                    "redmine",
                    "--issue",
                    "11625",
                    "--journal",
                    "55992",
                    "--kind",
                    "implementation_request",
                    "--target",
                    "%8",
                    "--target-repo",
                    nfc_spelling,
                    "--mode",
                    "standard",
                    "--landing-timeout",
                    "0.01",
                    "--submit-delay",
                    "0",
                ],
                pane=pane,
                sender_session="local",
            )

        json_lines = [
            line for line in stdout.splitlines() if line.strip().startswith("{")
        ]
        self.assertTrue(json_lines, f"no JSON outcome: {stdout!r}")
        outcome = json.loads(json_lines[-1])
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("marker_timeout", outcome["reason"])

    def test_target_repo_mismatch_hints_setup_when_identity_unestablished(
        self,
    ) -> None:
        # Redmine #11301 error UX: when the target cwd walks up to NO identity
        # marker at all, stay fail-closed but return a concrete setup hint
        # (scaffold the workspace) rather than forcing the operator to reason
        # about repo-root heuristics.
        with tempfile.TemporaryDirectory() as tmp_str:
            expected_workspace = Path(tmp_str) / "人形使い"
            (expected_workspace / ".mozyo-bridge").mkdir(parents=True)
            (expected_workspace / ".mozyo-bridge" / "scaffold.json").write_text(
                "{}", encoding="utf-8"
            )
            bare = Path(tmp_str) / "bare_no_marker"
            bare.mkdir()

            pane = {
                "id": "%8",
                "location": "local:1.0",
                "command": "claude",
                "cwd": str(bare),
                "window_name": "claude",
                "pane_active": "1",
            }
            _exc, sent, stdout, stderr = self.run_handoff(
                [
                    "handoff",
                    "send",
                    "--to",
                    "claude",
                    "--source",
                    "redmine",
                    "--issue",
                    "11301",
                    "--journal",
                    "54071",
                    "--kind",
                    "implementation_request",
                    "--target",
                    "%8",
                    "--target-repo",
                    str(expected_workspace),
                    "--mode",
                    "standard",
                ],
                pane=pane,
                sender_session="local",
            )

        self.assertFalse(
            any(call[:2] == ("send-keys", "-t") for call in sent),
            f"unexpected send-keys: {sent}",
        )
        json_lines = [
            line for line in stdout.splitlines() if line.strip().startswith("{")
        ]
        self.assertTrue(json_lines, f"no JSON outcome: {stdout!r}")
        outcome = json.loads(json_lines[-1])
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("target_repo_mismatch", outcome["reason"])
        # The hint names the concrete setup action, not repo-root internals.
        self.assertIn("scaffold", stderr)
        self.assertIn(".mozyo-bridge/scaffold.json", stderr)


if __name__ == "__main__":
    unittest.main()
