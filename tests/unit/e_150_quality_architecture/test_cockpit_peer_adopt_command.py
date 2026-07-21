"""Fake-port specifications for the cockpit peer-adopt resolver boundary (#12978).

These exercise the ``cockpit_peer_adopt_command`` use case directly with a
synthetic :class:`CockpitPeerAdoptOps` (canned geometry / pane-runtime reads, an
in-memory cwd-identity resolver, a recording ``execute_peer_adopt``, and a ``die``
that raises) — no real tmux server, no filesystem registry resolution. They pin:

- the pure ``project_pane_runtime`` parse (short / empty line right-pads to
  three fields) and the ``read_pane_runtime`` tolerance (raise / non-zero exit ->
  empty facts),
- the pure ``missing_flags`` / ``split_peer_unit`` parsing helpers,
- the candidate resolver: runtime read through the port seam, process-role
  gating, and the unknown-cwd tolerance vs. a resolved cwd identity,
- the target resolver: the opposite-role peer's lane label mirrored onto the
  destination, and the harmless missing-Unit / missing-peer paths,
- the confirm-gated handler outcome: the ``die`` on missing flags / empty
  workspace, the blocked (exit 1) / preview / json / confirmed-apply channels,
  and that the apply path gates on ``require_tmux`` and reuses the #12972
  ``execute_peer_adopt`` executor via the port.

The end-to-end behavior over the live ``commands`` seams stays pinned by the
``test_cockpit_peer_adopt`` characterization tests; this file pins the boundary
in isolation.
"""

from __future__ import annotations

import argparse
import unittest

from mozyo_bridge.application.cockpit_peer_adopt_command import (
    CockpitPeerAdoptUseCase,
    PeerAdoptOutcome,
    missing_flags,
    project_pane_runtime,
    read_pane_runtime,
    split_peer_unit,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_geometry import (
    PEER_ADOPT_OK,
    PEER_ADOPT_ROLE_ALREADY_PRESENT,
    diagnose_cockpit_geometry,
)


def _pane(pane_id, *, workspace_id="", role="", lane_id="default", left=0, top=0, width=80, height=40):
    return {
        "pane_id": pane_id,
        "workspace_id": workspace_id,
        "role": role,
        "lane_id": lane_id,
        "pane_left": left,
        "pane_top": top,
        "pane_width": width,
        "pane_height": height,
    }


def _drift_panes():
    """The #12130 drift: Unit ``video`` has a codex pane and a role-less ``%1106``."""
    return [
        _pane("%1104", workspace_id="video", role="codex", left=0, top=0, width=41, height=39),
        _pane("%1106", left=0, top=39, width=41, height=17),  # role-less
    ]


class _Die(Exception):
    """Raised by the fake port's ``die`` so aborts terminate like the real thing."""


class FakePeerAdoptOps:
    """In-memory :class:`CockpitPeerAdoptOps`: canned reads, recording apply.

    ``geometry`` is the panes ``read_geometry`` returns; ``runtimes`` maps a pane
    id to its ``{cwd, process, lane_label}`` (default empty); ``cwd_identity`` maps
    a cwd to its resolved ``(workspace_id, lane_id)`` (default unknown). ``die``
    raises :class:`_Die`; ``require_tmux`` / ``execute_peer_adopt`` record they ran.
    """

    def __init__(self, *, geometry=None, runtimes=None, cwd_identity=None) -> None:
        self._geometry = geometry
        self._runtimes = runtimes or {}
        self._cwd_identity = cwd_identity or {}
        self.required_tmux = False
        self.executed_plans: list = []
        self.runtime_reads: list[str] = []

    def read_geometry(self, session):
        return self._geometry

    def read_pane_runtime(self, session, pane_id):
        self.runtime_reads.append(pane_id)
        return self._runtimes.get(pane_id, {"cwd": "", "process": "", "lane_label": ""})

    def resolve_cwd_identity(self, cwd):
        return self._cwd_identity.get(cwd, ("", ""))

    def require_tmux(self):
        self.required_tmux = True

    def execute_peer_adopt(self, plan):
        self.executed_plans.append(plan)

    def die(self, message):
        raise _Die(message)


def _args(**kw):
    base = dict(
        action="peer-adopt",
        json_output=False,
        dry_run=False,
        confirm=False,
        peer_pane="%1106",
        peer_unit="video/default",
        peer_role="claude",
    )
    base.update(kw)
    return argparse.Namespace(**base)


class PureHelperTest(unittest.TestCase):
    """The pure parse / projection helpers (Redmine #12978)."""

    def test_project_pane_runtime_full_line(self) -> None:
        parsed = project_pane_runtime("/workspace/repo\tclaude\tfeature-x\n")
        self.assertEqual(
            {"cwd": "/workspace/repo", "process": "claude", "lane_label": "feature-x"}, parsed
        )

    def test_project_pane_runtime_short_line_pads(self) -> None:
        self.assertEqual(
            {"cwd": "/only/cwd", "process": "", "lane_label": ""},
            project_pane_runtime("/only/cwd"),
        )

    def test_project_pane_runtime_empty(self) -> None:
        self.assertEqual(
            {"cwd": "", "process": "", "lane_label": ""}, project_pane_runtime("")
        )

    def test_read_pane_runtime_parses_stdout(self) -> None:
        def run(*argv, check=True):
            return argparse.Namespace(returncode=0, stdout="/r\tcodex\tlane-1", stderr="")

        self.assertEqual(
            {"cwd": "/r", "process": "codex", "lane_label": "lane-1"},
            read_pane_runtime(run, "%9"),
        )

    def test_read_pane_runtime_nonzero_exit_is_empty(self) -> None:
        def run(*argv, check=True):
            return argparse.Namespace(returncode=1, stdout="junk", stderr="boom")

        self.assertEqual(
            {"cwd": "", "process": "", "lane_label": ""}, read_pane_runtime(run, "%9")
        )

    def test_read_pane_runtime_raise_is_empty(self) -> None:
        def run(*argv, check=True):
            raise OSError("no tmux")

        self.assertEqual(
            {"cwd": "", "process": "", "lane_label": ""}, read_pane_runtime(run, "%9")
        )

    def test_missing_flags_reports_absent_in_order(self) -> None:
        self.assertEqual(["--pane", "--role"], missing_flags(None, "video/default", ""))
        self.assertEqual([], missing_flags("%1", "video/default", "claude"))

    def test_split_peer_unit(self) -> None:
        self.assertEqual(("video", "default"), split_peer_unit("video/default"))
        self.assertEqual(("video", ""), split_peer_unit("video"))
        self.assertEqual(("group/sub", "lane"), split_peer_unit("group/sub/lane"))


class ResolverTest(unittest.TestCase):
    """The candidate / target resolvers over the fake port (Redmine #12978)."""

    def _diagnose(self, panes=None):
        return diagnose_cockpit_geometry(
            session="mozyo-cockpit", panes=_drift_panes() if panes is None else panes
        )

    def test_candidate_unknown_cwd_skips_identity_resolution(self) -> None:
        ops = FakePeerAdoptOps()  # empty runtime for %1106
        candidate = CockpitPeerAdoptUseCase(ops).resolve_candidate("s", "%1106")
        self.assertEqual("%1106", candidate.pane_id)
        self.assertEqual("", candidate.cwd_workspace_id)
        self.assertEqual("", candidate.cwd_lane_id)
        self.assertEqual("", candidate.process_role)

    def test_candidate_resolves_cwd_identity_and_process_role(self) -> None:
        ops = FakePeerAdoptOps(
            runtimes={"%1106": {"cwd": "/co/video", "process": "claude", "lane_label": ""}},
            cwd_identity={"/co/video": ("video", "default")},
        )
        candidate = CockpitPeerAdoptUseCase(ops).resolve_candidate("s", "%1106")
        self.assertEqual("video", candidate.cwd_workspace_id)
        self.assertEqual("default", candidate.cwd_lane_id)
        self.assertEqual("claude", candidate.process_role)
        self.assertEqual("claude", candidate.process_name)

    def test_candidate_non_role_process_is_not_a_role(self) -> None:
        ops = FakePeerAdoptOps(
            runtimes={"%1106": {"cwd": "", "process": "bash", "lane_label": ""}}
        )
        candidate = CockpitPeerAdoptUseCase(ops).resolve_candidate("s", "%1106")
        self.assertEqual("", candidate.process_role)
        self.assertEqual("bash", candidate.process_name)

    def test_target_mirrors_peer_lane_label(self) -> None:
        ops = FakePeerAdoptOps(
            runtimes={"%1104": {"cwd": "", "process": "", "lane_label": "feature-x"}}
        )
        target = CockpitPeerAdoptUseCase(ops).resolve_target(
            "s", self._diagnose(), "video", "default", "claude"
        )
        self.assertEqual("video", target.workspace_id)
        self.assertEqual("default", target.lane_id)
        self.assertEqual("feature-x", target.lane_label)
        self.assertEqual("video", target.label)
        self.assertEqual(["%1104"], ops.runtime_reads)  # read the codex peer's label

    def test_target_missing_unit_has_no_label(self) -> None:
        ops = FakePeerAdoptOps()
        target = CockpitPeerAdoptUseCase(ops).resolve_target(
            "s", self._diagnose(), "ghost", "default", "claude"
        )
        self.assertIsNone(target.lane_label)
        self.assertEqual([], ops.runtime_reads)  # no peer to read


class HandlerTest(unittest.TestCase):
    """The confirm-gated handler outcome channels over the fake port (#12978)."""

    def _ops(self, **kw):
        kw.setdefault("geometry", _drift_panes())
        return FakePeerAdoptOps(**kw)

    def test_missing_flag_dies(self) -> None:
        with self.assertRaises(_Die):
            CockpitPeerAdoptUseCase(self._ops()).handle(
                "s", _args(peer_pane=None), json_output=False, dry_run=False
            )

    def test_empty_workspace_dies(self) -> None:
        with self.assertRaises(_Die):
            CockpitPeerAdoptUseCase(self._ops()).handle(
                "s", _args(peer_unit="/default"), json_output=False, dry_run=False
            )

    def test_blocked_returns_exit_one(self) -> None:
        outcome = CockpitPeerAdoptUseCase(self._ops()).handle(
            "s", _args(peer_role="codex"), json_output=False, dry_run=False
        )
        self.assertIsInstance(outcome, PeerAdoptOutcome)
        self.assertEqual(1, outcome.exit_code)
        self.assertIn("blocked", outcome.text)
        self.assertIn(PEER_ADOPT_ROLE_ALREADY_PRESENT, outcome.text)
        self.assertIsNone(outcome.json_payload)

    def test_preview_without_confirm_mutates_nothing(self) -> None:
        ops = self._ops()
        outcome = CockpitPeerAdoptUseCase(ops).handle(
            "s", _args(), json_output=False, dry_run=False
        )
        self.assertEqual(0, outcome.exit_code)
        self.assertIn("preview", outcome.text)
        self.assertIn("--confirm", outcome.text)
        self.assertFalse(ops.required_tmux)
        self.assertEqual([], ops.executed_plans)

    def test_json_emits_decision_without_applying(self) -> None:
        ops = self._ops()
        outcome = CockpitPeerAdoptUseCase(ops).handle(
            "s", _args(), json_output=True, dry_run=False
        )
        self.assertEqual(0, outcome.exit_code)
        self.assertIsNotNone(outcome.json_payload)
        self.assertTrue(outcome.json_payload["ok"])
        self.assertEqual(PEER_ADOPT_OK, outcome.json_payload["reason_code"])
        self.assertFalse(outcome.json_payload["applied"])
        self.assertFalse(ops.required_tmux)
        self.assertEqual([], ops.executed_plans)

    def test_dry_run_previews_without_applying(self) -> None:
        ops = self._ops()
        outcome = CockpitPeerAdoptUseCase(ops).handle(
            "s", _args(confirm=True), json_output=False, dry_run=True
        )
        self.assertEqual(0, outcome.exit_code)
        self.assertFalse(ops.required_tmux)
        self.assertEqual([], ops.executed_plans)

    def test_confirm_gates_tmux_and_reuses_executor(self) -> None:
        ops = self._ops()
        outcome = CockpitPeerAdoptUseCase(ops).handle(
            "s", _args(confirm=True), json_output=False, dry_run=False
        )
        self.assertEqual(0, outcome.exit_code)
        self.assertIn("applied", outcome.text)
        self.assertIn("smoke:", outcome.text)
        self.assertTrue(ops.required_tmux)
        self.assertEqual(1, len(ops.executed_plans))
        self.assertEqual("%1106", ops.executed_plans[0].pane_id)


if __name__ == "__main__":
    unittest.main()
