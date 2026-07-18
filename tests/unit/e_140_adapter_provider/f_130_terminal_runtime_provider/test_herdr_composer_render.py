"""Specs for the #14065 composer-render measurement instrument (phase 1).

Exercises the capability-negotiated ANSI render read (:meth:`HerdrCliTransport.read_pane_render`),
its redacted parser, the fail-closed read-model (:func:`read_composer_render`), and
the read-only diagnostic CLI — all through injected runners / transports / temp
repos, with **sanitized** synthetic ANSI only. No test spawns a live herdr binary
and no live pane body appears in any fixture or failure (IR acceptance #4).

The load-bearing case is the adversarial pair: the *exact same* composer body text
rendered dim (a ghost idle-placeholder) vs normal (real unsent input) must classify
to different ``style_provenance`` — the positive discriminator #14064 could not
produce from plain text.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_composer_render_cli import (  # noqa: E501
    cmd_herdr_composer_render,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
    ComposerRenderView,
    read_composer_render,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.pane_render_observation import (  # noqa: E501
    CURSOR_RELATION_COMPOSER,
    CURSOR_RELATION_ELSEWHERE,
    RENDER_REASON_ANSI_ABSENT,
    RENDER_REASON_ANSI_UNSUPPORTED,
    RENDER_REASON_EMPTY_COMPOSER,
    RENDER_REASON_INVALID_TARGET,
    RENDER_REASON_NO_COMPOSER,
    RENDER_REASON_OK,
    RENDER_REASON_UNREADABLE,
    STYLE_PROVENANCE_DIM,
    STYLE_PROVENANCE_MIXED,
    STYLE_PROVENANCE_NORMAL,
    PaneRenderObservation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    HerdrCliTransport,
)

ESC = "\x1b"
BIN = "/opt/herdr/bin/herdr"


class RecordingRunner:
    """A ``subprocess.run``-shaped callable that records argv and replays a result."""

    def __init__(self, *, returncode=0, stdout="", stderr="", raises=None):
        self.calls: list = []
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._raises = raises

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self._raises is not None:
            raise self._raises
        return subprocess.CompletedProcess(
            argv, self._returncode, stdout=self._stdout, stderr=self._stderr
        )


def _ansi_payload(ansi: str, *, key: str = "ansi", cursor=None) -> str:
    read = {key: ansi}
    if cursor is not None:
        read["cursor"] = cursor
    return json.dumps({"result": {"read": read}})


def _render(runner_stdout: str, *, returncode=0, stderr="") -> PaneRenderObservation:
    runner = RecordingRunner(stdout=runner_stdout, returncode=returncode, stderr=stderr)
    transport = HerdrCliTransport(BIN, runner=runner)
    return transport.read_pane_render("poc_claude")


# The composer body text shared by the ghost and the real-input fixtures. Sanitized
# hint-shaped text; identical bytes in both, so only the style can tell them apart.
_BODY = "Type your message..."


class ArgvTest(unittest.TestCase):
    def test_render_argv_requests_ansi_format(self) -> None:
        runner = RecordingRunner(stdout=_ansi_payload(f"{ESC}[2m> {_BODY}{ESC}[0m"))
        HerdrCliTransport(BIN, runner=runner).read_pane_render("poc_claude", lines=40)
        self.assertEqual(
            runner.calls[0][0],
            [
                BIN, "agent", "read", "poc_claude", "--source", "visible",
                "--format", "ansi", "--ansi", "--lines", "40",
            ],
        )

    def test_legacy_read_pane_argv_is_byte_invariant(self) -> None:
        # The measurement capability must not perturb the legacy text contract: the
        # plain read never carries --format / --ansi (IR acceptance #3).
        runner = RecordingRunner(stdout='{"result":{"read":{"text":"x","truncated":false}}}')
        transport = HerdrCliTransport(BIN, runner=runner)
        result = transport.read_pane("poc_claude")
        self.assertTrue(result.ok)
        self.assertEqual(
            runner.calls[0][0], [BIN, "agent", "read", "poc_claude", "--source", "visible"]
        )
        self.assertNotIn("--format", runner.calls[0][0])
        self.assertNotIn("--ansi", runner.calls[0][0])


class AdversarialClassificationTest(unittest.TestCase):
    """ghost / normal user text / placeholder-similar / exact-same-text / marker /
    startup screen / unknown — the #14065 adversarial regression set."""

    def test_ghost_dim_placeholder_is_dim(self) -> None:
        obs = _render(_ansi_payload(f"scrollback\n{ESC}[2m> {_BODY}{ESC}[0m"))
        self.assertTrue(obs.readable)
        self.assertEqual(STYLE_PROVENANCE_DIM, obs.style_provenance)
        self.assertEqual(RENDER_REASON_OK, obs.reason)

    def test_exact_same_text_real_input_is_normal(self) -> None:
        # SAME body bytes as the ghost, but rendered at normal intensity → normal.
        obs = _render(_ansi_payload(f"scrollback\n{ESC}[0m> {_BODY}"))
        self.assertTrue(obs.readable)
        self.assertEqual(STYLE_PROVENANCE_NORMAL, obs.style_provenance)

    def test_ghost_and_real_same_body_differ_in_provenance(self) -> None:
        ghost = _render(_ansi_payload(f"{ESC}[2m> {_BODY}{ESC}[0m"))
        real = _render(_ansi_payload(f"{ESC}[0m> {_BODY}"))
        self.assertNotEqual(ghost.style_provenance, real.style_provenance)

    def test_bright_black_placeholder_is_dim(self) -> None:
        obs = _render(_ansi_payload(f"{ESC}[90m> {_BODY}{ESC}[39m"))
        self.assertEqual(STYLE_PROVENANCE_DIM, obs.style_provenance)

    def test_normal_user_text_is_normal(self) -> None:
        obs = _render(_ansi_payload(f"{ESC}[0m> deploy the release now"))
        self.assertEqual(STYLE_PROVENANCE_NORMAL, obs.style_provenance)

    def test_marker_text_classifies_by_style_not_content(self) -> None:
        # A handoff-marker-shaped body typed normally is a normal render; the render
        # instrument does not parse markers (that is the e110 observer's job).
        obs = _render(_ansi_payload(f"{ESC}[0m> [mozyo:handoff:issue=14065]"))
        self.assertEqual(STYLE_PROVENANCE_NORMAL, obs.style_provenance)

    def test_mixed_intensity_body_is_mixed(self) -> None:
        obs = _render(_ansi_payload(f"{ESC}[2m> dim{ESC}[22m tail"))
        self.assertEqual(STYLE_PROVENANCE_MIXED, obs.style_provenance)

    def test_startup_screen_without_prompt_is_no_composer(self) -> None:
        obs = _render(_ansi_payload(f"{ESC}[1mWelcome to Claude{ESC}[0m\ntrust this folder? [y/n]"))
        self.assertFalse(obs.readable)
        self.assertEqual(RENDER_REASON_NO_COMPOSER, obs.reason)
        self.assertFalse(obs.prompt_present)

    def test_empty_composer_is_empty_composer(self) -> None:
        obs = _render(_ansi_payload(f"{ESC}[0m> "))
        self.assertFalse(obs.readable)
        self.assertEqual(RENDER_REASON_EMPTY_COMPOSER, obs.reason)
        self.assertTrue(obs.prompt_present)

    def test_stripped_payload_without_ansi_is_ansi_absent(self) -> None:
        # The binary ignored --ansi (or the render is unstyled): no CSI escape at all.
        obs = _render(json.dumps({"result": {"read": {"text": f"> {_BODY}"}}}))
        self.assertFalse(obs.readable)
        self.assertEqual(RENDER_REASON_ANSI_ABSENT, obs.reason)

    def test_unknown_malformed_payload_is_ansi_absent(self) -> None:
        obs = _render("not json at all")
        self.assertFalse(obs.readable)
        self.assertEqual(RENDER_REASON_ANSI_ABSENT, obs.reason)

    def test_ansi_embedded_in_text_field_is_classified(self) -> None:
        # The likely real --ansi contract: SGR codes in the existing text field.
        obs = _render(_ansi_payload(f"{ESC}[2m> {_BODY}{ESC}[0m", key="text"))
        self.assertEqual(STYLE_PROVENANCE_DIM, obs.style_provenance)


class CursorRelationTest(unittest.TestCase):
    def test_cursor_on_composer_line(self) -> None:
        obs = _render(_ansi_payload(f"line0\n{ESC}[2m> {_BODY}", cursor={"row": 1, "col": 3}))
        self.assertEqual(CURSOR_RELATION_COMPOSER, obs.cursor_relation)

    def test_cursor_elsewhere(self) -> None:
        obs = _render(_ansi_payload(f"line0\n{ESC}[2m> {_BODY}", cursor={"row": 0, "col": 0}))
        self.assertEqual(CURSOR_RELATION_ELSEWHERE, obs.cursor_relation)


class FailClosedTransportTest(unittest.TestCase):
    def test_invalid_target_fails_closed_without_spawn(self) -> None:
        runner = RecordingRunner()
        obs = HerdrCliTransport(BIN, runner=runner).read_pane_render("bad target!")
        self.assertEqual(RENDER_REASON_INVALID_TARGET, obs.reason)
        self.assertEqual([], runner.calls)

    def test_invalid_source_fails_closed_without_spawn(self) -> None:
        runner = RecordingRunner()
        obs = HerdrCliTransport(BIN, runner=runner).read_pane_render(
            "poc_claude", source="nonsense"
        )
        self.assertEqual(RENDER_REASON_INVALID_TARGET, obs.reason)
        self.assertEqual([], runner.calls)

    def test_unknown_flag_exit_is_ansi_unsupported(self) -> None:
        obs = _render(
            "", returncode=2, stderr="error: unrecognized option '--format'"
        )
        self.assertFalse(obs.readable)
        self.assertEqual(RENDER_REASON_ANSI_UNSUPPORTED, obs.reason)

    def test_generic_nonzero_exit_is_unreadable(self) -> None:
        obs = _render("", returncode=1, stderr="pane not found")
        self.assertFalse(obs.readable)
        self.assertEqual(RENDER_REASON_UNREADABLE, obs.reason)

    def test_spawn_failure_is_unreadable(self) -> None:
        runner = RecordingRunner(raises=FileNotFoundError())
        obs = HerdrCliTransport(BIN, runner=runner).read_pane_render("poc_claude")
        self.assertFalse(obs.readable)
        self.assertEqual(RENDER_REASON_UNREADABLE, obs.reason)

    def test_timeout_is_unreadable(self) -> None:
        runner = RecordingRunner(raises=subprocess.TimeoutExpired(cmd="herdr", timeout=10))
        obs = HerdrCliTransport(BIN, runner=runner).read_pane_render("poc_claude")
        self.assertFalse(obs.readable)
        self.assertEqual(RENDER_REASON_UNREADABLE, obs.reason)


def _herdr_repo(tmp: str) -> Path:
    repo = Path(tmp) / "repo"
    (repo / ".mozyo-bridge").mkdir(parents=True)
    (repo / ".mozyo-bridge" / "config.yaml").write_text(
        "version: 1\nterminal_transport:\n  backend: herdr\n", encoding="utf-8"
    )
    return repo


def _tmux_repo(tmp: str) -> Path:
    repo = Path(tmp) / "repo"
    (repo / ".mozyo-bridge").mkdir(parents=True)
    (repo / ".mozyo-bridge" / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    return repo


class FakeRenderTransport:
    def __init__(self, observation: PaneRenderObservation):
        self._observation = observation
        self.targets: list = []

    def read_pane_render(self, target, **kwargs):
        self.targets.append(target)
        return self._observation


class ReadComposerRenderModelTest(unittest.TestCase):
    def test_non_herdr_backend_observes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            view = read_composer_render(_tmux_repo(tmp), "poc_claude", env={})
        self.assertFalse(view.backend_selected)
        self.assertIsNone(view.observation)

    def test_herdr_backend_returns_injected_observation(self) -> None:
        transport = FakeRenderTransport(
            PaneRenderObservation.classified(STYLE_PROVENANCE_DIM)
        )
        with tempfile.TemporaryDirectory() as tmp:
            view = read_composer_render(
                _herdr_repo(tmp), "poc_claude", env={}, transport=transport
            )
        self.assertTrue(view.backend_selected)
        self.assertEqual(["poc_claude"], transport.targets)
        self.assertTrue(view.observation.readable)
        self.assertEqual(STYLE_PROVENANCE_DIM, view.observation.style_provenance)

    def test_invalid_target_fails_closed_without_transport(self) -> None:
        transport = FakeRenderTransport(
            PaneRenderObservation.classified(STYLE_PROVENANCE_DIM)
        )
        with tempfile.TemporaryDirectory() as tmp:
            view = read_composer_render(
                _herdr_repo(tmp), "bad target!", env={}, transport=transport
            )
        self.assertTrue(view.backend_selected)
        self.assertEqual([], transport.targets)  # never reached the transport
        self.assertEqual(RENDER_REASON_INVALID_TARGET, view.observation.reason)

    def test_view_record_is_fully_redacted(self) -> None:
        transport = FakeRenderTransport(
            PaneRenderObservation.classified(STYLE_PROVENANCE_NORMAL)
        )
        with tempfile.TemporaryDirectory() as tmp:
            view = read_composer_render(
                _herdr_repo(tmp), "poc_claude", env={}, transport=transport
            )
        record = view.to_record()
        blob = json.dumps(record)
        for banned in ("body", "hash", "length", "excerpt", "ansi", "\\u001b", _BODY):
            self.assertNotIn(banned, blob)


class _Args:
    def __init__(self, repo, target, json_flag=False):
        self.repo = repo
        self.target = target
        self.json = json_flag


class DiagnosticCliTest(unittest.TestCase):
    def test_cli_non_herdr_reports_nothing_observed(self) -> None:
        import io
        import contextlib

        with tempfile.TemporaryDirectory() as tmp:
            args = _Args(str(_tmux_repo(tmp)), "poc_claude", json_flag=True)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cmd_herdr_composer_render(args)
        self.assertEqual(0, rc)
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload["backend_selected"])
        self.assertIsNone(payload["observation"])

    def test_cli_missing_target_dies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = _Args(str(_herdr_repo(tmp)), "   ")
            with self.assertRaises(SystemExit):
                cmd_herdr_composer_render(args)


if __name__ == "__main__":
    unittest.main()
