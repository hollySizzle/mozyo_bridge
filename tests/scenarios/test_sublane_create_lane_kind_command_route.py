"""`sublane create --lane-kind` from the REAL command route to the launched argv (#13647).

The acceptance review j#85848 F1 asked for and review j#85852 F2 found missing: every
earlier test built either the parsed args, the ``SublaneCreateRequest`` or the actuator ops
**by hand**, so the two production wiring lines that carry the operator's declaration —

    sublane_actuator.cmd_sublane_start:  lane_kind=getattr(args, "lane_kind", "") or ""
    sublane_actuator._resolve_sublane_ops:  lane_kind=request.lane_kind

— could both be deleted with every lane-kind test still green (measured). That is the exact
shape of the #14224 j#84882 escape: an argv -> request field drop that only an installed
black-box run caught, because the suite never crossed the mapping layer.

This scenario crosses it. For each case it:

1. parses the operator's argv with the REAL parser (``cli.build_parser``);
2. drives the REAL ``cmd_sublane_start`` and captures the ``SublaneCreateRequest`` **it**
   built — never a mirror of that construction, so a future site that drops the field fails
   here (the technique ``test_leaf_admission_cli_parity`` established for #14224);
3. hands that exact request to the production ``_resolve_sublane_ops`` to obtain the real
   actuator ops for the herdr backend;
4. runs the real ``append_lane_column`` against the shared fake herdr and asserts the
   ``agent start`` argv the lane is actually launched with.

Cross-cutting (CLI -> execution platform -> terminal adapter), so it lives in
``tests/scenarios/`` per the placement policy's decision tree branch 2. The actuator-seam
integration sibling stays as the narrower seam test.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from support.agent_provider_binaries import (  # noqa: E402
    FakeAgentBinaries,
    neutralized_overrides,
)
from support.herdr_fake import FakeHerdr  # noqa: E402

ISSUE = "13647"
LANE = "issue_13647_command_route"
HERDR_BACKEND_CONFIG = "version: 1\nterminal_transport:\n  backend: herdr\n"


class _StopBeforeActuation(Exception):
    """Unwinds the real handler once its request is captured (no side effect runs)."""


class SublaneCreateLaneKindCommandRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        (self.repo / ".mozyo-bridge").mkdir(parents=True)
        self.worktree = self.root / "lane-worktree"
        self.worktree.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.binpath = self.root / "fake-herdr"
        self.binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.binpath.chmod(self.binpath.stat().st_mode | stat.S_IEXEC)
        self._bins = FakeAgentBinaries(self.root / "provider-bins")

    def _write_config(self, placement: str) -> None:
        """The repo-local config the command route reads: herdr backend + placement."""
        (self.repo / ".mozyo-bridge" / "config.yaml").write_text(
            HERDR_BACKEND_CONFIG + placement, encoding="utf-8"
        )

    def _request_from_real_command(self, argv_extra):
        """The `SublaneCreateRequest` the REAL `cmd_sublane_start` builds from this argv."""
        import mozyo_bridge.application.cli as cli
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator as act  # noqa: E501

        args = cli.build_parser().parse_args(
            [
                "sublane",
                "create",
                "--issue",
                ISSUE,
                "--lane-label",
                LANE,
                "--branch",
                "lane-branch",
                "--worktree",
                str(self.worktree),
                "--journal",
                "85854",
                "--repo",
                str(self.repo),
                "--dry-run",
                *argv_extra,
            ]
        )
        captured = {}

        class _CapturingUseCase:
            def __init__(self, *a, **kw):
                pass

            def run(self, request, **kw):
                captured["request"] = request
                raise _StopBeforeActuation()

        original = act.SublaneActuateUseCase
        act.SublaneActuateUseCase = _CapturingUseCase
        try:
            act.cmd_sublane_start(args)
        except _StopBeforeActuation:
            pass
        finally:
            act.SublaneActuateUseCase = original
        if "request" not in captured:
            raise AssertionError(
                "cmd_sublane_start returned before building its request; the lane-kind "
                "input could not be observed"
            )
        return args, captured["request"]

    def _launch(self, argv_extra):
        """argv -> real request -> real ops -> real launch; returns the fake herdr tape."""
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator as act  # noqa: E501

        args, request = self._request_from_real_command(argv_extra)
        herdr = FakeHerdr()
        env = {
            "MOZYO_HERDR_BINARY": str(self.binpath),
            "PATH": str(self._bins.bin_dir),
            **neutralized_overrides(),
        }
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False):
            from mozyo_bridge.core.state.workspace_registry import register_workspace

            register_workspace(self.repo, home=self.home)
            ops = act._resolve_sublane_ops(
                args, repo_root=self.repo, request=request, quiet_stdout=True
            )
            # The production resolver picked the herdr adapter for this repo's backend...
            self.assertEqual(type(ops).__name__, "HerdrSublaneActuatorOps")
            # ...and the operator's env / runner are injected only for hermeticity.
            ops.env = env
            ops.runner = herdr.run
            ops.append_lane_column(str(self.worktree))
        return request, herdr

    @staticmethod
    def _second_split(herdr):
        second = herdr.start_argvs[1]
        return second[second.index("--split") + 1] if "--split" in second else None

    @staticmethod
    def _launch_order(herdr):
        return [argv[2].rsplit("_", 2)[1] for argv in herdr.start_argvs]

    def test_grandchild_command_places_and_orders_its_first_launch(self) -> None:
        self._write_config(
            "lane_placement:\n"
            "  sublane:\n"
            "    split: down\n"
            "  by_lane_kind:\n"
            "    implementation:\n"
            "      split: right\n"
            "      order: [claude, codex]\n"
        )
        request, herdr = self._launch(["--lane-kind", "implementation"])
        # The operator's token survived argv -> request...
        self.assertEqual(request.lane_kind, "implementation")
        # ...and reached the argv the panes are actually created with.
        self.assertEqual(self._second_split(herdr), "right")
        self.assertEqual(self._launch_order(herdr), ["claude", "codex"])

    def test_child_command_gets_its_own_geometry_from_the_same_config(self) -> None:
        self._write_config(
            "lane_placement:\n"
            "  sublane:\n"
            "    split: right\n"
            "  by_lane_kind:\n"
            "    delegated_coordinator:\n"
            "      split: down\n"
            "    implementation:\n"
            "      split: right\n"
        )
        request, herdr = self._launch(["--lane-kind", "delegated_coordinator"])
        self.assertEqual(request.lane_kind, "delegated_coordinator")
        self.assertEqual(self._second_split(herdr), "down")

    def test_command_without_the_flag_keeps_lane_class_geometry(self) -> None:
        # The pre-#13647 invocation through the same route: the by_lane_kind block is
        # present but never consulted, so the launch is byte-for-byte the old one.
        self._write_config(
            "lane_placement:\n"
            "  sublane:\n"
            "    split: right\n"
            "  by_lane_kind:\n"
            "    delegated_coordinator:\n"
            "      split: down\n"
        )
        request, herdr = self._launch([])
        self.assertEqual(request.lane_kind, "")
        self.assertEqual(self._second_split(herdr), "right")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
