"""The lifecycle-stored `lane_kind` as the OFFLINE heal authority (Redmine #13647 T1b).

Tranche 1a made a fresh launch place by the caller-supplied :class:`LaneLaunchContext`.
Tranche 1b stores that kind generation-bound on the lane's lifecycle authority record, so a
RELAUNCH of an existing lane reproduces the same geometry with no caller state and no
network (disposition j#85650 P1). These drive the real ``prepare_session`` chokepoint against
a real lifecycle store and the shared fake herdr.

Integration (`tests-placement-discovery-policy.md` 配置決定木 5): two real collaborators are
wired on purpose — the launch composition AND the durable lifecycle store — which is exactly
the seam under test (review j#85848 Finding 3 moved it out of the unit sibling, where the
single-real-collaborator rule puts the pure-config placement tests).

What it pins:

- **heal** — the stored kind places a relaunch with nothing handed to the launch;
- **fresh** — a caller context places a lane whose row records no kind;
- **contradiction** — both present and disagreeing is refused with zero Herdr side effect
  (one of the two facts is stale; the launch never picks one silently);
- **uninterpretable stored token** — a tampered / foreign value is NOT treated as absent
  (review j#85848 Finding 2): it refuses before any side effect, while a BLANK value keeps
  the sanctioned ``lane_class`` fallback.
"""

from __future__ import annotations

import os
import sqlite3
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

from mozyo_bridge.core.state.lane_lifecycle import (  # noqa: E402
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
    lane_lifecycle_path,
)
from mozyo_bridge.core.state.workspace_registry import (  # noqa: E402
    read_anchor,
    register_workspace,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501,E402
    LanePlacementConfig,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501,E402
    HerdrSessionStartError,
    prepare_session,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_lane_launch_context import (  # noqa: E501,E402
    LaneLaunchContext,
)

ISSUE = "13647"
JOURNAL = "85826"


class LaneKindHealAuthorityLaunchTest(unittest.TestCase):
    @staticmethod
    def _placement(**top):
        return LanePlacementConfig.from_record(top)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._bins = FakeAgentBinaries(self.root / "provider-bins")

    def _run(
        self,
        *,
        lane,
        stored_kind=None,
        launch_context=None,
        config=None,
        tamper="",
        herdr=None,
    ):
        """Prepare a lane session with an optional DURABLE lifecycle row for that lane."""
        repo = self.root / "repo"
        repo.mkdir(exist_ok=True)
        home = self.root / "home"
        home.mkdir(exist_ok=True)
        binpath = self.root / "fake-herdr"
        binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
        herdr = FakeHerdr() if herdr is None else herdr
        env = {
            "MOZYO_HERDR_BINARY": str(binpath),
            "PATH": str(self._bins.bin_dir),
            **neutralized_overrides(),
        }
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
            register_workspace(repo, home=home)
            ws = read_anchor(repo)["workspace_id"]
            if stored_kind is not None:
                # The durable authority row this lane was CREATED with, written through the
                # public declaration surface — not hand-poked SQL.
                outcome = LaneLifecycleStore(home=home).declare_active(
                    LaneLifecycleKey(ws, lane),
                    decision=DecisionPointer(
                        source="redmine", issue_id=ISSUE, journal_id=JOURNAL
                    ),
                    issue_id=ISSUE,
                    lane_kind=stored_kind,
                )
                self.assertTrue(outcome.applied)
            if tamper:
                # A value the write surfaces would never accept — a foreign writer, a
                # hand-edited DB, or a future build's vocabulary.
                conn = sqlite3.connect(lane_lifecycle_path(home))
                try:
                    conn.execute(
                        "UPDATE lane_lifecycle_records SET lane_kind = ?", (tamper,)
                    )
                    conn.commit()
                finally:
                    conn.close()
            result = prepare_session(
                repo_root=repo,
                providers=["codex", "claude"],
                lane_id=lane,
                env=env,
                runner=herdr.run,
                lane_placement=config,
                launch_context=launch_context,
            )
        return result, herdr

    @staticmethod
    def _writes(herdr):
        """Every herdr call that CREATES something (the side effects a refusal must avoid)."""
        return [
            c
            for c in herdr.calls
            if c[:2] in (["agent", "start"], ["workspace", "create"], ["tab", "create"])
        ]

    @staticmethod
    def _second_split(herdr):
        second = herdr.start_argvs[1]
        return second[second.index("--split") + 1] if "--split" in second else None

    # -- heal -----------------------------------------------------------------

    def test_stored_kind_places_the_relaunch_with_no_caller_context(self) -> None:
        # The heal acceptance: NOTHING is handed to the launch (`launch_context=None`), yet
        # the lane places by the kind its create recorded — read offline from the lifecycle
        # authority record. Without T1b this fell back to `lane_class`.
        _, herdr = self._run(
            lane="lane-heal",
            stored_kind="delegated_coordinator",
            launch_context=None,
            config=self._placement(
                by_lane_kind={"delegated_coordinator": {"split": "down"}}
            ),
        )
        self.assertEqual(self._second_split(herdr), "down")

    def test_stored_kind_distinguishes_child_from_grandchild_on_heal(self) -> None:
        config = self._placement(
            by_lane_kind={
                "delegated_coordinator": {"split": "down"},
                "implementation": {"split": "right"},
            },
            sublane={"split": "down"},
        )
        splits = {}
        for kind in ("delegated_coordinator", "implementation"):
            self.setUp()
            _, herdr = self._run(lane=f"lane-{kind}", stored_kind=kind, config=config)
            splits[kind] = self._second_split(herdr)
        self.assertEqual(
            splits, {"delegated_coordinator": "down", "implementation": "right"}
        )

    # -- fresh / agreement ----------------------------------------------------

    def test_context_wins_when_the_lane_has_no_stored_kind(self) -> None:
        _, herdr = self._run(
            lane="lane-fresh",
            stored_kind="",
            launch_context=LaneLaunchContext(lane_kind="implementation"),
            config=self._placement(by_lane_kind={"implementation": {"split": "right"}}),
        )
        self.assertEqual(self._second_split(herdr), "right")

    def test_agreeing_context_and_stored_kind_launch(self) -> None:
        _, herdr = self._run(
            lane="lane-agree",
            stored_kind="implementation",
            launch_context=LaneLaunchContext(lane_kind="implementation"),
            config=self._placement(by_lane_kind={"implementation": {"split": "right"}}),
        )
        self.assertEqual(len(herdr.start_argvs), 2)
        self.assertEqual(self._second_split(herdr), "right")

    # -- contradiction --------------------------------------------------------

    def test_contradicting_context_refuses_with_zero_side_effect(self) -> None:
        with self.assertRaises(HerdrSessionStartError) as caught:
            self._run(
                lane="lane-conflict",
                stored_kind="implementation",
                launch_context=LaneLaunchContext(lane_kind="delegated_coordinator"),
            )
        message = str(caught.exception)
        self.assertIn("implementation", message)
        self.assertIn("delegated_coordinator", message)

    def test_contradiction_creates_no_workspace_tab_or_agent(self) -> None:
        # The refusal must precede the FIRST herdr write, not merely the launch. Asserted on
        # the SAME fake the launch drives (injected), so the empty tape is a measurement and
        # not the vacuous "no calls because nothing ran" a separate instance would give.
        herdr = FakeHerdr()
        with self.assertRaises(HerdrSessionStartError):
            self._run(
                lane="lane-conflict2",
                stored_kind="implementation",
                launch_context=LaneLaunchContext(lane_kind="coordinator"),
                herdr=herdr,
            )
        self.assertEqual(self._writes(herdr), [])
        # Positive control on the identical harness: the same injected fake DOES record
        # workspace / agent writes when the launch is admitted, so the empty tape above is
        # load-bearing.
        self.setUp()
        control = FakeHerdr()
        self._run(lane="lane-control", stored_kind="implementation", herdr=control)
        self.assertNotEqual(self._writes(control), [])

    # -- uninterpretable stored token (review j#85848 F2) ---------------------

    def test_tampered_stored_token_refuses_before_any_side_effect(self) -> None:
        # "Uninterpretable is not absent": a stored token outside the canonical vocabulary
        # is an authority value this build cannot read, so it must NOT silently degrade to
        # the lane-class fallback (which would place the pair by a geometry the durable
        # record does not actually state).
        for bad in ("grandchild", "COORDINATOR", "coordinator_assistant"):
            self.setUp()
            with self.assertRaises(HerdrSessionStartError) as caught:
                self._run(
                    lane="lane-tampered",
                    stored_kind="implementation",
                    tamper=bad,
                    config=self._placement(
                        by_lane_kind={"implementation": {"split": "right"}}
                    ),
                )
            self.assertIn("cannot interpret", str(caught.exception))

    def test_tampered_stored_token_creates_no_workspace_tab_or_agent(self) -> None:
        herdr = FakeHerdr()
        with self.assertRaises(HerdrSessionStartError):
            self._run(
                lane="lane-tampered2",
                stored_kind="implementation",
                tamper="bogus",
                herdr=herdr,
            )
        self.assertEqual(self._writes(herdr), [])

    def test_blank_stored_token_still_falls_back(self) -> None:
        # The one legitimate absence: a legacy / pre-v7 lane has no durable kind fact, so it
        # keeps the pre-#13647 lane-class geometry rather than refusing.
        _, herdr = self._run(
            lane="lane-blank",
            stored_kind="",
            tamper="",
            config=self._placement(
                by_lane_kind={"implementation": {"split": "down"}},
            ),
        )
        self.assertEqual(self._second_split(herdr), "right")

    def test_rowless_lane_is_unchanged(self) -> None:
        _, herdr = self._run(lane="lane-scratch", stored_kind=None, launch_context=None)
        self.assertEqual(len(herdr.start_argvs), 2)
        self.assertEqual(self._second_split(herdr), "right")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
