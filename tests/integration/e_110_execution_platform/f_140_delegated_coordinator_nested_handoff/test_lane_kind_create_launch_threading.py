"""Redmine #13647 Tranche 1b — the create path threads the lane's geometry kind.

The creating coordinator ASSERTS where in the delegation tree a new lane sits
(``sublane create --lane-kind``); this pins that assertion travelling all the way to the two
places it must reach, with the REAL collaborators wired (actuator adapter -> session
composition -> fake herdr, and the create-time declaration -> lifecycle store -> SQLite):

- the lane's **first launch**, so the panes are created with the configured lane-role
  geometry (review j#85848 Finding 1: the lifecycle row is declared only AFTER that launch,
  so at a fresh create there is no stored kind to heal from — the caller's context is the
  only authority that exists at that moment);
- the lane's **lifecycle authority row**, so every later heal resolves the same geometry
  offline.

Integration placement per `tests-placement-discovery-policy.md` 配置決定木 5.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from support.agent_provider_binaries import (  # noqa: E402
    FakeAgentBinaries,
    neutralized_overrides,
)
from support.herdr_fake import FakeHerdr  # noqa: E402

from mozyo_bridge.core.state.lane_kind import (  # noqa: E402
    LANE_KIND_DELEGATED_COORDINATOR,
    LANE_KIND_IMPLEMENTATION,
)
from mozyo_bridge.core.state.lane_lifecycle import (  # noqa: E402
    LaneLifecycleKey,
    LaneLifecycleStore,
)

WS = "ws13647"
LANE = "issue_13647_lane"
ISSUE = "13647"

class LaneKindCreateThreadingTest(unittest.TestCase):
    """`sublane create --lane-kind` reaches the durable declaration (create threading)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def _declare(self, **kwargs) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_create_lifecycle_declaration import (  # noqa: E501
            declare_created_lane_lifecycle,
        )

        from unittest.mock import patch
        import os

        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False):
            declare_created_lane_lifecycle(
                repo_workspace_id=WS,
                lane_label=LANE,
                issue=ISSUE,
                journal="85826",
                worktree_identity="wt_13647",
                **kwargs,
            )

    def test_create_declaration_records_the_callers_kind(self) -> None:
        self._declare(lane_kind=LANE_KIND_IMPLEMENTATION)
        record = LaneLifecycleStore(home=self.home).get(LaneLifecycleKey(WS, LANE))
        self.assertIsNotNone(record)
        self.assertEqual(record.lane_kind, LANE_KIND_IMPLEMENTATION)

    def test_create_declaration_without_a_kind_is_unchanged(self) -> None:
        self._declare()
        record = LaneLifecycleStore(home=self.home).get(LaneLifecycleKey(WS, LANE))
        self.assertEqual(record.lane_kind, "")

    def test_an_off_vocabulary_kind_leaves_the_lane_owner_unbound(self) -> None:
        # Best-effort contract: the store refuses, the actuation does not break, and the
        # lane honestly reads as owner-unbound rather than carrying a bogus authority.
        self._declare(lane_kind="grandchild")
        self.assertIsNone(
            LaneLifecycleStore(home=self.home).get(LaneLifecycleKey(WS, LANE))
        )

class FreshCreateLaunchThreadingTest(unittest.TestCase):
    """The FIRST launch of a fresh lane places by the creating caller's kind (j#85848 F1).

    The bridge the earlier head was missing: `sublane create --lane-kind` stored the kind on
    the lifecycle row, but that row is declared only AFTER the create's launch returns — so
    the launch that actually creates the panes had neither a caller context nor a stored kind
    and fell back to `lane_class`. Only a LATER heal honoured the configured geometry, which
    inverts the owner intent (the placement is wanted on the panes being created).

    These drive the real `HerdrSublaneActuatorOps.append_lane_column` over the shared fake
    herdr and assert the `agent start` argv the lane is actually launched with.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.worktree = self.root / "lane-worktree"
        self.worktree.mkdir()
        self.binpath = self.root / "fake-herdr"
        self.binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.binpath.chmod(self.binpath.stat().st_mode | stat.S_IEXEC)
        self._bins = FakeAgentBinaries(self.root / "provider-bins")

    def _config(self, body: str) -> None:
        """Write the repo-local `lane_placement` config the launch chokepoint reads."""
        cfg_dir = self.repo / ".mozyo-bridge"
        cfg_dir.mkdir(exist_ok=True)
        (cfg_dir / "config.yaml").write_text(body, encoding="utf-8")

    def _create(self, *, lane_kind: str) -> FakeHerdr:
        """Run the real create-side launch for a FRESH lane (no lifecycle row yet)."""
        from mozyo_bridge.core.state.workspace_registry import (
            read_anchor,
            register_workspace,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E501
            HerdrSublaneActuatorOps,
        )

        herdr = FakeHerdr()
        env = {
            "MOZYO_HERDR_BINARY": str(self.binpath),
            "PATH": str(self._bins.bin_dir),
            **neutralized_overrides(),
        }
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False):
            register_workspace(self.repo, home=self.home)
            self.workspace_id = read_anchor(self.repo)["workspace_id"]
            # Precondition of the whole finding: this lane owns NO lifecycle row yet, so a
            # stored kind cannot be the thing that places it.
            self.assertIsNone(
                LaneLifecycleStore(home=self.home).get(
                    LaneLifecycleKey(self.workspace_id, LANE)
                )
            )
            HerdrSublaneActuatorOps(
                repo_root=self.repo,
                lane_label=LANE,
                issue=ISSUE,
                lane_kind=lane_kind,
                env=env,
                runner=herdr.run,
            ).append_lane_column(str(self.worktree))
        return herdr

    @staticmethod
    def _second_split(herdr: FakeHerdr):
        second = herdr.start_argvs[1]
        return second[second.index("--split") + 1] if "--split" in second else None

    def test_fresh_create_places_the_first_launch_by_the_declared_kind(self) -> None:
        self._config(
            "lane_placement:\n"
            "  sublane:\n"
            "    split: right\n"
            "  by_lane_kind:\n"
            "    delegated_coordinator:\n"
            "      split: down\n"
        )
        herdr = self._create(lane_kind=LANE_KIND_DELEGATED_COORDINATOR)
        self.assertEqual(len(herdr.start_argvs), 2)
        # `down` is the by_lane_kind entry; `right` would be the lane-class fallback the
        # unthreaded create produced.
        self.assertEqual(self._second_split(herdr), "down")

    def test_child_and_grandchild_creates_differ_at_first_launch(self) -> None:
        # Same lane class, same config, different declared kind -> different geometry on the
        # panes the create itself makes (the owner's 親/子/孫 intent).
        config = (
            "lane_placement:\n"
            "  sublane:\n"
            "    split: right\n"
            "  by_lane_kind:\n"
            "    delegated_coordinator:\n"
            "      split: down\n"
            "    implementation:\n"
            "      split: right\n"
            "      order: [claude, codex]\n"
        )
        splits = {}
        orders = {}
        for kind in (LANE_KIND_DELEGATED_COORDINATOR, LANE_KIND_IMPLEMENTATION):
            self.setUp()
            self._config(config)
            herdr = self._create(lane_kind=kind)
            splits[kind] = self._second_split(herdr)
            orders[kind] = [argv[2].rsplit("_", 2)[1] for argv in herdr.start_argvs]
        self.assertEqual(
            splits,
            {LANE_KIND_DELEGATED_COORDINATOR: "down", LANE_KIND_IMPLEMENTATION: "right"},
        )
        # `order` is threaded on the same context, so the grandchild lane launches claude
        # first while the child keeps the requested gateway-first order.
        self.assertEqual(orders[LANE_KIND_IMPLEMENTATION], ["claude", "codex"])
        self.assertEqual(orders[LANE_KIND_DELEGATED_COORDINATOR], ["codex", "claude"])

    def test_padded_caller_kind_fails_closed_before_any_launch(self) -> None:
        # Review j#85852 F1 applies to the caller side too: the actuator must hand its token
        # to the context UNTRIMMED, so a padded value is refused at the closed-vocabulary
        # boundary instead of being quietly repaired into a valid kind. Zero launch.
        from mozyo_bridge.core.state.lane_kind import LaneKindError

        self._config("lane_placement:\n  sublane:\n    split: right\n")
        with self.assertRaises(LaneKindError):
            self._create(lane_kind=" implementation ")

    def test_create_without_a_kind_is_byte_invariant(self) -> None:
        # No `--lane-kind`: the by_lane_kind block is present but never consulted, so the
        # create keeps its pre-#13647 lane-class geometry exactly.
        self._config(
            "lane_placement:\n"
            "  sublane:\n"
            "    split: right\n"
            "  by_lane_kind:\n"
            "    delegated_coordinator:\n"
            "      split: down\n"
        )
        herdr = self._create(lane_kind="")
        self.assertEqual(self._second_split(herdr), "right")


class HostileKindReachesLifecycleAdmissionTest(unittest.TestCase):
    """The context's kind survives the boundary that actually READS it (review j#86081).

    The unit cases pin what the carrier stores; this pins the consequence, with the real
    admission boundary and a real on-disk lifecycle store. Admission resolves the fresh-launch
    authority with `getattr(launch_context, "lane_kind", None) or None` — a truth test on the
    stored value — so a carrier that kept the caller's `str` subclass turned a dry-run
    admission into a raw `RuntimeError`. Pinned separately from the unit case on purpose: the
    defect was invisible from inside the carrier, and only this side shows the cost.
    """

    class _BoolRaises(str):
        def __bool__(self):
            raise RuntimeError("__bool__ exploded")

    def test_a_dry_run_admission_survives_a_hostile_caller_kind(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_lifecycle_admission import (  # noqa: E501
            admit_launch_against_lifecycle,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_lane_launch_context import (  # noqa: E501
            LaneLaunchContext,
        )

        context = LaneLaunchContext(lane_kind=self._BoolRaises(LANE_KIND_IMPLEMENTATION))
        self.assertIs(type(context.lane_kind), str)
        with tempfile.TemporaryDirectory() as home:
            admitted = admit_launch_against_lifecycle(
                workspace_id=WS,
                lane_id=LANE,
                store_home=home,
                launch_context=context,
                dry_run=True,
            )
        self.assertEqual(admitted, LANE_KIND_IMPLEMENTATION)
        self.assertIs(type(admitted), str)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
