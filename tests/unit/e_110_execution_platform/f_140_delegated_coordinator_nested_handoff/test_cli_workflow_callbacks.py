"""`workflow callbacks` CLI facade tests (Redmine #13520 / US #13518).

Drives the semantic facade over the callback outbox hermetically (a temp store + a
``--redmine-json`` snapshot + a patched sender):

- ``--ingest`` classifies against the exact source journal and enqueues (pending / dead_letter);
- ``--sweep`` reconciles inflight and surfaces the backlog (sends nothing);
- ``--deliver`` fires one send per row through the injected sender and maps the outcome;
- a bare ``--deliver`` (no configured sender) fail-closes rather than actuate a live handoff;
- the command is registered under ``workflow`` so it is reachable via the mozyo facade.
"""

from __future__ import annotations

import argparse
import json as _json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
from mozyo_bridge.core.state.workflow_runtime_store import (
    CALLBACK_DEAD_LETTER,
    CALLBACK_DELIVERED,
    CALLBACK_PENDING,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    cli_workflow_callbacks as cli,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    SEND_DELIVERED,
)

_SNAPSHOT = {
    "issue": {
        "id": "13518",
        "journals": [
            {"id": "75094", "notes": "impl [mozyo:workflow-event:gate=implementation_done]"},
            {"id": "75096", "notes": "review [mozyo:workflow-event:gate=review_request]"},
        ],
    }
}


def _args(**over) -> argparse.Namespace:
    base = dict(
        json=False,
        store_path=None,
        sweep=False,
        ingest=False,
        deliver=False,
        run_once=False,
        watch=False,
        max_passes=1,
        candidate=None,
        redmine_json=None,
        poll=False,
        source_issue=None,
        since=None,
        cursor=None,
        limit=32,
    )
    base.update(over)
    return argparse.Namespace(**base)


class _CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_path = Path(self._tmp.name) / "wf.sqlite"
        self.snapshot = Path(self._tmp.name) / "issue.json"
        self.snapshot.write_text(_json.dumps(_SNAPSHOT), encoding="utf-8")
        self.outbox = CallbackOutbox(path=self.store_path)
        # #13518 review R3-F3: pin a deterministic workspace so the mutating actions (deliver /
        # run-once / sweep) run partitioned + hermetic, independent of ambient MOZYO_WORKSPACE_ID
        # or the dev machine's workspace anchor. The fail-closed / --allow-unpartitioned behaviour
        # is exercised explicitly in PartitionRequirementTest, which re-patches this seam.
        self._orig_resolve_ws = cli._resolve_workspace_id
        cli._resolve_workspace_id = lambda args: "ws_cli_test"
        self.addCleanup(setattr, cli, "_resolve_workspace_id", self._orig_resolve_ws)

    def _candidate(self, spec: str):
        return cli._parse_candidate(spec)


class WakeWaitFnTest(unittest.TestCase):
    """#13520 review F1b: --watch binds the real Herdr event when a --wake-target resolves."""

    def test_no_wake_target_falls_back_to_bounded_interval(self):
        wait = cli._wake_wait_fn(_args(watch=True, wake_interval=0.0))
        self.assertFalse(wait())  # bounded sleep with interval 0 returns a falsy timeout hint

    def test_wake_target_builds_the_stable_herdr_event_wait(self):
        from unittest.mock import patch
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure import (
            herdr_transport,
        )

        recorded = {}

        class _Bin:
            path = "/opt/herdr"

        def _fake_build(binary, target, *, status, timeout_ms, runner=None):
            recorded.update(binary=binary, target=target, status=status, timeout_ms=timeout_ms)
            return lambda: True

        with patch.object(herdr_transport, "resolve_herdr_binary", lambda env: _Bin()), patch(
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.callback_wake.build_herdr_event_wait",
            _fake_build,
        ):
            wait = cli._wake_wait_fn(
                _args(watch=True, wake_target="mzb1_ws_codex_default",
                      wake_status="working", wake_timeout_ms=42000)
            )
        self.assertTrue(wait())
        self.assertEqual(recorded["target"], "mzb1_ws_codex_default")
        self.assertEqual(recorded["binary"], "/opt/herdr")
        self.assertEqual((recorded["status"], recorded["timeout_ms"]), ("working", 42000))

    def test_unresolvable_binary_falls_back_to_sleep(self):
        from unittest.mock import patch
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure import (
            herdr_transport,
        )

        def _raise(env):
            raise RuntimeError("herdr not on trusted PATH")

        with patch.object(herdr_transport, "resolve_herdr_binary", _raise):
            wait = cli._wake_wait_fn(_args(watch=True, wake_target="t", wake_interval=0.0))
        self.assertFalse(wait())  # fail-safe fallback: bounded sleep hint, never a crash


class RecoveryPlanMeasurementTest(unittest.TestCase):
    """#13520 review R2-F3: --recovery-plan MEASURES authorities; never hard-codes unknown->safe."""

    def _run_capture_obs(self, tmp, **over):
        from unittest.mock import patch
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            callback_recovery_command as cmd,
        )

        captured = {}
        _orig = cmd.recovery_plan_from_observation

        def _capture(obs):
            captured["obs"] = obs
            return _orig(obs)

        ns = argparse.Namespace(
            recovery_plan=True, json=True, store_path=str(tmp / "wf.sqlite"),
            workspace_id=None, anchor_readable=False, repo=None,
            sweep=False, ingest=False, deliver=False, run_once=False, watch=False, emit_gate=False,
        )
        ns.__dict__.update(over)
        with patch(
            "mozyo_bridge.core.state.workspace_registry.read_anchor",
            lambda repo_root: {"workspace_id": "wsX"},
        ), patch.object(cmd, "recovery_plan_from_observation", _capture), patch(
            "mozyo_bridge.application.commands_common.repo_root_from_args",
            lambda args: Path(tmp),
        ):
            cli.cmd_workflow_callbacks(ns)
        return captured["obs"]

    def test_anchor_readable_not_hardcoded_true_when_flag_unset(self):
        with tempfile.TemporaryDirectory() as t:
            obs = self._run_capture_obs(Path(t), anchor_readable=False)
        self.assertFalse(obs.redmine_anchor_readable)  # unverified -> fail-closed, NOT True

    def test_anchor_readable_asserted_by_flag(self):
        with tempfile.TemporaryDirectory() as t:
            obs = self._run_capture_obs(Path(t), anchor_readable=True)
        self.assertTrue(obs.redmine_anchor_readable)

    def test_outbox_present_measured_from_store_absence(self):
        with tempfile.TemporaryDirectory() as t:
            obs = self._run_capture_obs(Path(t))  # store file does not exist
        self.assertFalse(obs.outbox_present)  # measured, not hard-coded True

    def test_expected_workspace_not_self_matched_to_registry(self):
        with tempfile.TemporaryDirectory() as t:
            obs = self._run_capture_obs(Path(t), workspace_id=None)
        # --workspace-id unset -> expected is blank (unverified), NEVER silently == registry.
        self.assertEqual(obs.workspace_id_expected, "")
        self.assertEqual(obs.workspace_id_registry, "wsX")


class WatchPassSummaryTest(unittest.TestCase):
    """#13520 review R2-F2: the CLI renders an error pass without KeyError and surfaces it."""

    def test_error_pass_is_surfaced_not_keyerror(self):
        self.assertEqual(cli._watch_pass_summary({"error": "RuntimeError"}), "error=RuntimeError")

    def test_normal_pass_shows_delivered_count(self):
        self.assertEqual(
            cli._watch_pass_summary({"deliver": {"delivered": [1, 2]}}), "delivered=2"
        )

    def test_malformed_pass_is_safe(self):
        self.assertEqual(cli._watch_pass_summary(None), "error=malformed_pass")
        self.assertEqual(cli._watch_pass_summary({}), "delivered=0")


class RegistrationTest(unittest.TestCase):
    def test_callbacks_is_registered_under_workflow(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow import (
            register,
        )

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        register(sub)
        ns = parser.parse_args(["workflow", "callbacks", "--sweep"])
        self.assertTrue(ns.sweep)
        self.assertIs(ns.func, cli.cmd_workflow_callbacks)


class IngestCliTest(_CliTestCase):
    def test_ingest_classifies_and_enqueues(self):
        rc = cli.cmd_workflow_callbacks(
            _args(
                ingest=True,
                store_path=str(self.store_path),
                redmine_json=str(self.snapshot),
                candidate=[
                    self._candidate("13518:75094:coordinator:implementation_done"),
                    self._candidate("13518:99999:coordinator:implementation_done"),
                ],
                cursor="75096",
            )
        )
        self.assertEqual(rc, 0)
        self.assertEqual([r.journal for r in self.outbox.read(states=[CALLBACK_PENDING])], ["75094"])
        self.assertEqual(
            [r.journal for r in self.outbox.read(states=[CALLBACK_DEAD_LETTER])], ["99999"]
        )
        self.assertEqual(self.outbox.read_cursor("redmine"), "75096")

    def test_ingest_requires_a_source(self):
        with self.assertRaises(SystemExit):
            cli.cmd_workflow_callbacks(
                _args(
                    ingest=True,
                    store_path=str(self.store_path),
                    candidate=[self._candidate("13518:75094:coordinator")],
                )
            )

    def test_ingest_requires_a_candidate(self):
        with self.assertRaises(SystemExit):
            cli.cmd_workflow_callbacks(
                _args(ingest=True, store_path=str(self.store_path), redmine_json=str(self.snapshot))
            )


class SweepCliTest(_CliTestCase):
    def test_sweep_surfaces_backlog_and_sends_nothing(self):
        cli.cmd_workflow_callbacks(
            _args(
                ingest=True,
                store_path=str(self.store_path),
                redmine_json=str(self.snapshot),
                candidate=[self._candidate("13518:99999:coordinator:implementation_done")],
            )
        )
        rc = cli.cmd_workflow_callbacks(_args(sweep=True, store_path=str(self.store_path)))
        self.assertEqual(rc, 0)
        # Sweep never deletes / delivers; the dead-letter row is still surfaced, unchanged.
        self.assertEqual(len(self.outbox.read(states=[CALLBACK_DEAD_LETTER])), 1)


class DeliverCliTest(_CliTestCase):
    def _ingest_pending(self):
        cli.cmd_workflow_callbacks(
            _args(
                ingest=True,
                store_path=str(self.store_path),
                redmine_json=str(self.snapshot),
                candidate=[self._candidate("13518:75094:coordinator:implementation_done")],
            )
        )

    def test_deliver_with_injected_sender_delivers(self):
        self._ingest_pending()
        orig = cli._callback_sender
        cli._callback_sender = lambda args: (lambda row: SEND_DELIVERED)
        try:
            rc = cli.cmd_workflow_callbacks(_args(deliver=True, store_path=str(self.store_path)))
        finally:
            cli._callback_sender = orig
        self.assertEqual(rc, 0)
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DELIVERED)

    def test_callback_sender_builds_a_real_sender(self):
        # F1 (j#75147): _callback_sender no longer unconditionally fail-closes; it builds a real
        # HandoffCallbackSender over the handoff send port (safety = outbox fence + QA anchors).
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.handoff_callback_sender import (
            HandoffCallbackSender,
        )

        sender = cli._callback_sender(_args())
        self.assertIsInstance(sender, HandoffCallbackSender)


class RunOnceCliTest(_CliTestCase):
    def test_run_once_ingests_delivers_and_sweeps(self):
        orig = cli._callback_sender
        cli._callback_sender = lambda args: (lambda row: SEND_DELIVERED)
        try:
            rc = cli.cmd_workflow_callbacks(
                _args(
                    run_once=True, store_path=str(self.store_path), redmine_json=str(self.snapshot),
                    candidate=[self._candidate("13518:75094:coordinator:implementation_done")],
                )
            )
        finally:
            cli._callback_sender = orig
        self.assertEqual(rc, 0)
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DELIVERED)

    def test_run_once_discovers_candidates_from_source_issue(self):
        # F1-R1: --run-once with --source-issue discovers gate candidates from the issue's
        # structured markers (no explicit --candidate needed).
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
            render_workflow_event_marker,
        )

        snapshot = Path(self._tmp.name) / "gate.json"
        snapshot.write_text(
            _json.dumps(
                {"issue": {"id": "13543", "journals": [
                    {"id": "75212", "notes": f"review {render_workflow_event_marker('review_request')}"}]}}
            ),
            encoding="utf-8",
        )
        orig = cli._callback_sender
        cli._callback_sender = lambda args: (lambda row: SEND_DELIVERED)
        try:
            rc = cli.cmd_workflow_callbacks(
                _args(
                    run_once=True, store_path=str(self.store_path),
                    redmine_json=str(snapshot), source_issue="13543",
                )
            )
        finally:
            cli._callback_sender = orig
        self.assertEqual(rc, 0)
        row = self.outbox.read()[0]
        self.assertEqual(
            (row.journal, row.normalized_gate, row.state), ("75212", "review_request", CALLBACK_DELIVERED)
        )

    def test_watch_runs_bounded_passes(self):
        orig = cli._callback_sender
        cli._callback_sender = lambda args: (lambda row: SEND_DELIVERED)
        try:
            rc = cli.cmd_workflow_callbacks(
                _args(watch=True, store_path=str(self.store_path), max_passes=2)
            )
        finally:
            cli._callback_sender = orig
        self.assertEqual(rc, 0)


class PartitionRequirementTest(_CliTestCase):
    """#13518 review R3-F3: a mutating action over a shared home DB must REQUIRE a resolved
    workspace and claim / reclaim / route exactly that partition. An env-less / anchor-less
    process fails closed (zero claims / sends), and the legacy all-workspace bucket is reachable
    only behind the explicit --allow-unpartitioned-callbacks debug/migration surface."""

    def _seed_foreign_pending(self, workspace_id: str) -> None:
        from mozyo_bridge.core.state.callback_outbox import CallbackOutboxKey

        self.outbox.enqueue(
            CallbackOutboxKey(
                source="redmine", issue="13518", journal="75094",
                normalized_gate="implementation_done", callback_route="coordinator",
                workspace_id=workspace_id,
            ),
            notification_kind="implementation_done",
        )

    def test_envless_deliver_fails_closed_and_sends_nothing(self):
        # No resolvable workspace + no --allow-unpartitioned: refuse before any claim / send.
        cli._resolve_workspace_id = lambda args: ""
        self._seed_foreign_pending("ws_foreign")
        sends = []
        cli._callback_sender = lambda args: (lambda row: sends.append(row) or SEND_DELIVERED)
        with self.assertRaises(SystemExit):
            cli.cmd_workflow_callbacks(_args(deliver=True, store_path=str(self.store_path)))
        self.assertEqual(sends, [])  # zero sends
        # The foreign row was never claimed — it stays pending for its own workspace's sender.
        self.assertEqual([r.workspace_id for r in self.outbox.read(states=[CALLBACK_PENDING])], ["ws_foreign"])

    def test_envless_run_once_fails_closed(self):
        cli._resolve_workspace_id = lambda args: ""
        with self.assertRaises(SystemExit):
            cli.cmd_workflow_callbacks(_args(run_once=True, store_path=str(self.store_path)))

    def test_envless_sweep_fails_closed(self):
        cli._resolve_workspace_id = lambda args: ""
        with self.assertRaises(SystemExit):
            cli.cmd_workflow_callbacks(_args(sweep=True, store_path=str(self.store_path)))

    def test_allow_unpartitioned_claims_legacy_bucket(self):
        # The explicit debug/migration surface restores the legacy all-workspace claim/send.
        cli._resolve_workspace_id = lambda args: ""
        self._seed_foreign_pending("ws_foreign")
        sends = []
        cli._callback_sender = lambda args: (lambda row: sends.append(row) or SEND_DELIVERED)
        rc = cli.cmd_workflow_callbacks(
            _args(deliver=True, store_path=str(self.store_path), allow_unpartitioned_callbacks=True)
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(sends), 1)  # legacy bucket: the foreign row is claimed + sent
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DELIVERED)

    def test_partitioned_deliver_ignores_foreign_workspace_row(self):
        # A resolved workspace claims ONLY its own partition; a foreign row is left untouched.
        cli._resolve_workspace_id = lambda args: "ws_mine"
        self._seed_foreign_pending("ws_foreign")
        sends = []
        cli._callback_sender = lambda args: (lambda row: sends.append(row) or SEND_DELIVERED)
        rc = cli.cmd_workflow_callbacks(_args(deliver=True, store_path=str(self.store_path)))
        self.assertEqual(rc, 0)
        self.assertEqual(sends, [])  # the foreign row is not claimed by ws_mine
        self.assertEqual([r.workspace_id for r in self.outbox.read(states=[CALLBACK_PENDING])], ["ws_foreign"])


class ParseCandidateTest(unittest.TestCase):
    def test_full_spec(self):
        c = cli._parse_candidate("13518:75094:coordinator:review_request")
        self.assertEqual(
            (c.issue, c.journal, c.callback_route, c.notification_kind),
            ("13518", "75094", "coordinator", "review_request"),
        )

    def test_kind_optional(self):
        c = cli._parse_candidate("13518:75094:coordinator")
        self.assertEqual(c.notification_kind, "")

    def test_missing_field_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            cli._parse_candidate("13518:75094")


if __name__ == "__main__":
    unittest.main()
