"""herdr lane-choreography scenario harness — finding-1 MVP cell (Redmine #13408, US C).

Parent Feature #12531 ``120_シナリオ・受入テスト基盤``. Design source of truth:
``vibes/docs/logics/herdr-scenario-test-foundation.md`` (#13398, closed) §3 (scenario
harness) / §4.1 (the #13397 finding-1 mapping), auditor arbitration #13398 j#73769
(裁定 1 = the scenario acceptance must traverse the **real composition seam**, not a
short-circuited stub; 裁定 4 = the MVP is the single finding-1 grid cell with both a
predicate-level assert and an end-to-end worker-dispatch composition assert).

Why this scenario exists (design §0.1 / §1.2)
--------------------------------------------
Every herdr worker-dispatch test to date short-circuits the inner send seam
(``FakeParser`` / a monkeypatched ``_drive_worker_send_argv`` / a class-only backend
selector), so ``sublane dispatch-worker`` under herdr is *never* driven end-to-end
through the real ``build_parser`` → ``orchestrate_handoff`` → ``herdr_effective_backend_selected``
→ rail selection. That structural blind spot is exactly where #13397 finding 1 lived:
the inner send re-resolves its effective backend from the driving process's cwd, and
in an **external adopted project** (whose ``backend: herdr`` selection lives only at
the adopted root, not a committed config every checkout carries) a cwd that resolves
elsewhere re-derives ``backend: tmux`` and validates the herdr worker locator as an
invalid tmux target — failing closed with ``target_unavailable`` (#13379 j#73722).

This module drives that real seam against the shared stateful fake herdr
(``tests/support/herdr_fake.py``, US A #13407) and asserts the routing decision at two
altitudes:

1. **predicate level** — :func:`herdr_effective_backend_selected` on the inner send's
   parsed args: ``True`` (herdr rail) with the #13397 ``--repo`` pin, ``False`` (tmux
   misroute) without it. This is the fastest, root-cause observation.
2. **end-to-end composition** — the real :func:`_drive_worker_send_argv` (``build_parser``
   → ``orchestrate_handoff``): the pinned drive rides the herdr rail (the fake herdr
   receives the injection on the worker locator, exit 0); the un-pinned drive falls to
   the tmux rail and fails closed (non-zero, no herdr injection).

The red/green split is the #13397 fix itself (``_worker_dispatch_argv(repo_root=...)``
pinning the inner ``--repo`` to the outer-selected root): production applies the pin in
:class:`HerdrWorkerDispatchOps.dispatch_to_worker`; dropping it reproduces the
pre-#13397 blocker. A classical test catches the finding-1-shaped bug before live smoke.

Harness shape (design §3.3 parametrization)
-------------------------------------------
The harness is parametrizable over the three orthogonal axes the design fixes —
``backend`` (tmux / herdr), ``topology`` (git main / linked worktree / non-git /
external config-only), and ``root_inference`` (cwd = workspace root / cwd = child
dir). This US implements ONLY the finding-1 cell
(:data:`FINDING1_CELL` = ``herdr × external config-only × child cwd``); every other
cell fails closed with :class:`NotImplementedError` so the grid expansion (US D, the
#13377 j#73640 / #13379 j#73711 cells) is a visible next step, never a silent gap.

No live binary, no tmux, no real Redmine: the external project + the divergent driving
root are synthetic temp dirs, the herdr subprocess boundary is the shared fake, and git
/ tmux availability probes are answered hermetically (not-a-git-repo / no-tmux — exactly
the external non-git posture the cell models).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_send_entry import (  # noqa: E501
    herdr_effective_backend_selected,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

from tests.support.herdr_fake import FakeHerdr


# -- scenario grid cell (design §3.3 axes) ------------------------------------


@dataclass(frozen=True)
class ScenarioCell:
    """One cell of the design §3.3 routing grid (backend × topology × root-inference)."""

    backend: str
    topology: str
    root_inference: str


#: The #13397 finding-1 MVP cell (design §4.1 / auditor 裁定 4). The only cell this US
#: implements; the grid expands to the #13377 / #13379 cells in US D.
FINDING1_CELL = ScenarioCell(
    backend="herdr",
    topology="external_config_only",
    root_inference="child_cwd",
)

#: The lane token shared by the seeded inventory names and the sender identity env, so
#: the #13305 route authority's lane-in-match resolves the same-lane worker.
LANE = "lane-1"


# -- hermetic subprocess boundary (fake herdr + git/tmux probes) --------------


class _ScenarioRunner:
    """Route the driven subprocess calls of the real send composition, hermetically.

    The composition traverses three subprocess consumers: the herdr binary (→ the
    shared :class:`FakeHerdr`), the git-topology probe of ``herdr_workspace_segment``
    (→ *not a git repo*, the external non-git posture the cell models), and — only on
    the **tmux-misroute (red) path** — ``require_tmux``'s ``sh -c 'command -v tmux'``
    availability check (→ *no tmux*, so the misrouted send fails closed exactly as a
    pure-herdr / external session would). Any other subprocess argv is unexpected and
    raises, preserving the fake's fail-closed posture (no silent canned success).
    """

    def __init__(self, fake: FakeHerdr, herdr_bin: str) -> None:
        self.fake = fake
        self.herdr_bin = herdr_bin
        #: The per-target composer text accumulated from ``pane send-text`` injections,
        #: served back on ``agent read`` so the queue-enter landing observation
        #: (``wait_for_text`` over the herdr ``capture_pane``) sees the marker it just
        #: typed — exactly as a live composer echoes it (instant), instead of polling a
        #: fixed fake read to its wall-clock deadline. Scenario-level fidelity; the US-A
        #: shared fake stays a fixed-text read model.
        self._composer: dict = {}

    def run(self, argv, **kwargs):
        head = str(argv[0])
        if head == self.herdr_bin:
            rest = list(argv[1:])
            if rest[:2] == ["pane", "send-text"] and len(rest) > 3:
                target = rest[2]
                self._composer[target] = self._composer.get(target, "") + "\n" + rest[3]
            if rest[:2] == ["agent", "read"] and len(rest) > 2:
                # Serve the live composer echo (the injected marker+body), recording the
                # call on the fake's routing tape for observation fidelity.
                self.fake.calls.append(rest)
                text = self._composer.get(rest[2], self.fake.read_text)
                payload = {"result": {"read": {"text": text, "truncated": False}}}
                return subprocess.CompletedProcess(
                    list(argv), 0, stdout=json.dumps(payload), stderr=""
                )
            return self.fake.run(argv, **kwargs)
        if head == "git" or head.endswith("/git"):
            return subprocess.CompletedProcess(
                list(argv), 128, stdout="", stderr="not a git repository"
            )
        # The only non-herdr, non-git subprocess the modelled paths issue is the tmux
        # availability probe on the misroute path — answer "no tmux" so the send that
        # wrongly fell to the tmux rail fails closed (finding-1's downstream symptom).
        if head == "sh" and any("tmux" in str(tok) for tok in argv):
            return subprocess.CompletedProcess(
                list(argv), 1, stdout="", stderr="tmux not available"
            )
        raise AssertionError(f"unexpected subprocess call in scenario: {list(argv)!r}")

    def popen(self, argv, **kwargs):
        # The queue-enter worker-dispatch rail is submit-complete with no event wait
        # (design §3.2 ACK hop), so a ``wait`` popen is not expected. Delegate to the
        # fake (which models ``wait agent-status``) so a stray wait surfaces its
        # change-semantics outcome rather than a fabricated success.
        return self.fake.popen(argv, **kwargs)


# -- the finding-1 world builder ----------------------------------------------


class _Finding1World:
    """Build the synthetic ``herdr × external config-only × child cwd`` world.

    * ``external_root`` — the adopted external project: a ``.mozyo-bridge/config.yaml``
      selecting ``backend: herdr`` and **no ``.git``** (config-only marker). This is the
      root the outer ``sublane dispatch-worker`` selected herdr on and the ``--repo`` the
      #13397 pin carries into the inner send.
    * ``driving_child`` — the divergent driving cwd: a child dir under a separate root
      that carries **no herdr config** (a plain ``.git`` checkout). Un-pinned, the inner
      send's ``repo_root_from_args`` walks up from here to a non-herdr root and
      re-derives ``backend: tmux`` — the finding-1 misroute. This is the ``child cwd``
      root-inference axis.
    * a :class:`FakeHerdr` seeded with a same-lane gateway (codex) + worker (claude) so
      the herdr rail's #13305 route authority resolves the worker locator.
    """

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.home = tmp / "home"
        self.home.mkdir()

        # The external adopted project: config-only herdr marker, no .git.
        self.external_root = tmp / "external_project"
        (self.external_root / ".mozyo-bridge").mkdir(parents=True)
        (self.external_root / ".mozyo-bridge" / "config.yaml").write_text(
            "version: 1\nterminal_transport:\n  backend: herdr\n", encoding="utf-8"
        )
        register_workspace(self.external_root, home=self.home)
        self.workspace_id = read_anchor(self.external_root)["workspace_id"]

        # The divergent driving root: a plain git checkout with NO herdr config, and a
        # child dir the drive runs from (root-inference forced from a child cwd).
        self.driving_root = tmp / "driving_checkout"
        (self.driving_root / ".git").mkdir(parents=True)
        self.driving_child = self.driving_root / "pkg" / "sub"
        self.driving_child.mkdir(parents=True)

        # A resolvable, executable fake herdr binary (the resolver requires an
        # executable file; the runner routes argv[0] == this to the fake).
        herdr_bin_path = tmp / "fake-herdr"
        herdr_bin_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        herdr_bin_path.chmod(
            herdr_bin_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
        )
        self.herdr_bin = str(herdr_bin_path)

        # Seed the same-lane gateway (codex, sender) + worker (claude) inventory in one
        # workspace, keyed by the registry workspace_id so the mzb1 decode + lane-in-match
        # resolve. The worker locator is a live herdr locator (never ``%pane``).
        self.fake = FakeHerdr()
        ws = self.fake.seed_workspace(cwd=str(self.external_root))
        self.gateway_locator = self.fake.seed_agent(
            encode_assigned_name(self.workspace_id, "codex", LANE),
            workspace_id=ws,
            provider="codex",
        )
        self.worker_locator = self.fake.seed_agent(
            encode_assigned_name(self.workspace_id, "claude", LANE),
            workspace_id=ws,
            provider="claude",
        )
        self.runner = _ScenarioRunner(self.fake, self.herdr_bin)

    # -- environment / argv -----------------------------------------------

    def sender_env(self) -> dict:
        """The attested lane-sender identity env (codex gateway in the shared workspace).

        Deliberately carries **no ``MOZYO_REPO``**: the un-pinned inner send must fall
        through to the cwd marker walk (the finding-1 mechanism), not a pinned env repo.
        """
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        env.pop("MOZYO_REPO", None)
        env["MOZYO_HERDR_BINARY"] = self.herdr_bin
        env["MOZYO_BRIDGE_HOME"] = str(self.home)
        env["MOZYO_WORKSPACE_ID"] = self.workspace_id
        env["MOZYO_AGENT_ROLE"] = "codex"
        env["MOZYO_LANE_ID"] = LANE
        return env

    def inner_argv(self, *, pin: bool) -> list:
        """The inner same-lane ``handoff send`` argv, with or without the #13397 pin.

        ``pin=True`` is the production shape (``HerdrWorkerDispatchOps`` passes
        ``repo_root=external_root``); ``pin=False`` is the pre-#13397 shape (the tmux
        adapter's default — no ``--repo``), which reproduces the finding-1 misroute.
        """
        return _worker_dispatch_argv(
            issue="13408",
            journal="74092",
            worker_pane=self.worker_locator,
            lane_label=LANE,
            gateway_callback_target=self.gateway_locator,
            target_repo=str(self.external_root),
            repo_root=str(self.external_root) if pin else None,
        )

    # -- drive helpers (run under the divergent child cwd) ----------------

    @contextlib.contextmanager
    def _driving_context(self, env: dict):
        """Enter the divergent child cwd + hermetic subprocess boundary for a drive."""
        prev_cwd = os.getcwd()
        os.chdir(self.driving_child)
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch("subprocess.run", self.runner.run))
                stack.enter_context(mock.patch("subprocess.Popen", self.runner.popen))
                stack.enter_context(
                    mock.patch("mozyo_bridge.application.commands.time.sleep")
                )
                stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
                yield stack
        finally:
            os.chdir(prev_cwd)

    def effective_backend_is_herdr(self, *, pin: bool) -> bool:
        """The inner send's :func:`herdr_effective_backend_selected` under this cell.

        Parses the inner argv and evaluates the real predicate from the divergent child
        cwd — the pure, root-cause observation (no herdr subprocess needed).
        """
        from mozyo_bridge.application.cli import build_parser
        from mozyo_bridge.application.commands_common import repo_root_from_args

        args = build_parser().parse_args(self.inner_argv(pin=pin))
        prev_cwd = os.getcwd()
        os.chdir(self.driving_child)
        try:
            with mock.patch.dict(os.environ, self.sender_env(), clear=True):
                # Redmine #13729: pass the facade-resolved repo root + target scalar.
                return herdr_effective_backend_selected(
                    repo_root=repo_root_from_args(args),
                    target=getattr(args, "target", None),
                )
        finally:
            os.chdir(prev_cwd)

    def drive_inner_send(self, *, pin: bool):
        """Drive the real ``_drive_worker_send_argv`` composition; return (rc, out, err)."""
        env = self.sender_env()
        with self._driving_context(env):
            out = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = _drive_worker_send_argv(self.inner_argv(pin=pin))
        return rc, out.getvalue(), err.getvalue()

    def dispatch_via_production_ops(self):
        """Drive the real :meth:`HerdrWorkerDispatchOps.dispatch_to_worker` (pin applied).

        This is the true end-to-end worker-dispatch composition: production — not the
        test — applies the #13397 ``--repo`` pin (``repo_root=external_root``). Returns
        (rc, out, err).
        """
        ops = HerdrWorkerDispatchOps(
            repo_root=self.external_root,
            lane_label=LANE,
            issue="13408",
            env=self.sender_env(),
            runner=self.runner.run,
        )
        env = self.sender_env()
        with self._driving_context(env):
            out = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = ops.dispatch_to_worker(
                    issue="13408",
                    journal="74092",
                    worker_pane=self.worker_locator,
                    lane_label=LANE,
                    gateway_callback_target=self.gateway_locator,
                    target_repo=str(self.external_root),
                )
        return rc, out.getvalue(), err.getvalue()

    def herdr_injections_to_worker(self) -> list:
        """The fake herdr ``pane send-*`` calls that landed on the worker locator."""
        return [
            call
            for call in self.fake.calls
            if call[:2] in (["pane", "send-text"], ["pane", "send-keys"])
            and len(call) > 2
            and call[2] == self.worker_locator
        ]


def _build_world(cell: ScenarioCell, tmp: Path) -> _Finding1World:
    """Build the world for ``cell``; only the finding-1 cell is implemented (US C MVP).

    Every other grid cell fails closed (design §5: the grid expands to the #13377 /
    #13379 cells in US D) so an unimplemented cell is a loud NotImplementedError, never
    a silently-skipped coverage gap.
    """
    if cell != FINDING1_CELL:
        raise NotImplementedError(
            f"scenario cell {cell!r} is not implemented by #13408 (finding-1 MVP); "
            "the grid expansion (#13377 j#73640 / #13379 j#73711 cells) is US D"
        )
    return _Finding1World(tmp)


# -- the finding-1 scenario ---------------------------------------------------


class Finding1ExternalProjectChildCwdScenario(unittest.TestCase):
    """#13397 finding 1 reproduced as a classical scenario (design §4.1, cell MVP).

    ``herdr × external config-only project × child cwd``: the outer dispatch selected
    herdr on the external root; the inner send re-resolves its effective backend. With
    the #13397 ``--repo`` pin it stays on the herdr rail; without it, the child-cwd
    marker walk lands on a non-herdr root and it misroutes to the tmux rail. Asserted at
    both the predicate level and the end-to-end composition level, red↔green split on
    the pin.
    """

    def setUp(self) -> None:
        self._tmp_ctx = tempfile.TemporaryDirectory()
        self.world = _build_world(FINDING1_CELL, Path(self._tmp_ctx.name))

    def tearDown(self) -> None:
        self._tmp_ctx.cleanup()

    # -- predicate level (root cause) -------------------------------------

    def test_predicate_pinned_selects_herdr_rail(self) -> None:
        # With the #13397 pin the inner send resolves its config at the external root
        # (herdr) -> the effective backend predicate is True -> herdr rail.
        self.assertTrue(
            self.world.effective_backend_is_herdr(pin=True),
            msg="pinned inner send must select the herdr rail",
        )

    def test_predicate_unpinned_misroutes_to_tmux_rail(self) -> None:
        # Without the pin the child-cwd marker walk lands on the non-herdr driving root
        # -> the predicate is False -> the send would misroute to the tmux rail. This is
        # the finding-1 root cause, caught by a pure predicate assert.
        self.assertFalse(
            self.world.effective_backend_is_herdr(pin=False),
            msg="un-pinned inner send misroutes off the herdr rail (finding 1)",
        )

    # -- end-to-end composition (real seam) -------------------------------

    def test_e2e_pinned_rides_herdr_rail_green(self) -> None:
        rc, out, err = self.world.drive_inner_send(pin=True)
        self.assertEqual(rc, 0, msg=f"pinned drive must be green\nout={out}\nerr={err}")
        # The delivery actually rode the herdr rail: the fake herdr received the
        # injection on the worker locator (never the tmux rail).
        self.assertTrue(
            self.world.herdr_injections_to_worker(),
            msg=f"expected a herdr injection to the worker locator; calls={self.world.fake.calls}",
        )
        # The inner delivery record is contained on stdout by ``_drive_worker_send_argv``
        # and only surfaced (to stderr) on failure, so a green drive prints nothing here.
        # rc == 0 + the herdr injection above are the delivery-ACK + rail-choice signals.

    def test_e2e_unpinned_fails_closed_red(self) -> None:
        rc, out, err = self.world.drive_inner_send(pin=False)
        # The misrouted send falls to the tmux rail, which is unavailable in this
        # external / pure-herdr session -> fail closed (non-zero), and it NEVER injects
        # on the herdr worker locator.
        self.assertNotEqual(
            rc, 0, msg=f"un-pinned drive must fail closed (finding 1)\nout={out}\nerr={err}"
        )
        self.assertEqual(
            self.world.herdr_injections_to_worker(),
            [],
            msg=f"misrouted send must not reach the herdr rail; calls={self.world.fake.calls}",
        )

    def test_production_dispatch_op_applies_pin_and_is_green(self) -> None:
        # The true worker-dispatch composition: `HerdrWorkerDispatchOps.dispatch_to_worker`
        # applies the #13397 pin itself (repo_root=external_root), so driving it through
        # the real inner send rides the herdr rail green — the fix, exercised as
        # production wires it (not a test-applied pin).
        rc, out, err = self.world.dispatch_via_production_ops()
        self.assertEqual(
            rc, 0, msg=f"production dispatch op must be green\nout={out}\nerr={err}"
        )
        self.assertTrue(
            self.world.herdr_injections_to_worker(),
            msg=f"production dispatch must reach the herdr worker locator; calls={self.world.fake.calls}",
        )

    # -- harness skeleton contract ----------------------------------------

    def test_unimplemented_grid_cell_fails_closed(self) -> None:
        # The harness is parametrizable over the design §3.3 axes, but this US ships only
        # the finding-1 cell; any other cell must fail closed (US D expansion), never
        # silently pass as covered.
        other = ScenarioCell(backend="tmux", topology="git_main", root_inference="root_cwd")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(NotImplementedError):
                _build_world(other, Path(tmp))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
