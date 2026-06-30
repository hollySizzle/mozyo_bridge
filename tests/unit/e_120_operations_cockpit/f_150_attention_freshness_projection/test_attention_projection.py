"""tmux attention user-option projection tests (Redmine #11954).

Pins the pure ``set-option`` plan generation, the safe-by-default dry-run /
``--apply`` behavior of ``agents attention-project``, and the projection-cache /
non-routing boundary.
"""

from __future__ import annotations

import argparse
import contextlib
import inspect
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from types import SimpleNamespace

from mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.application import attention_projection
from mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.application.attention_projection import (
    ATTENTION_REASON_OPTION,
    ATTENTION_SEVERITY_OPTION,
    ATTENTION_STATE_OPTION,
    ATTENTION_UPDATED_AT_OPTION,
    build_attention_option_plan,
)
from mozyo_bridge.application.commands_agents import (
    AttentionProjectionEntry,
    ProjectAttentionUseCase,
)
from mozyo_bridge.application.tmux_option_port import (
    LiveTmuxOptionWriter,
    TmuxOptionWriterPort,
)
from mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.domain.attention import AttentionInputs, derive_attention


class _RecordingOptionWriter:
    """Fake :class:`TmuxOptionWriterPort` that records applied argv.

    Replaces the previous ``patch.object(commands, "run_tmux")`` monkeypatch seam
    for the attention-projection write boundary (Redmine #12785): the side effect
    is now injected as a port, so the test asserts the recorded writes instead of
    patching a module-level function.
    """

    def __init__(self, fail_option=None):
        self.writes: list[tuple] = []
        self._fail_option = fail_option

    def set_option(self, argv):
        argv = tuple(argv)
        self.writes.append(argv)
        if self._fail_option and len(argv) >= 5 and argv[4] == self._fail_option:
            return False
        return True


def _pane(pane_id, location, *, command="node", cwd="/work/repo",
          window_name="cockpit", pane_active="1", agent_role="",
          lane_id="", lane_label=""):
    return {
        "id": pane_id,
        "location": location,
        "command": command,
        "cwd": cwd,
        "window_name": window_name,
        "pane_active": pane_active,
        "agent_role": agent_role,
        "workspace_id": "",
        "lane_id": lane_id,
        "lane_label": lane_label,
    }


class BuildAttentionOptionPlanTest(unittest.TestCase):
    def _record(self, **over):
        base = dict(
            unit_id="unit:local:wsA:default",
            observed_at="2026-06-15T00:00:00Z",
            workspace_id="wsA",
            role="claude",
            target_key="tmux:local:%9",
            reason_code="no_attention_source",
        )
        base.update(over)
        return derive_attention(AttentionInputs(**base))

    def test_plan_sets_all_four_options(self) -> None:
        plan = build_attention_option_plan("%9", self._record())
        self.assertEqual(len(plan), 4)
        names = [argv[4] for argv in plan]
        self.assertEqual(
            names,
            [
                ATTENTION_STATE_OPTION,
                ATTENTION_SEVERITY_OPTION,
                ATTENTION_REASON_OPTION,
                ATTENTION_UPDATED_AT_OPTION,
            ],
        )
        # Each is a pane-scoped set-option for the given pane.
        for argv in plan:
            self.assertEqual(argv[:4], ("set-option", "-p", "-t", "%9"))

    def test_plan_values_match_record(self) -> None:
        rec = self._record()
        plan = dict((argv[4], argv[5]) for argv in build_attention_option_plan("%9", rec))
        self.assertEqual(plan[ATTENTION_STATE_OPTION], rec.attention_state)
        self.assertEqual(plan[ATTENTION_SEVERITY_OPTION], rec.severity)
        self.assertEqual(plan[ATTENTION_REASON_OPTION], rec.reason_code)
        self.assertEqual(plan[ATTENTION_UPDATED_AT_OPTION], rec.observed_at)

    def test_empty_plan_without_pane_id(self) -> None:
        self.assertEqual(build_attention_option_plan(None, self._record()), [])
        self.assertEqual(build_attention_option_plan("", self._record()), [])

    def test_module_does_not_import_tmux_or_routing(self) -> None:
        # Scope the check to import lines so docstring mentions of "handoff
        # preflight" (the boundary it explicitly does NOT cross) don't false-fail.
        import_lines = "\n".join(
            line
            for line in inspect.getsource(attention_projection).splitlines()
            if line.startswith(("import ", "from "))
        )
        for forbidden in (
            "tmux_client",
            "handoff",
            "pane_resolver",
            "infrastructure",
            "run_tmux",
        ):
            self.assertNotIn(forbidden, import_lines)


class AttentionProjectCommandTest(unittest.TestCase):
    def _run(self, panes, *, apply=False, dry_run=False, as_json=False,
             fail_option=None):
        from mozyo_bridge.application import commands
        from mozyo_bridge.application import commands_agents

        canon = argparse.Namespace(name="mozyo-bridge", workspace_id="wsA")
        args = argparse.Namespace(
            session=None, agent=None, apply=apply, dry_run=dry_run, as_json=as_json
        )
        # The tmux pane-option write boundary is now a port: inject a recording
        # fake instead of patching ``commands.run_tmux`` (Redmine #12749 / #12785).
        # The discovery deps the residual ``_agents_target_candidates`` reads stay
        # patched on ``commands``; the availability guard moved with the handler to
        # ``commands_agents``.
        writer = _RecordingOptionWriter(fail_option=fail_option)

        with patch.object(commands_agents, "require_tmux"), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery.pane_lines", return_value=panes), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery.infer_repo_root",
                  return_value="/work/repo"), \
            patch.object(commands, "resolve_canonical_session", return_value=canon), \
            patch.object(commands, "_probe_checkout_facts",
                         return_value={"branch": "main"}), \
            patch.object(commands_agents, "LiveTmuxOptionWriter", return_value=writer), \
            contextlib.redirect_stdout(io.StringIO()) as out:
            rc = commands.cmd_agents_attention_project(args)
        return rc, out.getvalue(), writer.writes

    def test_default_is_preview_and_writes_nothing(self) -> None:
        rc, out, calls = self._run([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex", agent_role="claude"),
        ])
        self.assertEqual(0, rc)
        self.assertIn("(dry-run)", out)
        self.assertIn("@mozyo_attention_state", out)  # plan is shown
        # Safe default: no tmux mutation happened.
        self.assertEqual([], calls)

    def test_dry_run_flag_overrides_apply(self) -> None:
        rc, out, calls = self._run([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex", agent_role="claude"),
        ], apply=True, dry_run=True)
        self.assertEqual(0, rc)
        self.assertIn("(dry-run)", out)
        self.assertEqual([], calls)  # dry-run wins

    def test_apply_writes_set_option_per_pane(self) -> None:
        rc, out, calls = self._run([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex", agent_role="claude"),
        ], apply=True)
        self.assertEqual(0, rc)
        self.assertIn("projected", out)
        # Four set-option writes for the single pane.
        self.assertEqual(len(calls), 4)
        for argv in calls:
            self.assertEqual(argv[:4], ("set-option", "-p", "-t", "%9"))
        names = {argv[4] for argv in calls}
        self.assertEqual(
            names,
            {
                ATTENTION_STATE_OPTION,
                ATTENTION_SEVERITY_OPTION,
                ATTENTION_REASON_OPTION,
                ATTENTION_UPDATED_AT_OPTION,
            },
        )

    def test_apply_only_writes_set_option_never_routing(self) -> None:
        # Non-routing boundary: the only tmux verbs the command issues are
        # set-option (the projection); it never sends keys / selects panes /
        # routes a handoff.
        _, _, calls = self._run([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex", agent_role="claude"),
        ], apply=True)
        verbs = {argv[0] for argv in calls}
        self.assertEqual(verbs, {"set-option"})

    def test_json_emits_plan_without_writing_in_preview(self) -> None:
        rc, out, calls = self._run([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex", agent_role="claude"),
        ], as_json=True)
        self.assertEqual(0, rc)
        payload = json.loads(out)
        self.assertEqual(len(payload), 1)
        entry = payload[0]
        self.assertEqual(entry["pane_id"], "%9")
        self.assertFalse(entry["applied"])
        self.assertIsNone(entry["applied_ok"])  # preview attempted no write
        self.assertEqual(entry["attention"]["attention_state"], "healthy")
        self.assertEqual(len(entry["plan"]), 4)
        self.assertEqual([], calls)  # --json default is preview

    def test_json_apply_actually_writes_and_reports_true(self) -> None:
        # Regression for #58539: --json --apply must perform the writes and only
        # then report applied, not claim applied while mutating nothing.
        rc, out, calls = self._run([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex", agent_role="claude"),
        ], apply=True, as_json=True)
        self.assertEqual(0, rc)
        # Writes actually happened (4 set-options for the pane).
        self.assertEqual(len(calls), 4)
        self.assertEqual({argv[0] for argv in calls}, {"set-option"})
        entry = json.loads(out)[0]
        self.assertTrue(entry["applied"])
        self.assertTrue(entry["applied_ok"])

    def test_json_apply_reports_partial_on_write_failure(self) -> None:
        # A failed option write is recorded as applied_ok=false (best-effort),
        # while the writes were still attempted.
        rc, out, calls = self._run([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex", agent_role="claude"),
        ], apply=True, as_json=True, fail_option=ATTENTION_STATE_OPTION)
        self.assertEqual(0, rc)
        self.assertEqual(len(calls), 4)  # still attempted all writes
        entry = json.loads(out)[0]
        self.assertTrue(entry["applied"])
        self.assertFalse(entry["applied_ok"])

    def test_json_dry_run_overrides_apply_and_writes_nothing(self) -> None:
        rc, out, calls = self._run([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex", agent_role="claude"),
        ], apply=True, dry_run=True, as_json=True)
        self.assertEqual(0, rc)
        entry = json.loads(out)[0]
        self.assertFalse(entry["applied"])
        self.assertIsNone(entry["applied_ok"])
        self.assertEqual([], calls)  # dry-run wins, no mutation

    def test_conservative_default_state_is_healthy_or_unknown(self) -> None:
        rc, out, _ = self._run([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex", agent_role="claude"),
        ], as_json=True)
        states = {e["attention"]["attention_state"] for e in json.loads(out)}
        self.assertTrue(states <= {"healthy", "unknown"}, states)

    def test_no_targets_message(self) -> None:
        rc, out, calls = self._run([
            _pane("%5", "s:0.0", window_name="shell", command="zsh"),  # unknown pane
        ])
        self.assertEqual(0, rc)
        self.assertIn("no agent targets", out)
        self.assertEqual([], calls)


class ProjectAttentionUseCaseFakePortTest(unittest.TestCase):
    """Drive the use case directly through a fake writer port (Redmine #12785).

    No discovery patch, no real tmux: the apply / preview / best-effort-failure
    contract is expressed against the injected :class:`TmuxOptionWriterPort`.
    """

    def _candidate(self, pane_id="%9"):
        return SimpleNamespace(
            role="claude",
            confidence="strong",
            role_source="process",
            ambiguous=False,
            host="local",
            workspace_id="wsA",
            lane_id="default",
            pane_id=pane_id,
        )

    def test_fake_writer_satisfies_port(self) -> None:
        self.assertIsInstance(_RecordingOptionWriter(), TmuxOptionWriterPort)
        self.assertIsInstance(LiveTmuxOptionWriter(), TmuxOptionWriterPort)

    def test_preview_writes_nothing(self) -> None:
        writer = _RecordingOptionWriter()
        entries = ProjectAttentionUseCase(writer).execute(
            [self._candidate()], "2026-06-15T00:00:00Z", apply=False
        )
        self.assertEqual(writer.writes, [])
        self.assertEqual(len(entries), 1)
        self.assertIsInstance(entries[0], AttentionProjectionEntry)
        self.assertIsNone(entries[0].applied_ok)  # preview attempted no write
        self.assertEqual(len(entries[0].plan), 4)

    def test_apply_writes_all_set_options_and_reports_ok(self) -> None:
        writer = _RecordingOptionWriter()
        entries = ProjectAttentionUseCase(writer).execute(
            [self._candidate()], "2026-06-15T00:00:00Z", apply=True
        )
        self.assertEqual(len(writer.writes), 4)
        self.assertEqual({argv[0] for argv in writer.writes}, {"set-option"})
        for argv in writer.writes:
            self.assertEqual(argv[:4], ("set-option", "-p", "-t", "%9"))
        self.assertTrue(entries[0].applied_ok)

    def test_partial_failure_attempts_all_but_reports_not_ok(self) -> None:
        writer = _RecordingOptionWriter(fail_option=ATTENTION_STATE_OPTION)
        entries = ProjectAttentionUseCase(writer).execute(
            [self._candidate()], "2026-06-15T00:00:00Z", apply=True
        )
        self.assertEqual(len(writer.writes), 4)  # best-effort: all attempted
        self.assertFalse(entries[0].applied_ok)

    def test_entry_value_object_is_frozen(self) -> None:
        entry = ProjectAttentionUseCase(_RecordingOptionWriter()).execute(
            [self._candidate()], "t", apply=False
        )[0]
        with self.assertRaises(Exception):
            entry.pane_id = "%x"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
