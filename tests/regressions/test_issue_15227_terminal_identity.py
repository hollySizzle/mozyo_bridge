"""Adversarial regression matrix for terminal-bound Herdr identity (#15227)."""

from __future__ import annotations

import ast
import inspect
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
    VERDICT_PRESENT,
    evaluate_attestation,
)
from mozyo_bridge.core.state.herdr_identity_attestation_replacement_binding import (
    herdr_identity_replacement_binding_path,
)
from mozyo_bridge.core.state.herdr_launch_generation import (
    completed_generation_startup_token,
    HerdrLaunchGenerationError,
    HerdrLaunchGenerationStore,
    LaunchGeneration,
    herdr_launch_generation_path,
)
from tests.support.current_launch_authority import (
    seed_completed_current_generation,
    seed_current_generation,
)
from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_COMPLETED_ROLLED_BACK,
    PHASE_HEALTH_CHECK,
    PHASE_ROLLBACK_OWED,
    Participant,
    StartupTransactionFence,
    StartupUnit,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_prepare_readonly_projection import (  # noqa: E501
    resolve_rollback_owed_startup_action,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    _norm, _norm_lane, encode_assigned_name,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_live_attestation_time import (  # noqa: E501
    FreshGenerationBoundary,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_offline_rollout_action import (  # noqa: E501
    PhaseExecutionResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_preflight import (  # noqa: E501
    preflight_managed_launch,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback import (  # noqa: E501
    REASON_BLOCKED,
    run_session_rollback,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback_ops import (  # noqa: E501
    LiveStartupRollbackOps,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
    build_attest_capability_epilog,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
    HerdrInventoryView,
    HerdrObservedAgent,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_inventory_identity import (  # noqa: E501
    private_agent_bindings,
    private_inventory_current,
    terminal_inventory_complete,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_restore_verification import (  # noqa: E501
    verify_restored_names,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_generation_rebuild import (  # noqa: E501
    backup_launch_generation,
    rebuild_launch_generation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_snapshot import (  # noqa: E501
    _launch_generation_store_snapshot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    terminal_identity_of_live_slot,
)


_TOKEN = "terminal-id-must-never-render"


def _agent(name="mzb1_ws_codex_default", locator="w1:p1", terminal=_TOKEN, **kw):
    return HerdrObservedAgent(
        name=name, managed=True, workspace_id=kw.get("workspace_id", "ws"),
        lane_id=kw.get("lane_id", "default"), role=kw.get("role", "codex"),
        locator=locator, terminal_id=terminal,
    )


def _view(*agents, raw=None, invalid=0):
    return HerdrInventoryView(
        backend_selected=True, ok=True, workspace_segment="ws", agents=agents,
        raw_row_count=len(agents) if raw is None else raw,
        invalid_row_count=invalid,
    )


def _legacy_generation(home: Path) -> dict:
    path = herdr_launch_generation_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "CREATE TABLE herdr_launch_generations ("
            "assigned_name TEXT NOT NULL PRIMARY KEY, "
            "startup_action_id TEXT NOT NULL, phase TEXT NOT NULL, "
            "workspace_id TEXT NOT NULL, role TEXT NOT NULL, lane_id TEXT NOT NULL, "
            "locator TEXT NOT NULL DEFAULT '', verdict TEXT NOT NULL DEFAULT '', "
            "observed_at TEXT NOT NULL DEFAULT '', reserved_at TEXT NOT NULL, "
            "attested_at TEXT NOT NULL DEFAULT '')"
        )
    path.chmod(0o600)
    return _launch_generation_store_snapshot(home).to_record()


class TerminalIdentityRegressionTests(unittest.TestCase):
    def test_public_rollback_redacts_terminal_text_from_close_failure(self):
        class _Ops:
            def __init__(self, row): self.row = row
            def agent_rows(self): return (self.row,)
            def runtime_state(self, _locator): return "turn_ended"
            def observe_composer(self, _locator): return True, False
            def startup_blocker(self, _provider, _locator): return ""
            def open_obligations(self, _workspace, _names): return ()
            def close_current_generation(self, action, targets, *, store_home):
                return SimpleNamespace(
                    closed=(),
                    failed=tuple((role, locator, _TOKEN) for role, locator in targets),
                )
            def prepared_pane(self, **_kwargs): raise AssertionError("not prepared")

        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            unit = StartupUnit("ws", "lane", ("codex",))
            fence = StartupTransactionFence(home=home)
            action = fence.reserve(unit, "redaction")
            name, locator, terminal = "agent", "w1:p1", "terminal-current"
            fence.record_participant(action.action_id, Participant(
                role="codex", assigned_name=name, locator=locator,
                receipt="workspace=w1",
            ))
            fence.set_phase(action.action_id, PHASE_HEALTH_CHECK)
            seed_current_generation(
                home, workspace_id="ws", lane_id="lane", role="codex",
                assigned_name=name, locator=locator, action_id=action.action_id,
                terminal_id=terminal,
            )
            HerdrIdentityAttestationStore(home=home).upsert(IdentityAttestationRecord(
                name, "ws", "codex", "lane", locator, VERDICT_PRESENT,
                observed_at="2026-08-11T00:00:00+00:00", terminal_id=terminal,
            ))
            verdict = run_session_rollback(
                action_id=action.action_id,
                ops=_Ops({"name": name, "pane_id": locator, "terminal_id": terminal}),
                fence=fence,
                execute=True,
            )
            self.assertNotIn(_TOKEN, repr(verdict))
            self.assertNotIn(_TOKEN, str(verdict.as_payload()))

    def test_rollback_destructive_edge_rejoins_terminal_before_low_level_close(self):
        class _Retire:
            def __init__(self, rows):
                self.rows = rows
                self.close_calls = []
            def agent_rows(self): return tuple(self.rows)
            def close(self, workspace, lane, targets):
                self.close_calls.append((workspace, lane, tuple(targets)))
                return SimpleNamespace(closed=tuple(targets), failed=())

        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            unit = StartupUnit("ws", "lane", ("codex",))
            fence = StartupTransactionFence(home=home)
            action = fence.reserve(unit, "rollback-edge")
            name, locator, terminal = "agent", "w1:p1", "old-terminal"
            fence.record_participant(action.action_id, Participant(
                role="codex", assigned_name=name, locator=locator,
                receipt="workspace=w1",
            ))
            fence.set_phase(action.action_id, PHASE_HEALTH_CHECK)
            action = fence.read(action.action_id)
            seed_current_generation(
                home, workspace_id="ws", lane_id="lane", role="codex",
                assigned_name=name, locator=locator, action_id=action.action_id,
                terminal_id=terminal,
            )
            HerdrIdentityAttestationStore(home=home).upsert(IdentityAttestationRecord(
                name, "ws", "codex", "lane", locator, VERDICT_PRESENT,
                observed_at="2026-08-11T00:00:00+00:00", terminal_id=terminal,
            ))
            retire = _Retire([{
                "name": name, "pane_id": locator, "terminal_id": "new-terminal",
            }])
            ops = LiveStartupRollbackOps.__new__(LiveStartupRollbackOps)
            ops._retire_ops = retire
            refused = ops.close_current_generation(
                action, [("codex", locator)], store_home=home
            )
            self.assertTrue(refused.failed)
            self.assertEqual(retire.close_calls, [])
            retire.rows[0]["terminal_id"] = terminal
            closed = ops.close_current_generation(
                action, [("codex", locator)], store_home=home
            )
            self.assertEqual(closed.closed, (("codex", locator),))
            self.assertEqual(len(retire.close_calls), 1)

    def test_public_rollback_never_closes_a_reused_locator_from_another_generation(self):
        class _Ops:
            def __init__(self, rows):
                self.rows = rows
                self.close_calls = []

            def agent_rows(self): return tuple(self.rows)
            def runtime_state(self, _locator): return "turn_ended"
            def observe_composer(self, _locator): return True, False
            def startup_blocker(self, _provider, _locator): return ""
            def open_obligations(self, _workspace, _names): return ()
            def close(self, _workspace, _lane, targets):
                self.close_calls.append(tuple(targets))
                return SimpleNamespace(closed=tuple(targets), failed=())
            def close_current_generation(self, action, targets, *, store_home):
                return self.close(action.unit.workspace_id, action.unit.lane_id, targets)
            def prepared_pane(self, **_kwargs): raise AssertionError("not prepared")
            def close_prepared_pane(self, **_kwargs): raise AssertionError("not prepared")

        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            workspace, lane, role = "ws", "lane", "codex"
            name = encode_assigned_name(workspace, role, lane)
            locator = "w1:p1"
            current_action = seed_completed_current_generation(
                home, workspace_id=workspace, lane_id=lane, role=role,
                assigned_name=name, locator=locator, terminal_id="terminal:new",
            )
            HerdrIdentityAttestationStore(home=home).upsert(IdentityAttestationRecord(
                name, workspace, role, lane, locator, VERDICT_PRESENT,
                observed_at="2026-08-11T00:00:00+00:00", terminal_id="terminal:new",
            ))
            fence = StartupTransactionFence(home=home)
            old = fence.reserve(StartupUnit(workspace, lane, (role,)), "old-rollback")
            fence.record_participant(old.action_id, Participant(
                role=role, assigned_name=name, locator=locator, receipt="workspace=old",
            ))
            fence.set_phase(old.action_id, PHASE_HEALTH_CHECK)
            fence.set_phase(old.action_id, PHASE_ROLLBACK_OWED)
            self.assertNotEqual(old.action_id, current_action)
            ops = _Ops(({
                "name": name, "pane_id": locator, "agent_status": "idle",
                "terminal_id": "terminal:new",
            },))
            verdict = run_session_rollback(
                action_id=old.action_id, ops=ops, fence=fence, execute=True
            )
            self.assertEqual(verdict.reason, REASON_BLOCKED)
            self.assertEqual(ops.close_calls, [])
            self.assertEqual(fence.read(old.action_id).phase, PHASE_ROLLBACK_OWED)

    def test_current_rollback_authority_ignores_corrupt_legacy_side_store(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home, repo = root / "home", root / "repo"
            home.mkdir(); repo.mkdir()
            workspace, lane, role = "ws", "lane", "codex"
            assigned_name = encode_assigned_name(workspace, role, lane)
            locator, terminal = "w1:p1", _TOKEN
            fence = StartupTransactionFence(home=home)
            action = fence.reserve(StartupUnit(workspace, lane, (role,)), "rollback-nonce")
            fence.record_participant(action.action_id, Participant(
                role=role, assigned_name=assigned_name, locator=locator,
                receipt="workspace=current",
            ))
            fence.set_phase(action.action_id, PHASE_HEALTH_CHECK)
            fence.set_phase(action.action_id, PHASE_ROLLBACK_OWED)
            store = HerdrLaunchGenerationStore(home=home)
            store.reserve_pending(
                assigned_name=assigned_name, startup_action_id=action.action_id,
                workspace_id=workspace, role=role, lane_id=lane,
            )
            store.finalize(
                assigned_name=assigned_name, startup_action_id=action.action_id,
                workspace_id=workspace, role=role, lane_id=lane, locator=locator,
                terminal_id=terminal, verdict=VERDICT_PRESENT,
                observed_at="2026-08-11T00:00:00+00:00",
            )
            HerdrIdentityAttestationStore(home=home).upsert(IdentityAttestationRecord(
                assigned_name, workspace, role, lane, locator, VERDICT_PRESENT,
                observed_at="2026-08-11T00:00:00+00:00", terminal_id=terminal,
                replacement_action_id=action.action_id,
            ))
            side = herdr_identity_replacement_binding_path(home)
            side.write_bytes(b"corrupt legacy side store")
            rows = [{"name": assigned_name, "pane_id": locator,
                     "terminal_id": terminal}]
            module = "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_prepare_readonly_projection"
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False), \
                 patch(module + ".resolve_gateway_provider", return_value=role), \
                 patch(module + ".resolve_worker_provider", return_value="claude"), \
                 patch(module + ".list_herdr_agent_rows", return_value=rows):
                self.assertEqual(resolve_rollback_owed_startup_action(
                    repo_root=repo, env={}, workspace=workspace, lane=lane,
                    action_id=action.action_id,
                ), action.action_id)
            mismatched_rows = [{"name": assigned_name, "pane_id": locator,
                                "terminal_id": "different-terminal"}]
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False), \
                 patch(module + ".resolve_gateway_provider", return_value=role), \
                 patch(module + ".resolve_worker_provider", return_value="claude"), \
                 patch(module + ".list_herdr_agent_rows", return_value=mismatched_rows):
                self.assertEqual(resolve_rollback_owed_startup_action(
                    repo_root=repo, env={}, workspace=workspace, lane=lane,
                    action_id=action.action_id,
                ), "")
            startup = fence.read(action.action_id)
            foreign_unit = replace(startup, unit=StartupUnit("foreign", lane, (role,)))
            no_receipt = replace(startup, participants=(
                replace(startup.participants[0], receipt=""),
            ))
            for label, observed in (("foreign_unit", foreign_unit),
                                    ("missing_receipt", no_receipt)):
                with self.subTest(label=label), \
                     patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False), \
                     patch(module + ".resolve_gateway_provider", return_value=role), \
                     patch(module + ".resolve_worker_provider", return_value="claude"), \
                     patch(module + ".list_herdr_agent_rows", return_value=rows), \
                     patch(module + ".StartupTransactionFence.read", return_value=observed):
                    self.assertEqual(resolve_rollback_owed_startup_action(
                        repo_root=repo, env={}, workspace=workspace, lane=lane,
                        action_id=action.action_id,
                    ), "")
            fence.set_phase(action.action_id, PHASE_COMPLETED_ROLLED_BACK)
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False), \
                 patch(module + ".resolve_gateway_provider", return_value=role), \
                 patch(module + ".resolve_worker_provider", return_value="claude"), \
                 patch(module + ".list_herdr_agent_rows", return_value=rows):
                self.assertEqual(resolve_rollback_owed_startup_action(
                    repo_root=repo, env={}, workspace=workspace, lane=lane,
                    action_id=action.action_id,
                ), "")

    def test_completed_generation_rejects_foreign_startup_unit_axes(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            seed_completed_current_generation(
                home, workspace_id="ws", lane_id="default", role="codex",
                assigned_name="agent", locator="w1:p1", terminal_id=_TOKEN,
            )
            generation = HerdrLaunchGenerationStore(home=home).read("agent")
            token = completed_generation_startup_token(
                home, generation, norm=_norm, norm_lane=_norm_lane
            )
            self.assertTrue(token)
            for field, value in (
                ("workspace_id", "foreign"),
                ("lane_id", "foreign"),
                ("role", "claude"),
            ):
                with self.subTest(field=field):
                    self.assertEqual(completed_generation_startup_token(
                        home, replace(generation, **{field: value}),
                        norm=_norm, norm_lane=_norm_lane,
                    ), "")

    def test_all_production_attestation_and_generation_calls_supply_terminal(self):
        root = Path(__file__).resolve().parents[2] / "src" / "mozyo_bridge"
        wanted = {"evaluate_attestation", "verified_generation_token"}
        missing = []
        for source in root.rglob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.id if isinstance(node.func, ast.Name) else (
                    node.func.attr if isinstance(node.func, ast.Attribute) else "")
                if name in wanted and not any(
                    keyword.arg == "live_terminal_id" for keyword in node.keywords
                ):
                    missing.append(f"{source.relative_to(root)}:{node.lineno}:{name}")
        self.assertEqual(missing, [])
        parameter = inspect.signature(evaluate_attestation).parameters["live_terminal_id"]
        self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_live_slot_rejects_same_pane_different_or_ambiguous_terminal(self):
        good = {"name": "agent", "pane_id": "p1", "terminal_id": _TOKEN}
        self.assertEqual(terminal_identity_of_live_slot("agent", "p1", [good]), _TOKEN)
        bad_rows = (
            [good, {"name": "other", "pane_id": "p2", "terminal_id": _TOKEN}],
            [good, {"name": "other", "pane_id": "p1", "terminal_id": "other"}],
            [good, {"name": "agent", "pane_id": "p2", "terminal_id": "other"}],
            [good, {"name": "other", "pane_id": "p2"}],
            [good, {"name": "other", "pane_id": "p2", "terminal_id": " "}],
            [good, {"name": "", "pane_id": "p2", "terminal_id": "other"}],
            [good, {"name": " other ", "pane_id": "p2", "terminal_id": "other"}],
            [good, {"name": "other", "pane_id": " p2 ", "terminal_id": "other"}],
            [good, {"name": "other", "pane_id": "p2", "terminal_id": "t2"},
             {"name": "other", "pane_id": "p3", "terminal_id": "t3"}],
            [good, {"name": "other-a", "pane_id": "p2", "terminal_id": "t2"},
             {"name": "other-b", "pane_id": "p2", "terminal_id": "t3"}],
            [good, {"name": "other-a", "pane_id": "p2", "terminal_id": "other"},
             {"name": "other-b", "pane_id": "p3", "terminal_id": "other"}],
            [good, object()],
        )
        for rows in bad_rows:
            with self.subTest(rows=rows):
                self.assertIsNone(terminal_identity_of_live_slot("agent", "p1", rows))
        record = IdentityAttestationRecord(
            "agent", "ws", "codex", "default", "p1", VERDICT_PRESENT,
            observed_at="2026-08-11T00:00:00+00:00", terminal_id=_TOKEN,
        )
        self.assertTrue(evaluate_attestation(
            record, live_locator="p1", live_terminal_id=_TOKEN,
            expected_workspace_id="ws", expected_role="codex",
            expected_lane="default").ok)
        for terminal in ("different", "", " ", None, True):
            with self.subTest(terminal=terminal):
                self.assertFalse(evaluate_attestation(
                    record, live_locator="p1", live_terminal_id=terminal,
                    expected_workspace_id="ws", expected_role="codex",
                    expected_lane="default").ok)

    def test_terminal_identity_never_reaches_public_repr_or_payload(self):
        record = IdentityAttestationRecord(
            "agent", "ws", "codex", "default", "p1", VERDICT_PRESENT,
            terminal_id=_TOKEN,
        )
        generation = LaunchGeneration(
            "agent", "action", "attested", "ws", "codex", "default",
            locator="p1", terminal_id=_TOKEN,
        )
        observed = _agent()
        private = PhaseExecutionResult(True, receipt={"terminal_id": _TOKEN})
        for rendered in (
            repr(record), str(record.as_payload()), repr(generation),
            str(generation.as_payload()), repr(observed), str(observed.to_record()),
            repr(private),
        ):
            self.assertNotIn(_TOKEN, rendered)

    def test_delivery_requires_canonical_v2_and_every_generation_axis(self):
        boundary = FreshGenerationBoundary(
            "agent", "p1", "codex", "7", "2026-08-11T00:00:00+00:00", "action")
        binding = {
            "assigned_name": "agent", "locator": "p1", "provider": "codex",
            "row_revision": "7", "attestation_observed_at": boundary.observed_at,
            "startup_action_id": "action",
        }
        record = SimpleNamespace(queue_enter_observation={
            "observation_version": 2, "gateway_binding": binding})
        self.assertTrue(boundary.matches_delivery(record))
        for key in ("assigned_name", "locator", "provider", "row_revision",
                    "attestation_observed_at", "startup_action_id"):
            broken = dict(binding)
            broken[key] = "old"
            self.assertFalse(boundary.matches_delivery(SimpleNamespace(
                queue_enter_observation={"observation_version": 2,
                                         "gateway_binding": broken})))
        self.assertFalse(boundary.matches_delivery(SimpleNamespace(
            queue_enter_observation={"gateway_binding": binding})))

    def test_convergence_final_pins_uses_one_inventory_snapshot_for_every_join(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import sublane_hibernated_bound_pair_convergence_live as live
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_convergence import BoundPairObservation
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_convergence import BoundSlot
        rows = ({"name": "agent", "pane_id": "p1", "terminal_id": _TOKEN},)
        observation = BoundPairObservation(
            workspace_id="ws", slots=(BoundSlot(
                "gateway", "codex", "agent", "p1", live.SLOT_HEALTHY),))
        ops = live.LiveBoundPairConvergenceOps(repo_root=Path("/unused"), env={})
        transaction = SimpleNamespace(participants=(
            SimpleNamespace(assigned_name="agent", old_locator="old"),))
        attestation = SimpleNamespace(observed_at="now")
        request = SimpleNamespace(lane="default")
        with patch.object(live, "list_herdr_agent_rows", return_value=list(rows)) as listed, \
             patch.object(ops, "observe", return_value=observation) as observed, \
             patch.object(ops, "_transaction", return_value=transaction), \
             patch.object(live.HerdrIdentityAttestationStore, "read", return_value=attestation), \
             patch.object(live, "terminal_identity_of_live_slot", return_value=_TOKEN) as terminal, \
             patch.object(live, "evaluate_attestation", return_value=SimpleNamespace(ok=True)), \
             patch.object(live, "_action_bound_after_identity_join", return_value=True) as bound:
            _, pins = ops.final_pins(request, action_id="action")
        listed.assert_called_once_with({})
        self.assertEqual(observed.call_args.kwargs["_snapshot_rows"], rows)
        terminal.assert_called_once_with("agent", "p1", rows)
        self.assertEqual(bound.call_args.kwargs["live_terminal_id"], _TOKEN)
        self.assertEqual(len(pins), 1)

    def test_composer_progress_and_destructive_edge_receive_the_same_snapshot(self):
        from unittest.mock import MagicMock
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import sublane_hibernated_bound_pair_composer_discard_live as composer
        rows = ({"name": "agent", "pane_id": "p1", "terminal_id": _TOKEN},)
        ops = composer.LiveBoundPairPreparationOps(repo_root=Path("/unused"), env={})
        pin = SimpleNamespace(role="gateway", phase="launch_owed")
        with patch.object(ops, "_action_bound_slot", return_value=True) as joined:
            roles = ops._progress_proven_roles(
                object(), object(), object(), (pin,), rows=rows)
        self.assertEqual(roles, ("gateway",))
        self.assertIs(joined.call_args.kwargs["rows"], rows)

        transaction = SimpleNamespace(participants=(pin,))
        owner = SimpleNamespace(
            env={}, _lifecycle=MagicMock(return_value=object()),
            _worktree=MagicMock(return_value=(Path("/wt"), "ws", "identity")),
            transaction_store=SimpleNamespace(get=MagicMock(return_value=transaction)),
            _observation_from_snapshot=MagicMock(return_value=object()),
            _progress_proven_roles=MagicMock(return_value=("gateway",)),
            _progress_snapshot_matches=MagicMock(return_value=True),
        )
        port = composer._ComposerDiscardActuatorPort(
            owner=owner, request=object(), expectation=SimpleNamespace(action_id="action"),
            live=object(), prepare_request=object(), approved_roles=("gateway",),
        )
        with patch.object(composer, "list_herdr_agent_rows", return_value=list(rows)), \
             patch.object(composer, "_git", return_value=(True, "")):
            self.assertIsNotNone(port._fresh_authority())
        self.assertEqual(
            owner._observation_from_snapshot.call_args.kwargs["rows"], rows)
        self.assertEqual(owner._progress_proven_roles.call_args.kwargs["rows"], rows)

    def test_offline_private_inventory_is_complete_and_exact(self):
        first = _agent()
        second = _agent("mzb1_ws_claude_default", "w1:p2", "terminal-2", role="claude")
        bindings = {
            first.name: {"workspace_id": "ws", "lane_id": "default", "provider": "codex",
                         "locator": "w1:p1", "terminal_id": _TOKEN},
            second.name: {"workspace_id": "ws", "lane_id": "default", "provider": "claude",
                          "locator": "w1:p2", "terminal_id": "terminal-2"},
        }
        self.assertTrue(private_inventory_current(_view(first, second), bindings))
        for view in (
            _view(first, replace(second, role="codex")),
            _view(first, replace(second, terminal_id=_TOKEN)),
            _view(first, replace(second, name=first.name)),
            _view(first, replace(second, locator=first.locator)),
            _view(first, replace(second, name=" bad ")),
            _view(first, replace(second, locator=" p2 ")),
            _view(first, second, raw=True),
            _view(first, second, invalid=True),
        ):
            self.assertFalse(private_inventory_current(view, bindings))
        self.assertFalse(terminal_inventory_complete(_view(first, second, raw=True)))
        self.assertFalse(terminal_inventory_complete(_view(first, second, invalid=True)))

    def test_offline_private_capture_rejects_same_name_on_changed_authority_axes(self):
        agent = _agent()
        plan = {"agents": [{
            "assigned_name": agent.name, "workspace_id": "ws",
            "lane_id": "default", "provider": "codex",
        }]}
        self.assertTrue(private_agent_bindings(_view(agent), plan).ok)
        for axis, value in (
            ("workspace_id", "foreign"), ("lane_id", "other"),
            ("provider", "claude"),
        ):
            changed = {"agents": [dict(plan["agents"][0], **{axis: value})]}
            result = private_agent_bindings(_view(agent), changed)
            self.assertFalse(result.ok)
            self.assertEqual(result.reason, "agent_set_drift")

    def test_restore_requires_generation_and_final_roster_is_exact(self):
        view = _view(_agent())
        join = SimpleNamespace(ok=True)
        module = "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_restore_verification"
        with patch(module + ".HerdrIdentityAttestationStore.read", return_value=object()), \
             patch(module + ".evaluate_attestation", return_value=join), \
             patch(module + ".verified_generation_token", return_value=""):
            self.assertFalse(verify_restored_names(
                view=view, names={"mzb1_ws_codex_default"}, home=Path("/unused"))[0])
        extra = _agent("mzb1_ws_claude_default", "w1:p2", "terminal-2", role="claude")
        with patch(module + ".HerdrIdentityAttestationStore.read", return_value=object()), \
             patch(module + ".evaluate_attestation", return_value=join), \
             patch(module + ".verified_generation_token", return_value="action"):
            self.assertFalse(verify_restored_names(
                view=_view(_agent(), extra), names={"mzb1_ws_codex_default"},
                home=Path("/unused"), exact_roster=True)[0])

    def test_legacy_generation_preflight_is_zero_transaction_boundary(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            _legacy_generation(home)
            rendered = f"usage: x [--assigned-name NAME]\n{build_attest_capability_epilog()}\n"
            runner = lambda argv, **_kw: subprocess.CompletedProcess(
                argv, 0, stdout=rendered, stderr="")
            with self.assertRaisesRegex(Exception, "offline v2 rebuild"):
                preflight_managed_launch(
                    "/wrapper", runner, 1.0, {},
                    repo_root=home, store_home=home, workspace_id="ws",
                    lane_id="default")
            preflight_managed_launch(
                "", lambda *_args, **_kwargs: None, 1.0, {},
                repo_root=home, store_home=home, workspace_id="ws",
                lane_id="default")
            self.assertFalse((home / "startup-transactions.sqlite").exists())

    def test_generation_orphan_or_unsafe_sidecars_are_never_absent(self):
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as raw:
                home = Path(raw)
                sidecar = Path(str(herdr_launch_generation_path(home)) + suffix)
                sidecar.parent.mkdir(parents=True, exist_ok=True)
                sidecar.write_bytes(b"orphan")
                sidecar.chmod(0o600)
                self.assertNotEqual(
                    _launch_generation_store_snapshot(home).to_record()["state"],
                    "absent",
                )
                with self.assertRaises(HerdrLaunchGenerationError):
                    HerdrLaunchGenerationStore(home=home).reserve_pending(
                        assigned_name="agent", startup_action_id="action",
                        workspace_id="ws", role="codex", lane_id="default")
                with self.assertRaises(HerdrLaunchGenerationError):
                    HerdrLaunchGenerationStore(home=home).read("agent")
                self.assertIsNone(HerdrLaunchGenerationStore(home=home).assigned_names())
                self.assertFalse(herdr_launch_generation_path(home).exists())
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            path = herdr_launch_generation_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            target = home / "foreign"
            target.write_bytes(b"x")
            Path(str(path) + "-wal").symlink_to(target)
            self.assertNotEqual(
                _launch_generation_store_snapshot(home).to_record()["state"],
                "absent",
            )

    def test_generation_backup_and_partial_delete_are_replayable(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home, backups = root / "home", root / "backups"
            backups.mkdir()
            planned = _legacy_generation(home)
            sidecar = Path(str(herdr_launch_generation_path(home)) + "-shm")
            sidecar.write_bytes(b"committed-sidecar-evidence")
            sidecar.chmod(0o600)
            observe = lambda: _launch_generation_store_snapshot(home).to_record()
            backup = backup_launch_generation(
                home=home, backup_root=backups, planned=planned, observe=observe)
            self.assertTrue(backup.ok, backup)
            target = "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_generation_rebuild.remove_attestation_store_artifacts"
            def partial_delete(_path):
                sidecar.unlink()
                raise OSError("injected crash")
            with patch(target, side_effect=partial_delete):
                first = rebuild_launch_generation(
                    home=home, backup_root=backups, planned=planned, observe=observe,
                    backup_receipt=backup.receipt, replaying=False)
            self.assertFalse(first.ok)
            drift = dict(planned, content_digest="partial-delete-drift")
            replay_observe = lambda: (
                drift if herdr_launch_generation_path(home).exists()
                else _launch_generation_store_snapshot(home).to_record())
            replay = rebuild_launch_generation(
                home=home, backup_root=backups, planned=planned,
                observe=replay_observe, backup_receipt=backup.receipt, replaying=True)
            self.assertTrue(replay.ok, replay)
            self.assertFalse(herdr_launch_generation_path(home).exists())

    def test_generation_rebuild_refuses_inode_aba_and_bad_backup(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home, backups = root / "home", root / "backups"
            backups.mkdir()
            planned = _legacy_generation(home)
            observe = lambda: _launch_generation_store_snapshot(home).to_record()
            backup = backup_launch_generation(
                home=home, backup_root=backups, planned=planned, observe=observe)
            target = "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_generation_rebuild.remove_attestation_store_artifacts"
            with patch(target, side_effect=OSError("crash after marker")):
                self.assertFalse(rebuild_launch_generation(
                    home=home, backup_root=backups, planned=planned, observe=observe,
                    backup_receipt=backup.receipt, replaying=False).ok)
            path = herdr_launch_generation_path(home)
            replacement = path.with_name("replacement.sqlite3")
            shutil.copy2(backups / "launch-generation.sqlite3", replacement)
            replacement.chmod(0o600)
            replacement.replace(path)
            self.assertFalse(rebuild_launch_generation(
                home=home, backup_root=backups, planned=planned, observe=observe,
                backup_receipt=backup.receipt, replaying=True).ok)
            self.assertTrue(path.exists())
            (backups / "launch-generation.sqlite3").chmod(0o644)
            self.assertFalse(rebuild_launch_generation(
                home=home, backup_root=backups, planned=planned, observe=observe,
                backup_receipt=backup.receipt, replaying=True).ok)

    def test_generation_rebuild_revalidates_backup_schema_and_sidecars(self):
        for tamper in ("version", "sidecar"):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                home, backups = root / "home", root / "backups"
                backups.mkdir()
                planned = _legacy_generation(home)
                observe = lambda: _launch_generation_store_snapshot(home).to_record()
                backup = backup_launch_generation(
                    home=home, backup_root=backups, planned=planned, observe=observe)
                artifact = backups / "launch-generation.sqlite3"
                if tamper == "version":
                    with sqlite3.connect(artifact) as conn:
                        conn.execute("PRAGMA user_version = 2")
                else:
                    foreign = backups / "foreign"
                    foreign.write_bytes(b"x")
                    Path(str(artifact) + "-shm").symlink_to(foreign)
                result = rebuild_launch_generation(
                    home=home, backup_root=backups, planned=planned, observe=observe,
                    backup_receipt=backup.receipt, replaying=False)
                self.assertFalse(result.ok)
                self.assertTrue(herdr_launch_generation_path(home).exists())

    def test_generation_backup_rejects_orphan_sidecar_and_staging_schema_tamper(self):
        module = "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_generation_rebuild"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home, backups = root / "home", root / "backups"
            backups.mkdir()
            planned = _legacy_generation(home)
            orphan = backups / "launch-generation.sqlite3-shm"
            orphan.write_bytes(b"orphan")
            orphan.chmod(0o600)
            result = backup_launch_generation(
                home=home, backup_root=backups, planned=planned,
                observe=lambda: _launch_generation_store_snapshot(home).to_record())
            self.assertFalse(result.ok)
            self.assertTrue(herdr_launch_generation_path(home).exists())
            self.assertFalse((backups / "launch-generation.sqlite3").exists())
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home, backups = root / "home", root / "backups"
            backups.mkdir()
            planned = _legacy_generation(home)
            from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import herdr_offline_rollout_generation_rebuild as rebuild_module
            real_match = rebuild_module._artifact_matches_plan
            def tamper_staging(path, expected):
                if path.name.endswith(".staging"):
                    with sqlite3.connect(path) as conn:
                        conn.execute("PRAGMA user_version = 2")
                return real_match(path, expected)
            with patch(module + "._artifact_matches_plan", side_effect=tamper_staging):
                result = backup_launch_generation(
                    home=home, backup_root=backups, planned=planned,
                    observe=lambda: _launch_generation_store_snapshot(home).to_record())
            self.assertFalse(result.ok)
            self.assertTrue(herdr_launch_generation_path(home).exists())
            self.assertFalse((backups / "launch-generation.sqlite3").exists())

    def test_generation_rebuild_absent_v2_and_positive_short_write_paths(self):
        module = "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_generation_rebuild"
        with tempfile.TemporaryDirectory() as raw:
            home, backups = Path(raw) / "home", Path(raw) / "backups"
            backups.mkdir()
            absent = _launch_generation_store_snapshot(home).to_record()
            self.assertTrue(rebuild_launch_generation(
                home=home, backup_root=backups, planned=absent,
                observe=lambda: _launch_generation_store_snapshot(home).to_record(),
                backup_receipt={}, replaying=False).ok)
            store = HerdrLaunchGenerationStore(home=home)
            store.reserve_pending(
                assigned_name="agent", startup_action_id="action",
                workspace_id="ws", role="codex", lane_id="default")
            current = _launch_generation_store_snapshot(home).to_record()
            self.assertTrue(rebuild_launch_generation(
                home=home, backup_root=backups, planned=current,
                observe=lambda: _launch_generation_store_snapshot(home).to_record(),
                backup_receipt={}, replaying=False).ok)
        with tempfile.TemporaryDirectory() as raw:
            home, backups = Path(raw) / "home", Path(raw) / "backups"
            backups.mkdir()
            planned = _legacy_generation(home)
            observe = lambda: _launch_generation_store_snapshot(home).to_record()
            backup = backup_launch_generation(
                home=home, backup_root=backups, planned=planned, observe=observe)
            real_write = os.write
            def short_write(fd, payload):
                return real_write(fd, payload[:max(1, len(payload) // 2)])
            with patch(module + ".os.write", side_effect=short_write):
                result = rebuild_launch_generation(
                    home=home, backup_root=backups, planned=planned, observe=observe,
                    backup_receipt=backup.receipt, replaying=False)
            self.assertTrue(result.ok, result)

    def test_generation_backup_source_vanish_and_short_marker_write_fail_closed(self):
        module = "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_generation_rebuild"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home, backups = root / "home", root / "backups"
            backups.mkdir()
            planned = _legacy_generation(home)
            path = herdr_launch_generation_path(home)
            def vanish():
                path.unlink(missing_ok=True)
                return planned
            vanished = backup_launch_generation(
                home=home, backup_root=backups, planned=planned, observe=vanish)
            self.assertFalse(vanished.ok)
            self.assertFalse((backups / "launch-generation.sqlite3").exists())
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home, backups = root / "home", root / "backups"
            backups.mkdir()
            planned = _legacy_generation(home)
            observe = lambda: _launch_generation_store_snapshot(home).to_record()
            backup = backup_launch_generation(
                home=home, backup_root=backups, planned=planned, observe=observe)
            with patch(module + ".os.write", return_value=0):
                result = rebuild_launch_generation(
                    home=home, backup_root=backups, planned=planned, observe=observe,
                    backup_receipt=backup.receipt, replaying=False)
            self.assertFalse(result.ok)
            self.assertTrue(herdr_launch_generation_path(home).exists())

    def test_generation_backup_and_marker_retries_repeat_directory_fsync(self):
        module = "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_generation_rebuild"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home, backups = root / "home", root / "backups"
            backups.mkdir()
            planned = _legacy_generation(home)
            observe = lambda: _launch_generation_store_snapshot(home).to_record()
            with patch(module + "._directory_fsync", side_effect=OSError("backup fsync")):
                first_backup = backup_launch_generation(
                    home=home, backup_root=backups, planned=planned, observe=observe)
            self.assertFalse(first_backup.ok)
            self.assertTrue((backups / "launch-generation.sqlite3").exists())
            with patch(module + "._directory_fsync") as retried_backup_fsync:
                backup = backup_launch_generation(
                    home=home, backup_root=backups, planned=planned, observe=observe)
            self.assertTrue(backup.ok, backup)
            retried_backup_fsync.assert_called_with(backups)
            with patch(module + "._directory_fsync", side_effect=OSError("marker fsync")):
                first_rebuild = rebuild_launch_generation(
                    home=home, backup_root=backups, planned=planned, observe=observe,
                    backup_receipt=backup.receipt, replaying=False)
            self.assertFalse(first_rebuild.ok)
            self.assertTrue(herdr_launch_generation_path(home).exists())
            with patch(module + "._directory_fsync", side_effect=OSError("retry fsync")):
                blocked_replay = rebuild_launch_generation(
                    home=home, backup_root=backups, planned=planned, observe=observe,
                    backup_receipt=backup.receipt, replaying=True)
            self.assertFalse(blocked_replay.ok)
            self.assertTrue(herdr_launch_generation_path(home).exists())
            replay = rebuild_launch_generation(
                home=home, backup_root=backups, planned=planned, observe=observe,
                backup_receipt=backup.receipt, replaying=True)
            self.assertTrue(replay.ok, replay)
            self.assertFalse(herdr_launch_generation_path(home).exists())


if __name__ == "__main__":
    unittest.main()
