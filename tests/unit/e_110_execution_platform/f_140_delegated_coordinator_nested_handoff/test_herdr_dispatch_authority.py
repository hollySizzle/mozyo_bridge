"""Live dispatch-authority resolution tests with injected sources (Redmine #13489 increment 2).

Drives :func:`resolve_dispatch_decision` hermetically: a fake Redmine journal source + a fake
inventory reader exercise authorize / monitor / fail-closed, including supersede, target drift,
ambiguity, mid-turn runtime, credential failure, and unreadable inventory.
"""

from __future__ import annotations

import argparse
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_authority import (
    resolve_dispatch_decision,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authority import (
    AUTHORIZE,
    BLOCKED,
    MONITOR,
    REASON_AUTHORIZATION_SUPERSEDED,
    REASON_NO_AUTHORIZATION,
    REASON_REDMINE_UNAVAILABLE,
    REASON_RUNTIME_NOT_READY,
    REASON_RUNTIME_UNAVAILABLE,
    REASON_TARGET_ABSENT,
    REASON_TARGET_AMBIGUOUS,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (
    build_dispatch_authorization_marker,
)

WS = "ws1"
LANE = "issue_13489"
ISSUE = "13489"
TARGET = "mzb1_ws1_claude_issue_13489"


@dataclass(frozen=True)
class _Entry:
    issue_id: str
    journal_id: str
    notes: str


class _FakeSource:
    def __init__(self, entries):
        self._entries = entries

    def read_entries(self, issue):
        return [e for e in self._entries if e.issue_id == issue]


class _RaisingSource:
    def read_entries(self, issue):
        raise RuntimeError("credentials unconfigured (redacted)")


def _auth_note(journal, **over):
    fields = dict(
        action_id="act-1",
        source_gate="74999",
        issue=ISSUE,
        workspace_id=WS,
        lane_id=LANE,
        target_assigned_name=TARGET,
    )
    fields.update(over)
    return _Entry(ISSUE, journal, "coordinator authorizes\n" + build_dispatch_authorization_marker(**fields))


def _gate_note(journal, gate):
    return _Entry(ISSUE, journal, f"[mozyo:workflow-event:gate={gate}:issue={ISSUE}]")


def _rows(*names_states):
    """Build fake `agent list` rows: (assigned_name, herdr_status)."""
    return [{"name": name, "agent_status": status} for name, status in names_states]


def _decide(entries, rows, *, source=None, agent_rows=None):
    args = argparse.Namespace()
    return resolve_dispatch_decision(
        args,
        workspace_id=WS,
        lane_id=LANE,
        issue=ISSUE,
        env={},
        journal_source_factory=(lambda a: source if source is not None else _FakeSource(entries)),
        agent_rows=(agent_rows if agent_rows is not None else (lambda env: rows)),
    )


class ResolveTest(unittest.TestCase):
    def test_authorize_when_authorized_and_idle(self):
        d = _decide([_auth_note("75010")], _rows((TARGET, "idle")))
        self.assertEqual(d.decision, AUTHORIZE)
        self.assertIsNotNone(d.authorization)
        self.assertEqual(d.authorization.action_id, "act-1")

    def test_no_authorization_is_monitor(self):
        d = _decide([_Entry(ISSUE, "1", "just a note")], _rows((TARGET, "idle")))
        self.assertEqual(d.decision, MONITOR)
        self.assertEqual(d.reason, REASON_NO_AUTHORIZATION)

    def test_superseded_by_later_gate_is_monitor(self):
        entries = [_auth_note("75010"), _gate_note("75020", "implementation_done")]
        d = _decide(entries, _rows((TARGET, "idle")))
        self.assertEqual(d.decision, MONITOR)
        self.assertEqual(d.reason, REASON_AUTHORIZATION_SUPERSEDED)

    def test_earlier_gate_does_not_supersede(self):
        # A gate BEFORE the authorization must not supersede it.
        entries = [_gate_note("75000", "review_result"), _auth_note("75010")]
        d = _decide(entries, _rows((TARGET, "idle")))
        self.assertEqual(d.decision, AUTHORIZE)

    def test_target_drift_is_blocked_absent(self):
        # The live worker renamed / different -> the exact target is absent.
        d = _decide([_auth_note("75010")], _rows(("mzb1_ws1_claude_other", "idle")))
        self.assertEqual(d.decision, BLOCKED)
        self.assertEqual(d.reason, REASON_TARGET_ABSENT)

    def test_duplicate_target_is_blocked_ambiguous(self):
        d = _decide([_auth_note("75010")], _rows((TARGET, "idle"), (TARGET, "working")))
        self.assertEqual(d.decision, BLOCKED)
        self.assertEqual(d.reason, REASON_TARGET_AMBIGUOUS)

    def test_busy_target_is_monitor(self):
        d = _decide([_auth_note("75010")], _rows((TARGET, "working")))
        self.assertEqual(d.decision, MONITOR)
        self.assertEqual(d.reason, REASON_RUNTIME_NOT_READY)

    def test_credential_failure_is_monitor_zero_send(self):
        # A Redmine read failure degrades to MONITOR (zero send), preserving the gateway's
        # resolution-only monitor no-op rather than hard-blocking a lane with no credentials.
        d = _decide([], [], source=_RaisingSource())
        self.assertEqual(d.decision, MONITOR)
        self.assertEqual(d.reason, REASON_REDMINE_UNAVAILABLE)

    def test_unreadable_inventory_is_blocked(self):
        def _raise(env):
            raise RuntimeError("inventory read failed")

        d = _decide([_auth_note("75010")], None, agent_rows=_raise)
        self.assertEqual(d.decision, BLOCKED)
        self.assertEqual(d.reason, REASON_RUNTIME_UNAVAILABLE)

    def test_reauthorization_latest_wins(self):
        # A later authorization row supersedes an earlier one (note order).
        entries = [
            _auth_note("75010", action_id="old"),
            _auth_note("75020", action_id="new"),
        ]
        d = _decide(entries, _rows((TARGET, "idle")))
        self.assertEqual(d.decision, AUTHORIZE)
        self.assertEqual(d.authorization.action_id, "new")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
