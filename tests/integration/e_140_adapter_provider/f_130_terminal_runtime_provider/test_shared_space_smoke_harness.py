"""Integration: the high-level isolated shared-space smoke harness (Redmine #14187).

End-to-end coverage that wires multiple real collaborators — a temp filesystem, the
workspace registry / SQLite fences, the production ``prepare_session``, and the shared
``FakeHerdr`` — so it lives under ``tests/integration/`` per the test-placement policy
(``vibes/docs/logics/tests-placement-discovery-policy.md``; review j#83870 F4). The
pure-helper unit tests stay in ``tests/unit/...``.

Proves the #14185 blocker is resolved AND the R1 review j#83870 findings are closed:
- F1: a split-brain (ambient home ≠ isolated home) fails closed with zero operator-home write;
- F2: a crashed concurrent worker does not vanish and cannot yield a false ``converged``;
- F3: an unreadable inventory during residue verification fails closed (never residue-0).
"""

from __future__ import annotations

import os
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from support.herdr_fake import FakeHerdr, attest_capability_epilog  # noqa: E402

from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    IdentityAttestationRecord,
    VERDICT_PRESENT,
    record_identity_attestation,
)
from mozyo_bridge.core.state.startup_execution_events import (  # noqa: E402
    STAGE_ATTESTATION_WRITE_SUCCEEDED,
    STAGE_PROVIDER_EXEC_CALL_REACHED,
    STAGE_WRAPPER_ENTERED,
    append_execution_event,
)
from mozyo_bridge.core.state.startup_transaction_fence import (  # noqa: E402
    StartupTransactionFence,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E402,E501
    ATTEST_CAPABILITY_MARKER,
)

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.shared_space_smoke_harness import (  # noqa: E402,E501
    PHASE_SESSION_START,
    PHASE_WORKER_ERROR,
    IsolationCapability,
    ProjectSmokeObservation,
    SharedSpaceSmokeError,
    SharedSpaceSmokeHarness,
    SharedSpaceSmokeObservation,
    SmokeIsolationError,
    _ProjectSpec,
    _count_duplicate_agents,
    isolated_smoke_home,
    smoke_shared_space_preflight,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (  # noqa: E402,E501
    SLOT_LAUNCHED,
    SessionStartResult,
    SlotResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_reflow import (  # noqa: E402,E501
    COLUMN_FAILED,
    COLUMN_NOT_APPLICABLE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pair_split_ratio import (  # noqa: E402,E501
    RATIO_MATCHED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_health import (  # noqa: E402,E501
    HEALTH_HEALTHY,
    HEALTH_NOT_PROBED,
)


def _make_env(bindir: Path) -> "dict[str, str]":
    """A trusted launch env: a fake herdr binary + a PATH of provider stubs.

    Mirrors the session-start test env (a fake executable satisfies
    ``resolve_herdr_binary`` while the injected fake ``runner`` never executes it; the
    provider stubs satisfy the launch preflight). The caller supplies this env — the
    harness never resolves a binary itself. No secret-shaped literal.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    for name in ("herdr", "claude", "codex", "mozyo-bridge"):
        path = bindir / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return {"MOZYO_HERDR_BINARY": str(bindir / "herdr"), "PATH": str(bindir)}


def _launcher_run(argv, **_kwargs):
    """Faithful positive response for the managed-launch capability probe."""
    if list(argv[1:4]) == ["herdr", "agent-attest", "--help"]:
        help_text = (
            "usage: mozyo-bridge herdr agent-attest "
            f"[{ATTEST_CAPABILITY_MARKER} ASSIGNED_NAME]\n\n"
            + attest_capability_epilog()
            + "\n"
        )
        return subprocess.CompletedProcess(list(argv), 0, stdout=help_text, stderr="")
    raise AssertionError(f"unexpected launcher subprocess: {list(argv)!r}")


class _HarnessFixture:
    """Set up an isolated smoke home + fake + env for the integration tests."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.operator = tmp / "real"
        self.operator.mkdir()
        self.isolated = tmp / "smoke"
        self.env = _make_env(tmp / "bin")
        self.fake = FakeHerdr(read_text="$ ready\n> ")
        self.tmp = tmp
        # Control the operator home the LEGITIMATE way (review j#83935 F1): set the
        # ambient MOZYO_BRIDGE_HOME to the temp fixture, so `isolated_smoke_home`
        # captures it as the operator home from the source of truth (no caller arg).
        self._env_patch = patch.dict(
            os.environ, {"MOZYO_BRIDGE_HOME": str(self.operator)}, clear=False
        )
        self._env_patch.start()
        self._ctx = isolated_smoke_home(self.isolated)
        self.capability = self._ctx.__enter__()
        self.home = self.capability.isolated_home
        # This suite exercises the smoke harness, shared-space creation/adoption,
        # startup evidence and cleanup.  The nested pane-tree geometry collaborator
        # has its own full regression suite; isolate it here so this fake need not
        # fabricate pane-move/layout semantics it does not model.
        self._column_patch = patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_project_column_reflow.reflow_project_columns",
            return_value=(
                COLUMN_NOT_APPLICABLE,
                "geometry collaborator isolated by this harness test",
            ),
        )
        self._column_patch.start()
        self._ratio_patch = patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_pair_split_ratio._pair_geometry",
            return_value=(RATIO_MATCHED, "ratio collaborator accepted"),
        )
        self._ratio_patch.start()
        return self

    def __exit__(self, *exc):
        self._ratio_patch.stop()
        self._column_patch.stop()
        self._ctx.__exit__(*exc)
        self._env_patch.stop()
        self._tmp.cleanup()
        return False

    def specs(self, n: int) -> "list[_ProjectSpec]":
        out = []
        for i in range(n):
            repo = self.tmp / f"proj{i}"
            repo.mkdir()
            out.append(_ProjectSpec(f"p{i}", repo))
        return out

    def harness(self) -> SharedSpaceSmokeHarness:
        return SharedSpaceSmokeHarness(
            capability=self.capability,
            runner=self._herdr_run,
            launcher_runner=_launcher_run,
            env=self.env,
        )

    def _herdr_run(self, argv, *args, **kwargs):
        """Drive FakeHerdr and model the managed wrapper's durable after-effects."""
        result = self.fake.run(argv, *args, **kwargs)
        rest = list(argv[1:])
        if result.returncode == 0 and rest[:2] == ["agent", "list"]:
            payload = json.loads(result.stdout)
            for row in payload.get("agents", []):
                pane = row.get("pane_id", "")
                agent = self.fake._agents.get(pane)
                if agent is not None and agent.cwd:
                    row["cwd"] = agent.cwd
            result = subprocess.CompletedProcess(
                list(argv), 0, stdout=json.dumps(payload), stderr=""
            )
        if (
            result.returncode == 0
            and rest[:2] == ["agent", "start"]
            and rest != ["agent", "start", "--help"]
        ):
            pane = rest[rest.index("--pane") + 1]
            agent = self.fake._agents[pane]
            if "agent-attest" in agent.launch_argv:
                # A managed provider that reached its wrapper is visible as that
                # provider in the live Herdr inventory (not as an anonymous shell).
                agent.detected_agent = agent.provider
                name = agent.logical_name
                record_identity_attestation(
                    IdentityAttestationRecord(
                        assigned_name=name,
                        workspace_id=agent.env.get("MOZYO_WORKSPACE_ID", ""),
                        role=agent.provider,
                        lane_id=agent.env.get("MOZYO_LANE_ID", ""),
                        locator=pane,
                        terminal_id=agent.terminal_id,
                        verdict=VERDICT_PRESENT,
                        observed_at="2026-08-08T00:00:00+00:00",
                    ),
                    home=self.home,
                )
                action_id = agent.env.get("MOZYO_STARTUP_ACTION_ID", "")
                fence = StartupTransactionFence(home=self.home)
                for stage in (
                    STAGE_WRAPPER_ENTERED,
                    STAGE_ATTESTATION_WRITE_SUCCEEDED,
                    STAGE_PROVIDER_EXEC_CALL_REACHED,
                ):
                    append_execution_event(
                        fence, action_id, stage, participant=name
                    )
        return result


class SharedSpaceSmokeIntegrationTests(unittest.TestCase):
    """The end-to-end shared-space smoke through the fake (Acceptance 2/3/4/5)."""

    def test_single_project_creates_labelled_coordinators_space(self) -> None:
        with _HarnessFixture() as fx:
            harness = fx.harness()
            [obs] = harness.run_concurrent(fx.specs(1))
            self.assertEqual(obs.outcome, "created")
            self.assertTrue(obs.coordinators_workspace_id)
            self.assertEqual(sorted(obs.launched_roles), ["claude", "codex"])
            self.assertEqual(harness.recorder.coordinators_create_count, 1)

    def test_two_projects_concurrent_converge_to_one_space(self) -> None:
        # Acceptance 3: 2-process concurrent start -> exactly one `coordinators`
        # workspace created/adopted, zero duplicate agents. Repeated so the race is
        # exercised in both winner orders.
        for _ in range(6):
            with _HarnessFixture() as fx:
                harness = fx.harness()
                observations = harness.run_concurrent(fx.specs(2))
                self.assertEqual(len(observations), 2, "both projects must complete")
                created = [o for o in observations if o.outcome == "created"]
                adopted = [o for o in observations if o.outcome == "adopted"]
                self.assertEqual(len(created), 1, "exactly one project creates the space")
                self.assertEqual(len(adopted), 1, "the other adopts it")
                self.assertEqual(
                    {o.coordinators_workspace_id for o in observations},
                    {created[0].coordinators_workspace_id},
                )
                self.assertEqual(harness.recorder.coordinators_create_count, 1)
                self.assertEqual(_count_duplicate_agents(observations), 0)

    def test_generic_smoke_has_zero_destructive_cleanup_and_is_nonclear(self) -> None:
        # A generic/non-disposable harness has no server-minter capability. Its pane
        # locators are generation-unbound, so teardown sends no close for them.
        with _HarnessFixture() as fx:
            harness = fx.harness()
            summary = harness.smoke(fx.specs(2))
            self.assertIsInstance(summary, SharedSpaceSmokeObservation)
            self.assertEqual(summary.coordinators_create_count, 1)
            self.assertEqual(summary.duplicate_agents, 0)
            self.assertTrue(summary.all_projects_completed)
            self.assertTrue(summary.converged)
            self.assertTrue(summary.residue_verified)
            self.assertGreater(summary.residue_workspaces, 0)
            self.assertGreater(summary.residue_agents, 0)
            self.assertFalse(summary.cleanup_attempted)
            self.assertFalse(summary.cleanup_completed)
            self.assertFalse(summary.residue_clear)
            self.assertTrue(fx.fake.workspace_ids)
            self.assertFalse(
                any(call[:2] == ["workspace", "close"] for call in fx.fake.calls)
            )
            for locator in harness.recorder.launched_locators:
                self.assertNotIn(["pane", "close", locator], fx.fake.calls)

    def test_smoke_observes_lock_engaged_and_released(self) -> None:
        with _HarnessFixture() as fx:
            summary = fx.harness().smoke(fx.specs(2))
            self.assertTrue(summary.lock_engaged)
            self.assertTrue(summary.lock_released_clean)

    def test_evidence_summary_is_redaction_safe(self) -> None:
        # Acceptance 4/6: counts + closed phase tokens only — no home path, no env value.
        with _HarnessFixture() as fx:
            summary = fx.harness().smoke(fx.specs(2))
            evidence = summary.as_evidence()
            blob = repr(evidence)
            self.assertNotIn(str(fx.home), blob)
            self.assertNotIn(str(fx.tmp), blob)
            self.assertNotIn("MOZYO_HERDR_BINARY", blob)
            self.assertEqual(evidence["coordinators_create_count"], 1)
            self.assertEqual(evidence["requested_projects"], 2)
            self.assertEqual(evidence["completed_projects"], 2)
            self.assertTrue(evidence["converged"])
            self.assertFalse(evidence["cleanup_attempted"])
            self.assertFalse(evidence["cleanup_completed"])
            self.assertFalse(evidence["residue_clear"])

    def test_second_sequential_project_adopts_existing_space(self) -> None:
        with _HarnessFixture() as fx:
            harness = fx.harness()
            specs = fx.specs(2)
            first = harness.run_project(specs[0])
            second = harness.run_project(specs[1])
            self.assertEqual(first.outcome, "created")
            self.assertEqual(second.outcome, "adopted")
            self.assertEqual(
                first.coordinators_workspace_id, second.coordinators_workspace_id
            )
            self.assertEqual(harness.recorder.coordinators_create_count, 1)

    def _assert_returned_session_failure_is_not_green(
        self, result: SessionStartResult
    ) -> None:
        """A returned (non-exception) session failure stays a smoke failure."""
        self.assertFalse(result.ok, "the fixture must exercise the returned-failure path")
        with _HarnessFixture() as fx:
            harness = fx.harness()
            [spec] = fx.specs(1)
            with patch(
                "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
                "application.shared_space_smoke_harness._session.prepare_session",
                return_value=result,
            ):
                observation = harness.run_project(spec)

            self.assertEqual(observation.outcome, "failed")
            self.assertEqual(observation.failure_phase, PHASE_SESSION_START)
            self.assertEqual(
                observation.coordinators_workspace_id, result.herdr_workspace_id
            )
            summary = SharedSpaceSmokeObservation(
                projects=(observation,),
                requested_projects=1,
                coordinators_create_count=1,
            )
            self.assertFalse(summary.all_projects_completed)
            self.assertFalse(summary.converged)
            self.assertFalse(summary.residue_clear)
            evidence = summary.as_evidence()
            self.assertEqual(evidence["projects"][0]["outcome"], "failed")
            self.assertEqual(
                evidence["projects"][0]["failure_phase"], PHASE_SESSION_START
            )
            blob = repr(evidence)
            self.assertNotIn(result.herdr_workspace_id, blob)
            for slot in result.slots:
                self.assertIn(slot.provider, observation.launched_roles)
                self.assertIn(slot.assigned_name, observation.launched_names)
                if slot.locator:
                    self.assertIn(slot.locator, observation.launched_locators)
                    self.assertNotIn(slot.locator, blob)
                self.assertNotIn(slot.assigned_name, blob)

    def test_returned_failure_keeps_actual_create_count_without_green(self) -> None:
        """A failed session may have actuated; the tape, not success, counts it."""
        with _HarnessFixture() as fx:
            env = dict(fx.env)
            env["MOZYO_BRIDGE_LAUNCHER"] = str(fx.tmp / "missing-launcher")
            harness = SharedSpaceSmokeHarness(
                capability=fx.capability,
                runner=fx._herdr_run,
                launcher_runner=_launcher_run,
                env=env,
            )
            summary = harness.smoke(fx.specs(1))

            self.assertEqual(summary.projects[0].outcome, "failed")
            self.assertEqual(summary.projects[0].failure_phase, PHASE_SESSION_START)
            self.assertEqual(summary.coordinators_create_count, 1)
            self.assertFalse(summary.all_projects_completed)
            self.assertFalse(summary.converged)
            self.assertFalse(summary.residue_clear)
            evidence = summary.as_evidence()
            self.assertEqual(evidence["coordinators_create_count"], 1)
            self.assertNotIn("w1:p", repr(evidence))

    def test_returned_startup_evidence_failure_is_not_green(self) -> None:
        self._assert_returned_session_failure_is_not_green(
            SessionStartResult(
                workspace_id="project-ws",
                lane_id="default",
                herdr_workspace_id="shared-ws",
                base_pane_id="root-pane",
                slots=[
                    SlotResult(
                        provider=provider,
                        assigned_name=f"agent-{provider}",
                        outcome=SLOT_LAUNCHED,
                        locator=f"shared-ws:pane-{provider}",
                        health=HEALTH_NOT_PROBED,
                    )
                    for provider in ("claude", "codex")
                ],
            )
        )

    def test_returned_column_failure_is_not_green(self) -> None:
        self._assert_returned_session_failure_is_not_green(
            SessionStartResult(
                workspace_id="project-ws",
                lane_id="default",
                herdr_workspace_id="shared-ws",
                base_pane_id="root-pane",
                slots=[
                    SlotResult(
                        provider=provider,
                        assigned_name=f"agent-{provider}",
                        outcome=SLOT_LAUNCHED,
                        locator=f"shared-ws:pane-{provider}",
                        health=HEALTH_HEALTHY,
                    )
                    for provider in ("claude", "codex")
                ],
                column_outcome=COLUMN_FAILED,
                column_detail="private runtime detail must not enter evidence",
            )
        )

    def test_returned_partial_launch_is_not_green(self) -> None:
        self._assert_returned_session_failure_is_not_green(
            SessionStartResult(
                workspace_id="project-ws",
                lane_id="default",
                herdr_workspace_id="shared-ws",
                base_pane_id="root-pane",
                slots=[
                    SlotResult(
                        provider="claude",
                        assigned_name="agent-claude",
                        outcome=SLOT_LAUNCHED,
                        locator="shared-ws:pane-claude",
                        health=HEALTH_HEALTHY,
                    ),
                    SlotResult(
                        provider="codex",
                        assigned_name="agent-codex",
                        outcome=SLOT_LAUNCHED,
                        locator="shared-ws:pane-codex",
                        health=HEALTH_NOT_PROBED,
                    ),
                ],
            )
        )

    def test_preexisting_coordinators_space_fails_closed_before_create(self) -> None:
        # Acceptance 5 (herdr dimension): a workspace already labelled `coordinators`
        # makes the smoke refuse BEFORE any create.
        with _HarnessFixture() as fx:
            fx.fake.run(["herdr", "workspace", "create", "--label", "coordinators"])
            harness = fx.harness()
            with self.assertRaises(SharedSpaceSmokeError):
                harness.smoke(fx.specs(2))
            self.assertEqual(harness.recorder.coordinators_create_count, 0)


class IsolationBindingTests(unittest.TestCase):
    """F1: the operator home is the ambient source of truth, never a caller arg (j#83935)."""

    def test_harness_requires_a_capability(self) -> None:
        # The harness cannot be constructed without an IsolationCapability, so the
        # "never used the context manager" misuse cannot even build a harness.
        with tempfile.TemporaryDirectory() as tmp:
            env = _make_env(Path(tmp) / "bin")
            with self.assertRaises(SmokeIsolationError):
                SharedSpaceSmokeHarness(
                    capability=Path(tmp) / "isolated",  # type: ignore[arg-type]
                    runner=FakeHerdr().run, env=env,
                )

    def test_hand_built_capability_is_refused(self) -> None:
        # A capability can only be minted by isolated_smoke_home (token-guarded).
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SmokeIsolationError):
                IsolationCapability(Path(tmp) / "a", Path(tmp) / "b")

    def test_capability_is_immutable(self) -> None:
        # review j#83935 F1: the "unforgeable" claim is backed by immutability — a
        # frozen dataclass whose fields cannot be reassigned.
        with tempfile.TemporaryDirectory() as tmp:
            operator = Path(tmp) / "operator"
            operator.mkdir()
            isolated = Path(tmp) / "isolated"
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(operator)}, clear=False):
                with isolated_smoke_home(isolated) as capability:
                    with self.assertRaises(Exception):
                        capability.isolated_home = operator  # type: ignore[misc]

    def test_ab_override_hole_is_closed(self) -> None:
        # review j#83935 F1 counterexample: the ambient MOZYO_BRIDGE_HOME is the REAL
        # operator home A; a caller tries to isolate INTO A while naming a fake distinct
        # B as the operator. There is no `operator_home` param any more, so the operator
        # home is ALWAYS the ambient A — isolating into A fails closed (A == A), with no
        # write to A. (The A/B override that R3 left open cannot even be expressed.)
        import inspect
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.shared_space_smoke_harness import (  # noqa: E501
            isolated_smoke_home as _mint,
        )

        self.assertNotIn(
            "operator_home", inspect.signature(_mint).parameters,
            "isolated_smoke_home must not expose a caller-supplied operator_home",
        )
        with tempfile.TemporaryDirectory() as tmp:
            operator_A = Path(tmp) / "operator_A"
            operator_A.mkdir()
            fake_B = Path(tmp) / "fake_B"  # a caller can no longer pass this anywhere
            fake_B.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(operator_A)}, clear=False):
                # Isolating INTO the ambient operator home A is refused (A == captured A).
                with self.assertRaises(SmokeIsolationError):
                    with isolated_smoke_home(operator_A):
                        self.fail("must not mint a capability isolating into the ambient home")
            self.assertEqual(
                list(operator_A.rglob("*")), [], "the real operator home must be untouched"
            )

    def test_ambient_drift_fails_closed_with_zero_write(self) -> None:
        # A validly-minted capability, but the ambient MOZYO_BRIDGE_HOME no longer
        # resolves to its isolated home (the split-brain). The actuation-time guard
        # refuses before any write, so the operator home gains no artifact.
        with tempfile.TemporaryDirectory() as tmp:
            operator = Path(tmp) / "operator"
            operator.mkdir()
            isolated = Path(tmp) / "isolated"
            env = _make_env(Path(tmp) / "bin")
            repo = Path(tmp) / "proj"
            repo.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(operator)}, clear=False):
                with isolated_smoke_home(isolated) as capability:
                    pass  # mint a real capability, then let the context restore ambient
                harness = SharedSpaceSmokeHarness(
                    capability=capability, runner=FakeHerdr().run, env=env
                )
                with self.assertRaises(SmokeIsolationError):
                    harness.run_project(_ProjectSpec("p0", repo))
                with self.assertRaises(SmokeIsolationError):
                    harness.preflight_clean_slate()
            self.assertEqual(
                list(operator.rglob("*")), [], "operator home was written"
            )


class ConcurrentFailureTests(unittest.TestCase):
    """F2: a crashed worker cannot vanish or yield a false ``converged``."""

    def test_worker_crash_is_recorded_and_blocks_converged(self) -> None:
        with _HarnessFixture() as fx:
            harness = fx.harness()
            specs = fx.specs(2)
            original = harness.run_project

            def _flaky(spec: _ProjectSpec) -> ProjectSmokeObservation:
                if spec.project_key == "p1":
                    raise RuntimeError("injected unclassified worker crash")
                return original(spec)

            harness.run_project = _flaky  # type: ignore[assignment]
            observations = harness.run_concurrent(specs)
            # The crashed project did NOT vanish — both indices are present.
            self.assertEqual(len(observations), 2)
            failed = [o for o in observations if o.outcome == "failed"]
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0].project_key, "p1")
            self.assertEqual(failed[0].failure_phase, PHASE_WORKER_ERROR)
            # An aggregate over these must not claim convergence.
            summary = SharedSpaceSmokeObservation(
                projects=tuple(observations),
                requested_projects=2,
                coordinators_create_count=sum(
                    1 for o in observations if o.created_coordinators_space
                ),
                duplicate_agents=_count_duplicate_agents(observations),
            )
            self.assertFalse(summary.all_projects_completed)
            self.assertFalse(summary.converged)

    def test_post_actuation_crash_without_disposable_capability_is_not_cleaned(self) -> None:
        # The receipt tape preserves evidence, but a generic harness cannot turn
        # generation-unbound pane locators into destructive authority.
        for n in (1, 2):
            with _HarnessFixture() as fx:
                harness = fx.harness()
                specs = fx.specs(n)
                original = harness.run_project

                def _crash_after(spec, _orig=original):
                    obs = _orig(spec)  # actuates: launches this project's agents
                    if spec.project_key == f"p{n - 1}":  # the last project crashes
                        raise RuntimeError("crash AFTER actuation succeeded")
                    return obs

                harness.run_project = _crash_after  # type: ignore[assignment]
                summary = harness.smoke(specs)
                # Honest: a failed project -> neither converged nor residue_clear.
                self.assertFalse(summary.all_projects_completed)
                self.assertFalse(summary.residue_clear)
                self.assertTrue(fx.fake.workspace_ids)
                self.assertGreater(summary.residue_workspaces, 0)
                self.assertGreater(summary.residue_agents, 0)
                self.assertFalse(summary.cleanup_attempted)
                for locator in harness.recorder.launched_locators:
                    self.assertNotIn(["pane", "close", locator], fx.fake.calls)


class ResidueVerificationTests(unittest.TestCase):
    """F3: an unreadable inventory during residue verification fails closed."""

    def test_unreadable_workspace_list_fails_residue_verification(self) -> None:
        with _HarnessFixture() as fx:
            harness = fx.harness()
            created = ProjectSmokeObservation(
                project_key="p0", workspace_id="wa", outcome="created",
                coordinators_workspace_id="w1", launched_names=("mzb1_wa_claude_default",),
            )

            def _unreadable(argv, *a, **k):
                if list(argv[1:3]) == ["workspace", "list"]:
                    return subprocess.CompletedProcess(list(argv), 0, stdout="nope", stderr="")
                return fx.fake.run(argv, *a, **k)

            harness.recorder._inner = _unreadable
            with self.assertRaises(SharedSpaceSmokeError):
                harness.verify_residue([created])

    def test_smoke_records_residue_unverified_on_unreadable_inventory(self) -> None:
        # Force `workspace list` unreadable only for the post-cleanup residue read.
        with _HarnessFixture() as fx:
            harness = fx.harness()
            state = {"cleanup_started": False}
            inner = fx.fake.run

            def _rig(argv, *a, **k):
                head = list(argv[1:3])
                if head == ["workspace", "list"] and state["cleanup_started"]:
                    return subprocess.CompletedProcess(list(argv), 0, stdout="nope", stderr="")
                return inner(argv, *a, **k)

            harness.recorder._inner = _rig
            original_cleanup = harness.cleanup

            def _mark_cleanup(*args, **kwargs):
                state["cleanup_started"] = True
                return original_cleanup(*args, **kwargs)

            harness.cleanup = _mark_cleanup  # type: ignore[assignment]
            summary = harness.smoke(fx.specs(1))
            self.assertFalse(summary.residue_verified)
            self.assertFalse(summary.residue_clear)


class PreflightSurfaceTests(unittest.TestCase):
    """The read-only `smoke_shared_space_preflight` surface (the CLI's core)."""

    def test_clean_slate_reports_ready_without_actuation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            operator = Path(tmp) / "real"
            operator.mkdir()
            env = _make_env(Path(tmp) / "bin")
            fake = FakeHerdr()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(operator)}, clear=False):
                report = smoke_shared_space_preflight(
                    Path(tmp) / "smoke", runner=fake.run, env=env, projects=3,
                )
            self.assertTrue(report["isolated_home_ok"])
            self.assertTrue(report["clean_slate_ok"])
            self.assertEqual(report["mode"], "shared_space")
            self.assertEqual(report["projects"], 3)
            self.assertFalse(report["actuated"])
            self.assertEqual(fake.agents, [])

    def test_preexisting_space_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            operator = Path(tmp) / "real"
            operator.mkdir()
            env = _make_env(Path(tmp) / "bin")
            fake = FakeHerdr()
            fake.run(["herdr", "workspace", "create", "--label", "coordinators"])
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(operator)}, clear=False):
                with self.assertRaises(SharedSpaceSmokeError):
                    smoke_shared_space_preflight(
                        Path(tmp) / "smoke", runner=fake.run, env=env,
                    )


if __name__ == "__main__":
    unittest.main()
