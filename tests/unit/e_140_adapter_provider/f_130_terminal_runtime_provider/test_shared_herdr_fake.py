"""Unit pins for the shared stateful fake herdr (Redmine #13407, US A of #13398).

These tests exercise the fake **directly** — the fake is the subject-under-test
here (design §5-A deliverable: "fake の state 遷移を直接検証"). They pin every
modelled face (A–F of ``herdr-scenario-test-foundation.md`` §1.1): workspace
create + base pane, ``pane split`` placement, pane-bound ``agent start`` / locator
mint and its mislocated-launch injection, ``pane close`` →
workspace auto-vanish (E), ``agent list`` decode + malformed/alias faces (B),
``agent get`` / ``agent read`` (C), ``wait`` change-semantics (F), and the
fail-closed posture on an unmodelled argv (§2.3).

Where it is cheap, the real herdr decoders (``HerdrCliAgentLister`` /
``HerdrCliAgentStateReader``) are driven against the fake to prove the emitted
envelopes decode through production code — a decode-layer faithfulness check
(the full live-binary contract is US B, not this US).
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))
_TESTS_ROOT = Path(__file__).resolve().parents[3]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402
    _agent_locator,
    encode_assigned_name,
)
from mozyo_bridge.core.state.herdr_native_identity_binding import native_name_for  # noqa: E402
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_discovery import (  # noqa: E402
    HerdrCliAgentLister,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_state import (  # noqa: E402
    HerdrCliAgentStateReader,
)

from support.herdr_fake import (  # noqa: E402
    STATUS_IDLE,
    STATUS_WORKING,
    FakeHerdr,
    UnknownHerdrCommandError,
)

BINARY = "herdr"  # any non-empty token; the injected runner never spawns it


def _payload(completed):
    return json.loads(completed.stdout)


def _start(fake, workspace, provider, *, lane="lane-x", extra=()):
    logical = encode_assigned_name("ws", provider, lane)
    native = native_name_for(logical)
    anchor = fake.panes_of(workspace)[-1]
    prepared = _payload(
        fake.run(
            [
                BINARY,
                "pane",
                "split",
                anchor,
                "--direction",
                "down",
                "--cwd",
                "/workspace/project",
                "--env",
                "MOZYO_WORKSPACE_ID=ws",
                "--env",
                f"MOZYO_AGENT_ROLE={provider}",
                "--env",
                f"MOZYO_LANE_ID={lane}",
                "--no-focus",
            ]
        )
    )["result"]["pane"]["pane_id"]
    fake.run(
        [
            BINARY,
            "pane",
            "run",
            prepared,
            f'{provider}() {{ exec /private/action/{provider} "$@"; }}',
        ]
    )
    completed = fake.run([
        BINARY,
        "agent",
        "start",
        native,
        "--kind",
        provider,
        "--pane",
        prepared,
        "--",
        *extra,
    ])
    return completed, logical, prepared


# -- D: workspace create -------------------------------------------------------


class WorkspaceCreateTest(unittest.TestCase):
    def test_create_mints_workspace_with_single_base_pane(self) -> None:
        fake = FakeHerdr()
        completed = fake.run([BINARY, "workspace", "create", "--cwd", "/w", "--no-focus"])
        result = _payload(completed)["result"]
        self.assertEqual(result["type"], "workspace_created")
        self.assertEqual(result["pane_count"], 1)
        wid = result["workspace"]["workspace_id"]
        self.assertEqual(result["root_pane"]["pane_id"], f"{wid}:p1")
        # State: one workspace born with exactly its root pane.
        self.assertEqual(fake.workspace_ids, [wid])
        self.assertEqual(fake.panes_of(wid), [f"{wid}:p1"])

    def test_each_create_mints_a_distinct_workspace(self) -> None:
        fake = FakeHerdr()
        first = _payload(fake.run([BINARY, "workspace", "create"]))["result"]
        second = _payload(fake.run([BINARY, "workspace", "create"]))["result"]
        self.assertNotEqual(
            first["workspace"]["workspace_id"], second["workspace"]["workspace_id"]
        )


# -- H: workspace list (label authority, Redmine #14139) -----------------------


class WorkspaceListTest(unittest.TestCase):
    def test_list_reports_each_live_workspace_with_its_label(self) -> None:
        fake = FakeHerdr()
        # A labelled create (the shared `coordinators` space) and an unlabelled one.
        labelled = _payload(
            fake.run([BINARY, "workspace", "create", "--label", "coordinators"])
        )["result"]["workspace"]["workspace_id"]
        plain = _payload(fake.run([BINARY, "workspace", "create"]))["result"][
            "workspace"
        ]["workspace_id"]
        result = _payload(fake.run([BINARY, "workspace", "list"]))["result"]
        self.assertEqual(result["type"], "workspace_list")
        by_id = {w["workspace_id"]: w["label"] for w in result["workspaces"]}
        self.assertEqual(by_id, {labelled: "coordinators", plain: ""})

    def test_vanished_workspace_is_absent_from_list(self) -> None:
        # A workspace whose last pane closed auto-vanishes (residue verification).
        fake = FakeHerdr()
        wid = _payload(fake.run([BINARY, "workspace", "create", "--label", "x"]))[
            "result"
        ]["workspace"]["workspace_id"]
        fake.run([BINARY, "pane", "close", f"{wid}:p1"])
        result = _payload(fake.run([BINARY, "workspace", "list"]))["result"]
        self.assertEqual(result["workspaces"], [])


# -- A: agent start ------------------------------------------------------------


class AgentStartTest(unittest.TestCase):
    def test_split_from_explicit_seeded_tab_stays_in_that_tab(self) -> None:
        fake = FakeHerdr()
        wid = fake.seed_workspace(cwd="/workspace/project")
        tab_id = f"{wid}:t1"
        anchor = fake.seed_agent(
            "mzb1_ws_codex_lane-x",
            workspace_id=wid,
            provider="codex",
            tab_id=tab_id,
        )

        prepared = _payload(
            fake.run(
                [
                    BINARY,
                    "pane",
                    "split",
                    anchor,
                    "--direction",
                    "down",
                    "--cwd",
                    "/workspace/project",
                    "--no-focus",
                ]
            )
        )["result"]["pane"]

        self.assertEqual(prepared["tab_id"], tab_id)
        self.assertEqual(fake.tab_of(anchor), tab_id)
        self.assertEqual(fake.tab_of(prepared["pane_id"]), tab_id)

    def test_herdr_080_help_and_pane_bound_launch(self) -> None:
        fake = FakeHerdr()
        self.assertIn(
            "--pane",
            fake.run([BINARY, "agent", "start", "--help"]).stdout,
        )
        self.assertIn(
            "--direction",
            fake.run([BINARY, "pane", "split", "--help"]).stdout,
        )
        wid = fake.seed_workspace(cwd="/workspace/project")
        prepared = _payload(
            fake.run(
                [
                    BINARY,
                    "pane",
                    "split",
                    f"{wid}:p1",
                    "--direction",
                    "down",
                    "--cwd",
                    "/workspace/project",
                    "--env",
                    "MOZYO_WORKSPACE_ID=ws",
                    "--env",
                    "MOZYO_AGENT_ROLE=codex",
                    "--env",
                    "MOZYO_LANE_ID=default",
                    "--no-focus",
                ]
            )
        )["result"]["pane"]
        native = "mza1_aaaaaaaaaaaaaaaaaaaaaaaaaaa"
        fake.run(
            [
                BINARY,
                "pane",
                "run",
                prepared["pane_id"],
                'codex() { exec /private/action/codex "$@"; }',
            ]
        )
        started = _payload(
            fake.run(
                [
                    BINARY,
                    "agent",
                    "start",
                    native,
                    "--kind",
                    "codex",
                    "--pane",
                    prepared["pane_id"],
                    "--",
                    "--model",
                    "gpt-test",
                ]
            )
        )["result"]["agent"]
        self.assertEqual(started["name"], native)
        self.assertEqual(started["pane_id"], prepared["pane_id"])
        logical = fake.agent_named("mzb1_ws_codex_default")
        self.assertEqual(logical["native_name"], native)

    def test_start_lands_in_requested_workspace_and_applies_name(self) -> None:
        fake = FakeHerdr()
        wid = fake.seed_workspace()
        completed, logical, prepared = _start(fake, wid, "claude")
        agent = _payload(completed)["result"]["agent"]
        self.assertEqual(_payload(completed)["result"]["type"], "agent_started")
        self.assertEqual(agent["name"], native_name_for(logical))
        self.assertEqual(agent["pane_id"], prepared)
        # State restores the long logical identity from the prepared pane env.
        live = fake.agent_named(logical)
        self.assertEqual(live["pane_id"], agent["pane_id"])
        self.assertEqual(live["status"], STATUS_IDLE)

    def test_legacy_start_without_kind_and_pane_is_rejected(self) -> None:
        fake = FakeHerdr()
        with self.assertRaises(UnknownHerdrCommandError):
            fake.run([BINARY, "agent", "start", "solo", "--", "claude"])
        self.assertEqual(fake.workspace_ids, [])

    def test_start_records_launch_argv_after_separator(self) -> None:
        fake = FakeHerdr()
        wid = fake.seed_workspace()
        _start(fake, wid, "claude", extra=["--permission-mode", "auto"])
        (started,) = fake.start_argvs
        tail = started[started.index("--") + 1 :]
        self.assertEqual(tail, ["--permission-mode", "auto"])

    def test_start_in_unknown_pane_fails_closed(self) -> None:
        fake = FakeHerdr()
        completed = fake.run(
            [
                BINARY,
                "agent",
                "start",
                native_name_for("mzb1_ws_claude_lane"),
                "--kind",
                "claude",
                "--pane",
                "w999:p2",
                "--",
            ]
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("no such pane", completed.stderr)

    def test_start_without_matching_pane_function_fails_closed(self) -> None:
        fake = FakeHerdr()
        wid = fake.seed_workspace()
        pane = fake.panes_of(wid)[0]
        completed = fake.run(
            [
                BINARY,
                "agent",
                "start",
                native_name_for("mzb1_ws_claude_lane"),
                "--kind",
                "claude",
                "--pane",
                pane,
                "--",
            ]
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("no matching prepared claude function", completed.stderr)

    def test_start_rejects_a_function_prepared_for_the_other_provider(self) -> None:
        fake = FakeHerdr()
        wid = fake.seed_workspace()
        pane = fake.panes_of(wid)[0]
        fake.run(
            [
                BINARY,
                "pane",
                "run",
                pane,
                'codex() { exec /private/action/codex "$@"; }',
            ]
        )
        completed = fake.run(
            [
                BINARY,
                "agent",
                "start",
                native_name_for("mzb1_ws_claude_lane"),
                "--kind",
                "claude",
                "--pane",
                pane,
                "--",
            ]
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("no matching prepared claude function", completed.stderr)

    def test_start_missing_name_positional_is_unmodelled(self) -> None:
        fake = FakeHerdr()
        with self.assertRaises(UnknownHerdrCommandError):
            fake.run(
                [
                    BINARY,
                    "agent",
                    "start",
                    "--kind",
                    "claude",
                    "--pane",
                    "w1:p2",
                    "--",
                ]
            )

    def test_start_with_unmodelled_flag_fails_closed(self) -> None:
        fake = FakeHerdr()
        wid = fake.seed_workspace()
        with self.assertRaises(UnknownHerdrCommandError):
            fake.run(
                [
                    BINARY,
                    "agent",
                    "start",
                    native_name_for("mzb1_ws_claude_lane"),
                    "--kind",
                    "claude",
                    "--pane",
                    f"{wid}:p1",
                    "--frobnicate",
                    "--",
                ]
            )
        self.assertIn(wid, fake.workspace_ids)


# -- A prefix validation faces (mislocated / blank locator injection) ----------


class LaunchInjectionTest(unittest.TestCase):
    def test_misplace_next_launch_renders_a_mismatched_prefix(self) -> None:
        # The stimulus for the #13330 review j#73231 fail-closed guard: herdr
        # returned a locator in the wrong workspace. The fake renders the
        # mislocated locator; the real session-start code renders the verdict.
        fake = FakeHerdr()
        wid = fake.seed_workspace()
        fake.misplace_next_launch("wOTHER")
        completed, _logical, _prepared = _start(fake, wid, "claude")
        agent = _payload(completed)["result"]["agent"]
        self.assertTrue(agent["pane_id"].startswith("wOTHER:"))
        self.assertFalse(agent["pane_id"].startswith(f"{wid}:"))

    def test_misplace_is_one_shot(self) -> None:
        fake = FakeHerdr()
        wid = fake.seed_workspace()
        fake.misplace_next_launch("wOTHER")
        _start(fake, wid, "claude", lane="a")
        second, _logical, _prepared = _start(fake, wid, "claude", lane="b")
        second = _payload(second)["result"]["agent"]
        self.assertTrue(second["pane_id"].startswith(f"{wid}:"))

    def test_drop_next_locator_renders_blank_pane_id(self) -> None:
        fake = FakeHerdr()
        wid = fake.seed_workspace()
        fake.drop_next_locator()
        completed, _logical, _prepared = _start(fake, wid, "claude")
        agent = _payload(completed)["result"]["agent"]
        self.assertEqual(agent["pane_id"], "")


# -- B: agent list decode ------------------------------------------------------


class AgentListTest(unittest.TestCase):
    def test_list_renders_live_agents_and_decodes_through_production(self) -> None:
        fake = FakeHerdr()
        wid = fake.seed_workspace()
        p1 = fake.seed_agent("mzb1_ws_claude_default", workspace_id=wid)
        p2 = fake.seed_agent("mzb1_ws_codex_default", workspace_id=wid)
        # Drive the REAL discovery lister against the fake: the emitted rows decode
        # through production (name + locator alias resolution).
        rows = HerdrCliAgentLister(BINARY, runner=fake.run).list_agent_rows()
        by_name = {r["name"]: _agent_locator(r) for r in rows}
        self.assertEqual(by_name["mzb1_ws_claude_default"], p1)
        self.assertEqual(by_name["mzb1_ws_codex_default"], p2)

    def test_list_locator_alias_render_still_decodes(self) -> None:
        fake = FakeHerdr()
        wid = fake.seed_workspace()
        loc = fake.seed_agent("n", workspace_id=wid)
        fake.locator_render_key = "location"  # exercise the alias decode path
        rows = HerdrCliAgentLister(BINARY, runner=fake.run).list_agent_rows()
        self.assertNotIn("pane_id", rows[0])
        self.assertEqual(_agent_locator(rows[0]), loc)

    def test_malformed_extra_rows_are_spliced_and_skipped_by_production(self) -> None:
        fake = FakeHerdr()
        wid = fake.seed_workspace()
        fake.seed_agent("mzb1_ws_claude_default", workspace_id=wid, status=STATUS_WORKING)
        fake.extra_list_rows = [{"no_handle": True}, "not-a-row"]
        reader = HerdrCliAgentStateReader(BINARY, runner=fake.run)
        result = reader.list_agent_states()
        # The good row maps; the two malformed rows are skipped (not a whole-read
        # failure), and the skip count stays observable.
        self.assertTrue(result.ok)
        handles = [h for h, _ in result.states]
        self.assertEqual(handles, ["mzb1_ws_claude_default"])
        self.assertIn("skipped 2", result.detail)

    def test_extra_rows_are_one_shot(self) -> None:
        fake = FakeHerdr()
        wid = fake.seed_workspace()
        fake.seed_agent("n", workspace_id=wid)
        fake.extra_list_rows = [{"no_handle": True}]
        fake.run([BINARY, "agent", "list"])
        second = _payload(fake.run([BINARY, "agent", "list"]))
        self.assertEqual(len(second["agents"]), 1)

    def test_duplicate_names_surface_as_two_rows(self) -> None:
        # A duplicate assigned name is the stimulus for the real fail-closed
        # ``multiple_matches`` verdict; the fake just carries both live rows.
        fake = FakeHerdr()
        wid = fake.seed_workspace()
        fake.seed_agent("dup", workspace_id=wid)
        fake.seed_agent("dup", workspace_id=wid)
        rows = _payload(fake.run([BINARY, "agent", "list"]))["agents"]
        self.assertEqual([r["name"] for r in rows], ["dup", "dup"])


# -- C: agent get / read -------------------------------------------------------


class AgentGetReadTest(unittest.TestCase):
    def test_get_reports_status_and_decodes_through_production(self) -> None:
        fake = FakeHerdr()
        wid = fake.seed_workspace()
        loc = fake.seed_agent("n", workspace_id=wid, status=STATUS_WORKING)
        reader = HerdrCliAgentStateReader(BINARY, runner=fake.run)
        result = reader.read_agent_state(loc)
        self.assertTrue(result.ok)
        self.assertEqual(result.raw_status, STATUS_WORKING)

    def test_get_absent_target_fails_closed(self) -> None:
        fake = FakeHerdr()
        completed = fake.run([BINARY, "agent", "get", "w1:p9"])
        self.assertEqual(completed.returncode, 1)

    def test_read_returns_rendered_text_for_live_agent(self) -> None:
        fake = FakeHerdr(read_text="hello composer")
        wid = fake.seed_workspace()
        loc = fake.seed_agent("n", workspace_id=wid)
        read = _payload(fake.run([BINARY, "agent", "read", loc]))["result"]["read"]
        self.assertEqual(read["text"], "hello composer")

    def test_read_absent_target_fails_closed(self) -> None:
        fake = FakeHerdr()
        completed = fake.run([BINARY, "agent", "read", "w1:p9"])
        self.assertEqual(completed.returncode, 1)


# -- E: pane close -> workspace auto-vanish ------------------------------------


class PaneCloseTest(unittest.TestCase):
    def test_closing_base_pane_keeps_workspace_while_agents_live(self) -> None:
        fake = FakeHerdr()
        wid = fake.seed_workspace()
        fake.seed_agent("n", workspace_id=wid)
        fake.run([BINARY, "pane", "close", f"{wid}:p1"])  # the base pane
        # The workspace survives: its agent pane is still open.
        self.assertIn(wid, fake.workspace_ids)
        self.assertEqual(fake.panes_of(wid), [f"{wid}:p2"])

    def test_closing_last_pane_auto_vanishes_workspace(self) -> None:
        fake = FakeHerdr()
        wid = fake.seed_workspace()
        loc = fake.seed_agent("n", workspace_id=wid)
        fake.run([BINARY, "pane", "close", f"{wid}:p1"])  # base pane
        self.assertIn(wid, fake.workspace_ids)
        fake.run([BINARY, "pane", "close", loc])  # last (agent) pane
        # E: no husk — the workspace is gone, and a later list can't see the agent.
        self.assertNotIn(wid, fake.workspace_ids)
        self.assertEqual(_payload(fake.run([BINARY, "agent", "list"]))["agents"], [])

    def test_closing_unknown_pane_fails_closed(self) -> None:
        fake = FakeHerdr()
        completed = fake.run([BINARY, "pane", "close", "w1:p9"])
        self.assertEqual(completed.returncode, 1)


# -- send primitives (routing observation) ------------------------------------


class PaneSendTest(unittest.TestCase):
    def test_send_to_live_pane_succeeds_and_is_recorded(self) -> None:
        fake = FakeHerdr()
        wid = fake.seed_workspace()
        loc = fake.seed_agent("n", workspace_id=wid)
        completed = fake.run([BINARY, "pane", "send-text", loc, "hi"])
        self.assertEqual(completed.returncode, 0)
        self.assertIn(["pane", "send-text", loc, "hi"], fake.calls)

    def test_send_to_unknown_pane_fails_closed(self) -> None:
        fake = FakeHerdr()
        completed = fake.run([BINARY, "pane", "send-keys", "w1:p9", "enter"])
        self.assertEqual(completed.returncode, 1)


# -- F: wait change-semantics --------------------------------------------------


class WaitChangeSemanticsTest(unittest.TestCase):
    def _wait_argv(self, target, status):
        return [BINARY, "wait", "agent-status", target, "--status", status, "--timeout", "45000"]

    def test_armed_transition_returns_changed_and_advances_status(self) -> None:
        fake = FakeHerdr()
        wid = fake.seed_workspace()
        loc = fake.seed_agent("n", workspace_id=wid, status=STATUS_IDLE)
        fake.arm_transition(loc, STATUS_WORKING)  # arm before "injecting"
        proc = fake.popen(self._wait_argv(loc, STATUS_WORKING))
        stdout, _ = proc.communicate(timeout=1)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(stdout)["status"], STATUS_WORKING)
        # The transition actually happened: a snapshot now reads working.
        self.assertEqual(fake.agent_named("n")["status"], STATUS_WORKING)

    def test_wait_without_armed_change_times_out_even_if_already_in_state(self) -> None:
        # Change-semantics (PoC E9 c2): already being idle does NOT satisfy a wait
        # for idle — only a *change into* it returns; otherwise it times out.
        fake = FakeHerdr()
        wid = fake.seed_workspace()
        loc = fake.seed_agent("n", workspace_id=wid, status=STATUS_IDLE)
        proc = fake.popen(self._wait_argv(loc, STATUS_IDLE))
        _, stderr = proc.communicate(timeout=1)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("timed out", stderr)

    def test_wait_on_absent_target_reports_absent(self) -> None:
        fake = FakeHerdr()
        proc = fake.popen(self._wait_argv("w1:p9", STATUS_WORKING))
        _, stderr = proc.communicate(timeout=1)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no such pane", stderr)

    def test_armed_transition_is_consumed_once(self) -> None:
        fake = FakeHerdr()
        wid = fake.seed_workspace()
        loc = fake.seed_agent("n", workspace_id=wid)
        fake.arm_transition(loc, STATUS_WORKING)
        self.assertEqual(fake.popen(self._wait_argv(loc, STATUS_WORKING)).returncode, 0)
        # The armed event is spent; a second identical wait times out.
        self.assertEqual(fake.popen(self._wait_argv(loc, STATUS_WORKING)).returncode, 1)


# -- fail-closed on unmodelled argv (design §2.3) ------------------------------


class FailClosedTest(unittest.TestCase):
    def test_unmodelled_run_command_raises(self) -> None:
        fake = FakeHerdr()
        with self.assertRaises(UnknownHerdrCommandError):
            fake.run([BINARY, "workspace", "rename"])  # not modelled

    def test_unmodelled_popen_command_raises(self) -> None:
        fake = FakeHerdr()
        with self.assertRaises(UnknownHerdrCommandError):
            fake.popen([BINARY, "server", "stop"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
