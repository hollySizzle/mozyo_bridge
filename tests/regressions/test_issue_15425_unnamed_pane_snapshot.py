"""Unnamed unmanaged panes must not poison the terminal identity snapshot (#15425).

Live regression (rc3 cutover, #15422 j#104638): an operator-launched pane rides the
herdr inventory with ``name: None``. The slot-scoped identity join demanded a
canonical name from EVERY row, so that one unrelated pane failed the whole snapshot,
every managed launch's self-lookup died as ``row_ambiguous``, and no attestation was
ever written. FakeHerdr fixtures always name their rows, which is why the hermetic
suite never saw it — these tests pin the live inventory shape.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mozyo_bridge.core.state.startup_execution_events import (
    STAGE_SELF_LOOKUP_FAILED,
    STAGE_SELF_LOOKUP_SUCCEEDED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_agent_attest import (  # noqa: E501
    SELF_LOOKUP_REASON_ROW_AMBIGUOUS,
    SELF_LOOKUP_REASON_SNAPSHOT_INCOMPLETE,
    _match_own_identity,
    bounded_self_lookup,
    perform_self_attestation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_terminal_identity import (  # noqa: E501
    terminal_identity_of_live_slot,
    terminal_identity_of_locator,
    terminal_identity_snapshot_complete,
)

_MANAGED = {"name": "agent", "pane_id": "p1", "terminal_id": "terminal-1"}
_UNNAMED = {"name": None, "pane_id": "p9", "terminal_id": "terminal-9"}


def _fake_herdr(root: Path) -> Path:
    binary = root / "herdr"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


class UnnamedPaneSnapshotRegressionTests(unittest.TestCase):
    def test_snapshot_tolerates_unnamed_unmanaged_rows(self):
        rows = [
            _MANAGED,
            _UNNAMED,
            {"pane_id": "p8", "terminal_id": "terminal-8"},  # name key absent
        ]
        self.assertTrue(terminal_identity_snapshot_complete(rows))
        self.assertEqual(
            terminal_identity_of_live_slot("agent", "p1", rows), "terminal-1"
        )
        self.assertEqual(terminal_identity_of_locator("p1", rows), "terminal-1")
        self.assertEqual(
            _match_own_identity("agent", rows), ("p1", "terminal-1", "")
        )

    def test_unnamed_row_axis_collisions_stay_fail_closed(self):
        for foreign in (
            {"name": None, "pane_id": "p1", "terminal_id": "terminal-9"},
            {"name": None, "pane_id": "p9", "terminal_id": "terminal-1"},
        ):
            with self.subTest(foreign=foreign):
                rows = [_MANAGED, foreign]
                self.assertFalse(terminal_identity_snapshot_complete(rows))
                self.assertIsNone(
                    terminal_identity_of_live_slot("agent", "p1", rows)
                )
                self.assertIsNone(terminal_identity_of_locator("p1", rows))

    def test_malformed_foreign_rows_still_fail_closed(self):
        for foreign in (
            {"name": "", "pane_id": "p9", "terminal_id": "terminal-9"},
            {"name": " padded ", "pane_id": "p9", "terminal_id": "terminal-9"},
            {"name": 7, "pane_id": "p9", "terminal_id": "terminal-9"},
            {"name": None, "pane_id": "", "terminal_id": "terminal-9"},
            {"name": None, "terminal_id": "terminal-9"},
            {"name": None, "pane_id": "p9", "terminal_id": " "},
            {"name": None, "pane_id": "p9"},
        ):
            with self.subTest(foreign=foreign):
                rows = [_MANAGED, foreign]
                self.assertFalse(terminal_identity_snapshot_complete(rows))
                self.assertIsNone(
                    terminal_identity_of_live_slot("agent", "p1", rows)
                )

    def test_snapshot_failure_is_not_reported_as_row_ambiguous(self):
        incomplete = [
            _MANAGED,
            {"name": " padded ", "pane_id": "p9", "terminal_id": "terminal-9"},
        ]
        self.assertEqual(
            _match_own_identity("agent", incomplete),
            ("", "", SELF_LOOKUP_REASON_SNAPSHOT_INCOMPLETE),
        )
        duplicate = [
            _MANAGED,
            {"name": "agent", "pane_id": "p2", "terminal_id": "terminal-2"},
        ]
        self.assertEqual(
            _match_own_identity("agent", duplicate),
            ("", "", SELF_LOOKUP_REASON_ROW_AMBIGUOUS),
        )

    def test_live_self_lookup_and_attestation_succeed_beside_unnamed_pane(self):
        payload = json.dumps([_MANAGED, _UNNAMED])
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env = {"MOZYO_HERDR_BINARY": str(_fake_herdr(root))}
            runner = lambda argv, **_kw: SimpleNamespace(  # noqa: E731
                returncode=0, stdout=payload, stderr=""
            )
            self.assertEqual(
                bounded_self_lookup("agent", env, runner=runner),
                ("p1", "terminal-1", STAGE_SELF_LOOKUP_SUCCEEDED, ""),
            )
            record = perform_self_attestation(
                assigned_name="agent",
                workspace_id="ws",
                role="codex",
                lane="default",
                env=env,
                home=root / "home",
                runner=runner,
            )
            self.assertIsNotNone(record)
            self.assertEqual(record.locator, "p1")
            self.assertEqual(record.terminal_id, "terminal-1")

    def test_incomplete_snapshot_fails_typed_without_retry(self):
        payload = json.dumps(
            [_MANAGED, {"name": "", "pane_id": "p9", "terminal_id": "terminal-9"}]
        )
        calls = []
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env = {"MOZYO_HERDR_BINARY": str(_fake_herdr(root))}

            def runner(argv, **_kw):
                calls.append(argv)
                return SimpleNamespace(returncode=0, stdout=payload, stderr="")

            self.assertEqual(
                bounded_self_lookup("agent", env, runner=runner),
                (
                    "",
                    "",
                    STAGE_SELF_LOOKUP_FAILED,
                    SELF_LOOKUP_REASON_SNAPSHOT_INCOMPLETE,
                ),
            )
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
