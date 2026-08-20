"""orchestrate_handoff wiring for repo-local ADR context injection (Redmine #15722).

ADR-0011 trade-off 3 records that "ADR is referenced by every layer" has no
mechanism behind it. This pins the mechanism at the seam every layer already
goes through: a ``handoff send`` that carries a role profile (``sublane
dispatch-worker`` / gateway dispatch / delegated launch all emit one) also
carries a resolvable, status-faithful pointer to the repo's ADR set.

What is pinned here (the acceptance criteria of #15722):

1. the resolved pointer reaches the pane body, the structured JSON outcome, and
   the durable delivery record;
2. a ``proposed`` ADR is presented as ``proposed`` and marked non-binding — and
   an unrecognised status becomes ``unknown``, never ``active``;
3. a repo without ``vibes/docs/adr/`` sends exactly as before — the payload
   carries no ``adr_context`` key at all (byte-identical to the pre-#15722
   shape, review j#108679), no clause in the body, no record block.

Everything runs against a fake tmux rail + a temp repo — no real tmux, no
external send, no real ``~/.mozyo_bridge``.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from . import (  # noqa: E402,F401
    setUpModule,
    tearDownModule,
)

from mozyo_bridge.application.cli import build_parser

_INDEX_BODY = "# ADR index\n\n| ID | title | status |\n"

_ADR_BODY = "# {adr_id}: title\n\n- status: {status}\n- date: 2026-08-19\n\n## 決定\n\nx\n"


class HandoffAdrContextInjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)

    def _write_adr_repo(self) -> None:
        adr_dir = self.repo / "vibes" / "docs" / "adr"
        adr_dir.mkdir(parents=True, exist_ok=True)
        (adr_dir / "README.md").write_text(_INDEX_BODY, encoding="utf-8")
        for name, adr_id, status in (
            ("adr-0001-adr-practice.md", "ADR-0001", "active"),
            ("adr-0002-enter-resend.md", "ADR-0002", "superseded (by ADR-0007)"),
            ("adr-0011-three-layer.md", "ADR-0011", "proposed (owner ratify 待ち)"),
            ("adr-0012-no-status.md", "ADR-0012", "draft-ish"),
        ):
            (adr_dir / name).write_text(
                _ADR_BODY.format(adr_id=adr_id, status=status), encoding="utf-8"
            )
        # A non-ADR file beside the index must not become a ref.
        (adr_dir / "notes.md").write_text("not an adr\n", encoding="utf-8")

    def _run(self, argv):
        parser = build_parser()
        args = parser.parse_args(argv)
        sent: list[tuple[str, ...]] = []
        pane_text = ""

        def fake_capture(_target: str, _lines: int) -> str:
            return pane_text

        def fake_run_tmux(*tmux_args: str, check: bool = True):
            nonlocal pane_text
            if tmux_args[:4] == ("send-keys", "-t", "%2", "-l"):
                pane_text += tmux_args[-1]
                sent.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            if tmux_args[:3] == ("send-keys", "-t", "%2"):
                sent.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            if tmux_args[:1] == ("select-pane",):
                sent.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected tmux call: {tmux_args}")

        pane = {
            "id": "%2",
            "location": "agents:0.1",
            "command": "node",
            "cwd": "/repo",
            "window_name": "claude",
            "pane_active": "1",
        }

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch(
                "mozyo_bridge.application.commands.repo_root_from_args",
                return_value=self.repo,
            ), \
            patch("mozyo_bridge.application.commands.capture_pane", side_effect=fake_capture), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch("mozyo_bridge.application.commands.current_session_name", return_value="agents"), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.validate_target"), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines", return_value=[pane]), \
            contextlib.redirect_stdout(io.StringIO()) as stdout, \
            contextlib.redirect_stderr(io.StringIO()):
            result = args.func(args)

        return result, stdout.getvalue(), pane_text

    def _outcome_from_stdout(self, stdout: str) -> dict:
        lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        self.assertTrue(lines, f"no JSON outcome found in stdout: {stdout!r}")
        return json.loads(lines[-1])

    def _worker_argv(self) -> list[str]:
        # The exact role-profile shape `sublane dispatch-worker` emits.
        return [
            "handoff", "send", "--to", "claude",
            "--source", "redmine", "--issue", "15722", "--journal", "108275",
            "--kind", "implementation_request",
            "--target", "%2", "--mode", "queue-enter", "--submit-delay", "0",
            "--role-profile", "implementation_worker",
            "--profile-field", "lane=issue_15722_adr_context_resolution",
            "--profile-field", "gateway_callback_target=w1V:p4",
        ]

    def test_resolved_adr_context_reaches_body_outcome_and_record(self) -> None:
        self._write_adr_repo()
        result, stdout, pane_text = self._run(self._worker_argv())
        self.assertEqual(0, result)

        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        adr = outcome["role_profile"]["adr_context"]
        self.assertIsNotNone(adr)
        self.assertEqual("vibes/docs/adr/README.md", adr["index_canonical_path"])
        # Catalog-resolvable in both the sender-repo-relative and the
        # monorepo-nested receiver form.
        self.assertIn(
            "projects/giken-3800-mozyo-bridge/vibes/docs/adr/README.md",
            adr["index_resolvable_paths"],
        )
        self.assertEqual(["active"], adr["binding_statuses"])

        # The pane body — the context actually injected into the receiver's turn
        # — names the index on the single landing-marker line.
        self.assertIn("adr context: index vibes/docs/adr/README.md", pane_text)
        self.assertNotIn("\n", pane_text.strip())

        # The durable delivery record carries the per-ADR block.
        self.assertIn("# ADR context (repo-local, resolved at send time", stdout)
        self.assertIn("- active: adr-0001", stdout)

    def test_non_active_statuses_are_carried_but_never_binding(self) -> None:
        self._write_adr_repo()
        _result, stdout, pane_text = self._run(self._worker_argv())
        outcome = self._outcome_from_stdout(stdout)
        refs = {ref["adr_id"]: ref for ref in outcome["role_profile"]["adr_context"]["refs"]}

        self.assertEqual({"adr-0001", "adr-0002", "adr-0011", "adr-0012"}, set(refs))
        self.assertEqual("active", refs["adr-0001"]["status"])
        self.assertIs(True, refs["adr-0001"]["binding"])
        # proposed stays proposed; superseded stays superseded; an unrecognised
        # status degrades to `unknown` — none of them become a standing rule.
        self.assertEqual("proposed", refs["adr-0011"]["status"])
        self.assertEqual("superseded", refs["adr-0002"]["status"])
        self.assertEqual("unknown", refs["adr-0012"]["status"])
        for adr_id in ("adr-0002", "adr-0011", "adr-0012"):
            self.assertIs(False, refs[adr_id]["binding"], adr_id)

        self.assertIn("1 active (binding), 3 non-active (not binding)", pane_text)
        self.assertIn("- proposed (NOT binding): adr-0011", stdout)

    def test_repo_without_adr_directory_sends_unchanged(self) -> None:
        # Backward compatibility (#15722 AC3, review j#108679
        # finding_nullkeybreaksnoadrcompat): an adopting repo with no ADR practice
        # keeps the pre-#15722 payload byte-identically — no `adr_context` key at
        # all, no body clause, no record block, and the send still succeeds.
        result, stdout, pane_text = self._run(self._worker_argv())
        self.assertEqual(0, result)

        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertNotIn("adr_context", outcome["role_profile"])
        self.assertNotIn("adr context:", pane_text)
        self.assertNotIn("# ADR context", stdout)

    def test_adr_directory_without_index_resolves_nothing(self) -> None:
        # The index is the anchor: without it the sender does not invent one.
        adr_dir = self.repo / "vibes" / "docs" / "adr"
        adr_dir.mkdir(parents=True, exist_ok=True)
        (adr_dir / "adr-0001-adr-practice.md").write_text(
            _ADR_BODY.format(adr_id="ADR-0001", status="active"), encoding="utf-8"
        )
        _result, stdout, pane_text = self._run(self._worker_argv())
        outcome = self._outcome_from_stdout(stdout)
        self.assertNotIn("adr_context", outcome["role_profile"])
        self.assertNotIn("adr context:", pane_text)

    def test_send_without_role_profile_carries_no_adr_context(self) -> None:
        # The pointer is a companion of the role-profile expansion, not a new
        # unconditional payload: no `--role-profile` -> no ADR context.
        self._write_adr_repo()
        argv = [
            "handoff", "send", "--to", "claude",
            "--source", "redmine", "--issue", "15722", "--journal", "108275",
            "--kind", "implementation_request",
            "--target", "%2", "--mode", "queue-enter", "--submit-delay", "0",
        ]
        _result, stdout, pane_text = self._run(argv)
        outcome = self._outcome_from_stdout(stdout)
        self.assertIsNone(outcome["role_profile"])
        self.assertNotIn("adr context:", pane_text)


if __name__ == "__main__":  # pragma: no cover - unittest entrypoint
    unittest.main()
