"""herdr sublane retire guarded-close tests (Redmine #13331 option A, j#73314).

Pins the fail-closed plan (only managed default-lane codex/claude slots are close targets;
a foreign agent is never a target) and the non-fatal executor over a fake herdr.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import unittest
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
    execute_herdr_retire_close,
    plan_herdr_retire_close,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

HERDR_ENV = "MOZYO_HERDR_BINARY"


def _row(ws, role, lane, locator):
    return {"name": encode_assigned_name(ws, role, lane), "pane_id": locator}


class PlanHerdrRetireCloseTest(unittest.TestCase):
    def test_plans_managed_slots_only(self) -> None:
        rows = [
            _row("wsL", "codex", "", "wL:p2"),
            _row("wsL", "claude", "", "wL:p3"),
            # other workspace -> ignored
            _row("wsOther", "codex", "", "wO:p2"),
            # foreign non-mzb1 -> ignored
            {"name": "someones-shell", "pane_id": "wZ:p1"},
            # managed-scheme but non-default lane in THIS workspace -> recorded, not closed
            _row("wsL", "codex", "lane-x", "wL:p9"),
        ]
        plan = plan_herdr_retire_close(rows, workspace_id="wsL")
        self.assertEqual(
            sorted(plan.close_targets), sorted([("codex", "wL:p2"), ("claude", "wL:p3")])
        )
        self.assertTrue(plan.has_targets)
        self.assertEqual(len(plan.foreign_names), 1)  # the lane-x codex

    def test_empty_workspace_id_matches_nothing(self) -> None:
        rows = [_row("wsL", "codex", "", "wL:p2")]
        plan = plan_herdr_retire_close(rows, workspace_id="")
        self.assertFalse(plan.has_targets)

    def test_row_without_locator_not_a_target(self) -> None:
        rows = [{"name": encode_assigned_name("wsL", "codex", ""), "pane_id": ""}]
        plan = plan_herdr_retire_close(rows, workspace_id="wsL")
        self.assertFalse(plan.has_targets)


class _CloseHerdr:
    def __init__(self, *, fail_locators=()):
        self.fail_locators = set(fail_locators)
        self.closed: list[str] = []

    def run(self, argv, capture_output=None, text=None, timeout=None, env=None, **kw):
        rest = list(argv[1:])
        if rest[:2] == ["pane", "close"]:
            locator = rest[2]
            self.closed.append(locator)
            if locator in self.fail_locators:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="refused")
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"result": {"type": "ok"}}), stderr=""
            )
        raise AssertionError(f"unexpected: {argv!r}")


class ExecuteHerdrRetireCloseTest(unittest.TestCase):
    def _env(self, tmp):
        binpath = Path(tmp) / "fake-herdr"
        binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return {HERDR_ENV: str(binpath)}

    def test_closes_managed_targets(self) -> None:
        herdr = _CloseHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_herdr_retire_close(
                [_row("wsL", "codex", "", "wL:p2"), _row("wsL", "claude", "", "wL:p3")],
                workspace_id="wsL",
            )
            result = execute_herdr_retire_close(
                plan, env=self._env(tmp), runner=herdr.run
            )
        self.assertEqual(sorted(herdr.closed), ["wL:p2", "wL:p3"])
        self.assertEqual(len(result.closed), 2)
        self.assertEqual(len(result.failed), 0)

    def test_close_failure_is_non_fatal(self) -> None:
        herdr = _CloseHerdr(fail_locators={"wL:p3"})
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_herdr_retire_close(
                [_row("wsL", "codex", "", "wL:p2"), _row("wsL", "claude", "", "wL:p3")],
                workspace_id="wsL",
            )
            result = execute_herdr_retire_close(
                plan, env=self._env(tmp), runner=herdr.run
            )
        self.assertEqual([r for r, _ in result.closed], ["codex"])
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(result.failed[0][0], "claude")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
