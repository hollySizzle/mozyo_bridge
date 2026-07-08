"""Fake-port / pure specifications for the launch command boundary (#12933).

These exercise the ``launch_command`` use cases and pure policy directly with a
synthetic :class:`LaunchOps` — no real tmux server, no ``os.execvp``. They pin:

- the pure helpers (the attach-command form + argv, the ``list-windows`` row
  parse, the ``mozyo --json`` payload, and the ``layout apply cockpit`` dry-run
  text / JSON payload),
- the ``MozyoLaunchUseCase`` walk: the underivable-name and cwd-mismatch
  refusals, the select-window failure (which still carries the non-JSON notice),
  the JSON payload path, and the text attach outcome (attach argv + no-attach),
- the ``CockpitLayoutUseCase`` walk: the preset and no-workspace refusals, the
  JSON / dry-run non-mutating paths, the reuse-vs-build execute messages, and the
  mid-build ``SystemExit`` teardown (kill-session then re-raise),
- the terminal delivery tails (#13105): the exact ``emit`` / ``die`` / ``attach``
  branch order the thin handlers used to carry — JSON short-circuit,
  notice-before-die, the ``end=""`` pre-attach block, the ``--no-attach`` hint,
  and the exec attach.

The end-to-end behavior over the real tmux helpers stays pinned by the
``cmd_mozyo`` / ``cmd_layout_apply`` characterization tests
(``tests/integration/.../test_mozyo_bridge.py`` and ``.../test_cockpit_layout.py``);
this file pins the boundary in isolation, which is the OOP-first carve's payoff —
the policy is now exercisable without patching the live side effects.
"""

from __future__ import annotations

import argparse
import json
import unittest
from pathlib import Path

from mozyo_bridge.application.launch_command import (
    AgentWindowLaunchUseCase,
    CockpitLayoutUseCase,
    LayoutLaunchOutcome,
    MozyoLaunchOutcome,
    MozyoLaunchUseCase,
    _parse_mozyo_window_rows,
    attach_argv,
    attach_command_line,
    build_cockpit_layout_json_payload,
    build_mozyo_json_payload,
    deliver_layout_launch_outcome,
    deliver_mozyo_launch_outcome,
    new_agent_session_argv,
    new_agent_window_argv,
    render_cockpit_layout_dry_run,
    render_mozyo_session_block,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
    CockpitWorkspace,
)


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> argparse.Namespace:
    return argparse.Namespace(returncode=returncode, stdout=stdout, stderr=stderr)


class _FakeLaunchOps:
    """A synthetic :class:`LaunchOps` recording calls; every read is configured."""

    def __init__(
        self,
        *,
        repo_root: Path = Path("/repo"),
        adoption_marker: str | None = ".mozyo-bridge/config.yaml",
        canonical: str = "mozyo-repo",
        session_exists: bool = False,
        cwd_mismatch: list[str] | None = None,
        notice: str | None = None,
        tmux_conf: Path = Path("/repo/.tmux.conf"),
        created: list[str] | None = None,
        select_result: argparse.Namespace | None = None,
        list_result: argparse.Namespace | None = None,
        workspaces: list | None = None,
        execute_raises: BaseException | None = None,
    ) -> None:
        self._repo_root = repo_root
        self._adoption_marker = adoption_marker
        self._canonical = canonical
        self._session_exists = session_exists
        self._cwd_mismatch = cwd_mismatch or []
        self._notice = notice
        self._tmux_conf = tmux_conf
        self._created = created or []
        self._select_result = select_result or _result(returncode=0)
        self._list_result = list_result or _result(returncode=0, stdout="")
        self._workspaces = workspaces if workspaces is not None else []
        self._execute_raises = execute_raises
        self.calls: list[tuple] = []
        self.setup_args: argparse.Namespace | None = None
        self.attached: list[list[str]] = []
        self.emitted: list[tuple[str, str]] = []
        self.died: list[str] = []

    # -- mozyo reads --
    def require_tmux(self) -> None:
        self.calls.append(("require_tmux",))

    def repo_root(self, args: argparse.Namespace) -> Path:
        return self._repo_root

    def adoption_marker(self, repo_root: Path) -> str | None:
        self.calls.append(("adoption_marker", repo_root))
        return self._adoption_marker

    def canonical_session_name(self, repo_root: Path) -> str:
        return self._canonical

    def session_exists(self, session: str) -> bool:
        self.calls.append(("session_exists", session))
        return self._session_exists

    def session_cwd_mismatch(self, session: str, repo_root: Path) -> list[str]:
        return list(self._cwd_mismatch)

    def legacy_notice(self, repo_root: Path, session: str) -> str | None:
        return self._notice

    def default_tmux_conf(self, repo_root: Path) -> Path:
        return self._tmux_conf

    def ensure_windows(self, setup_args: argparse.Namespace) -> list[str]:
        self.setup_args = setup_args
        return list(self._created)

    def run_tmux(self, *args, **kwargs):
        self.calls.append(("run_tmux", args))
        if args and args[0] == "select-window":
            return self._select_result
        if args and args[0] == "list-windows":
            return self._list_result
        return _result(returncode=0)

    def attach(self, argv: list[str]):
        self.attached.append(list(argv))
        raise RuntimeError("attach")

    def emit(self, text: str, end: str = "\n") -> None:
        self.emitted.append((text, end))

    def die(self, message: str):
        # Mirror the live ``die`` contract: never returns.
        self.died.append(message)
        raise SystemExit(message)

    # -- layout reads --
    def resolve_cockpit_workspaces(self, args: argparse.Namespace) -> list:
        return list(self._workspaces)

    def agent_launch_command(self, role, session, repo_root, *, permission_mode_default):
        return f"{role}-cmd"

    def execute_cockpit_plan(self, plan, *, cleanup_captured: bool = False):
        self.calls.append(("execute_cockpit_plan", cleanup_captured))
        if self._execute_raises is not None:
            raise self._execute_raises


def _mozyo_args(**over) -> argparse.Namespace:
    base = dict(
        session=None, cwd=None, config_path=None, ready_timeout=0, force=False,
        no_attach=False, json_output=False, cc=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _layout_args(**over) -> argparse.Namespace:
    base = dict(
        preset="cockpit", codex_ratio=70, cockpit_session=None, layout_repos=["/a"],
        dry_run=False, json_output=False, cc=False, no_attach=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


class PureHelpersTest(unittest.TestCase):
    def test_attach_command_line_and_argv(self) -> None:
        self.assertEqual("tmux attach -t s", attach_command_line("s", False))
        self.assertEqual("tmux -CC attach -t s", attach_command_line("s", True))
        self.assertEqual(["tmux", "attach", "-t", "s"], attach_argv("s", False))
        self.assertEqual(["tmux", "-CC", "attach", "-t", "s"], attach_argv("s", True))

    def test_parse_window_rows_matches_legacy_shape(self) -> None:
        rows = _parse_mozyo_window_rows("0\tclaude\tclaude\n1\tcodex\tnode\n")
        self.assertEqual(
            [
                {"index": 0, "name": "claude", "process": "claude"},
                {"index": 1, "name": "codex", "process": "node"},
            ],
            rows,
        )

    def test_parse_window_rows_blank_process_becomes_none_and_nonnumeric_index(self) -> None:
        # Byte-for-byte with the legacy parser: a blank process -> None, and a
        # non-numeric index is kept as a string (never dropped).
        rows = _parse_mozyo_window_rows("x\tclaude\t\n")
        self.assertEqual([{"index": "x", "name": "claude", "process": None}], rows)

    def test_mozyo_json_payload_is_effectively_no_attach(self) -> None:
        payload = build_mozyo_json_payload(
            session="s", repo_root="/r", cwd="/r", created=["claude:%1"],
            windows=[{"index": 0, "name": "claude", "process": "claude"},
                     {"index": 1, "name": "codex", "process": "node"}],
            attach_command="tmux attach -t s", control_mode=False,
            raw_no_attach=False, notice="note",
        )
        self.assertTrue(payload["no_attach"])  # JSON never attaches (review #54111)
        self.assertFalse(payload["attached"])
        self.assertEqual("s", payload["attach_target"])
        self.assertEqual("note", payload["legacy_session_notice"])
        self.assertTrue(payload["ready"])  # both agent windows present

    def test_mozyo_json_ready_false_when_agent_window_missing(self) -> None:
        payload = build_mozyo_json_payload(
            session="s", repo_root="/r", cwd="/r", created=[],
            windows=[{"index": 0, "name": "claude", "process": "claude"}],
            attach_command="tmux attach -t s", control_mode=False,
            raw_no_attach=True, notice=None,
        )
        self.assertFalse(payload["ready"])

    def test_render_mozyo_session_block_matches_legacy_stdout(self) -> None:
        # Byte-for-byte with the legacy handler: session line, header, then the
        # raw table appended verbatim (the table carries its own trailing newline).
        self.assertEqual(
            "session=s created=claude:%1,codex:%2\nINDEX\tNAME\tPROCESS\n0\tclaude\tclaude\n",
            render_mozyo_session_block("s", ["claude:%1", "codex:%2"], "0\tclaude\tclaude\n"),
        )

    def test_render_mozyo_session_block_empty_created_and_no_table(self) -> None:
        # No created windows collapses to ``-``; a ``None`` table (probe failed)
        # appends no rows, so the block ends at the header newline.
        self.assertEqual(
            "session=s created=-\nINDEX\tNAME\tPROCESS\n",
            render_mozyo_session_block("s", [], None),
        )

    def test_layout_dry_run_and_json_render(self) -> None:
        cmd = argparse.Namespace(argv=["new-session", "-d", "-s", "mozyo-cockpit"])
        plan = argparse.Namespace(
            columns=2, codex_ratio=70, claude_ratio=30, commands=[cmd],
            as_dict=lambda: {"columns": 2, "codex_ratio": 70},
        )
        text = render_cockpit_layout_dry_run(plan, "mozyo-cockpit", "tmux attach -t mozyo-cockpit")
        self.assertIn("cockpit plan: session=mozyo-cockpit columns=2 codex=70% claude=30%", text)
        self.assertIn("  tmux new-session -d -s mozyo-cockpit", text)
        self.assertTrue(text.endswith("attach: tmux attach -t mozyo-cockpit"))
        payload = build_cockpit_layout_json_payload(plan, "tmux -CC attach -t mozyo-cockpit", True)
        self.assertEqual("tmux -CC attach -t mozyo-cockpit", payload["attach"])
        self.assertTrue(payload["control_mode"])


class AdoptionRefusalTest(unittest.TestCase):
    """The pure #13379 gate policy (wording + precedence), home injectable."""

    def test_home_refusal_wins_even_with_marker(self) -> None:
        from mozyo_bridge.application.launch_adoption_gate import adoption_refusal

        home = Path("/home/someone")
        refusal = adoption_refusal(
            home, ".mozyo-bridge/scaffold.json", home=home
        )
        self.assertIn("home directory", refusal)

    def test_unadopted_names_root_and_scaffold_guidance(self) -> None:
        from mozyo_bridge.application.launch_adoption_gate import adoption_refusal

        refusal = adoption_refusal(
            Path("/somewhere"), None, home=Path("/home/someone")
        )
        self.assertIn("/somewhere", refusal)
        self.assertIn("mozyo-bridge scaffold apply", refusal)

    def test_adopted_non_home_root_proceeds(self) -> None:
        from mozyo_bridge.application.launch_adoption_gate import adoption_refusal

        self.assertIsNone(
            adoption_refusal(
                Path("/somewhere"),
                ".mozyo-bridge/config.yaml",
                home=Path("/home/someone"),
            )
        )


class MozyoLaunchUseCaseTest(unittest.TestCase):
    def test_unadopted_root_refuses_before_any_session_side_effect(self) -> None:
        # Redmine #13379: bare `mozyo` in an unadopted directory must fail
        # closed with the resolved root + scaffold guidance, and must not
        # touch sessions/windows (the observed trap started two real agents
        # in the home directory the root silently resolved up to).
        ops = _FakeLaunchOps(repo_root=Path("/unadopted"), adoption_marker=None)
        outcome = MozyoLaunchUseCase(ops).run(_mozyo_args())
        self.assertIsNotNone(outcome.error_message)
        self.assertIn("/unadopted", outcome.error_message)
        self.assertIn("not an adopted mozyo workspace", outcome.error_message)
        self.assertIn("mozyo-bridge scaffold apply", outcome.error_message)
        self.assertIsNone(ops.setup_args)  # ensure_windows never ran
        self.assertNotIn(
            ("session_exists", "mozyo-repo"), ops.calls
        )  # refused before session probes

    def test_home_root_refuses_even_with_adoption_marker(self) -> None:
        # A stray home-level manifest (a forgotten `scaffold apply` run from
        # home — observed live on the trap host) must not re-open the trap:
        # bare `mozyo` never targets the home directory.
        ops = _FakeLaunchOps(
            repo_root=Path.home(), adoption_marker=".mozyo-bridge/scaffold.json"
        )
        outcome = MozyoLaunchUseCase(ops).run(_mozyo_args())
        self.assertIsNotNone(outcome.error_message)
        self.assertIn("home directory", outcome.error_message)
        self.assertIsNone(ops.setup_args)

    def test_explicit_session_does_not_bypass_adoption_gate(self) -> None:
        # The trap is about the directory, not the session name: naming a
        # session must not launch agents in an unadopted root.
        ops = _FakeLaunchOps(adoption_marker=None)
        outcome = MozyoLaunchUseCase(ops).run(_mozyo_args(session="custom"))
        self.assertIn("not an adopted mozyo workspace", outcome.error_message)

    def test_adopted_root_launches_unchanged(self) -> None:
        # Any explicit adoption marker (scaffold manifest here) passes the
        # gate; the flow below stays byte-identical to the pre-gate path.
        ops = _FakeLaunchOps(adoption_marker=".mozyo-bridge/scaffold.json")
        outcome = MozyoLaunchUseCase(ops).run(_mozyo_args())
        self.assertIsNone(outcome.error_message)
        self.assertIsNotNone(outcome.pre_attach_text)

    def test_underivable_name_refuses(self) -> None:
        ops = _FakeLaunchOps(canonical="")
        outcome = MozyoLaunchUseCase(ops).run(_mozyo_args())
        self.assertIsNotNone(outcome.error_message)
        self.assertIn("could not derive a session name", outcome.error_message)

    def test_cwd_mismatch_on_existing_session_refuses(self) -> None:
        ops = _FakeLaunchOps(session_exists=True, cwd_mismatch=["/elsewhere"])
        outcome = MozyoLaunchUseCase(ops).run(_mozyo_args())
        self.assertIn("already exists but its panes are outside", outcome.error_message)
        self.assertIn("/elsewhere", outcome.error_message)

    def test_explicit_session_override_skips_mismatch_guard(self) -> None:
        # An explicit --session bypasses the existing-session cwd guard.
        ops = _FakeLaunchOps(session_exists=True, cwd_mismatch=["/elsewhere"])
        outcome = MozyoLaunchUseCase(ops).run(_mozyo_args(session="custom"))
        self.assertIsNone(outcome.error_message)
        self.assertIn("session=custom created=-", outcome.pre_attach_text)

    def test_select_window_failure_carries_notice_and_error(self) -> None:
        ops = _FakeLaunchOps(
            notice="legacy notice",
            select_result=_result(returncode=1, stderr="boom"),
        )
        outcome = MozyoLaunchUseCase(ops).run(_mozyo_args())
        # The non-JSON notice must still be reported (the handler prints it before
        # the die), matching the original ordering.
        self.assertEqual("legacy notice", outcome.notice)
        self.assertIn("failed to select `claude` window", outcome.error_message)
        self.assertIn("stderr=boom", outcome.error_message)

    def test_json_path_emits_payload_and_no_attach(self) -> None:
        ops = _FakeLaunchOps(
            created=["claude:%1", "codex:%2"],
            list_result=_result(returncode=0, stdout="0\tclaude\tclaude\n1\tcodex\tnode\n"),
        )
        outcome = MozyoLaunchUseCase(ops).run(_mozyo_args(json_output=True, no_attach=True))
        self.assertIsNotNone(outcome.json_stdout)
        payload = json.loads(outcome.json_stdout)
        self.assertEqual("mozyo-repo", payload["session"])
        self.assertEqual(["claude:%1", "codex:%2"], payload["created"])
        self.assertTrue(payload["ready"])
        # JSON short-circuits: no separate notice/text carried.
        self.assertIsNone(outcome.notice)
        self.assertIsNone(outcome.pre_attach_text)

    def test_text_attach_outcome_carries_argv(self) -> None:
        ops = _FakeLaunchOps(
            created=["claude:%1"],
            list_result=_result(returncode=0, stdout="0\tclaude\tclaude\n"),
        )
        outcome = MozyoLaunchUseCase(ops).run(_mozyo_args(cc=True))
        # The session line, header, and raw window table are rendered into the
        # pre-attach block byte-for-byte (#12984).
        self.assertEqual(
            "session=mozyo-repo created=claude:%1\nINDEX\tNAME\tPROCESS\n0\tclaude\tclaude\n",
            outcome.pre_attach_text,
        )
        self.assertEqual(("tmux", "-CC", "attach", "-t", "mozyo-repo"), outcome.attach_argv)
        self.assertFalse(outcome.no_attach)

    def test_list_windows_failure_yields_no_table(self) -> None:
        ops = _FakeLaunchOps(list_result=_result(returncode=1, stdout="ignored"))
        outcome = MozyoLaunchUseCase(ops).run(_mozyo_args())
        # A failed ``list-windows`` probe contributes no rows: the block ends at
        # the header newline.
        self.assertEqual(
            "session=mozyo-repo created=-\nINDEX\tNAME\tPROCESS\n",
            outcome.pre_attach_text,
        )

    def test_setup_args_thread_defaults(self) -> None:
        ops = _FakeLaunchOps()
        MozyoLaunchUseCase(ops).run(_mozyo_args())
        self.assertIsNotNone(ops.setup_args)
        self.assertTrue(ops.setup_args.config)
        self.assertTrue(ops.setup_args.config_path_was_default)
        self.assertEqual("/repo/.tmux.conf", ops.setup_args.config_path)


class CockpitLayoutUseCaseTest(unittest.TestCase):
    def _workspace(self) -> CockpitWorkspace:
        return CockpitWorkspace(workspace_id="ws", label="repo", repo_root="/repo")

    def test_unsupported_preset_refuses(self) -> None:
        outcome = CockpitLayoutUseCase(_FakeLaunchOps()).run(_layout_args(preset="grid"))
        self.assertIn("unsupported layout preset", outcome.error_message)

    def test_no_workspace_refuses(self) -> None:
        outcome = CockpitLayoutUseCase(_FakeLaunchOps(workspaces=[])).run(_layout_args())
        self.assertIn("no active workspace", outcome.error_message)

    def test_json_path_is_non_mutating(self) -> None:
        ops = _FakeLaunchOps(workspaces=[self._workspace()])
        outcome = CockpitLayoutUseCase(ops).run(_layout_args(json_output=True, cc=True))
        payload = json.loads(outcome.json_stdout)
        self.assertEqual(1, payload["columns"])
        self.assertTrue(payload["control_mode"])
        self.assertEqual("tmux -CC attach -t mozyo-cockpit", payload["attach"])
        # No require_tmux / execute on the read-only path.
        self.assertNotIn(("require_tmux",), ops.calls)

    def test_dry_run_renders_plan_text(self) -> None:
        ops = _FakeLaunchOps(workspaces=[self._workspace()])
        outcome = CockpitLayoutUseCase(ops).run(_layout_args(dry_run=True))
        self.assertIn("cockpit plan:", outcome.dry_run_stdout)
        self.assertIn("attach: tmux attach -t mozyo-cockpit", outcome.dry_run_stdout)
        self.assertNotIn(("require_tmux",), ops.calls)

    def test_reuse_existing_session_skips_execute(self) -> None:
        ops = _FakeLaunchOps(workspaces=[self._workspace()], session_exists=True)
        outcome = CockpitLayoutUseCase(ops).run(_layout_args())
        self.assertEqual(1, len(outcome.pre_attach_lines))
        self.assertIn("already exists", outcome.pre_attach_lines[0])
        self.assertFalse(any(c[0] == "execute_cockpit_plan" for c in ops.calls))
        self.assertEqual(("tmux", "attach", "-t", "mozyo-cockpit"), outcome.attach_argv)

    def test_build_path_executes_and_reports_built(self) -> None:
        ops = _FakeLaunchOps(workspaces=[self._workspace()], session_exists=False)
        outcome = CockpitLayoutUseCase(ops).run(_layout_args(no_attach=True))
        self.assertTrue(any(c[0] == "execute_cockpit_plan" for c in ops.calls))
        self.assertIn("cockpit built", outcome.pre_attach_lines[0])
        self.assertTrue(outcome.no_attach)

    def test_mid_build_failure_tears_down_and_reraises(self) -> None:
        ops = _FakeLaunchOps(
            workspaces=[self._workspace()],
            session_exists=False,
            execute_raises=SystemExit(1),
        )
        with self.assertRaises(SystemExit):
            CockpitLayoutUseCase(ops).run(_layout_args())
        kill = [c for c in ops.calls if c[0] == "run_tmux" and c[1][:1] == ("kill-session",)]
        self.assertTrue(kill, "expected a kill-session teardown before re-raise")


class MozyoLaunchDeliveryTest(unittest.TestCase):
    """The ``cmd_mozyo`` terminal tail, byte-for-byte over the fake port (#13105)."""

    def test_json_short_circuits_without_attach(self) -> None:
        ops = _FakeLaunchOps()
        rc = deliver_mozyo_launch_outcome(MozyoLaunchOutcome(json_stdout='{"a": 1}'), ops)
        self.assertEqual(0, rc)
        self.assertEqual([('{"a": 1}', "\n")], ops.emitted)
        self.assertEqual([], ops.attached)
        self.assertEqual([], ops.died)

    def test_notice_prints_before_die(self) -> None:
        # The non-JSON legacy notice must land before a late failure's die,
        # matching the original handler ordering.
        ops = _FakeLaunchOps()
        outcome = MozyoLaunchOutcome(notice="legacy notice", error_message="boom")
        with self.assertRaises(SystemExit):
            deliver_mozyo_launch_outcome(outcome, ops)
        self.assertEqual([("legacy notice", "\n")], ops.emitted)
        self.assertEqual(["boom"], ops.died)
        self.assertEqual([], ops.attached)

    def test_no_attach_prints_block_and_hint(self) -> None:
        ops = _FakeLaunchOps()
        outcome = MozyoLaunchOutcome(
            pre_attach_text="session=s created=-\nINDEX\tNAME\tPROCESS\n",
            attach_command="tmux attach -t s",
            attach_argv=("tmux", "attach", "-t", "s"),
            no_attach=True,
        )
        self.assertEqual(0, deliver_mozyo_launch_outcome(outcome, ops))
        # The pre-attach block carries its own trailing newline -> end="".
        self.assertEqual(
            [
                ("session=s created=-\nINDEX\tNAME\tPROCESS\n", ""),
                ("attach: tmux attach -t s", "\n"),
            ],
            ops.emitted,
        )
        self.assertEqual([], ops.attached)

    def test_attach_path_drives_port_attach(self) -> None:
        ops = _FakeLaunchOps()
        outcome = MozyoLaunchOutcome(
            pre_attach_text="session=s created=-\nINDEX\tNAME\tPROCESS\n",
            attach_command="tmux -CC attach -t s",
            attach_argv=("tmux", "-CC", "attach", "-t", "s"),
        )
        with self.assertRaisesRegex(RuntimeError, "attach"):
            deliver_mozyo_launch_outcome(outcome, ops)
        self.assertEqual([["tmux", "-CC", "attach", "-t", "s"]], ops.attached)


class LayoutLaunchDeliveryTest(unittest.TestCase):
    """The ``cmd_layout_apply`` terminal tail, byte-for-byte over the fake port (#13105)."""

    def test_refusal_dies_first(self) -> None:
        ops = _FakeLaunchOps()
        with self.assertRaises(SystemExit):
            deliver_layout_launch_outcome(LayoutLaunchOutcome(error_message="nope"), ops)
        self.assertEqual(["nope"], ops.died)
        self.assertEqual([], ops.emitted)
        self.assertEqual([], ops.attached)

    def test_json_and_dry_run_short_circuit(self) -> None:
        ops = _FakeLaunchOps()
        self.assertEqual(0, deliver_layout_launch_outcome(LayoutLaunchOutcome(json_stdout="{}"), ops))
        self.assertEqual(0, deliver_layout_launch_outcome(LayoutLaunchOutcome(dry_run_stdout="cockpit plan:"), ops))
        self.assertEqual([("{}", "\n"), ("cockpit plan:", "\n")], ops.emitted)
        self.assertEqual([], ops.attached)

    def test_no_attach_prints_lines_and_hint(self) -> None:
        ops = _FakeLaunchOps()
        outcome = LayoutLaunchOutcome(
            pre_attach_lines=("cockpit built: session=mozyo-cockpit columns=1 codex=70% claude=30%",),
            attach_command="tmux attach -t mozyo-cockpit",
            attach_argv=("tmux", "attach", "-t", "mozyo-cockpit"),
            no_attach=True,
        )
        self.assertEqual(0, deliver_layout_launch_outcome(outcome, ops))
        self.assertEqual(
            [
                ("cockpit built: session=mozyo-cockpit columns=1 codex=70% claude=30%", "\n"),
                ("attach: tmux attach -t mozyo-cockpit", "\n"),
            ],
            ops.emitted,
        )
        self.assertEqual([], ops.attached)

    def test_attach_path_drives_port_attach(self) -> None:
        ops = _FakeLaunchOps()
        outcome = LayoutLaunchOutcome(
            pre_attach_lines=("cockpit session 'mozyo-cockpit' already exists; attaching without rebuild (reuse over duplicate panes)",),
            attach_command="tmux attach -t mozyo-cockpit",
            attach_argv=("tmux", "attach", "-t", "mozyo-cockpit"),
        )
        with self.assertRaisesRegex(RuntimeError, "attach"):
            deliver_layout_launch_outcome(outcome, ops)
        self.assertEqual([["tmux", "attach", "-t", "mozyo-cockpit"]], ops.attached)


class _FakeAgentWindowOps:
    """A synthetic :class:`AgentWindowLaunchOps` recording the driven calls."""

    def __init__(
        self,
        *,
        supported: bool = True,
        run_result: argparse.Namespace | None = None,
        launch_command: str = "env OTEL=1 claude",
    ) -> None:
        self._supported = supported
        self._run_result = run_result or _result(returncode=0, stdout="%7\n")
        self._launch_command = launch_command
        self.calls: list[tuple] = []
        self.recorded: list[tuple] = []
        self.died: list[str] = []

    def require_tmux(self) -> None:
        self.calls.append(("require_tmux",))

    def is_supported_agent(self, agent: str) -> bool:
        return self._supported

    def agent_launch_command(self, agent: str, session: str, cwd) -> str:
        self.calls.append(("agent_launch_command", agent, session, cwd))
        return self._launch_command

    def run_tmux(self, *args, **kwargs):
        self.calls.append(("run_tmux", args, kwargs))
        return self._run_result

    def record_pane_created(self, agent, session, pane_id, cwd) -> None:
        self.recorded.append((agent, session, pane_id, cwd))

    def die(self, message: str):
        # Mirror the live ``die`` contract: never returns.
        self.died.append(message)
        raise SystemExit(message)


class AgentWindowArgvTest(unittest.TestCase):
    def test_new_session_argv_matches_legacy_shape(self) -> None:
        argv = new_agent_session_argv("claude", "s", "/repo", "env X=1 claude")
        self.assertEqual(
            [
                "new-session", "-d", "-s", "s", "-n", "claude",
                "-P", "-F", "#{pane_id}", "-c", "/repo", "env X=1 claude",
            ],
            argv,
        )

    def test_new_window_argv_matches_legacy_shape(self) -> None:
        argv = new_agent_window_argv("codex", "s", None, "env X=1 codex")
        # No cwd -> no ``-c`` pair; window is added to ``<session>:``.
        self.assertEqual(
            [
                "new-window", "-d", "-t", "s:", "-n", "codex",
                "-P", "-F", "#{pane_id}", "env X=1 codex",
            ],
            argv,
        )

    def test_launch_command_is_always_the_trailing_arg(self) -> None:
        # The tmux boundary tests assert on ``captured[0][-1]``; keep it last.
        self.assertEqual(
            "env X=1 claude", new_agent_session_argv("claude", "s", "/r", "env X=1 claude")[-1]
        )
        self.assertEqual(
            "env X=1 codex", new_agent_window_argv("codex", "s", None, "env X=1 codex")[-1]
        )


class AgentWindowLaunchUseCaseTest(unittest.TestCase):
    def test_new_session_window_returns_pane_and_records_event(self) -> None:
        ops = _FakeAgentWindowOps(run_result=_result(returncode=0, stdout="%1\n"))
        pane = AgentWindowLaunchUseCase(ops).new_session_window("claude", "s", "/repo")
        self.assertEqual("%1", pane)
        # The env-wrapped launch command rides the trailing tmux arg.
        run = [c for c in ops.calls if c[0] == "run_tmux"][0]
        self.assertEqual("new-session", run[1][0])
        self.assertEqual("env OTEL=1 claude", run[1][-1])
        self.assertEqual({"check": False}, run[2])
        self.assertEqual([("claude", "s", "%1", "/repo")], ops.recorded)

    def test_new_window_uses_new_window_verb(self) -> None:
        ops = _FakeAgentWindowOps(run_result=_result(returncode=0, stdout="%2\n"))
        pane = AgentWindowLaunchUseCase(ops).new_window("codex", "s")
        self.assertEqual("%2", pane)
        run = [c for c in ops.calls if c[0] == "run_tmux"][0]
        self.assertEqual("new-window", run[1][0])

    def test_unsupported_agent_dies_before_run(self) -> None:
        ops = _FakeAgentWindowOps(supported=False)
        with self.assertRaises(SystemExit):
            AgentWindowLaunchUseCase(ops).new_session_window("bogus", "s")
        self.assertEqual(["unsupported agent: bogus"], ops.died)
        self.assertFalse(any(c[0] == "run_tmux" for c in ops.calls))
        self.assertEqual([], ops.recorded)

    def test_nonzero_return_dies_with_verb_specific_message(self) -> None:
        ops = _FakeAgentWindowOps(run_result=_result(returncode=1, stderr="boom"))
        with self.assertRaises(SystemExit):
            AgentWindowLaunchUseCase(ops).new_window("claude", "s")
        self.assertEqual(["tmux new-window failed: boom"], ops.died)
        self.assertEqual([], ops.recorded)

    def test_empty_pane_id_dies(self) -> None:
        ops = _FakeAgentWindowOps(run_result=_result(returncode=0, stdout="  \n"))
        with self.assertRaises(SystemExit):
            AgentWindowLaunchUseCase(ops).new_session_window("claude", "s")
        self.assertEqual(["tmux new-session did not return a pane id"], ops.died)
        self.assertEqual([], ops.recorded)


if __name__ == "__main__":
    unittest.main()
