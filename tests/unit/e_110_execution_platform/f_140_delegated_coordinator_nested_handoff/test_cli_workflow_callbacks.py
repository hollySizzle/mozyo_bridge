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

    def _candidate(self, spec: str):
        return cli._parse_candidate(spec)


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

    def test_bare_deliver_fail_closes(self):
        self._ingest_pending()
        with self.assertRaises(SystemExit):
            cli.cmd_workflow_callbacks(_args(deliver=True, store_path=str(self.store_path)))
        # Nothing was delivered — the row stays pending (no unsafe bare-CLI actuation).
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_PENDING)


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
