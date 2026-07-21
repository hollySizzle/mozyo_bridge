"""herdr sublane actuation adapter tests (Redmine #13377 shared project workspace).

Drives :class:`HerdrSublaneActuatorOps` through a stateful fake herdr CLI (0.7.1 shape)
and a real (temp) workspace registry — no live herdr, no tmux. Covers the lane-slot
stand-up inside the shared project workspace (``append_lane_column`` =
``prepare_session`` with ``lane_id=lane_label``), the live-inventory read-back
(``read_lane`` mzb1 unit decode), the presence-based gateway readiness probe, the
explicit-lane dispatch argv, and the backend selector.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.workspace_registry import read_anchor, register_workspace
from mozyo_bridge.core.state.herdr_identity_attestation import (
    HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
    IdentityAttestationRecord,
    VERDICT_PRESENT,
    record_identity_attestation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E501
    HerdrSublaneActuatorOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_runtime_fence import (  # noqa: E501
    HEAL_REASON_PAIR_INCOMPLETE,
    HEAL_REASON_PAIR_SPLIT,
    HEAL_REASON_TARGET_ABSENT,
    SublaneHealError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
    SUBLANE_STATE_ACTIVE,
    SUBLANE_STATE_GATEWAY_ONLY,
    SUBLANE_STATE_PAIR_SPLIT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_runtime_fence import (  # noqa: E501
    RuntimePlacementFingerprint,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
    build_attest_capability_contract_line,
)

from tests.support.agent_provider_binaries import provider_bin_path, with_provider_path

HERDR_ENV = "MOZYO_HERDR_BINARY"


@contextlib.contextmanager
def _no_probe_wait():
    """Keep the #13948 startup probe's ROUNDS but not its sleeps (Redmine #13948).

    `heal_lane_column` goes through `prepare_session`, which since #13948 waits a bounded
    deadline for each requested role to come up. That bound is real production behaviour
    and is deliberately NOT disabled here — the probe still performs every poll, so these
    tests exercise the same retry path an operator hits. Only the wall-clock interval is
    removed, so a scenario whose slot never comes up costs no seconds.
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
        herdr_startup_health,
    )

    with patch.object(herdr_startup_health, "DEFAULT_PROBE_INTERVAL", 0.0):
        yield


class _StatefulHerdr:
    """A fake herdr whose ``agent list`` reflects agents launched via ``agent start``.

    ``workspace create`` mints a fresh ``wL`` workspace with a root pane; ``agent start``
    lands each launch in the requested ``--workspace`` at a distinct pane and records it,
    so a later ``agent list`` returns those live rows (name + pane_id). This is what lets
    the append → read-back round trip resolve the lane from the live inventory.
    """

    def __init__(self, *, created_workspace="wL"):
        self.created_workspace = created_workspace
        self.agents: list[dict] = []  # {"name", "pane_id", "tab_id"}
        self.start_argvs: list[list] = []
        self._pane_seq = 1
        self._tab_seq = 0  # monotonic tab counter (Redmine #13411 lane=tab)
        # #13378: rendered pane text served by `agent read`; set to "" to simulate a
        # live-but-still-booting TUI (blank render).
        self.read_text = "codex composer rendered"
        self.attest_home = None

    def run(self, argv, capture_output=None, text=None, timeout=None, env=None, **kw):
        rest = list(argv[1:])
        if rest[:2] == ["herdr", "agent-attest"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=(
                    "usage: mozyo-bridge herdr agent-attest --assigned-name ...\n"
                    + build_attest_capability_contract_line(
                        HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION
                    )
                ),
                stderr="",
            )
        if rest == ["agent", "list"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"agents": self.agents}), stderr=""
            )
        if rest[:2] == ["agent", "read"]:
            pane = rest[2]
            if not any(a["pane_id"] == pane for a in self.agents):
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="agent_not_found"
                )
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {"result": {"read": {"text": self.read_text, "truncated": False}}}
                ),
                stderr="",
            )
        if rest[:2] == ["workspace", "create"]:
            wid = self.created_workspace
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "result": {
                            "type": "workspace_created",
                            "workspace": {"workspace_id": wid},
                            "root_pane": {"pane_id": f"{wid}:p1"},
                        }
                    }
                ),
                stderr="",
            )
        if rest[:2] == ["tab", "create"]:
            # #13411 lane=tab: mint a tab in the requested workspace with a root pane.
            wid = rest[rest.index("--workspace") + 1] if "--workspace" in rest else "w1"
            self._tab_seq += 1
            tab_id = f"{wid}:t{self._tab_seq}"
            self._pane_seq += 1
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "result": {
                            "type": "tab_created",
                            "tab": {"tab_id": tab_id},
                            "root_pane": {"pane_id": f"{wid}:p{self._pane_seq}"},
                        }
                    }
                ),
                stderr="",
            )
        if rest[:2] == ["pane", "close"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"result": {"type": "ok"}}), stderr=""
            )
        if rest[:2] == ["agent", "start"]:
            self.start_argvs.append(rest)
            name = rest[2]
            wid = rest[rest.index("--workspace") + 1] if "--workspace" in rest else "w1"
            tab_id = rest[rest.index("--tab") + 1] if "--tab" in rest else ""
            self._pane_seq += 1
            pane_id = f"{wid}:p{self._pane_seq}"
            row = {"name": name, "pane_id": pane_id}
            if tab_id:
                row["tab_id"] = tab_id
            self.agents.append(row)
            launch_env = {}
            for index, token in enumerate(rest):
                if token == "--env" and index + 1 < len(rest):
                    key, _, value = rest[index + 1].partition("=")
                    launch_env[key] = value
            if "agent-attest" in rest and self.attest_home is not None:
                record_identity_attestation(
                    IdentityAttestationRecord(
                        assigned_name=name,
                        workspace_id=launch_env.get("MOZYO_WORKSPACE_ID", ""),
                        role=launch_env.get("MOZYO_AGENT_ROLE", ""),
                        lane_id=launch_env.get("MOZYO_LANE_ID", ""),
                        locator=pane_id,
                        verdict=VERDICT_PRESENT,
                        observed_at="2026-07-18T00:00:00+00:00",
                    ),
                    home=Path(self.attest_home),
                )
                # Redmine #14222 j#85125 F2: a real wrapped launch also appends its
                # attributed execution-stage rows before exec'ing, and the health
                # probe now demands them before a green. Model that exactly.
                action_id = launch_env.get("MOZYO_STARTUP_ACTION_ID", "")
                if action_id:
                    from mozyo_bridge.core.state.startup_execution_events import (
                        STAGE_PROVIDER_EXEC_CALL_REACHED,
                        STAGE_WRAPPER_ENTERED,
                        append_execution_event,
                    )
                    from mozyo_bridge.core.state.startup_transaction_fence import (
                        StartupTransactionFence,
                    )

                    events_fence = StartupTransactionFence(home=Path(self.attest_home))
                    for stage in (
                        STAGE_WRAPPER_ENTERED,
                        STAGE_PROVIDER_EXEC_CALL_REACHED,
                    ):
                        append_execution_event(
                            events_fence, action_id, stage, participant=name
                        )
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "result": {
                            "agent": {
                                "name": name,
                                "pane_id": pane_id,
                                "workspace_id": wid,
                                # #13411: echo the requested tab so the landing guard
                                # (returned tab_id == --tab) passes on the happy path.
                                "tab_id": tab_id,
                            },
                            "type": "agent_started",
                        }
                    }
                ),
                stderr="",
            )
        raise AssertionError(f"unexpected herdr call: {argv!r}")


def _fake_binary(tmp: str) -> Path:
    binpath = Path(tmp) / "fake-herdr"
    binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return binpath


def _with_launcher_path(tmp: str, env: dict[str, str]) -> dict[str, str]:
    """Put a capable source-style launcher beside the hermetic provider binaries."""
    launcher_bin = Path(tmp) / "launcher-bin"
    launcher_bin.mkdir(exist_ok=True)
    launcher = launcher_bin / "mozyo-bridge"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return {**env, "PATH": os.pathsep.join((str(launcher_bin), env["PATH"]))}


class _SplitOnHealHerdr(_StatefulHerdr):
    """A herdr that *reports* the requested tab but actually lands the pane elsewhere.

    Models a spec-drift / lying runtime (Redmine #13705): when ``split_next_start`` is
    set, the next ``agent start`` echoes the requested ``--tab`` in its ``agent_started``
    payload (so the launch landing guard passes) but records the live row in a DIFFERENT
    tab, so ``agent list`` later shows a split pair. The same-tab postcondition must
    catch it even though the launch guard did not.
    """

    def __init__(self, *, created_workspace="wL"):
        super().__init__(created_workspace=created_workspace)
        self.split_next_start = False

    def run(self, argv, **kw):
        rest = list(argv[1:])
        if rest[:2] == ["agent", "start"] and self.split_next_start and "--tab" in rest:
            self.split_next_start = False
            result = super().run(argv, **kw)
            # The live row we just appended is split into a different tab.
            self.agents[-1]["tab_id"] = self.agents[-1]["tab_id"] + "_split"
            return result
        return super().run(argv, **kw)


class _ListControlHerdr(_StatefulHerdr):
    """A herdr whose ``agent list`` breaks in a named way once the relaunch has happened.

    ``fail_list_on`` is a set of 1-indexed ``agent list`` call numbers to answer with a
    non-zero exit (so ``_live_rows`` raises ``HerdrSessionStartError`` — an unreadable
    inventory). Ordinals are fine for a PREflight read, which is the first call by
    construction.

    ``fail_list_after_start`` / ``drop_role_after_start`` are the post-relaunch faults, and
    they are deliberately NOT keyed on a call ordinal (Redmine #13948 correction, j#81034).
    They were, and it made these tests depend on how many times `prepare_session` happens
    to read the inventory internally — so adding the #13948 startup probe silently
    re-aimed the fault at a probe read instead of the postcondition. Worse, the fault
    itself changed the count (a dropped role makes the probe retry), so no fixed number
    could be correct. Keyed on "the relaunch has happened" instead, each fault means what
    the test says it means — the slot vanished / the inventory went unreadable after the
    relaunch — for EVERY subsequent read, whoever makes it. Redmine #13705 R1-F3.
    """

    def __init__(
        self,
        *,
        fail_list_on=(),
        fail_list_after_start=False,
        drop_role_after_start=None,
        **kw,
    ):
        super().__init__(**kw)
        self._list_calls = 0
        self._fail_list_on = set(fail_list_on)
        self._fail_list_after_start = fail_list_after_start
        self._drop_role_after_start = drop_role_after_start

    @property
    def _relaunched(self) -> bool:
        return bool(self.start_argvs)

    def run(self, argv, **kw):
        rest = list(argv[1:])
        if rest == ["agent", "list"]:
            self._list_calls += 1
            unreadable = self._list_calls in self._fail_list_on or (
                self._fail_list_after_start and self._relaunched
            )
            if unreadable:
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="herdr inventory unavailable"
                )
            if self._drop_role_after_start and self._relaunched:
                keep = [
                    a for a in self.agents if self._drop_role_after_start not in a["name"]
                ]
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"agents": keep}), stderr=""
                )
        return super().run(argv, **kw)


class HerdrSublaneOpsTest(unittest.TestCase):
    def setUp(self):
        # Every op here goes through `prepare_session`, which since #13948 waits a bounded
        # deadline for each requested role to come up. Keep the ROUNDS (so these tests
        # exercise the same retry path production does) and drop only the wall clock —
        # otherwise a scenario whose slot never comes up costs 10s of real sleeping.
        self._probe_wait = _no_probe_wait()
        self._probe_wait.__enter__()
        self.addCleanup(self._probe_wait.__exit__, None, None, None)

    def _ops(self, tmp, herdr, *, lane_label="issue_13331_x", issue="13331"):
        home = Path(tmp) / "home"
        home.mkdir(exist_ok=True)
        coord = Path(tmp) / "coord"
        coord.mkdir(exist_ok=True)
        binpath = _fake_binary(tmp)
        env = _with_launcher_path(
            tmp,
            with_provider_path({HERDR_ENV: str(binpath), "MOZYO_BRIDGE_HOME": str(home)}),
        )
        herdr.attest_home = home
        ops = HerdrSublaneActuatorOps(
            repo_root=coord,
            lane_label=lane_label,
            issue=issue,
            env=env,
            runner=herdr.run,
        )
        return ops, home

    def test_sender_preflight_fails_closed_before_anchor_or_env_attestation(self) -> None:
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            ok, detail = ops.preflight_dispatch_sender()
            self.assertFalse(ok)
            self.assertIn("missing_sender_env", detail)

            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                registered = register_workspace(ops.repo_root, home=home)
            ops.env = {
                **ops.env,
                "MOZYO_WORKSPACE_ID": registered.record.workspace_id,
                "MOZYO_AGENT_ROLE": "codex",
                "MOZYO_LANE_ID": "default",
            }
            ok, detail = ops.preflight_dispatch_sender()
        self.assertTrue(ok)
        self.assertIn("matches", detail)

    def test_sender_preflight_rejects_wrong_nonempty_workspace(self) -> None:
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                register_workspace(ops.repo_root, home=home)
            ops.env = {
                **ops.env,
                "MOZYO_WORKSPACE_ID": "wrong-but-nonempty",
                "MOZYO_AGENT_ROLE": "codex",
                "MOZYO_LANE_ID": "default",
            }
            ok, detail = ops.preflight_dispatch_sender()
        self.assertFalse(ok)
        self.assertIn("env_anchor_workspace_mismatch", detail)

    def test_sender_preflight_rejects_default_bound_claude_sender(self) -> None:
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                registered = register_workspace(ops.repo_root, home=home)
            ops.env = {
                **ops.env,
                "MOZYO_WORKSPACE_ID": registered.record.workspace_id,
                "MOZYO_AGENT_ROLE": "claude",
                "MOZYO_LANE_ID": "default",
            }
            ok, detail = ops.preflight_dispatch_sender()
        self.assertFalse(ok)
        self.assertIn("configured coordinator provider 'codex'", detail)

    def test_sender_preflight_rejects_nondefault_lane(self) -> None:
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                registered = register_workspace(ops.repo_root, home=home)
            ops.env = {
                **ops.env,
                "MOZYO_WORKSPACE_ID": registered.record.workspace_id,
                "MOZYO_AGENT_ROLE": "codex",
                "MOZYO_LANE_ID": "issue_13613_nested",
            }
            ok, detail = ops.preflight_dispatch_sender()
        self.assertFalse(ok)
        self.assertIn("is not the coordinator default lane", detail)

    def test_sender_preflight_follows_coordinator_provider_binding(self) -> None:
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                registered = register_workspace(ops.repo_root, home=home)
            ops.env = {
                **ops.env,
                "MOZYO_WORKSPACE_ID": registered.record.workspace_id,
                "MOZYO_AGENT_ROLE": "claude",
                "MOZYO_LANE_ID": "default",
            }
            target = (
                "mozyo_bridge.e_110_execution_platform."
                "f_140_delegated_coordinator_nested_handoff.application."
                "main_lane_guard_gate.resolve_coordinator_provider"
            )
            with patch(target, return_value="claude"):
                ok, detail = ops.preflight_dispatch_sender()
        self.assertTrue(ok)
        self.assertIn("coordinator binding", detail)

    def test_append_then_read_lane_round_trips(self) -> None:
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                # A fresh worktree has no herdr workspace yet -> read_lane is absent.
                self.assertIsNone(ops.read_lane(str(worktree)))
                startup = ops.append_lane_column(str(worktree))
                view = ops.read_lane(str(worktree))
                lane_ws = read_anchor(worktree)["workspace_id"]
        self.assertTrue(startup.ok)
        self.assertTrue(startup.action_id)
        self.assertEqual({role.provider for role in startup.roles}, {"codex", "claude"})
        self.assertTrue(all(role.health == "healthy" for role in startup.roles))
        self.assertIsNotNone(view)
        self.assertEqual(view.workspace_id, lane_ws)
        # The lane label IS the mzb1 lane segment (#13377 shared model).
        self.assertEqual(view.lane_id, "issue_13331_x")
        self.assertEqual(view.lane_label, "issue_13331_x")
        self.assertEqual(view.issue, "13331")
        self.assertEqual(view.repo_root, str(worktree))
        # Both managed slots resolve to live herdr locators in the lane workspace.
        self.assertTrue(view.gateway_pane and view.gateway_pane.startswith("wL:"))
        self.assertTrue(view.worker_pane and view.worker_pane.startswith("wL:"))
        self.assertNotEqual(view.gateway_pane, view.worker_pane)
        self.assertEqual(view.state, SUBLANE_STATE_ACTIVE)

    def test_append_launches_claude_worker_in_auto_permission_mode(self) -> None:
        # Redmine #13360: lane creation is a managed-pane chokepoint, so the lane's
        # Claude worker must launch reproducibly auto (#11925 parity with the tmux
        # `cockpit append` path) — without it every herdr lane worker stalls on its
        # first permission prompt (coordinator-measured, 2026-07-07). Codex never
        # gets the flag.
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
        by_provider = {}
        for argv in herdr.start_argvs:
            # A wrapped provider follows the final ``--`` (the launcher follows the first).
            last_sep = len(argv) - 1 - argv[::-1].index("--")
            argv0 = argv[last_sep + 1]
            provider = next(
                (p for p in ("claude", "codex") if argv0 == provider_bin_path(p)),
                argv0,
            )
            by_provider[provider] = argv
        claude = by_provider["claude"]
        idx = claude.index("--permission-mode")
        self.assertEqual(claude[idx + 1], "auto")
        self.assertGreater(idx, len(claude) - 1 - claude[::-1].index("--"))
        self.assertNotIn("--permission-mode", by_provider["codex"])

    def test_append_upserts_lane_metadata_record(self) -> None:
        # Redmine #13356 j#73386 Q2 / #13377: the create command boundary records the
        # display join keyed on the worktree's stable path token, carrying the lane
        # unit fields (`repo_workspace_id`, `lane_id`) the shared-model reads join on.
        from mozyo_bridge.core.state.lane_metadata import load_lane_records
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            derive_lane_workspace_token,
        )

        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            ops.branch = "issue_13331_x"
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                records = load_lane_records(home=home)
            token = derive_lane_workspace_token(str(worktree.resolve()))
        self.assertIn(token, records)
        record = records[token]
        self.assertEqual(record.lane_label, "issue_13331_x")
        self.assertEqual(record.lane_id, "issue_13331_x")
        self.assertEqual(record.issue_id, "13331")
        self.assertEqual(record.branch, "issue_13331_x")
        self.assertEqual(record.worktree_path, str(worktree))
        self.assertEqual(record.source_backend, "herdr")
        self.assertEqual(record.status, "active")

    def test_append_declares_lane_owner_binding(self) -> None:
        # Redmine #13681 W1: with a `--journal` anchor the create command boundary
        # declares the lane's owner binding in the lifecycle component, keyed on the
        # live `(project workspace, lane_label)` unit — separate from (and CAS'd,
        # unlike) the display-metadata upsert. The lane resolves as the single active
        # owner of its issue, and the decision anchor is re-readable.
        from mozyo_bridge.core.state.lane_lifecycle import (
            DISPOSITION_ACTIVE,
            OWNER_RESOLVED,
            LaneLifecycleKey,
            LaneLifecycleStore,
        )

        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            ops.journal = "76630"
            root = str(ops.repo_root)
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(root)
                project_ws = read_anchor(ops.repo_root)["workspace_id"]
                store = LaneLifecycleStore(home=home)
                record = store.get(LaneLifecycleKey(project_ws, "issue_13331_x"))
                owner = store.resolve_owner(project_ws, "13331")
        self.assertIsNotNone(record)
        self.assertEqual(record.lane_disposition, DISPOSITION_ACTIVE)
        self.assertEqual(record.issue_id, "13331")
        self.assertEqual(record.decision_source, "redmine")
        self.assertEqual(record.decision_issue_id, "13331")
        self.assertEqual(record.decision_journal, "76630")
        self.assertEqual(owner.status, OWNER_RESOLVED)
        self.assertEqual(owner.lane_id, "issue_13331_x")

    def test_append_without_journal_leaves_lane_owner_unbound(self) -> None:
        # Redmine #13681 W1: a create with no `--journal` anchor is owner-unbound — no
        # lifecycle row is written, so the issue has no resolvable owner. The gap is
        # honest (fail-closed at the roster / send gate), never a guessed owner.
        from mozyo_bridge.core.state.lane_lifecycle import (
            OWNER_ABSENT,
            LaneLifecycleKey,
            LaneLifecycleStore,
        )

        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)  # no journal supplied
            root = str(ops.repo_root)
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(root)
                project_ws = read_anchor(ops.repo_root)["workspace_id"]
                store = LaneLifecycleStore(home=home)
                record = store.get(LaneLifecycleKey(project_ws, "issue_13331_x"))
                owner = store.resolve_owner(project_ws, "13331")
        self.assertIsNone(record)
        self.assertEqual(owner.status, OWNER_ABSENT)

    def test_non_git_lane_launches_as_project_lane_unit_not_default(self) -> None:
        # Redmine #13392 (required test 2): a non-git lane's runtime root IS the workspace
        # root (the use case collapses skip_no_git to repo_root). Its slots must stand up
        # as a ``(project workspace, lane_label)`` unit — a NON-default lane distinct from
        # the coordinator's default-lane pair — so the #13380 dedicated-host placement
        # (``_launch_target_for_lane`` excludes the coordinator's default workspace for a
        # non-default lane) applies exactly as it does for a git lane. Here the append
        # target == the workspace root, the collapsed non-git shape.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            DEFAULT_LANE,
            decode_assigned_name,
        )

        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)  # repo_root = coord (a non-git dir)
            root = str(ops.repo_root)
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(root)
                view = ops.read_lane(root)
                project_ws = read_anchor(ops.repo_root)["workspace_id"]
        self.assertIsNotNone(view)
        # The lane is a (project workspace, lane_label) unit — project identity (shared
        # with the coordinator pair), lane segment the label, NEVER the default lane.
        self.assertEqual(view.workspace_id, project_ws)
        self.assertEqual(view.lane_id, "issue_13331_x")
        self.assertNotEqual(view.lane_id, DEFAULT_LANE)
        # Every launched managed slot decodes to that same lane unit (not default-lane).
        self.assertTrue(herdr.agents)
        for agent in herdr.agents:
            decode = decode_assigned_name(agent["name"])
            self.assertTrue(decode.ok and decode.identity is not None)
            self.assertEqual(decode.identity.workspace_id, project_ws)
            self.assertEqual(decode.identity.lane_id, "issue_13331_x")
            self.assertNotEqual(decode.identity.lane_id, DEFAULT_LANE)

    def test_two_non_git_lanes_on_one_root_keep_distinct_metadata(self) -> None:
        # Redmine #13392 (required test 3): two lanes on ONE non-git workspace root must
        # not collide in the token-keyed lane_metadata store — the path-only ``wt_`` token
        # is identical for both (same root), so without the ``dl_`` (root, lane_id) key the
        # second create would overwrite the first record. Both records must survive and the
        # ``(repo_workspace_id, lane_id)`` unit join must distinguish them.
        from mozyo_bridge.core.state.lane_metadata import (
            lane_records_by_unit,
            load_lane_records,
        )

        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops_a, home = self._ops(
                tmp, herdr, lane_label="issue_13392_a", issue="13392"
            )
            root = str(ops_a.repo_root)  # the shared non-git workspace root
            ops_b = HerdrSublaneActuatorOps(
                repo_root=ops_a.repo_root,
                lane_label="issue_13392_b",
                issue="13392",
                env=ops_a.env,
                runner=herdr.run,
            )
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops_a.append_lane_column(root)
                ops_b.append_lane_column(root)
                records = load_lane_records(home=home)
                project_ws = read_anchor(ops_a.repo_root)["workspace_id"]
        # Two distinct records survive — the second lane never overwrote the first.
        self.assertEqual(len(records), 2)
        self.assertEqual(
            sorted(r.lane_label for r in records.values()),
            ["issue_13392_a", "issue_13392_b"],
        )
        self.assertTrue(all(tok.startswith("dl_") for tok in records))
        # The unit join keys both lanes on the shared project workspace, distinctly.
        by_unit = lane_records_by_unit(records)
        self.assertIn((project_ws, "issue_13392_a"), by_unit)
        self.assertIn((project_ws, "issue_13392_b"), by_unit)

    def test_read_lane_gateway_only(self) -> None:
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                # Drop the worker slot from the live inventory (lost worker).
                herdr.agents = [
                    a for a in herdr.agents if "_claude_" not in a["name"]
                ]
                view = ops.read_lane(str(worktree))
        self.assertIsNotNone(view)
        self.assertTrue(view.gateway_pane)
        self.assertIsNone(view.worker_pane)
        self.assertEqual(view.state, SUBLANE_STATE_GATEWAY_ONLY)

    def test_read_lane_ignores_foreign_and_other_workspace_rows(self) -> None:
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                # A foreign (non-mzb1) agent and a mzb1 agent in ANOTHER workspace must
                # not be folded into this lane.
                herdr.agents.append({"name": "someones-shell", "pane_id": "wX:p9"})
                herdr.agents.append(
                    {"name": "mzb1_otherZ2Dws_codex_default", "pane_id": "wY:p9"}
                )
                view = ops.read_lane(str(worktree))
        self.assertIsNotNone(view)
        self.assertTrue(view.gateway_pane.startswith("wL:"))
        self.assertTrue(view.worker_pane.startswith("wL:"))

    def test_probe_gateway_ready_presence(self) -> None:
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                view = ops.read_lane(str(worktree))
                self.assertTrue(ops.probe_gateway_ready(view.gateway_pane))
                self.assertFalse(ops.probe_gateway_ready("wL:p999"))
                self.assertFalse(ops.probe_gateway_ready(""))

    def test_probe_gateway_ready_requires_rendered_content(self) -> None:
        # Redmine #13378: a live-but-blank pane (TUI still booting) is NOT ready —
        # the liveness-only probe fired the in-create dispatch into a still-booting
        # composer (the measured #13366 空振り). Rendered content flips it ready.
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                view = ops.read_lane(str(worktree))
                herdr.read_text = "   \n  "
                self.assertFalse(ops.probe_gateway_ready(view.gateway_pane))
                herdr.read_text = "▌ composer"
                self.assertTrue(ops.probe_gateway_ready(view.gateway_pane))

    def test_heal_lane_column_relaunches_only_the_missing_slot(self) -> None:
        # Redmine #13378: the self-heal is append_lane_column again — prepare_session
        # is adopt-or-launch idempotent, so the surviving worker is adopted (workspace
        # pin) and only the vanished gateway slot is relaunched into the SAME workspace.
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                launches_before = len(herdr.start_argvs)
                # The gateway codex slot vanishes (host-level kill, not a pane close).
                herdr.agents = [a for a in herdr.agents if "_codex_" not in a["name"]]
                ops.heal_lane_column(str(worktree))
                view = ops.read_lane(str(worktree))
        self.assertEqual(len(herdr.start_argvs), launches_before + 1)
        relaunch = herdr.start_argvs[-1]
        last_sep = len(relaunch) - 1 - relaunch[::-1].index("--")
        self.assertEqual(relaunch[last_sep + 1], provider_bin_path("codex"))
        # The relaunch is pinned into the surviving worker's workspace (adopt pin),
        # so no second workspace (and no new base pane) is created.
        self.assertEqual(relaunch[relaunch.index("--workspace") + 1], "wL")
        self.assertIsNotNone(view)
        self.assertEqual(view.state, SUBLANE_STATE_ACTIVE)
        self.assertTrue(view.gateway_pane.startswith("wL:"))
        self.assertTrue(view.worker_pane.startswith("wL:"))

    def test_heal_from_incompatible_runtime_fails_closed_zero_side_effect(self) -> None:
        # Redmine #13705: the measured incident — a lane built under the #13411 same-tab
        # contract healed by an older installed runtime that lacks it. The mutating heal
        # fences BEFORE any pane side effect: no new `agent start`, and a fail-closed
        # RuntimeError naming the runtime skew.
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            # Simulate the older installed runtime that lacks the same-tab contract.
            ops.runtime_placement_fingerprint = RuntimePlacementFingerprint(
                version="0.10.0", capabilities=frozenset()
            )
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                launches_before = len(herdr.start_argvs)
                # The gateway vanishes; a heal would relaunch it — but the runtime is
                # incompatible with the lane's placement contract.
                herdr.agents = [a for a in herdr.agents if "_codex_" not in a["name"]]
                with self.assertRaises(RuntimeError) as ctx:
                    ops.heal_lane_column(str(worktree))
        self.assertIn("runtime_lacks_placement_contract", str(ctx.exception))
        # Zero side effect: no new agent start ran after the fence blocked.
        self.assertEqual(len(herdr.start_argvs), launches_before)

    def test_heal_from_unknown_provenance_runtime_fails_closed(self) -> None:
        # Redmine #13705: a runtime with no resolvable build version cannot attest its
        # placement provenance -> fail closed with zero side effect.
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            ops.runtime_placement_fingerprint = RuntimePlacementFingerprint(
                version="", capabilities=frozenset()
            )
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                launches_before = len(herdr.start_argvs)
                herdr.agents = [a for a in herdr.agents if "_codex_" not in a["name"]]
                with self.assertRaises(RuntimeError) as ctx:
                    ops.heal_lane_column(str(worktree))
        self.assertIn("provenance_unknown", str(ctx.exception))
        self.assertEqual(len(herdr.start_argvs), launches_before)

    def test_compatible_heal_passes_same_tab_postcondition(self) -> None:
        # Redmine #13705: a compatible heal rejoins the surviving slot's tab, so the
        # postcondition confirms both slots share one (workspace, tab) container.
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                herdr.agents = [a for a in herdr.agents if "_codex_" not in a["name"]]
                ops.heal_lane_column(str(worktree))  # no raise
                view = ops.read_lane(str(worktree))
        self.assertEqual(view.state, SUBLANE_STATE_ACTIVE)

    def test_heal_that_splits_the_pair_fails_the_postcondition(self) -> None:
        # Redmine #13705 postcondition: even when the launch landing guard is satisfied
        # (the runtime reports the requested tab), a relaunch that actually split the
        # pair across tabs is caught on read-back and fails closed.
        herdr = _SplitOnHealHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                herdr.agents = [a for a in herdr.agents if "_codex_" not in a["name"]]
                herdr.split_next_start = True
                with self.assertRaises(RuntimeError) as ctx:
                    ops.heal_lane_column(str(worktree))
        self.assertIn("postcondition", str(ctx.exception))
        self.assertIn("pair is split", str(ctx.exception))

    def test_heal_preflight_inventory_unreadable_fails_closed_zero_side_effect(self) -> None:
        # Redmine #13705 R1-F3: an unreadable inventory at preflight is fail-closed —
        # the pair invariant is unverifiable, so a mutating heal refuses BEFORE any
        # side effect (never proceeds on unknown topology).
        herdr = _ListControlHerdr(fail_list_on={1})  # first `agent list` fails
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                launches_before = len(herdr.start_argvs)
                with self.assertRaises(RuntimeError) as ctx:
                    ops.heal_lane_column(str(worktree))
        self.assertIn("inventory_unreadable", str(ctx.exception))
        self.assertIn("preflight", str(ctx.exception))
        self.assertEqual(len(herdr.start_argvs), launches_before)

    def test_heal_postcondition_inventory_unreadable_fails_closed(self) -> None:
        # Redmine #13705 R1-F3: an unreadable inventory AFTER the relaunch does not pass
        # as success — the same-tab placement is unverified, so it fails closed.
        # list calls: 1=preflight, 2=append(prepare_session), 3=postcondition.
        herdr = _ListControlHerdr(fail_list_after_start=True)
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                herdr.start_argvs.clear()  # arm the fault for the HEAL's relaunch only
                herdr.agents = [a for a in herdr.agents if "_codex_" not in a["name"]]
                with self.assertRaises(RuntimeError) as ctx:
                    ops.heal_lane_column(str(worktree))
        self.assertIn("postcondition", str(ctx.exception))
        self.assertIn("inventory_unreadable", str(ctx.exception))

    def test_heal_postcondition_missing_slot_fails_closed(self) -> None:
        # Redmine #13705 R1-F3: if the post-heal read-back cannot confirm BOTH slots
        # co-located (a slot vanished), the heal fails closed rather than reporting
        # success on an incomplete pair.
        herdr = _ListControlHerdr(drop_role_after_start="_codex_")
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                herdr.start_argvs.clear()  # arm the fault for the HEAL's relaunch only
                herdr.agents = [a for a in herdr.agents if "_codex_" not in a["name"]]
                with self.assertRaises(RuntimeError) as ctx:
                    ops.heal_lane_column(str(worktree))
        self.assertIn("postcondition", str(ctx.exception))
        self.assertIn("split or incomplete", str(ctx.exception))
        # Redmine #13933 R11: the full-pair heal's typed reason distinguishes an incomplete
        # pair from a live split.
        self.assertIsInstance(ctx.exception, SublaneHealError)
        self.assertEqual(ctx.exception.reason, HEAL_REASON_PAIR_INCOMPLETE)

    def test_target_scoped_heal_tolerates_absent_sibling(self) -> None:
        # Redmine #13933 R11 j#81429 #3: the pair-level launcher, driven for ONE owed
        # participant (the worker), converges an approved partial pair — the still-absent
        # sibling (gateway) is a partial state a later leg converges, NOT a launch failure.
        # This is the exact live shape (#13846) the full-pair postcondition fenced into a
        # permanent effect_failed; a target-scoped launch must NOT raise.
        herdr = _ListControlHerdr(drop_role_after_start="_codex_")
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                herdr.start_argvs.clear()  # arm the drop for the HEAL's relaunch only
                herdr.agents = [a for a in herdr.agents if "_codex_" not in a["name"]]
                # No raise: the worker (target) is live; the absent gateway is tolerated.
                ops.heal_lane_column(str(worktree), target_provider="claude")

    def test_target_scoped_heal_fails_closed_when_its_own_slot_is_absent(self) -> None:
        # Redmine #13933 R11: tolerance is only for the SIBLING. If the target provider's
        # own owed slot did not come up live, the launch genuinely failed — fail closed with
        # the typed `launch_target_absent` reason (never a silent success on a dead target).
        herdr = _ListControlHerdr(drop_role_after_start="_codex_")
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                herdr.start_argvs.clear()
                herdr.agents = [a for a in herdr.agents if "_codex_" not in a["name"]]
                with self.assertRaises(SublaneHealError) as ctx:
                    ops.heal_lane_column(str(worktree), target_provider="codex")
        self.assertEqual(ctx.exception.reason, HEAL_REASON_TARGET_ABSENT)

    def test_target_scoped_heal_still_fails_closed_on_a_live_split(self) -> None:
        # Redmine #13933 R11 j#81429 #3: a target-scoped launch NEVER bypasses same-tab
        # placement. When the sibling is ALSO live, a relaunch that split the pair across
        # tabs still fails closed with the typed `pair_split` reason.
        herdr = _SplitOnHealHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                herdr.agents = [a for a in herdr.agents if "_codex_" not in a["name"]]
                herdr.split_next_start = True
                with self.assertRaises(SublaneHealError) as ctx:
                    ops.heal_lane_column(str(worktree), target_provider="codex")
        self.assertIn("pair is split", str(ctx.exception))
        self.assertEqual(ctx.exception.reason, HEAL_REASON_PAIR_SPLIT)

    def test_target_scoped_heal_converges_a_healthy_pair(self) -> None:
        # A target-scoped launch whose sibling DOES come up healthy and co-located passes
        # exactly like the full-pair heal (the ordinary converge path where the sibling is a
        # live participant): no raise, both slots active on read-back.
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                herdr.agents = [a for a in herdr.agents if "_codex_" not in a["name"]]
                ops.heal_lane_column(str(worktree), target_provider="codex")  # no raise
                view = ops.read_lane(str(worktree))
        self.assertEqual(view.state, SUBLANE_STATE_ACTIVE)

    def test_front_door_fingerprint_gate_blocks_drifted_runtime_zero_side_effect(
        self,
    ) -> None:
        # Redmine #13705 R1-F1: the OFFICIAL mutating front door (the use case) goes
        # ZERO-WRITE when the action-time runtime is a source/installed skew missing the
        # same-tab placement behavior the repo-local source ships. Driven through the
        # REAL `evaluate_mutation_placement_gate` policy over an injected drift
        # fingerprint (a `run_runtime_fingerprint`-shaped result), NOT a capability
        # self-injection.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_use_case import (  # noqa: E501
            SublaneActuateUseCase,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
            ACTUATE_BLOCKED,
            REASON_RUNTIME_FINGERPRINT,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
            SublaneCreateRequest,
        )

        # A realistic fingerprint the drift-detection policy produces: the active runtime
        # is missing the placement behavior the repo-local source ships.
        drift_fingerprint = {
            "ok": False,
            "status": "drifted",
            "summary": "active surface is missing gate-critical behavior (same_tab_pair_placement)",
            "probe_mismatch": [
                {"probe": "same_tab_pair_placement", "source": True, "active": False}
            ],
        }
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            coord = Path(tmp) / "coord"
            coord.mkdir()
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            binpath = _fake_binary(tmp)
            ops = HerdrSublaneActuatorOps(
                repo_root=coord,
                lane_label="issue_13705_x",
                issue="13705",
                env=with_provider_path(
                    {HERDR_ENV: str(binpath), "MOZYO_BRIDGE_HOME": str(home)}
                ),
                runner=herdr.run,
                runtime_fingerprint_reader=lambda: drift_fingerprint,
            )
            request = SublaneCreateRequest(
                issue="13705",
                lane_label="issue_13705_x",
                branch="issue_13705_x",
                worktree_path=str(worktree),
                journal="77128",
            )
            use_case = SublaneActuateUseCase(ops, gateway_ready_probes=0)
            # dispatch=False isolates the placement gate: a create/adopt-only run still
            # APPENDS panes (the #13441-lane scenario), so it must be fenced. The gate
            # runs at `execute` scope, before any worktree / append side effect.
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                outcome = use_case.run(
                    request, execute=True, dispatch=False, target_repo=str(worktree)
                )
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_RUNTIME_FINGERPRINT, outcome.blocked_reasons)
        # Zero side effect: the front door blocked before any herdr write.
        self.assertEqual(herdr.start_argvs, [])
        self.assertEqual(herdr.agents, [])

    def test_front_door_gate_blocks_via_real_fingerprint_composition_zero_write(
        self,
    ) -> None:
        # Redmine #13705 R2-F1: prove the front door goes ZERO-WRITE through the REAL
        # `run_runtime_fingerprint` composition — active probe + source scan +
        # `evaluate_fingerprint` — NOT a precomputed fingerprint dict. The mixed-runtime
        # skew is simulated by the single fact that a stale runtime lacks the #13411
        # placement behavior: the active placement probe is patched to False while the
        # repo-local source really ships the `def _tab_target_for_lane` marker, so the
        # real drift-detection produces the placement `probe_mismatch` that blocks the
        # mutation. No `runtime_fingerprint_reader` is injected.
        from mozyo_bridge import __version__ as REAL_VERSION
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_use_case import (  # noqa: E501
            SublaneActuateUseCase,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
            ACTUATE_BLOCKED,
            REASON_RUNTIME_FINGERPRINT,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
            SublaneCreateRequest,
        )

        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            # A real repo-local source tree that SHIPS the #13411 placement marker,
            # with a version equal to the active runtime's (the silent-drift class).
            src_pkg = Path(tmp) / "repo" / "src" / "mozyo_bridge"
            src_pkg.mkdir(parents=True)
            (src_pkg / "__init__.py").write_text(
                f'__version__ = "{REAL_VERSION}"\n', encoding="utf-8"
            )
            (src_pkg / "herdr_lane_topology.py").write_text(
                "def _tab_target_for_lane(rows, ws, target, lane):\n    return ''\n",
                encoding="utf-8",
            )
            repo_root = Path(tmp) / "repo"
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            binpath = _fake_binary(tmp)
            ops = HerdrSublaneActuatorOps(
                repo_root=repo_root,
                lane_label="issue_13705_x",
                issue="13705",
                env=with_provider_path(
                    {HERDR_ENV: str(binpath), "MOZYO_BRIDGE_HOME": str(home)}
                ),
                runner=herdr.run,
                # No runtime_fingerprint_reader -> the REAL run_runtime_fingerprint runs.
            )
            request = SublaneCreateRequest(
                issue="13705",
                lane_label="issue_13705_x",
                branch="issue_13705_x",
                worktree_path=str(worktree),
                journal="77188",
            )
            use_case = SublaneActuateUseCase(ops, gateway_ready_probes=0)
            # Patch ONLY the active placement probe to False — the one fact a stale
            # runtime lacking #13411 would report. Source scan / evaluate_fingerprint /
            # the gate policy all run for real.
            with patch(
                "mozyo_bridge.application.doctor_runtime._probe_active_same_tab_pair",
                return_value=False,
            ):
                with patch.dict(
                    os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False
                ):
                    outcome = use_case.run(
                        request, execute=True, dispatch=False, target_repo=str(worktree)
                    )
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_RUNTIME_FINGERPRINT, outcome.blocked_reasons)
        # Zero side effect: the real-composition drift detection blocked before any write.
        self.assertEqual(herdr.start_argvs, [])
        self.assertEqual(herdr.agents, [])

    def test_front_door_fingerprint_gate_allows_matching_runtime(self) -> None:
        # A non-drifted fingerprint (no placement probe mismatch) allows actuation.
        ok_fingerprint = {"ok": True, "status": "ok", "probe_mismatch": []}
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            ops.runtime_fingerprint_reader = lambda: ok_fingerprint
            gate_ok, _ = ops.preflight_runtime_placement_gate()
        self.assertTrue(gate_ok)

    def test_read_lane_reports_pair_split_across_tabs(self) -> None:
        # Redmine #13705: `sublane list` / read-back must report a pair split across
        # tabs as `pair_split`, never `active`.
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                # Move the gateway into a different tab of the same workspace.
                for agent in herdr.agents:
                    if "_codex_" in agent["name"]:
                        agent["tab_id"] = agent.get("tab_id", "wL:t1") + "_moved"
                view = ops.read_lane(str(worktree))
        self.assertIsNotNone(view)
        self.assertTrue(view.gateway_pane and view.worker_pane)
        self.assertEqual(view.state, SUBLANE_STATE_PAIR_SPLIT)

    def test_append_failure_raises_runtime_error(self) -> None:
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            # No MOZYO_HERDR_BINARY in the adapter env -> prepare_session fails closed.
            home = Path(tmp) / "home"
            home.mkdir()
            ops = HerdrSublaneActuatorOps(
                repo_root=Path(tmp),
                lane_label="issue_13331_x",
                issue="13331",
                env={"MOZYO_BRIDGE_HOME": str(home)},
                runner=herdr.run,
            )
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                with self.assertRaises(RuntimeError):
                    ops.append_lane_column(str(worktree))

    def test_dispatch_argv_is_cross_workspace_herdr_send(self) -> None:
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, _ = self._ops(tmp, herdr)
        argv = ops.dispatch_argv(
            issue="13331",
            journal="73320",
            gateway_pane="wL:p2",
            lane_label="issue_13331_x",
            upstream_coordinator="w2:p2",
            target_repo="/path/to/lane-wt",
        )
        self.assertEqual(argv[:2], ["handoff", "send"])
        # #13377: the lane slot is named EXPLICITLY (--target-lane); --target-repo stays
        # the repo/cwd gate, never the workspace selector (j#73613).
        self.assertIn("--target-lane", argv)
        self.assertEqual(argv[argv.index("--target-lane") + 1], "issue_13331_x")
        self.assertIn("--target-repo", argv)
        self.assertEqual(argv[argv.index("--target-repo") + 1], "/path/to/lane-wt")
        # the herdr locator target is NOT a %pane -> rides the herdr rail (#13320).
        self.assertEqual(argv[argv.index("--target") + 1], "wL:p2")
        self.assertFalse(argv[argv.index("--target") + 1].startswith("%"))
        self.assertIn("--mode", argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "queue-enter")
        self.assertEqual(argv[argv.index("--role-profile") + 1], "implementation_gateway")
        self.assertIn("lane=issue_13331_x", argv)
        self.assertIn("upstream_coordinator=w2:p2", argv)


class BackendSelectorTest(unittest.TestCase):
    """`sublane start --execute` picks the herdr adapter only under backend: herdr."""

    @staticmethod
    def _repo(tmp, backend):
        repo = Path(tmp) / f"repo-{backend}"
        repo.mkdir()
        (repo / ".mozyo-bridge").mkdir()
        (repo / ".mozyo-bridge" / "config.yaml").write_text(
            f"version: 1\nterminal_transport:\n  backend: {backend}\n", encoding="utf-8"
        )
        return repo

    def _select(self, repo):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator import (  # noqa: E501
            _resolve_sublane_ops,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
            SublaneCreateRequest,
        )

        request = SublaneCreateRequest(
            issue="13331",
            lane_label="issue_13331_x",
            branch="issue_13331_x",
            worktree_path=str(repo) + "-wt",
        )
        ns = argparse.Namespace(repo=str(repo))
        return _resolve_sublane_ops(
            ns, repo_root=repo, request=request, quiet_stdout=False
        )

    def test_herdr_backend_selects_herdr_ops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ops = self._select(self._repo(tmp, "herdr"))
        self.assertIsInstance(ops, HerdrSublaneActuatorOps)

    def test_tmux_backend_selects_live_ops(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_ops import (  # noqa: E501
            LiveSublaneActuatorOps,
        )

        with tempfile.TemporaryDirectory() as tmp:
            ops = self._select(self._repo(tmp, "tmux"))
        self.assertIsInstance(ops, LiveSublaneActuatorOps)

    def test_missing_config_defaults_to_tmux(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_ops import (  # noqa: E501
            LiveSublaneActuatorOps,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo-none"
            repo.mkdir()
            ops = self._select(repo)
        self.assertIsInstance(ops, LiveSublaneActuatorOps)


class HerdrLinkedWorktreeRoundTripTest(unittest.TestCase):
    """Redmine #13377 (design j#73613): the `sublane create --execute` shape on a REAL
    linked git worktree. append_lane_column (prepare_session with lane_id=lane_label)
    mints the lane agents as slots of the MAIN checkout's project workspace, and
    read_lane resolves them by the same `(project workspace, lane_label)` unit. A live
    legacy lane (pre-#13377 `wt_<hash>` default-lane pair) is still adopted through the
    compatibility read instead of being double-created. Scratch standalone dirs (the
    other tests) do not reproduce the inheritance."""

    def _git(self, path, *args):
        subprocess.run(
            ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
        )

    def _repo_pair(self, tmp):
        main = Path(tmp) / "main"
        main.mkdir()
        self._git(main, "init", "-q")
        self._git(main, "config", "user.email", "t@t")
        self._git(main, "config", "user.name", "t")
        (main / "README.md").write_text("x", encoding="utf-8")
        self._git(main, "add", "-A")
        self._git(main, "commit", "-qm", "init")
        wt = Path(tmp) / "lane"
        self._git(main, "worktree", "add", str(wt), "-b", "issue_13331_x")
        return main, wt

    def test_append_then_read_lane_on_real_worktree_uses_project_unit(self) -> None:
        from mozyo_bridge.core.state.workspace_registry import register_workspace
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            derive_lane_workspace_token,
        )

        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            main, wt = self._repo_pair(tmp)
            home = Path(tmp) / "home"
            home.mkdir()
            binpath = _fake_binary(tmp)
            ops = HerdrSublaneActuatorOps(
                repo_root=main,
                lane_label="issue_13331_x",
                issue="13331",
                env=with_provider_path({HERDR_ENV: str(binpath), "MOZYO_BRIDGE_HOME": str(home)}),
                runner=herdr.run,
            )
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                register_workspace(main)
                main_ws = read_anchor(main)["workspace_id"]
                self.assertIsNone(ops.read_lane(str(wt)))  # fresh worktree, no agents yet
                ops.append_lane_column(str(wt))
                view = ops.read_lane(str(wt))
            token = derive_lane_workspace_token(str(wt.resolve()))
        self.assertIsNotNone(view)
        self.assertEqual(view.workspace_id, main_ws)
        self.assertNotEqual(view.workspace_id, token)  # wt_<hash> is legacy-only
        self.assertEqual(view.lane_id, "issue_13331_x")
        self.assertTrue(view.gateway_pane and view.gateway_pane.startswith("wL:"))
        self.assertTrue(view.worker_pane and view.worker_pane.startswith("wL:"))
        self.assertEqual(view.state, SUBLANE_STATE_ACTIVE)

    def test_read_lane_adopts_live_legacy_lane(self) -> None:
        from mozyo_bridge.core.state.workspace_registry import register_workspace
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            derive_lane_workspace_token,
            encode_assigned_name,
        )

        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            main, wt = self._repo_pair(tmp)
            home = Path(tmp) / "home"
            home.mkdir()
            binpath = _fake_binary(tmp)
            token = derive_lane_workspace_token(str(wt.resolve()))
            # A pre-#13377 lane pair is still live in its own wt_ workspace.
            herdr.agents = [
                {"name": encode_assigned_name(token, "codex", ""), "pane_id": "wO:p2"},
                {"name": encode_assigned_name(token, "claude", ""), "pane_id": "wO:p3"},
            ]
            ops = HerdrSublaneActuatorOps(
                repo_root=main,
                lane_label="issue_13331_x",
                issue="13331",
                env=with_provider_path({HERDR_ENV: str(binpath), "MOZYO_BRIDGE_HOME": str(home)}),
                runner=herdr.run,
            )
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                register_workspace(main)
                view = ops.read_lane(str(wt))
        self.assertIsNotNone(view)
        self.assertEqual(view.workspace_id, token)
        self.assertEqual(view.lane_id, "default")
        self.assertEqual(view.gateway_pane, "wO:p2")
        self.assertEqual(view.worker_pane, "wO:p3")


class HerdrUseCaseIntegrationTest(unittest.TestCase):
    """The pure SublaneActuateUseCase choreography over the herdr adapter (--no-dispatch,
    so the create → append → read-back → confirm legs run without driving a live send)."""

    def _run(self, tmp, herdr, *, dispatch=False):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_use_case import (  # noqa: E501
            SublaneActuateUseCase,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
            SublaneCreateRequest,
        )

        home = Path(tmp) / "home"
        home.mkdir(exist_ok=True)
        coord = Path(tmp) / "coord"  # non-git -> worktree launch is skipped
        coord.mkdir(exist_ok=True)
        worktree = Path(tmp) / "lane-wt"
        worktree.mkdir(exist_ok=True)
        binpath = _fake_binary(tmp)
        ops = HerdrSublaneActuatorOps(
            repo_root=coord,
            lane_label="issue_13331_lane",
            issue="13331",
            env=_with_launcher_path(
                tmp,
                with_provider_path(
                    {HERDR_ENV: str(binpath), "MOZYO_BRIDGE_HOME": str(home)}
                ),
            ),
            runner=herdr.run,
        )
        herdr.attest_home = home
        request = SublaneCreateRequest(
            issue="13331",
            lane_label="issue_13331_lane",
            branch="issue_13331_lane",
            worktree_path=str(worktree),
            journal="73320",
        )
        use_case = SublaneActuateUseCase(ops, gateway_ready_probes=0)
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
            return use_case.run(
                request, execute=True, dispatch=dispatch, target_repo=str(worktree)
            )

    def test_execute_no_dispatch_stands_up_lane(self) -> None:
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            outcome = self._run(tmp, herdr)
        self.assertFalse(outcome.is_blocked, msg=outcome.reason)
        self.assertTrue(outcome.gateway_pane and outcome.gateway_pane.startswith("wL:"))
        self.assertTrue(outcome.worker_pane and outcome.worker_pane.startswith("wL:"))
        self.assertFalse(outcome.adopted)

    def test_second_run_adopts_existing_lane(self) -> None:
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            first = self._run(tmp, herdr)
            self.assertFalse(first.is_blocked, msg=first.reason)
            # Re-run against the SAME tmp: the lane workspace + agents already exist, so
            # read_lane resolves both slots and the use case adopts (no new launch).
            second = self._run(tmp, herdr)
        self.assertFalse(second.is_blocked, msg=second.reason)
        self.assertTrue(second.adopted)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
