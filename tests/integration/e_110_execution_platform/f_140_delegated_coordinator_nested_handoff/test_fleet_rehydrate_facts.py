"""The fleet rehydrate fact join, wired to real collaborators (Redmine #15745).

Several REAL collaborators at once, hermetic: a real lane lifecycle store and a real lane
metadata store in a temp home, a real herdr delivery ledger, and real git worktrees on a
temp repo. Only the host-shaped edges are faked — the workspace identity, the live
assigned-name inventory, the provider binding, and the Redmine open/closed read — because
each needs a registry, a herdr server, or a network this suite must not touch.

What the wiring must prove, as opposed to what the pure unit tests already pin:

- the join reads the lane's delegation geometry, worktree binding and branch from the
  DURABLE row + metadata (not from a lane label or a pane), and the resulting plan matches;
- the durable delivery record actually gates the dispatch action end to end: seed a confirmed
  delivery for the lane's exact causal key and ``restore_dispatch`` disappears;
- an unreadable delivery ledger blocks that lane instead of reading as "never delivered";
- producing the plan mutates nothing — the temp home's byte content is identical before and
  after, so the read-only stage genuinely has an effect budget of zero.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.herdr_delivery_ledger import (  # noqa: E402
    HerdrDeliveryLedger,
    HerdrDeliveryLedgerRecord,
    RAIL_EVENT,
)
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore  # noqa: E402
from mozyo_bridge.core.state.lane_kind import (  # noqa: E402
    LANE_KIND_DELEGATED_COORDINATOR,
    LANE_KIND_IMPLEMENTATION,
)
from mozyo_bridge.core.state.lane_lifecycle_model import (  # noqa: E402
    DecisionPointer,
    LaneLifecycleKey,
)
from mozyo_bridge.core.state.lane_lifecycle_readonly import (  # noqa: E402
    LaneLifecycleReader,
)
from mozyo_bridge.core.state.lane_metadata import record_lane_created  # noqa: E402
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402
    sublane_fleet_rehydrate as rehydrate,
    sublane_herdr_projection as projection,
    sublane_reboot_audit as audit,
    workflow_provider_resolution as providers,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.fleet_rehydrate import (  # noqa: E402
    ACTION_HEAL_PAIR,
    ACTION_RESTORE_DISPATCH,
    ACTION_RESUME_BRIEF,
    BLOCKED,
    BLOCK_DISPATCH_ATTRIBUTION_UNKNOWN,
    BLOCK_DISPATCH_UNREADABLE,
    BLOCK_RESUME_PROFILE_INCOMPLETE,
    BLOCK_STARTUP_INTERACTION,
    DISPATCH_ATTRIBUTION_UNKNOWN,
    DISPATCH_DELIVERED,
    DISPATCH_NOT_APPLICABLE,
    DISPATCH_OWED,
    DISPATCH_UNREADABLE,
    REHYDRATE,
    SKIP,
    SKIP_IDLE,
    STARTUP_SCREEN_BLOCKED,
    STARTUP_SCREEN_NOT_PROBED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.fleet_rehydrate_dispatch_fold import (  # noqa: E402
    KIND_IMPLEMENTATION_REQUEST,
    ReceiverGeneration,
    redmine_marker,
)

WORKSPACE = "ws-fleet-15745"
GATEWAY = "codex"
WORKER = "claude"
ISSUE = "15745"
LANE = "issue_15745_demo"
BRANCH = "issue_15745_demo"
ANCHOR = "108799"
GATEWAY_LOCATOR = "w1V:pF"
GATEWAY_REVISION = "3"


def _git(*args, cwd):
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _home_digest(home: Path) -> str:
    """A byte digest of every file under ``home`` — the effect-budget probe."""
    digest = hashlib.sha256()
    for path in sorted(p for p in home.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(home)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class FleetRehydrateFactJoinTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.repo = root / "repo"
        self.repo.mkdir()
        _git("init", "-q", "-b", "main", cwd=self.repo)
        _git("config", "user.email", "dev@example.invalid", cwd=self.repo)
        _git("config", "user.name", "dev", cwd=self.repo)
        (self.repo / "README.md").write_text("fleet\n")
        _git("add", "README.md", cwd=self.repo)
        _git("commit", "-qm", "seed", cwd=self.repo)
        self.worktree = root / "wt-lane"
        _git("worktree", "add", "-q", "-b", BRANCH, str(self.worktree), cwd=self.repo)
        self.addCleanup(self._tmp.cleanup)

    # -- fixtures ---------------------------------------------------------

    def _declare(self, *, lane=LANE, lane_kind=LANE_KIND_IMPLEMENTATION, issue=ISSUE):
        from mozyo_bridge.core.state.lane_lifecycle_schema import lane_lifecycle_path

        store = LaneDeclarationStore(path=lane_lifecycle_path(self.home))
        outcome = store.declare_lane(
            LaneLifecycleKey(WORKSPACE, lane),
            decision=DecisionPointer(
                source="redmine", issue_id=issue, journal_id=ANCHOR
            ),
            issue_id=issue,
            worktree_identity="wt_" + lane,
            lane_kind=lane_kind,
        )
        self.assertTrue(outcome.applied, outcome.reason)
        record_lane_created(
            lane_workspace_token="wt_" + lane,
            repo_workspace_id=WORKSPACE,
            issue_id=issue,
            lane_label=lane,
            branch=BRANCH,
            worktree_path=str(self.worktree),
            lane_id=lane,
            home=self.home,
        )

    def _seed_delivery(
        self, *, journal=ANCHOR, status="sent", reason="ok", lane=LANE, target=None
    ):
        """Record a delivery aimed at the lane's gateway slot, as the real rail does."""
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            encode_assigned_name,
        )

        marker = redmine_marker(ISSUE, journal, KIND_IMPLEMENTATION_REQUEST, GATEWAY)
        HerdrDeliveryLedger(home=self.home).append(
            HerdrDeliveryLedgerRecord(
                notification_marker=marker,
                receiver=GATEWAY,
                source="redmine",
                issue_id=ISSUE,
                journal_id=journal,
                status=status,
                reason=reason,
                rail=RAIL_EVENT,
                target=target if target is not None else GATEWAY_LOCATOR,
                queue_enter_observation={
                    "gateway_binding": {
                        "assigned_name": encode_assigned_name(WORKSPACE, GATEWAY, lane),
                        "row_revision": GATEWAY_REVISION,
                    }
                },
            )
        )

    def _rows(self, *, live=True, lane=LANE):
        """Real `herdr agent list` row shape: the canonical assigned name is the key."""
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            encode_assigned_name,
        )

        if not live:
            return []
        return [
            {
                "name": encode_assigned_name(WORKSPACE, role, lane),
                "pane_id": GATEWAY_LOCATOR if role == GATEWAY else "w1V:pW",
                "agent": role,
                "agent_status": "awaiting_input",
                # Real `agent list` rows carry a revision; it is what closes the
                # recycled-locator hole in the attribution join.
                "revision": GATEWAY_REVISION,
            }
            for role in (GATEWAY, WORKER)
        ]

    def _proven_generation(self, *, locators=(GATEWAY_LOCATOR,)):
        """A live receiver whose #15227 proof holds for a record aimed at it.

        The proof itself belongs to `fresh_attestation_identity` (attestation store +
        terminal id + verified generation token) and is tested by #15227; this suite pins
        the JOIN around it, so it is injected rather than re-staged here.
        """
        return ReceiverGeneration(
            inventory_readable=True,
            live_locators=frozenset(locators),
            matches=lambda record: (record.target or "") in locators,
        )

    def _gather(
        self,
        *,
        rows=None,
        resume_inputs=None,
        issue_closed=False,
        startup_screens=None,
        receiver_generations=None,
    ):
        startup_screens = (
            startup_screens
            if startup_screens is not None
            else {LANE: STARTUP_SCREEN_NOT_PROBED}
        )
        env = mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)})
        with env, mock.patch.object(
            projection, "repo_scope_workspace_id", return_value=WORKSPACE
        ), mock.patch.object(
            providers, "resolve_gateway_provider", return_value=GATEWAY
        ), mock.patch.object(
            providers, "resolve_worker_provider", return_value=WORKER
        ), mock.patch.object(
            audit,
            "read_issue_closed_states",
            return_value={ISSUE: issue_closed},
        ), mock.patch(
            "mozyo_bridge.shared.paths.mozyo_bridge_home", return_value=self.home
        ):
            return rehydrate.gather_fleet_facts(
                self.repo,
                home=self.home,
                rows=rows if rows is not None else [],
                resume_inputs=resume_inputs,
                # No herdr binary in this hermetic suite: inject the screen verdict rather
                # than letting the probe resolve one. `observe_startup_screen` has its own
                # focused coverage.
                startup_screens=startup_screens,
                receiver_generations=receiver_generations,
            )

    # -- the join ---------------------------------------------------------

    def test_durable_row_and_metadata_drive_the_plan_not_the_lane_label(self):
        self._declare()
        facts = self._gather()
        self.assertEqual(len(facts), 1)
        fact = facts[0]
        self.assertEqual(fact.lane_id, LANE)
        self.assertEqual(fact.lane_kind, LANE_KIND_IMPLEMENTATION)
        self.assertEqual(fact.managed_roles, (GATEWAY, WORKER))
        self.assertEqual(fact.reboot.branch, BRANCH)
        self.assertTrue(fact.reboot.worktree_present)
        self.assertTrue(fact.reboot.branch_exists)
        # No delivery was ever recorded for this lane's causal key, so the anchored
        # implementation_request is genuinely owed and bound to the lifecycle anchor.
        self.assertEqual(fact.dispatch.state, DISPATCH_OWED)
        self.assertEqual(fact.dispatch.anchor_journal, ANCHOR)

        plan = rehydrate.plan_fleet(facts)[0]
        self.assertEqual(plan.disposition, REHYDRATE)
        self.assertEqual(plan.actions, (ACTION_HEAL_PAIR, ACTION_RESTORE_DISPATCH))

    def test_a_confirmed_delivery_to_the_LIVE_gateway_removes_the_dispatch_action(self):
        """Suppression is correct only while the receiver that got it is still there."""
        self._declare()
        self._seed_delivery()
        facts = self._gather(
            rows=self._rows(),
            receiver_generations={LANE: self._proven_generation()},
        )
        self.assertEqual(facts[0].dispatch.state, DISPATCH_DELIVERED)
        plan = rehydrate.plan_fleet(facts)[0]
        self.assertNotIn(ACTION_RESTORE_DISPATCH, plan.actions)

    def test_a_confirmed_delivery_to_a_GONE_gateway_does_not_suppress_the_dispatch(self):
        """Review j#108920 finding_generationfence, end to end over the real ledger.

        The pre-restart delivery landed on a process that no longer exists; the fresh pair
        this rail is about to launch has never seen the pointer, so it is still owed.
        """
        self._declare()
        self._seed_delivery()
        facts = self._gather(rows=[])
        self.assertEqual(facts[0].dispatch.state, DISPATCH_OWED)
        plan = rehydrate.plan_fleet(facts)[0]
        self.assertEqual(plan.actions, (ACTION_HEAL_PAIR, ACTION_RESTORE_DISPATCH))

    def test_a_live_locator_without_a_generation_proof_blocks(self):
        """Review j#108953: unprovable is never a licence to resend.

        A live locator whose same-generation proof does not hold (a re-launch under the
        same pane id, an unreadable attestation, a mere revision drift) must block, not
        drop the record and re-promote the key to `owed`.
        """
        self._declare()
        self._seed_delivery()
        facts = self._gather(
            rows=self._rows(),
            receiver_generations={
                LANE: ReceiverGeneration(
                    inventory_readable=True,
                    live_locators=frozenset({GATEWAY_LOCATOR}),
                    matches=lambda record: False,
                )
            },
        )
        self.assertEqual(facts[0].dispatch.state, DISPATCH_ATTRIBUTION_UNKNOWN)
        plan = rehydrate.plan_fleet(facts)[0]
        self.assertEqual(plan.disposition, BLOCKED)
        self.assertEqual(plan.reason, BLOCK_DISPATCH_ATTRIBUTION_UNKNOWN)
        self.assertEqual(plan.actions, ())

    def test_the_real_resolver_yields_no_proof_without_an_attestation(self):
        """The production default is fail-closed: no boundary resolves, so nothing is proven."""
        generation = rehydrate.receiver_generation_for(
            self._rows(),
            home=self.home,
            workspace_id=WORKSPACE,
            lane_id=LANE,
            role=GATEWAY,
        )
        self.assertTrue(generation.inventory_readable)
        self.assertIn(GATEWAY_LOCATOR, generation.live_locators)
        self.assertIsNone(
            generation.matches, "an absent attestation proves no generation"
        )
        self.assertFalse(generation.proves_current(object()))

    def test_the_real_resolver_reports_an_unreadable_inventory(self):
        generation = rehydrate.receiver_generation_for(
            None,
            home=self.home,
            workspace_id=WORKSPACE,
            lane_id=LANE,
            role=GATEWAY,
        )
        self.assertFalse(generation.inventory_readable)

    def test_a_live_pair_needs_neither_heal_nor_dispatch(self):
        self._declare()
        self._seed_delivery()
        facts = self._gather(
            rows=self._rows(),
            receiver_generations={LANE: self._proven_generation()},
        )
        self.assertTrue(facts[0].pair_whole)
        plan = rehydrate.plan_fleet(facts)[0]
        self.assertEqual(plan.disposition, SKIP)
        self.assertEqual(plan.reason, SKIP_IDLE)
        self.assertEqual(plan.actions, ())

    def test_an_unreadable_ledger_blocks_rather_than_reading_as_undelivered(self):
        self._declare()
        ledger = self.home / "herdr-delivery-ledger.sqlite"
        ledger.write_bytes(b"not a sqlite database at all")
        facts = self._gather()
        plan = rehydrate.plan_fleet(facts)[0]
        self.assertEqual(plan.disposition, BLOCKED)
        self.assertEqual(plan.reason, BLOCK_DISPATCH_UNREADABLE)
        self.assertEqual(plan.actions, ())

    def test_a_delegated_lane_without_its_project_fields_blocks(self):
        self._declare(lane_kind=LANE_KIND_DELEGATED_COORDINATOR)
        facts = self._gather()
        plan = rehydrate.plan_fleet(facts)[0]
        self.assertEqual(plan.disposition, BLOCKED)
        self.assertEqual(plan.reason, BLOCK_RESUME_PROFILE_INCOMPLETE)

    def test_a_delegated_lane_with_a_fresh_anchor_and_fields_briefs(self):
        self._declare(lane_kind=LANE_KIND_DELEGATED_COORDINATOR)
        # The IR anchor is delivered; the brief rides a DIFFERENT, fresh anchor, which is
        # exactly what makes its causal key owed after a restart.
        self._seed_delivery()
        facts = self._gather(
            resume_inputs={
                LANE: rehydrate.ResumeBriefInput(
                    anchor_journal="108900",
                    fields=(
                        ("parent_project", "giken-3800-mozyo-bridge"),
                        ("child_project", "giken-3800-mozyo-bridge"),
                        # A lane created BY the default-lane coordinator carries an empty
                        # `parent_lane_id`, so its parent issue is genuinely not derivable
                        # from the child's own row: it is supplied, never guessed.
                        ("parent_issue", "15631"),
                    ),
                )
            }
        )
        fact = facts[0]
        self.assertEqual(fact.resume_brief.anchor_journal, "108900")
        self.assertEqual(fact.resume_brief.state, DISPATCH_OWED)
        self.assertEqual(
            dict(fact.resume_profile_fields)["parent_callback_target"], "coordinator"
        )
        plan = rehydrate.plan_fleet(facts)[0]
        # The lane is live-zero, so the pre-restart IR delivery no longer suppresses the
        # dispatch either — the fresh pair needs both the pointer and its brief.
        self.assertEqual(
            plan.actions,
            (ACTION_HEAL_PAIR, ACTION_RESTORE_DISPATCH, ACTION_RESUME_BRIEF),
        )

    def test_a_live_startup_screen_blocks_the_lane(self):
        """The fence review j#108920 finding_startupinteraction found unreachable."""
        self._declare()
        facts = self._gather(
            rows=self._rows(), startup_screens={LANE: STARTUP_SCREEN_BLOCKED}
        )
        self.assertEqual(facts[0].startup_screen, STARTUP_SCREEN_BLOCKED)
        plan = rehydrate.plan_fleet(facts)[0]
        self.assertEqual(plan.disposition, BLOCKED)
        self.assertEqual(plan.reason, BLOCK_STARTUP_INTERACTION)
        self.assertEqual(plan.actions, ())

    def test_a_delegated_lane_missing_only_the_parent_issue_still_blocks(self):
        """Every one of the four fields is load-bearing; three out of four is not a brief."""
        self._declare(lane_kind=LANE_KIND_DELEGATED_COORDINATOR)
        facts = self._gather(
            resume_inputs={
                LANE: rehydrate.ResumeBriefInput(
                    anchor_journal="108900",
                    fields=(("parent_project", "p"), ("child_project", "c")),
                )
            }
        )
        plan = rehydrate.plan_fleet(facts)[0]
        self.assertEqual(plan.disposition, BLOCKED)
        self.assertEqual(plan.reason, BLOCK_RESUME_PROFILE_INCOMPLETE)
        self.assertIn("parent_issue", plan.detail)

    def test_the_read_only_plan_mutates_nothing(self):
        """The effect budget of the plan stage, measured on the real home."""
        self._declare()
        self._seed_delivery()
        before = _home_digest(self.home)
        facts = self._gather()
        plans = rehydrate.plan_fleet(facts)
        payload = rehydrate.rehydrate_payload(facts, plans, execute=False)
        text = rehydrate.format_rehydrate_text(facts, plans, execute=False)
        self.assertEqual(payload["state"], "plan")
        self.assertIn("read-only", text)
        self.assertEqual(
            _home_digest(self.home),
            before,
            "producing a plan must not write a byte of managed state",
        )
        # And the lifecycle authority is still exactly the row we declared.
        row = LaneLifecycleReader(home=self.home).get(LaneLifecycleKey(WORKSPACE, LANE))
        self.assertIsNotNone(row)
        self.assertEqual(row.revision, 1)
        self.assertEqual(row.lane_generation, 1)

    def test_the_pasteable_payload_carries_no_host_local_worktree_path(self):
        self._declare()
        facts = self._gather()
        plans = rehydrate.plan_fleet(facts)
        payload = rehydrate.rehydrate_payload(facts, plans, execute=False)
        import json

        rendered = json.dumps(payload) + rehydrate.format_rehydrate_text(
            facts, plans, execute=False
        )
        self.assertNotIn(str(self.worktree), rendered)
        self.assertNotIn("recorded_worktree", rendered)

    def test_an_unresolvable_workspace_is_unavailable_not_empty(self):
        self._declare()
        with mock.patch.object(
            projection, "repo_scope_workspace_id", return_value=""
        ):
            with self.assertRaises(rehydrate.FleetRehydrateUnavailable) as caught:
                rehydrate.gather_fleet_facts(self.repo, home=self.home, rows=[])
        self.assertIn("workspace identity", str(caught.exception))

    def test_an_unreadable_lifecycle_store_is_unavailable_not_empty(self):
        with mock.patch.object(
            projection, "repo_scope_workspace_id", return_value=WORKSPACE
        ), mock.patch(
            "mozyo_bridge.core.state.lane_lifecycle_readonly.load_lane_lifecycle_readonly",
            return_value=None,
        ):
            with self.assertRaises(rehydrate.FleetRehydrateUnavailable) as caught:
                rehydrate.gather_fleet_facts(self.repo, home=self.home, rows=[])
        self.assertIn("NOT the same as the store having no rows", str(caught.exception))


class StartupScreenObservationTests(unittest.TestCase):
    """The live half of the fence review j#108920 ``finding_startupinteraction`` found stubbed.

    Wires the REAL #13760 evaluator and the REAL slot scoping against an injected pane read,
    so what is under test is the fold this rail performs — not the provider profiles, which
    own the screen strings and are exercised by #13760's own suite.
    """

    def _rows(self, *, lane=LANE, agent_status="awaiting_input", residue=False):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            encode_assigned_name,
        )

        row = {
            "name": encode_assigned_name(WORKSPACE, GATEWAY, lane),
            "pane_id": GATEWAY_LOCATOR,
            "agent_status": agent_status,
            "revision": GATEWAY_REVISION,
        }
        if not residue:
            row["agent"] = GATEWAY
        else:
            # #13518 shell residue: the name survives, the provider process does not.
            row["agent"] = ""
        return [row]

    def _observe(self, rows, reader):
        return rehydrate.observe_startup_screen(
            rows,
            workspace_id=WORKSPACE,
            lane_id=LANE,
            managed_roles=(GATEWAY, WORKER),
            read_visible=reader,
        )

    def test_no_live_slot_is_not_probed_and_reads_nothing(self):
        reads = []
        self.assertEqual(
            self._observe([], lambda locator: reads.append(locator)),
            STARTUP_SCREEN_NOT_PROBED,
        )
        self.assertEqual(reads, [], "a lane with no process is never read")

    def test_an_unreadable_inventory_is_not_probed(self):
        self.assertEqual(
            self._observe(None, lambda locator: "composer"), STARTUP_SCREEN_NOT_PROBED
        )

    def test_a_clear_composer_is_clear(self):
        self.assertEqual(
            self._observe(self._rows(), lambda locator: "> type your request"),
            "clear",
        )

    def test_a_declared_startup_screen_blocks(self):
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (  # noqa: E501
            AGENT_PROVIDER_PROFILES,
        )

        profile = AGENT_PROVIDER_PROFILES.get(GATEWAY)
        self.assertIsNotNone(profile, "the gateway provider must be profiled")
        blockers = getattr(profile, "startup_blockers", ())
        self.assertTrue(blockers, "the provider declares at least one startup screen")
        # Render a screen from the provider's OWN declared blocker, so this test asserts
        # the fold rather than re-encoding a provider string of its own. A blocker matches
        # when every one of its `all_of` phrases is present.
        screen = "\n".join(blockers[0].all_of)
        self.assertTrue(screen.strip(), "a declared blocker carries matchable phrases")
        self.assertEqual(
            self._observe(self._rows(), lambda locator: f"header\n{screen}\nfooter"),
            STARTUP_SCREEN_BLOCKED,
        )

    def test_an_unreadable_pane_is_unverified_not_clear(self):
        def boom(locator):
            raise RuntimeError("herdr read failed")

        self.assertEqual(self._observe(self._rows(), boom), "unreadable")

    def test_a_blank_read_is_unreadable_not_clear(self):
        self.assertEqual(self._observe(self._rows(), lambda locator: "   "), "unreadable")

    def test_shell_residue_is_never_read_as_a_provider_screen(self):
        reads = []
        self.assertEqual(
            self._observe(
                self._rows(residue=True, agent_status="unknown"),
                lambda locator: reads.append(locator) or "x",
            ),
            STARTUP_SCREEN_NOT_PROBED,
        )
        self.assertEqual(reads, [], "a pane with no provider process carries no screen")


class LiveOpsProductionWiringTests(unittest.TestCase):
    """The REAL adapter, through the REAL imports (Redmine #15745 review j#109007).

    Every other suite for this rail substitutes a fake ops port, so the production
    ``LiveFleetRehydrateOps`` methods were never executed — and an action-time fence that
    raised ``ImportError`` on its first line shipped with 117 green tests. These exercise the
    adapter itself against a temp home, and add a drift guard over the whole defect class:
    a name that only a method body imports must still resolve.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def _ops(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_fleet_rehydrate_ops import (  # noqa: E501
            LiveFleetRehydrateOps,
        )

        return LiveFleetRehydrateOps(repo_root=Path.cwd(), home=self.home)

    def _facts(self, *, journal=ANCHOR, roles=(GATEWAY, WORKER)):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.fleet_rehydrate import (  # noqa: E501
            FleetLaneFacts,
            LaneDispatchFact,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reboot_residue_convergence import (  # noqa: E501
            RebootLaneFacts,
        )

        marker = redmine_marker(ISSUE, journal, KIND_IMPLEMENTATION_REQUEST, GATEWAY)
        return FleetLaneFacts(
            reboot=RebootLaneFacts(
                workspace_id=WORKSPACE, lane_id=LANE, issue_id=ISSUE
            ),
            managed_roles=tuple(roles),
            dispatch=LaneDispatchFact(
                state=DISPATCH_OWED,
                anchor_issue=ISSUE,
                anchor_journal=journal,
                marker=marker,
            ),
        )

    def _state(self, facts, *, rows):
        """Drive the real method with only the live-herdr edge faked."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_herdr_projection as live_projection,
        )

        with mock.patch.object(
            live_projection, "list_herdr_agent_rows", return_value=rows
        ):
            return self._ops().current_dispatch_state(
                facts, kind=KIND_IMPLEMENTATION_REQUEST
            )

    # -- the finding itself ------------------------------------------------

    def test_the_action_time_fence_runs_and_returns_a_typed_state(self):
        """j#109007: this raised ImportError before the send on every execute run."""
        state = self._state(self._facts(), rows=[])
        self.assertIn(
            state,
            {
                DISPATCH_OWED,
                DISPATCH_DELIVERED,
                DISPATCH_ATTRIBUTION_UNKNOWN,
            },
            "the fence must return a closed token, not raise",
        )
        self.assertEqual(state, DISPATCH_OWED, "nothing recorded for this key")

    def test_an_unreadable_inventory_is_attribution_unknown_not_owed(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            HerdrSessionStartError,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_herdr_projection as live_projection,
        )

        with mock.patch.object(
            live_projection,
            "list_herdr_agent_rows",
            side_effect=HerdrSessionStartError("no herdr"),
        ):
            state = self._ops().current_dispatch_state(
                self._facts(), kind=KIND_IMPLEMENTATION_REQUEST
            )
        self.assertEqual(state, DISPATCH_ATTRIBUTION_UNKNOWN)

    def test_an_absent_anchor_is_not_applicable(self):
        facts = self._facts(journal="")
        self.assertEqual(self._state(facts, rows=[]), DISPATCH_NOT_APPLICABLE)

    def test_an_unreadable_ledger_is_unreadable_not_owed(self):
        (self.home / "herdr-delivery-ledger.sqlite").write_bytes(b"not a database")
        self.assertEqual(
            self._state(self._facts(), rows=[]), DISPATCH_UNREADABLE
        )

    def test_a_confirmed_delivery_to_a_dead_receiver_is_still_owed(self):
        """The R3 attribution, exercised through the production fence rather than a fake."""
        self._seed_delivery_for_wiring()
        self.assertEqual(self._state(self._facts(), rows=[]), DISPATCH_OWED)

    def _seed_delivery_for_wiring(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            encode_assigned_name,
        )

        marker = redmine_marker(ISSUE, ANCHOR, KIND_IMPLEMENTATION_REQUEST, GATEWAY)
        HerdrDeliveryLedger(home=self.home).append(
            HerdrDeliveryLedgerRecord(
                notification_marker=marker,
                receiver=GATEWAY,
                source="redmine",
                issue_id=ISSUE,
                journal_id=ANCHOR,
                status="sent",
                reason="ok",
                rail=RAIL_EVENT,
                target=GATEWAY_LOCATOR,
                queue_enter_observation={
                    "gateway_binding": {
                        "assigned_name": encode_assigned_name(WORKSPACE, GATEWAY, LANE),
                        "row_revision": GATEWAY_REVISION,
                    }
                },
            )
        )

    def test_current_identity_reads_the_injected_home(self):
        """The other real adapter method, so it is not the next one to drift unnoticed."""
        self.assertIsNone(
            self._ops().current_identity(workspace_id=WORKSPACE, lane_id=LANE),
            "an empty home has no row for this lane",
        )

    # -- the defect CLASS --------------------------------------------------

    def test_every_lazy_import_in_the_rail_resolves(self):
        """A name only a method body imports must still exist (the j#109007 class).

        Module-level imports are checked when the module loads; names imported inside a
        function are not, so a rename can leave a production call site broken while every
        module still imports cleanly and every fake-backed test stays green. This walks the
        rail's own modules and resolves each imported name.
        """
        import ast
        import importlib

        modules = (
            "domain/fleet_rehydrate.py",
            "domain/fleet_rehydrate_dispatch_fold.py",
            "application/sublane_fleet_rehydrate.py",
            "application/sublane_fleet_rehydrate_ops.py",
        )
        root = (
            ROOT
            / "src/mozyo_bridge/e_110_execution_platform"
            / "f_140_delegated_coordinator_nested_handoff"
        )
        unresolved = []
        for relative in modules:
            tree = ast.parse((root / relative).read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.level:
                    continue
                imported = importlib.import_module(node.module)
                for alias in node.names:
                    if not hasattr(imported, alias.name):
                        unresolved.append(
                            f"{relative}:{node.lineno} {node.module}.{alias.name}"
                        )
        self.assertEqual(unresolved, [], "a lazy import names something that is gone")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
