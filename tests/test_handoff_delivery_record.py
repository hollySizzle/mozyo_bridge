from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser
import mozyo_bridge.domain.pane_resolver as pane_resolver
from mozyo_bridge.domain.handoff import (
    MODE_PENDING,
    MODE_STANDARD,
    build_delivery_record,
    build_execution_root,
    build_notification_body,
    make_outcome,
    normalize_anchor,
)

class ExecutionRootPropagationTest(unittest.TestCase):
    """Nested project execution-root / workdir propagation (Redmine #12098).

    A handoff must be able to carry an explicit target execution root distinct
    from the pane cwd / cross-workspace repo root, so a receiver recovers a
    nested project root from the durable record instead of pane scrollback.
    Abstract `/workspace/...` placeholders are used deliberately — no personal
    home path or private project absolute path in tracked test files
    (`vibes/docs/rules/public-private-boundary.md`).
    """

    def test_build_execution_root_derives_relative_pointer_under_repo(self) -> None:
        er = build_execution_root(
            "/workspace/project-alpha/services/api",
            repo_root_abs="/workspace/project-alpha",
        )

        self.assertEqual("/workspace/project-alpha/services/api", er.workdir)
        self.assertEqual("/workspace/project-alpha", er.repo_root)
        self.assertEqual("services/api", er.relative)
        self.assertTrue(er.is_nested)
        self.assertEqual(
            "`services/api` (relative to the target repo root)", er.portable_pointer()
        )

    def test_build_execution_root_resolves_nested_unicode_path(self) -> None:
        # The #12098 reproduction nested a Japanese checkout below a Japanese
        # workspace root; NFC/NFD spelling drift must still yield a relative
        # pointer rather than collapsing to absolute-only.
        import unicodedata

        repo = unicodedata.normalize("NFC", "/workspace/IT導入/anchor")
        workdir = unicodedata.normalize("NFD", "/workspace/IT導入/anchor/rovoice/shinsei_llm")

        er = build_execution_root(workdir, repo_root_abs=repo)

        self.assertEqual("rovoice/shinsei_llm", er.relative)
        self.assertTrue(er.is_nested)

    def test_build_execution_root_out_of_tree_omits_absolute_from_pasteable(self) -> None:
        # An out-of-tree workdir has no repo-relative form. The absolute path
        # must NOT surface in the pane body or the pasteable record (Redmine
        # #12098 review j#59662); it stays only in the structured outcome.
        er = build_execution_root(
            "/workspace/other/checkout", repo_root_abs="/workspace/project-alpha"
        )

        self.assertIsNone(er.relative)
        self.assertFalse(er.is_nested)
        self.assertIsNone(er.portable_pointer())
        self.assertNotIn("/workspace/other/checkout", er.record_pointer())
        self.assertNotIn("/workspace/other/checkout", er.notification_clause())
        self.assertIn("execution_root.workdir", er.record_pointer())
        # The absolute is still retained as a structured runtime fact.
        self.assertEqual("/workspace/other/checkout", er.workdir)

    def test_build_execution_root_equal_to_repo_root_is_not_nested(self) -> None:
        er = build_execution_root(
            "/workspace/project-alpha", repo_root_abs="/workspace/project-alpha"
        )

        self.assertEqual(".", er.relative)
        self.assertFalse(er.is_nested)

    def test_build_execution_root_without_anchor_omits_absolute_from_pasteable(self) -> None:
        er = build_execution_root("/workspace/project-alpha/services/api")

        self.assertIsNone(er.repo_root)
        self.assertIsNone(er.relative)
        # No anchor → no portable form → absolute is kept out of pasteable text.
        self.assertIsNone(er.portable_pointer())
        self.assertNotIn(
            "/workspace/project-alpha/services/api", er.record_pointer()
        )
        self.assertEqual("/workspace/project-alpha/services/api", er.workdir)

    def test_notification_body_appends_execution_root_clause(self) -> None:
        anchor = normalize_anchor("redmine", issue="12098", journal="59652")
        er = build_execution_root(
            "/workspace/project-alpha/services/api",
            repo_root_abs="/workspace/project-alpha",
        )

        body = build_notification_body(
            anchor, "implementation_request", None, "claude", execution_root=er
        )

        # The durable-anchor contract is preserved verbatim ...
        self.assertIn("durable anchor", body)
        self.assertIn("read it from the source-of-truth", body)
        # ... and the execution-root pointer is appended as a portable,
        # confirm-from-anchor hint (not a new authority).
        self.assertIn("Target execution root: `services/api`", body)
        self.assertIn("confirm it from the durable anchor", body)

    def test_notification_body_unchanged_without_execution_root(self) -> None:
        anchor = normalize_anchor("redmine", issue="12098", journal="59652")

        with_none = build_notification_body(
            anchor, "implementation_request", None, "claude"
        )
        explicit_none = build_notification_body(
            anchor, "implementation_request", None, "claude", execution_root=None
        )

        self.assertEqual(with_none, explicit_none)
        self.assertNotIn("Target execution root", with_none)

    def test_make_outcome_carries_execution_root_in_json(self) -> None:
        anchor = normalize_anchor("redmine", issue="12098", journal="59652")
        er = build_execution_root(
            "/workspace/project-alpha/services/api",
            repo_root_abs="/workspace/project-alpha",
        )

        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_STANDARD,
            kind="implementation_request",
            notification_marker="[marker]",
            execution_root=er,
        )

        payload = json.loads(outcome.to_json())
        self.assertEqual(
            {
                "workdir": "/workspace/project-alpha/services/api",
                "repo_root": "/workspace/project-alpha",
                "relative": "services/api",
            },
            payload["execution_root"],
        )

    def test_make_outcome_execution_root_defaults_to_none(self) -> None:
        anchor = normalize_anchor("redmine", issue="12098", journal="59652")
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_STANDARD,
            kind="implementation_request",
            notification_marker="[marker]",
        )

        self.assertIsNone(outcome.execution_root)
        self.assertIsNone(json.loads(outcome.to_json())["execution_root"])

    def test_delivery_record_shows_relative_pointer_without_absolute(self) -> None:
        # Pasteable record carries the portable repo-relative pointer only;
        # the absolute workdir must never land in a Redmine-pastable record
        # (Redmine #12098 review j#59662; public-private-boundary.md).
        anchor = normalize_anchor("redmine", issue="12098", journal="59652")
        er = build_execution_root(
            "/workspace/project-alpha/services/api",
            repo_root_abs="/workspace/project-alpha",
        )
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_STANDARD,
            kind="implementation_request",
            notification_marker="[marker]",
            execution_root=er,
        )

        record = build_delivery_record(outcome)

        self.assertIn(
            "- Target execution root: `services/api` (relative to the target repo root)",
            record,
        )
        # No absolute path leaks into the pasteable markdown record ...
        self.assertNotIn("/workspace/project-alpha", record)
        self.assertNotIn("abs ", record)
        # ... while the structured outcome still retains it for tooling/replay.
        self.assertEqual(
            "/workspace/project-alpha/services/api",
            json.loads(outcome.to_json())["execution_root"]["workdir"],
        )

    def test_delivery_record_execution_root_dash_when_absent(self) -> None:
        anchor = normalize_anchor("redmine", issue="12098", journal="59652")
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_STANDARD,
            kind="implementation_request",
            notification_marker="[marker]",
        )

        record = build_delivery_record(outcome)

        self.assertIn("- Target execution root: —", record)


class DeliveryRecordTest(unittest.TestCase):
    """Coverage for the durable delivery-record generator.

    The structured outcome contract guarantees every field the record needs
    (after the source-preservation fix on task ``1214760548032349``), so
    ``build_delivery_record`` must be a pure function over a ``DeliveryOutcome``
    and produce a deterministic, source-of-truth-pastable text block for every
    status/reason permutation the primitive can emit.
    """

    def _sent_outcome(self):
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")
        return make_outcome(
            status="sent",
            reason="ok",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_STANDARD,
            kind="implementation_request",
            notification_marker="[mozyo:handoff:source=asana:task=T1:comment=C1:kind=implementation_request:to=claude]",
        )

    def test_sent_record_includes_receiver_target_marker_anchor_and_contract(self) -> None:
        record = build_delivery_record(self._sent_outcome())

        self.assertIn("Delivery result — sent", record)
        self.assertIn("Receiver: `claude`", record)
        self.assertIn("Source: `asana`", record)
        self.assertIn("Kind: `implementation_request`", record)
        self.assertIn("Mode: `standard`", record)
        self.assertIn("Target pane: `%2`", record)
        self.assertIn(
            "Notification marker: "
            "`[mozyo:handoff:source=asana:task=T1:comment=C1:kind=implementation_request:to=claude]`",
            record,
        )
        self.assertIn("Asana task T1", record)
        self.assertIn("comment C1", record)
        self.assertIn("Landing marker observed", record)
        self.assertIn("Enter was pressed", record)
        self.assertIn("Next action owner: `receiver`", record)
        self.assertIn("Receiver-side contract", record)
        self.assertIn("durable anchor", record)

    def test_sent_record_includes_command_line_when_supplied(self) -> None:
        record = build_delivery_record(
            self._sent_outcome(),
            command="mozyo-bridge handoff send --to claude --source asana --task-id T1 --comment-id C1 --kind implementation_request",
        )

        self.assertIn(
            "- Command: `mozyo-bridge handoff send --to claude --source asana "
            "--task-id T1 --comment-id C1 --kind implementation_request`",
            record,
        )

    def test_pending_input_record_labels_operator_action(self) -> None:
        anchor = normalize_anchor("redmine", issue="9020", journal="46005")
        outcome = make_outcome(
            status="pending_input",
            reason="ok",
            receiver="codex",
            target="%111",
            anchor=anchor,
            mode=MODE_PENDING,
            kind="reply",
            notification_marker="[mozyo:handoff:source=redmine:issue=9020:journal=46005:kind=reply:to=codex]",
        )

        record = build_delivery_record(outcome)

        self.assertIn("Delivery result — pending input", record)
        self.assertIn("Mode: `pending`", record)
        self.assertIn("intentionally not pressed", record)
        self.assertIn("Redmine #9020", record)
        self.assertIn("journal #46005", record)
        self.assertIn("Next action owner: `operator`", record)
        self.assertNotIn("Receiver-side contract", record)

    def test_marker_timeout_record_states_rollback_and_sender_action(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")
        outcome = make_outcome(
            status="blocked",
            reason="marker_timeout",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_STANDARD,
            kind="review_result",
            notification_marker="[marker]",
        )

        record = build_delivery_record(outcome)

        self.assertIn("Delivery result — not delivered (marker_timeout)", record)
        # #12188: the narrative claims only that a C-u rollback was issued, not
        # that the receiver composer was verified cleared (a sender cannot
        # confirm composer state from tmux capture).
        self.assertIn("C-u rollback was issued", record)
        self.assertIn("Enter was not pressed", record)
        self.assertIn("cannot verify", record)
        self.assertNotIn("input was cleared via C-u", record)
        self.assertIn("Receiver-side contract", record)
        self.assertIn("manually if action is still required", record)
        self.assertIn("Next action owner: `sender`", record)
        self.assertIn("un-notified", record)
        # Asana task 1214779823377861: the durable record must also surface
        # the ordered fallback path so an auditor (or any agent re-reading
        # the comment later) sees the retry budget before the un-notified
        # terminal label.
        self.assertIn("- Fallback path:", record)
        self.assertIn("mozyo-bridge read claude", record)
        self.assertIn("mozyo-bridge message claude", record)
        self.assertIn("--no-submit", record)
        self.assertIn("Notification fails", record)

    def test_target_unavailable_record_lacks_target_and_marker(self) -> None:
        outcome = make_outcome(
            status="blocked",
            reason="target_unavailable",
            receiver="claude",
            target=None,
            anchor=normalize_anchor("asana", task_id="T1", comment_id="C1"),
            mode=MODE_STANDARD,
            kind="reply",
            notification_marker=None,
        )

        record = build_delivery_record(outcome)

        self.assertIn("Delivery result — not delivered (target_unavailable)", record)
        self.assertIn("Target pane: `—`", record)
        self.assertIn("Notification marker: `—`", record)
        self.assertIn("no notification was typed", record)
        self.assertIn("Next action owner: `sender`", record)
        self.assertIn("mozyo-bridge init claude", record)

    def test_target_not_agent_record_keeps_target_but_no_marker(self) -> None:
        outcome = make_outcome(
            status="blocked",
            reason="target_not_agent",
            receiver="claude",
            target="%2",
            anchor=normalize_anchor("asana", task_id="T1", comment_id="C1"),
            mode=MODE_STANDARD,
            kind="reply",
            notification_marker=None,
        )

        record = build_delivery_record(outcome)

        self.assertIn("Delivery result — not delivered (target_not_agent)", record)
        self.assertIn("Target pane: `%2`", record)
        self.assertIn("Notification marker: `—`", record)
        self.assertIn("not running an agent process", record)
        self.assertIn("--force", record)
        self.assertIn("Next action owner: `sender`", record)

    def test_invalid_anchor_record_preserves_source_without_anchor_payload(self) -> None:
        outcome = make_outcome(
            status="blocked",
            reason="invalid_anchor",
            receiver="claude",
            target=None,
            anchor=None,
            mode=MODE_STANDARD,
            kind="reply",
            notification_marker=None,
            source="asana",
        )

        record = build_delivery_record(outcome)

        self.assertIn("Delivery result — not delivered (invalid_anchor)", record)
        self.assertIn("Source: `asana`", record)
        self.assertIn("Durable anchor: —", record)
        self.assertIn("aborted before resolving the receiver pane", record)
        self.assertIn("supply a valid durable anchor", record)

    def test_invalid_args_record_states_arg_validation_failure(self) -> None:
        outcome = make_outcome(
            status="blocked",
            reason="invalid_args",
            receiver="codex",
            target=None,
            anchor=None,
            mode=MODE_STANDARD,
            kind=None,
            notification_marker=None,
            source="redmine",
        )

        record = build_delivery_record(outcome)

        self.assertIn("Delivery result — not delivered (invalid_args)", record)
        self.assertIn("Source: `redmine`", record)
        self.assertIn("Kind: `—`", record)
        self.assertIn("missing or invalid", record)
        self.assertIn("Next action owner: `sender`", record)

    def test_record_is_deterministic_for_same_outcome(self) -> None:
        outcome = self._sent_outcome()

        self.assertEqual(build_delivery_record(outcome), build_delivery_record(outcome))


class HandoffRecordEmissionTest(unittest.TestCase):
    """The orchestrator must emit the delivery record alongside the structured
    outcome so callers do not have to invent phrasing or re-read the pane to
    describe what happened.
    """

    def run_handoff_with_fake_tmux(
        self,
        argv: list[str],
        captures: list[str] | None = None,
        allow_exit: bool = False,
        pane: dict[str, str] | None = None,
    ):
        # Mirrors HandoffOrchestratorTest's helper so we can drive the CLI end
        # to end without launching tmux.
        parser = build_parser()
        args = parser.parse_args(argv)
        sent: list[tuple[str, ...]] = []
        pane_text = ""
        forced_captures = captures is not None
        capture_outputs = list(captures or [])

        def fake_capture(_target: str, _lines: int) -> str:
            if capture_outputs:
                return capture_outputs.pop(0)
            if forced_captures:
                return ""
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
            raise AssertionError(f"unexpected tmux call: {tmux_args}")

        default_pane = {
            "id": "%2",
            "location": "agents:0.1",
            "command": "node",
            "cwd": "/repo",
            "window_name": "claude",
        }
        pane_value = pane if pane is not None else default_pane

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.capture_pane", side_effect=fake_capture), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch(
                "mozyo_bridge.application.commands.current_session_name",
                return_value=None,
            ), \
            patch("mozyo_bridge.domain.pane_resolver.validate_target"), \
            patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=[pane_value]), \
            contextlib.redirect_stdout(io.StringIO()) as stdout, \
            contextlib.redirect_stderr(io.StringIO()):
            try:
                result = args.func(args)
            except SystemExit as exc:
                if not allow_exit:
                    raise
                result = exc

        return result, sent, stdout.getvalue()

    def test_workdir_propagates_nested_execution_root_to_record_and_body(self) -> None:
        # Redmine #12098: an explicit --workdir below the pane cwd / repo root
        # must surface a repo-relative execution-root pointer in both the typed
        # notification body and the durable delivery record, so the receiver
        # recovers the nested project root without pane scrollback.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp).resolve()
            (repo_root / ".git").mkdir()
            nested = repo_root / "services" / "api"
            nested.mkdir(parents=True)
            pane = {
                "id": "%2",
                "location": "agents:0.1",
                "command": "node",
                "cwd": str(repo_root),
                "window_name": "claude",
            }

            result, sent, stdout = self.run_handoff_with_fake_tmux(
                [
                    "handoff",
                    "send",
                    "--to",
                    "claude",
                    "--source",
                    "redmine",
                    "--kind",
                    "implementation_request",
                    "--issue",
                    "12098",
                    "--journal",
                    "59652",
                    "--target",
                    "%2",
                    "--target-repo",
                    str(repo_root),
                    "--workdir",
                    str(nested),
                    "--mode",
                    "standard",
                    "--submit-delay",
                    "0",
                ],
                pane=pane,
            )

        self.assertEqual(0, result)
        # Durable record carries the portable relative pointer only — the
        # absolute nested path must not leak into a Redmine-pastable record
        # (Redmine #12098 review j#59662).
        self.assertIn(
            "- Target execution root: `services/api` (relative to the target repo root)",
            stdout,
        )
        self.assertNotIn(str(nested), stdout.split("{", 1)[0])
        # The typed pane body carries the portable pointer and keeps the
        # confirm-from-anchor contract, without the absolute path.
        typed = "".join(call[-1] for call in sent if call[:4] == ("send-keys", "-t", "%2", "-l"))
        self.assertIn("Target execution root: `services/api`", typed)
        self.assertIn("confirm it from the durable anchor", typed)
        self.assertNotIn(str(nested), typed)
        # Structured outcome retains both the relative pointer and the absolute
        # workdir for tooling/replay (the runtime fact, separate from the
        # pasteable markdown).
        json_lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        outcome = json.loads(json_lines[-1])
        self.assertEqual("services/api", outcome["execution_root"]["relative"])
        self.assertEqual(str(nested), outcome["execution_root"]["workdir"])

    def test_standard_mode_emits_record_then_json_outcome_by_default(self) -> None:
        # Pinned to `--mode standard` so the record/JSON ordering is verified
        # against the strict-rail happy path (queue-enter has its own coverage
        # in `RelaxedQueueEnterRailTest`). v0.4 default is queue-enter.
        result, _sent, stdout = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "implementation_request",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "standard",
                "--submit-delay",
                "0",
            ]
        )

        self.assertEqual(0, result)
        self.assertIn("Delivery result — sent", stdout)
        self.assertIn("Asana task T1", stdout)
        self.assertIn("Next action owner: `receiver`", stdout)
        json_lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        self.assertEqual(1, len(json_lines), f"expected exactly one JSON outcome line, got: {stdout!r}")
        outcome = json.loads(json_lines[-1])
        self.assertEqual("sent", outcome["status"])

    def test_record_format_json_suppresses_markdown_record(self) -> None:
        # Pinned to `--mode standard` so the format-suppression test is not
        # eclipsed by the v0.4 queue-enter force-rejection.
        result, _sent, stdout = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "implementation_request",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "standard",
                "--submit-delay",
                "0",
                "--record-format",
                "json",
            ]
        )

        self.assertEqual(0, result)
        self.assertNotIn("Delivery result —", stdout)
        json_lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        self.assertEqual(1, len(json_lines))

    def test_record_format_text_suppresses_json_outcome(self) -> None:
        # Pinned to `--mode standard` so the format-suppression test is not
        # eclipsed by the v0.4 queue-enter force-rejection.
        result, _sent, stdout = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "implementation_request",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "standard",
                "--submit-delay",
                "0",
                "--record-format",
                "text",
            ]
        )

        self.assertEqual(0, result)
        self.assertIn("Delivery result — sent", stdout)
        json_lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        self.assertEqual([], json_lines)

    def test_record_command_is_included_when_provided(self) -> None:
        # Pinned to `--mode standard` so the record-command trailer is verified
        # against the strict-rail happy path without the v0.4 queue-enter
        # force-rejection.
        result, _sent, stdout = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "reply",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "standard",
                "--submit-delay",
                "0",
                "--record-command",
                "mozyo-bridge handoff send --to claude --source asana --kind reply",
            ]
        )

        self.assertEqual(0, result)
        self.assertIn(
            "- Command: `mozyo-bridge handoff send --to claude --source asana --kind reply`",
            stdout,
        )

    def test_marker_timeout_emits_record_describing_rollback(self) -> None:
        # Pinned to `--mode standard` so the rollback narrative is verified on
        # the strict rail. v0.4 queue-enter does not roll back on marker miss;
        # that contract is covered in `RelaxedQueueEnterRailTest`.
        result, sent, stdout = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "review_result",
                "--task-id",
                "T1",
                "--anchor-url",
                "https://example/x",
                "--target",
                "%2",
                "--mode",
                "standard",
                "--landing-timeout",
                "0.01",
                "--submit-delay",
                "0",
            ],
            captures=["", "", ""],
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        self.assertIn(("send-keys", "-t", "%2", "C-u"), sent)
        self.assertIn("Delivery result — not delivered (marker_timeout)", stdout)
        # #12188: rollback issued, composer clearing not verified.
        self.assertIn("C-u rollback was issued", stdout)
        self.assertIn("cannot verify", stdout)
        self.assertNotIn("input was cleared via C-u", stdout)
        self.assertIn("Next action owner: `sender`", stdout)

    def test_invalid_anchor_emits_record_preserving_source(self) -> None:
        # Pinned to `--mode standard` so the invalid_anchor narrative is not
        # eclipsed by the v0.4 queue-enter force-rejection (which would emit
        # `invalid_args` first).
        result, _sent, stdout = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "reply",
                "--task-id",
                "T1",
                "--target",
                "%2",
                "--mode",
                "standard",
            ],
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        self.assertIn("Delivery result — not delivered (invalid_anchor)", stdout)
        self.assertIn("Source: `asana`", stdout)
        self.assertIn("Durable anchor: —", stdout)


if __name__ == "__main__":
    unittest.main()
