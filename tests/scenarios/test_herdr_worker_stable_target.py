"""herdr worker dispatch → stable assigned target scenario (Redmine #13485, Test #13487).

Parent US #13485 ``herdr sublane worker dispatch を stable assigned target で配送し実 turn
開始を保証する``; Bug #13486 (the false-positive ACK), Task #13488 (the resolution fix).
Design anchors: ``vibes/docs/specs/herdr-native-identity.md`` §3.1 (route authority =
lane-in-match; ``derive_target_lane`` precedence explicit > sender-same-lane > coordinator
default > legacy default) and ``vibes/docs/logics/herdr-scenario-test-foundation.md``
(the single shared fake herdr at the outermost ``Runner`` boundary).

Why this scenario exists (the #13483 j#74570 live divergence)
-------------------------------------------------------------
The herdr ``sublane dispatch-worker`` leg composes ``handoff send --to claude --target
<worker locator>``. The herdr rail's #13305 route authority **discards that locator** and
re-resolves the target from the SENDER's identity + a derived lane. Pre-#13485 the worker
dispatch passed no ``--target-lane``, so the rail derived the lane from the sender
(``derive_target_lane`` tier-2 sender-same-lane / tier-4 legacy-default). When the sender's
launch-time lane attestation **diverges** from the worker's lane — a coordinator /
cross-lane stall-drive is attested in the workspace *default* lane, not the target
sublane — the rail resolves a DIFFERENT ``claude`` slot (the coordinator's own default-lane
peer), the send delivery-ACKs (exit 0) on that wrong agent, and the real lane worker is
never touched → it stays idle. The ACK and the actual worker turn-start diverge (#13483
live recovery: ``w16:pJ`` ACKed but idle; the stable assigned name resolved and turned).

The fix (Task #13488): the worker dispatch pins ``--target-lane <lane_label>`` (the lane
the ``read_lane`` inventory decode confirmed), so the rail resolves the stable
``(workspace_id, lane_label, claude)`` identity — the same explicit-lane authority the
coordinator→gateway leg already uses — and the ACK measures submit-completion to the
intended worker. A cross-lane drive is then admitted only under the explicit
``--allow-direct-worker`` gateway-route exception (#12918), exactly as the #13483 j#74578
passing route used.

This scenario drives the **real send composition** (``build_parser`` → ``orchestrate_handoff``
→ ``herdr_effective_backend_selected`` → herdr rail → #13305 route authority) against the
shared fake herdr, with two ``claude`` slots live in two lanes, and asserts *which agent the
delivery lands on* — the routing observation, not a stubbed verdict. No live herdr, no tmux,
no real Redmine.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.workspace_registry import read_anchor, register_workspace
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_dispatch_herdr_ops import (  # noqa: E501
    HerdrWorkerDispatchOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_dispatcher import (  # noqa: E501
    _drive_worker_send_argv,
    _worker_dispatch_argv,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

from tests.support.herdr_fake import FakeHerdr
from tests.scenarios.test_herdr_lane_choreography import _ScenarioRunner

#: The target sublane the worker lives in — the stable lane the dispatch must resolve.
LANE = "issue_13485_stable_worker_target"
#: The sender's attested lane: a coordinator / cross-lane stall-drive runs in the
#: workspace default lane, DIVERGENT from the worker's sublane (the #13483 condition).
SENDER_LANE = "default"


class _DivergentSenderWorld:
    """A workspace with two live ``claude`` slots in two lanes + a divergent sender.

    * ``project_root`` — a registered herdr workspace (``backend: herdr`` config, no
      ``.git`` — the external / pure-herdr posture the shared fake models). Its registry
      ``workspace_id`` is the mzb1 workspace segment every seeded slot is keyed on.
    * ``worker_locator`` — the target sublane's ``claude`` slot
      (``encode_assigned_name(ws, "claude", LANE)``): the agent the ``read_lane``
      inventory decode found and the dispatch intends.
    * ``default_locator`` — a SECOND live ``claude`` in the workspace *default* lane
      (``encode_assigned_name(ws, "claude", SENDER_LANE)``): the coordinator's own
      default-lane peer, the wrong agent the sender-lane derivation resolves.
    * the sender is attested (env) as a ``codex`` in ``SENDER_LANE`` — the coordinator
      driving a cross-lane recovery, its lane divergent from the worker's.
    """

    def __init__(self, tmp: Path, *, seed_lane_worker: bool = True) -> None:
        self.tmp = tmp
        self.home = tmp / "home"
        self.home.mkdir()

        self.project_root = tmp / "project"
        (self.project_root / ".mozyo-bridge").mkdir(parents=True)
        (self.project_root / ".mozyo-bridge" / "config.yaml").write_text(
            "version: 1\nterminal_transport:\n  backend: herdr\n", encoding="utf-8"
        )
        register_workspace(self.project_root, home=self.home)
        self.workspace_id = read_anchor(self.project_root)["workspace_id"]

        # A resolvable, executable fake herdr binary (the resolver requires an
        # executable file; the runner routes argv[0] == this to the fake).
        herdr_bin_path = tmp / "fake-herdr"
        herdr_bin_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        herdr_bin_path.chmod(0o755)
        self.herdr_bin = str(herdr_bin_path)

        # One workspace, four live slots keyed by the registry workspace_id so the mzb1
        # decode + lane-in-match resolve. Two `claude` in two lanes is the crux: the
        # dispatch must resolve the LANE worker, never the default-lane peer.
        self.fake = FakeHerdr()
        ws = self.fake.seed_workspace(cwd=str(self.project_root))
        self.gateway_locator = self.fake.seed_agent(
            encode_assigned_name(self.workspace_id, "codex", LANE),
            workspace_id=ws,
            provider="codex",
        )
        self.worker_locator = ""
        if seed_lane_worker:
            self.worker_locator = self.fake.seed_agent(
                encode_assigned_name(self.workspace_id, "claude", LANE),
                workspace_id=ws,
                provider="claude",
            )
        # The divergent-lane peer: the coordinator's own default-lane claude + codex.
        self.default_locator = self.fake.seed_agent(
            encode_assigned_name(self.workspace_id, "claude", SENDER_LANE),
            workspace_id=ws,
            provider="claude",
        )
        self.sender_codex_locator = self.fake.seed_agent(
            encode_assigned_name(self.workspace_id, "codex", SENDER_LANE),
            workspace_id=ws,
            provider="codex",
        )
        self.runner = _ScenarioRunner(self.fake, self.herdr_bin)

    def sender_env(self) -> dict:
        """The attested cross-lane (coordinator) sender identity env.

        ``MOZYO_LANE_ID = SENDER_LANE`` diverges from the worker's ``LANE`` — the exact
        #13483 stall-drive condition. No ``MOZYO_REPO`` (the ``--repo`` pin carries the
        herdr root instead).
        """
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        env.pop("MOZYO_REPO", None)
        env["MOZYO_HERDR_BINARY"] = self.herdr_bin
        env["MOZYO_BRIDGE_HOME"] = str(self.home)
        env["MOZYO_WORKSPACE_ID"] = self.workspace_id
        env["MOZYO_AGENT_ROLE"] = "codex"
        env["MOZYO_LANE_ID"] = SENDER_LANE
        return env

    @contextlib.contextmanager
    def _driving_context(self):
        prev_cwd = os.getcwd()
        os.chdir(self.project_root)
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch("subprocess.run", self.runner.run))
                stack.enter_context(mock.patch("subprocess.Popen", self.runner.popen))
                stack.enter_context(
                    mock.patch("mozyo_bridge.application.commands.time.sleep")
                )
                stack.enter_context(
                    mock.patch.dict(os.environ, self.sender_env(), clear=True)
                )
                yield stack
        finally:
            os.chdir(prev_cwd)

    def drive_unpinned(self):
        """Pre-#13485 shape: no ``--target-lane`` (repo pinned, so only the lane varies)."""
        argv = _worker_dispatch_argv(
            issue="13485",
            journal="74651",
            worker_pane=self.worker_locator,
            lane_label=LANE,
            gateway_callback_target=self.gateway_locator,
            target_repo=str(self.project_root),
            repo_root=str(self.project_root),
            target_lane=None,
        )
        with self._driving_context():
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = _drive_worker_send_argv(argv)
        return rc, out.getvalue(), err.getvalue()

    def dispatch_via_production_ops(self, *, worker_pane: str = None):
        """The fix, as production wires it: ``HerdrWorkerDispatchOps`` pins the lane.

        A cross-lane recovery drive passes ``--allow-direct-worker`` (the #12918
        gateway-route exception), exactly the #13483 j#74578 passing route. ``worker_pane``
        defaults to the live worker locator; a caller can pass a **stale** locator to model
        an alias that no longer resolves (the herdr rail ignores the locator and resolves
        the pinned lane slot, so a stale value must fail closed, not silently land).
        """
        ops = HerdrWorkerDispatchOps(
            repo_root=self.project_root,
            lane_label=LANE,
            issue="13485",
            env=self.sender_env(),
            runner=self.runner.run,
        )
        with self._driving_context():
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = ops.dispatch_to_worker(
                    issue="13485",
                    journal="74651",
                    worker_pane=self.worker_locator if worker_pane is None else worker_pane,
                    lane_label=LANE,
                    gateway_callback_target=self.gateway_locator,
                    target_repo=str(self.project_root),
                    allow_direct_worker=True,
                )
        return rc, out.getvalue(), err.getvalue()

    def injections_to(self, locator: str) -> list:
        """The fake herdr ``pane send-*`` calls that landed on ``locator``."""
        return [
            call
            for call in self.fake.calls
            if call[:2] in (["pane", "send-text"], ["pane", "send-keys"])
            and len(call) > 2
            and call[2] == locator
        ]


class StableWorkerTargetScenario(unittest.TestCase):
    """#13485 reproduced + fixed as a classical scenario (route observation)."""

    def setUp(self) -> None:
        self._tmp_ctx = tempfile.TemporaryDirectory()
        self.world = _DivergentSenderWorld(Path(self._tmp_ctx.name))

    def tearDown(self) -> None:
        self._tmp_ctx.cleanup()

    def test_unpinned_dispatch_false_positive_acks_on_the_wrong_lane(self) -> None:
        # Bug #13486: with no `--target-lane`, the herdr rail derives the sender's
        # (default) lane and resolves the coordinator's own default-lane claude — the
        # send delivery-ACKs (exit 0) on that WRONG agent while the real LANE worker is
        # never touched (idle). This is the ACK↔turn-start divergence (#13483).
        rc, out, err = self.world.drive_unpinned()
        self.assertEqual(rc, 0, msg=f"the misrouted send still ACKs\nout={out}\nerr={err}")
        # The delivery landed on the default-lane peer, NOT the target sublane worker.
        self.assertTrue(
            self.world.injections_to(self.world.default_locator),
            msg=f"expected the misroute onto the default-lane claude; calls={self.world.fake.calls}",
        )
        self.assertEqual(
            self.world.injections_to(self.world.worker_locator),
            [],
            msg="the real lane worker must have received nothing (idle) — the bug",
        )

    def test_pinned_dispatch_resolves_the_stable_lane_worker(self) -> None:
        # Task #13488: the production op pins `--target-lane LANE`, so the rail resolves
        # the stable `(workspace, LANE, claude)` identity — the delivery lands on the
        # real lane worker and the ACK measures submit-completion to the intended target.
        rc, out, err = self.world.dispatch_via_production_ops()
        self.assertEqual(rc, 0, msg=f"pinned dispatch must be green\nout={out}\nerr={err}")
        self.assertTrue(
            self.world.injections_to(self.world.worker_locator),
            msg=f"the delivery must reach the stable lane worker; calls={self.world.fake.calls}",
        )
        # And it never leaks onto the divergent-lane peer.
        self.assertEqual(
            self.world.injections_to(self.world.default_locator),
            [],
            msg="the pinned dispatch must not touch the default-lane claude",
        )

    def test_pinned_dispatch_fails_closed_when_lane_worker_absent(self) -> None:
        # Acceptance: a stale / unresolved lane alias fails closed — the pinned lane slot
        # `(workspace, LANE, claude)` is not live, so the route authority fails closed
        # (#13302 ledger vocabulary) and MUST NOT fall back to an all-lane `(ws, role)`
        # scan onto the live default-lane claude. Rc non-zero, no injection anywhere.
        with tempfile.TemporaryDirectory() as tmp:
            world = _DivergentSenderWorld(Path(tmp), seed_lane_worker=False)
            rc, out, err = world.dispatch_via_production_ops(worker_pane="wZ:p99")
            self.assertNotEqual(
                rc, 0, msg=f"absent lane worker must fail closed\nout={out}\nerr={err}"
            )
            self.assertEqual(
                world.injections_to(world.default_locator),
                [],
                msg=f"must not fall back onto the default-lane claude; calls={world.fake.calls}",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
