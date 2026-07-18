"""herdr worker-dispatch ack-drive adapter tests (Redmine #13357).

Drives :class:`HerdrWorkerDispatchOps` through the stateful fake herdr CLI (the
#13331 actuator-test shape) and a real (temp) workspace registry — no live herdr,
no tmux. Covers the live-inventory lane read-back, the presence-based worker
readiness probe (#13301 herdr form), the composed same-lane ``handoff send`` argv
+ j#71597 containment, the backend selector (tmux stays byte-invariant), and the
pure use-case drive over the herdr adapter (#12988 contract: exit 0 promotes to
``worker_dispatched``; every failure keeps ``gateway_notified``, fail-closed).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E501
    HerdrSublaneActuatorOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_dispatch_herdr_ops import (  # noqa: E501
    HerdrWorkerDispatchOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_dispatcher import (  # noqa: E501
    LiveWorkerDispatchOps,
    WorkerDispatchOps,
    WorkerDispatchUseCase,
    _replayable_command,
    _resolve_worker_dispatch_ops,
    _worker_dispatch_argv,
)
from mozyo_bridge.core.state.lane_lifecycle_model import ProcessGenerationPin
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    ACTUATE_BLOCKED,
    ACTUATE_EXECUTED,
    DISPATCH_WORKER_DISPATCHED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_worker_dispatch import (  # noqa: E501
    ADMISSION_HEALTHY,
    ADMISSION_STALE_WORKER_RECOVERY_REQUIRED,
    ADMISSION_WORKER_LIVENESS_AUTHORITY_CONFLICT,
    WORKER_DISPATCH_DELIVERY_FAILED,
    WorkerDispatchAdmission,
    WorkerDispatchAdmissionFacts,
    WorkerDispatchRequest,
    lane_identity_matches,
)

from tests.support.agent_provider_binaries import provider_bin_path, with_provider_path
from tests.unit.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_sublane_actuator_herdr_ops import (  # noqa: E501
    HERDR_ENV,
    _fake_binary,
    _StatefulHerdr,
)

LANE_LABEL = "issue_13357_dispatch_worker_herdr"
ISSUE = "13357"


def _healthy_admission() -> WorkerDispatchAdmission:
    return WorkerDispatchAdmission(
        ADMISSION_HEALTHY,
        "healthy",
        WorkerDispatchAdmissionFacts(
            True,
            True,
            True,
            True,
            "live",
            True,
            "awaiting_input",
            generation_binding_current=True,
            lane_generation=1,
            worker_assigned_name="mzb1_ws_claude_lane",
            workspace_id="ws",
            lane_id=LANE_LABEL,
            action_id="lane_generation_1",
        ),
    )


class _HerdrLaneFixture:
    """Stand a fake per-lane herdr workspace up and build the worker-dispatch ops."""

    def __init__(self, tmp: str):
        self.herdr = _StatefulHerdr()
        self.home = Path(tmp) / "home"
        self.home.mkdir(exist_ok=True)
        self.worktree = Path(tmp) / "lane-wt"
        self.worktree.mkdir(exist_ok=True)
        binpath = _fake_binary(tmp)
        self.env = with_provider_path({HERDR_ENV: str(binpath), "MOZYO_BRIDGE_HOME": str(self.home)})

    def stand_up_lane(self) -> None:
        actuator = HerdrSublaneActuatorOps(
            repo_root=self.worktree,
            lane_label=LANE_LABEL,
            issue=ISSUE,
            env=self.env,
            runner=self.herdr.run,
        )
        actuator.append_lane_column(str(self.worktree))

    def ops(self) -> HerdrWorkerDispatchOps:
        return HerdrWorkerDispatchOps(
            repo_root=self.worktree,
            lane_label=LANE_LABEL,
            issue=ISSUE,
            env=self.env,
            runner=self.herdr.run,
        )


class PortConformanceTests(unittest.TestCase):
    def test_herdr_ops_satisfies_protocol(self):
        self.assertIsInstance(
            HerdrWorkerDispatchOps(repo_root=Path("."), lane_label="x", issue="1"),
            WorkerDispatchOps,
        )


class ReadLaneTests(unittest.TestCase):
    def test_read_lane_resolves_live_lane_and_identity_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = _HerdrLaneFixture(tmp)
            with patch.dict(
                os.environ, {"MOZYO_BRIDGE_HOME": str(fx.home)}, clear=False
            ):
                ops = fx.ops()
                # A fresh worktree has no herdr workspace yet -> lane absent.
                self.assertIsNone(ops.read_lane(str(fx.worktree)))
                fx.stand_up_lane()
                view = ops.read_lane(str(fx.worktree))
        self.assertIsNotNone(view)
        # Both managed slots resolve to live herdr locators (never %pane).
        self.assertTrue(view.gateway_pane and not view.gateway_pane.startswith("%"))
        self.assertTrue(view.worker_pane and not view.worker_pane.startswith("%"))
        # The echoed lane identity passes the j#70250 guard for the request.
        self.assertTrue(
            lane_identity_matches(view, issue=ISSUE, lane_label=LANE_LABEL)
        )

    def test_read_lane_mismatched_request_fails_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = _HerdrLaneFixture(tmp)
            with patch.dict(
                os.environ, {"MOZYO_BRIDGE_HOME": str(fx.home)}, clear=False
            ):
                fx.stand_up_lane()
                view = fx.ops().read_lane(str(fx.worktree))
        self.assertIsNotNone(view)
        self.assertFalse(
            lane_identity_matches(view, issue="99999", lane_label="issue_99999_other")
        )


class WorkerReadinessProbeTests(unittest.TestCase):
    def test_probe_worker_ready_presence(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = _HerdrLaneFixture(tmp)
            with patch.dict(
                os.environ, {"MOZYO_BRIDGE_HOME": str(fx.home)}, clear=False
            ):
                fx.stand_up_lane()
                ops = fx.ops()
                view = ops.read_lane(str(fx.worktree))
                self.assertTrue(ops.probe_worker_ready(view.worker_pane))
                self.assertFalse(ops.probe_worker_ready("wL:p999"))
                self.assertFalse(ops.probe_worker_ready(""))


class WorkerAdmissionObservationTests(unittest.TestCase):
    def _observe(self, rows, attestation, *, lifecycle_overrides=None):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
            encode_assigned_name,
        )

        lane = SimpleNamespace(
            workspace_id="ws", lane_id=LANE_LABEL, worker_pane="w28:p75"
        )
        request = WorkerDispatchRequest(ISSUE, LANE_LABEL, "/repo", "81683")
        worker_name = encode_assigned_name("ws", "claude", LANE_LABEL)
        lifecycle_values = dict(
            issue_id=ISSUE,
            lane_disposition="active",
            lane_generation=7,
            decision_journal="81683",
            replacement_action_id="",
            declared_pins=(
                ProcessGenerationPin(
                    role="gateway",
                    provider="codex",
                    assigned_name=encode_assigned_name("ws", "codex", LANE_LABEL),
                    locator="w28:p74",
                ),
                ProcessGenerationPin(
                    role="worker",
                    provider="claude",
                    assigned_name=worker_name,
                    locator="w28:p75",
                ),
            ),
        )
        lifecycle_values.update(lifecycle_overrides or {})
        lifecycle = SimpleNamespace(**lifecycle_values)
        ops = HerdrWorkerDispatchOps(Path("/repo"), LANE_LABEL, ISSUE)
        with patch.object(ops, "worker_provider", return_value="claude"), patch(
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection.list_herdr_agent_rows",
            return_value=rows,
        ), patch(
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection.repo_scope_workspace_id",
            return_value="ws",
        ), patch(
            "mozyo_bridge.core.state.lane_lifecycle.LaneLifecycleStore.get",
            return_value=lifecycle,
        ), patch(
            "mozyo_bridge.core.state.herdr_identity_attestation.HerdrIdentityAttestationStore.read",
            return_value=attestation,
        ), patch(
            "mozyo_bridge.core.state.herdr_delivery_ledger.HerdrDeliveryLedger.records_for_issue",
            return_value=[],
        ):
            return ops.observe_worker_dispatch_admission(lane=lane, request=request)

    def _attestation(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
            encode_assigned_name,
        )

        return SimpleNamespace(
            assigned_name=encode_assigned_name("ws", "claude", LANE_LABEL),
            workspace_id="ws",
            role="claude",
            lane_id=LANE_LABEL,
            locator="w28:p75",
            verdict="present",
            replacement_action_id="",
        )

    def test_current_live_attested_idle_receiver_is_healthy(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
            encode_assigned_name,
        )

        name = encode_assigned_name("ws", "claude", LANE_LABEL)
        result = self._observe(
            [
                {
                    "name": name,
                    "pane_id": "w28:p75",
                    "agent": "claude",
                    "agent_status": "idle",
                }
            ],
            self._attestation(),
        )
        self.assertEqual(result.decision, ADMISSION_HEALTHY)
        self.assertEqual(result.facts.lane_generation, 7)

    def test_fresh_generation_live_runtime_revision_is_healthy_not_conflict(self):
        # Redmine #13846: the declared worker pin carries no runtime_revision (the fixture's
        # declared_pins leave it empty, exactly as adopt/hibernate declarations do — the
        # generation discriminant is the live locator), yet the live `agent list` row DOES
        # surface a runtime_revision. A full match_key equality treated that asymmetry as a
        # mismatch and raised a false `worker_liveness_authority_conflict`; binding on the
        # (role/provider/assigned_name/locator) identity keeps this current fresh generation
        # healthy while the locator still matches.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
            encode_assigned_name,
        )

        name = encode_assigned_name("ws", "claude", LANE_LABEL)
        result = self._observe(
            [
                {
                    "name": name,
                    "pane_id": "w28:p75",
                    "provider": "claude",
                    "agent": "claude",
                    "agent_status": "idle",
                    "runtime_revision": "cli-2.1.0",
                }
            ],
            self._attestation(),
        )
        self.assertEqual(result.decision, ADMISSION_HEALTHY)
        self.assertTrue(result.facts.generation_binding_current)

    def test_live_runtime_revision_diverging_from_declared_is_conflict(self):
        # Adversarial: when the declared pin DID observe a runtime revision and the live row
        # surfaces a DIFFERENT one (a same-name process re-launched at a newer runtime), that
        # is a distinct generation and must fail closed even though the locator matches.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
            encode_assigned_name,
        )

        name = encode_assigned_name("ws", "claude", LANE_LABEL)
        declared_pins = (
            ProcessGenerationPin(
                role="gateway",
                provider="codex",
                assigned_name=encode_assigned_name("ws", "codex", LANE_LABEL),
                locator="w28:p74",
            ),
            ProcessGenerationPin(
                role="worker",
                provider="claude",
                assigned_name=name,
                locator="w28:p75",
                runtime_revision="cli-1.0.0",
            ),
        )
        result = self._observe(
            [
                {
                    "name": name,
                    "pane_id": "w28:p75",
                    "provider": "claude",
                    "agent": "claude",
                    "agent_status": "idle",
                    "runtime_revision": "cli-2.1.0",
                }
            ],
            self._attestation(),
            lifecycle_overrides={"declared_pins": declared_pins},
        )
        self.assertEqual(
            result.decision, ADMISSION_WORKER_LIVENESS_AUTHORITY_CONFLICT
        )

    def test_locator_bearing_stale_row_is_authority_conflict(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
            encode_assigned_name,
        )

        name = encode_assigned_name("ws", "claude", LANE_LABEL)
        result = self._observe(
            [{"name": name, "pane_id": "w28:p75", "agent": "", "status": "unknown"}],
            self._attestation(),
        )
        self.assertEqual(
            result.decision, ADMISSION_WORKER_LIVENESS_AUTHORITY_CONFLICT
        )

    def test_current_slot_absence_routes_recovery_without_send_authority(self):
        attestation = self._attestation()
        attestation.replacement_action_id = "replace-7"
        result = self._observe(
            [],
            attestation,
            lifecycle_overrides={"replacement_action_id": "replace-7"},
        )
        self.assertEqual(
            result.decision, ADMISSION_STALE_WORKER_RECOVERY_REQUIRED
        )

    def test_live_locator_drift_from_declared_worker_pin_is_conflict(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
            encode_assigned_name,
        )

        name = encode_assigned_name("ws", "claude", LANE_LABEL)
        attestation = self._attestation()
        attestation.locator = "w28:p-new"
        result = self._observe(
            [
                {
                    "name": name,
                    "pane_id": "w28:p-new",
                    "provider": "claude",
                    "agent": "claude",
                    "agent_status": "idle",
                }
            ],
            attestation,
        )
        self.assertEqual(
            result.decision, ADMISSION_WORKER_LIVENESS_AUTHORITY_CONFLICT
        )

    def test_missing_or_incomplete_declared_pair_is_conflict(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
            encode_assigned_name,
        )

        name = encode_assigned_name("ws", "claude", LANE_LABEL)
        row = {
            "name": name,
            "pane_id": "w28:p75",
            "provider": "claude",
            "agent": "claude",
            "agent_status": "idle",
        }
        incomplete = (
            ProcessGenerationPin("worker", "claude", name, "w28:p75"),
        )
        for pins in ((), incomplete):
            with self.subTest(pins=pins):
                result = self._observe(
                    [row],
                    self._attestation(),
                    lifecycle_overrides={"declared_pins": pins},
                )
                self.assertEqual(
                    result.decision, ADMISSION_WORKER_LIVENESS_AUTHORITY_CONFLICT
                )

    def test_terminal_absence_with_stale_anchor_or_action_is_conflict(self):
        attestation = self._attestation()
        attestation.replacement_action_id = "replace-old"
        for overrides in (
            {"decision_journal": "old", "replacement_action_id": "replace-old"},
            {"replacement_action_id": "replace-new"},
        ):
            with self.subTest(overrides=overrides):
                result = self._observe([], attestation, lifecycle_overrides=overrides)
                self.assertEqual(
                    result.decision, ADMISSION_WORKER_LIVENESS_AUTHORITY_CONFLICT
                )


class DispatchContainmentTests(unittest.TestCase):
    """The herdr adapter drives the shared composed-CLI containment (j#71597)."""

    def _dispatch(self, fake_func, *, capture_argv=None):
        ops = HerdrWorkerDispatchOps(
            repo_root=Path("/wt/13357"), lane_label=LANE_LABEL, issue=ISSUE
        )

        class FakeParser:
            def parse_args(self, argv):
                if capture_argv is not None:
                    capture_argv.append(list(argv))
                return Namespace(func=fake_func)

        out, err = io.StringIO(), io.StringIO()
        with patch(
            "mozyo_bridge.application.cli.build_parser",
            return_value=FakeParser(),
        ), patch(
            "mozyo_bridge.application.cli.normalize_paths",
            side_effect=lambda a: a,
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ops.dispatch_to_worker(
                issue=ISSUE,
                journal="73381",
                worker_pane="wC:p3",
                lane_label=LANE_LABEL,
                gateway_callback_target="wC:p2",
                target_repo="auto",
            )
        return rc, out.getvalue(), err.getvalue()

    def test_argv_is_the_governed_same_lane_forward_on_the_herdr_rail(self):
        seen: list[list[str]] = []
        rc, out, _err = self._dispatch(lambda args: 0, capture_argv=seen)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertEqual(len(seen), 1)
        argv = seen[0]
        # Redmine #13397: the inner send pins the top-level `--repo` to the ops'
        # own repo_root (the outer-resolved herdr root) so its effective-backend
        # resolution matches the outer selection instead of the driving cwd. The
        # `--repo` flag MUST precede the `handoff` subcommand.
        self.assertEqual(argv[:2], ["--repo", "/wt/13357"])
        self.assertEqual(argv[2:4], ["handoff", "send"])
        self.assertEqual(argv[argv.index("--to") + 1], "claude")
        self.assertEqual(argv[argv.index("--kind") + 1], "implementation_request")
        # The herdr locator target is NOT a %pane -> rides the herdr rail (#13320),
        # where `--target-repo auto` resolves to the sender's own repo root (#13331).
        self.assertEqual(argv[argv.index("--target") + 1], "wC:p3")
        self.assertFalse(argv[argv.index("--target") + 1].startswith("%"))
        self.assertEqual(argv[argv.index("--target-repo") + 1], "auto")
        # Redmine #13485: the herdr worker dispatch pins the explicit lane authority so
        # the route authority resolves the stable `(workspace, lane_label, claude)`
        # identity, not the sender-derived lane. Placed with the target coordinates,
        # before `--mode` (mirrors the gateway dispatch's `--target-lane`).
        self.assertEqual(argv[argv.index("--target-lane") + 1], LANE_LABEL)
        self.assertLess(argv.index("--target-lane"), argv.index("--mode"))
        self.assertEqual(argv[argv.index("--mode") + 1], "queue-enter")
        self.assertEqual(
            argv[argv.index("--role-profile") + 1], "implementation_worker"
        )
        self.assertIn(f"lane={LANE_LABEL}", argv)
        self.assertIn("gateway_callback_target=wC:p2", argv)
        self.assertNotIn("--allow-direct-worker", argv)

    def test_die_style_system_exit_becomes_rc_and_stdout_stays_clean(self):
        def fake_func(args):
            print("inner delivery record body")
            raise SystemExit(2)

        rc, out, err = self._dispatch(fake_func)
        self.assertEqual(rc, 2)
        self.assertNotIn("inner delivery record body", out)
        self.assertIn("inner delivery record body", err)

    def test_system_exit_zero_never_acks(self):
        rc, out, _err = self._dispatch(
            lambda args: (_ for _ in ()).throw(SystemExit(0))
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")


class TurnStartLedgerTests(unittest.TestCase):
    def _observe(self, record):
        ops = HerdrWorkerDispatchOps(Path("/repo"), LANE_LABEL, ISSUE)
        with patch.object(ops, "worker_provider", return_value="claude"), patch(
            "mozyo_bridge.core.state.herdr_delivery_ledger.HerdrDeliveryLedger.records_for_issue",
            return_value=[record],
        ):
            return ops._observe_worker_turn_start(
                "mzb1_ws_claude_lane",
                issue=ISSUE,
                journal="81683",
                worker_locator="w28:p75",
            )

    def test_queue_event_busy_is_causally_started_even_without_provider_projection(self):
        record = SimpleNamespace(
            journal_id="81683",
            receiver="claude",
            provider=None,
            target="w28:p75",
            turn_start_outcome=None,
            queue_enter_observation={"runtime_state": "busy"},
        )
        self.assertEqual(self._observe(record), "started")

    def test_queue_event_awaiting_input_is_delivered_not_started(self):
        record = SimpleNamespace(
            journal_id="81683",
            receiver="claude",
            provider=None,
            target="w28:p75",
            turn_start_outcome=None,
            queue_enter_observation={"runtime_state": "awaiting_input"},
        )
        self.assertEqual(self._observe(record), "delivered_not_started")


class DispatchOutboxAdmissionTests(unittest.TestCase):
    def test_exact_key_is_reserved_once_and_turn_start_marks_delivered(self):
        request = WorkerDispatchRequest(ISSUE, LANE_LABEL, "/repo", "81683")
        ops = HerdrWorkerDispatchOps(Path("/repo"), LANE_LABEL, ISSUE)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"MOZYO_BRIDGE_HOME": tmp}, clear=False
        ), patch(
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution.target_is_retiring",
            return_value=(False, ""),
        ):
            won, _state = ops.reserve_worker_dispatch(
                admission=_healthy_admission(), request=request
            )
            self.assertTrue(won)
            self.assertTrue(
                ops.complete_worker_dispatch(
                    admission=_healthy_admission(),
                    request=request,
                    delivered=True,
                    detail="turn started",
                )
            )
            replay_won, replay_state = ops.reserve_worker_dispatch(
                admission=_healthy_admission(), request=request
            )
        self.assertFalse(replay_won)
        self.assertIn("delivered", replay_state)


class TargetLanePinArgvTests(unittest.TestCase):
    """Redmine #13485: `--target-lane` pins the worker's stable lane identity on the
    herdr rail; the tmux path (no `target_lane`) stays byte-for-byte the prior shape."""

    _BASE = dict(
        issue=ISSUE,
        journal="73381",
        worker_pane="wC:p3",
        lane_label=LANE_LABEL,
        gateway_callback_target="wC:p2",
        target_repo="auto",
    )

    def test_tmux_argv_omits_target_lane_byte_invariant(self):
        # The tmux `LiveWorkerDispatchOps` default (`target_lane=None`) must never emit
        # `--target-lane`: the tmux worker addresses an explicit `%pane` and never rides
        # the herdr lane-derivation rail.
        argv = _worker_dispatch_argv(**self._BASE)
        self.assertNotIn("--target-lane", argv)
        # The exact pre-#13485 tmux shape (also the `repo_root=None` tmux default).
        self.assertEqual(
            argv,
            [
                "handoff", "send",
                "--to", "claude",
                "--source", "redmine",
                "--issue", ISSUE,
                "--journal", "73381",
                "--kind", "implementation_request",
                "--target", "wC:p3",
                "--target-repo", "auto",
                "--mode", "queue-enter",
                "--role-profile", "implementation_worker",
                "--profile-field", f"lane={LANE_LABEL}",
                "--profile-field", "gateway_callback_target=wC:p2",
            ],
        )

    def test_target_lane_pins_explicit_lane_before_mode(self):
        argv = _worker_dispatch_argv(**self._BASE, target_lane=LANE_LABEL)
        self.assertEqual(argv[argv.index("--target-lane") + 1], LANE_LABEL)
        # Grouped with the target coordinates: after `--target-repo`, before `--mode`.
        self.assertLess(argv.index("--target-repo"), argv.index("--target-lane"))
        self.assertLess(argv.index("--target-lane"), argv.index("--mode"))

    def test_empty_target_lane_is_omitted(self):
        # A blank/None lane is never emitted as an empty `--target-lane` token.
        self.assertNotIn("--target-lane", _worker_dispatch_argv(**self._BASE, target_lane=""))
        self.assertNotIn(
            "--target-lane", _worker_dispatch_argv(**self._BASE, target_lane=None)
        )


class ReplayCommandAuthorityTests(unittest.TestCase):
    """Redmine #13485 review F1: the outcome / dry-run `command` (a *replayable* retry
    command) must carry the same stable-lane authority the actual herdr dispatch pins, so
    replaying it re-resolves the stable slot — never the sender-derived lane. tmux
    unchanged."""

    _BASE = dict(
        issue=ISSUE,
        journal="73381",
        worker_pane="wC:p3",
        lane_label=LANE_LABEL,
        gateway_callback_target="wC:p2",
        target_repo="auto",
    )

    def test_tmux_replay_command_carries_no_pins(self):
        # No pins (tmux `LiveWorkerDispatchOps` default) -> byte-for-byte the prior command.
        cmd = _replayable_command(**self._BASE)
        self.assertNotIn("--target-lane", cmd)
        self.assertNotIn("--repo", cmd)
        self.assertTrue(cmd.startswith("mozyo-bridge handoff send "))

    def test_herdr_replay_command_carries_lane_and_repo_pins(self):
        cmd = _replayable_command(
            **self._BASE, target_lane=LANE_LABEL, repo_root="/wt/13485"
        )
        self.assertIn(f"--target-lane {LANE_LABEL}", cmd)
        # The #13397 `--repo` pin precedes the `handoff` subcommand.
        self.assertTrue(cmd.startswith("mozyo-bridge --repo /wt/13485 handoff send "))

    def test_herdr_ops_supplies_lane_and_repo_pins(self):
        ops = HerdrWorkerDispatchOps(
            repo_root=Path("/wt/13485"), lane_label=LANE_LABEL, issue=ISSUE
        )
        pins = ops.command_authority_pins()
        self.assertEqual(pins["target_lane"], LANE_LABEL)
        self.assertEqual(pins["repo_root"], "/wt/13485")

    def test_tmux_ops_has_no_pins_capability(self):
        # The optional capability is absent on the tmux adapter, so the use case reads {}.
        self.assertFalse(
            hasattr(LiveWorkerDispatchOps(repo_root=Path(".")), "command_authority_pins")
        )

    def test_herdr_dry_run_outcome_command_carries_pins_end_to_end(self):
        # The true wiring: use case -> ops.command_authority_pins() -> outcome.command.
        with tempfile.TemporaryDirectory() as tmp:
            fx = _HerdrLaneFixture(tmp)
            request = WorkerDispatchRequest(
                issue=ISSUE,
                lane_label=LANE_LABEL,
                worktree_path=str(fx.worktree),
                journal="73381",
            )
            with patch.dict(
                os.environ, {"MOZYO_BRIDGE_HOME": str(fx.home)}, clear=False
            ):
                fx.stand_up_lane()
                ops = fx.ops()
                with patch.object(
                    ops, "observe_worker_dispatch_admission", return_value=_healthy_admission()
                ):
                    outcome = WorkerDispatchUseCase(
                        ops, worker_ready_probes=0
                    ).run(request, execute=False)
        self.assertIn(f"--target-lane {LANE_LABEL}", outcome.command)
        self.assertIn("--repo", outcome.command)
        # And the replayed command's argv is exactly what the herdr adapter drives.
        self.assertIn("handoff send", outcome.command)


class BackendSelectorTests(unittest.TestCase):
    """`sublane dispatch-worker` picks the herdr adapter only under backend: herdr."""

    @staticmethod
    def _repo(tmp, backend):
        repo = Path(tmp) / f"repo-{backend}"
        repo.mkdir()
        (repo / ".mozyo-bridge").mkdir()
        (repo / ".mozyo-bridge" / "config.yaml").write_text(
            f"version: 1\nterminal_transport:\n  backend: {backend}\n",
            encoding="utf-8",
        )
        return repo

    def _select(self, repo):
        request = WorkerDispatchRequest(
            issue=ISSUE,
            lane_label=LANE_LABEL,
            worktree_path=str(repo),
            journal="73381",
        )
        return _resolve_worker_dispatch_ops(repo_root=repo, request=request)

    def test_herdr_backend_selects_herdr_ops_with_request_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = self._select(self._repo(tmp, "herdr"))
        self.assertIsInstance(ops, HerdrWorkerDispatchOps)
        self.assertEqual(ops.lane_label, LANE_LABEL)
        self.assertEqual(ops.issue, ISSUE)

    def test_tmux_backend_selects_live_ops(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = self._select(self._repo(tmp, "tmux"))
        self.assertIsInstance(ops, LiveWorkerDispatchOps)

    def test_missing_config_defaults_to_tmux(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo-none"
            repo.mkdir()
            ops = self._select(repo)
        self.assertIsInstance(ops, LiveWorkerDispatchOps)


class HerdrUseCaseDriveTests(unittest.TestCase):
    """The pure #12988 use case over the herdr adapter: ACK promotes, failure stays
    fail-closed (`gateway_notified` semantics), with the readiness wait recorded."""

    def _run(self, tmp, *, send_rc):
        fx = _HerdrLaneFixture(tmp)
        request = WorkerDispatchRequest(
            issue=ISSUE,
            lane_label=LANE_LABEL,
            worktree_path=str(fx.worktree),
            journal="73381",
        )
        with patch.dict(
            os.environ, {"MOZYO_BRIDGE_HOME": str(fx.home)}, clear=False
        ):
            fx.stand_up_lane()
            ops = fx.ops()
            use_case = WorkerDispatchUseCase(
                ops, worker_ready_probes=1, sleep=lambda s: None
            )
            with patch.object(
                ops, "observe_worker_dispatch_admission", return_value=_healthy_admission()
            ), patch.object(
                ops, "reserve_worker_dispatch", return_value=(True, "reserved")
            ), patch.object(
                ops, "complete_worker_dispatch", return_value=True
            ), patch.object(
                ops, "_observe_worker_turn_start", return_value="started"
            ), patch(
                "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_dispatcher._drive_worker_send_argv",  # noqa: E501
                return_value=send_rc,
            ) as drive:
                outcome = use_case.run(request, execute=True)
        return outcome, drive

    def test_delivery_ack_promotes_to_worker_dispatched(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome, drive = self._run(tmp, send_rc=0)
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertEqual(outcome.dispatch_result, DISPATCH_WORKER_DISPATCHED)
        self.assertTrue(outcome.worker_dispatch_confirmed)
        self.assertTrue(outcome.worker_ready)
        drive.assert_called_once()
        argv = drive.call_args.args[0]
        # The forward targets the live herdr worker locator on the herdr rail.
        self.assertEqual(argv[argv.index("--target") + 1], outcome.worker_pane)
        self.assertFalse(outcome.worker_pane.startswith("%"))

    def test_failed_send_stays_gateway_notified_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome, _drive = self._run(tmp, send_rc=1)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertEqual(outcome.dispatch_result, WORKER_DISPATCH_DELIVERY_FAILED)
        self.assertFalse(outcome.worker_dispatch_confirmed)
        self.assertIn("gateway_notified", outcome.reason)


class InnerSendBackendPinTests(unittest.TestCase):
    """Redmine #13397: the composed inner send resolves the herdr backend from the
    outer-selected repo, not the driving process's cwd.

    The #13379 j#73722 blocker: an external adopted project carries its
    ``backend: herdr`` selection only at the adopted root (not a committed config
    every checkout inherits, as ``mozyo_bridge`` does), so a worker-dispatch drive
    whose cwd resolved elsewhere re-derived ``backend: tmux`` on the inner
    ``handoff send`` and validated the herdr worker locator as an invalid tmux
    target. The fix pins the top-level ``--repo`` to the ops' own ``repo_root``. This
    exercises the *real* send-path backend predicate against the composed argv from a
    deliberately divergent cwd — hermetic (no live herdr, no tmux).
    """

    @staticmethod
    def _herdr_external_project(tmp: str) -> Path:
        ext = Path(tmp) / "external_project"
        (ext / ".mozyo-bridge").mkdir(parents=True)
        (ext / ".mozyo-bridge" / "config.yaml").write_text(
            "version: 1\nterminal_transport:\n  backend: herdr\n", encoding="utf-8"
        )
        return ext

    def _effective_backend_from_argv(self, argv: list) -> bool:
        from mozyo_bridge.application.cli import build_parser, normalize_paths
        from mozyo_bridge.application.commands_common import repo_root_from_args
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_send_entry import (  # noqa: E501
            herdr_effective_backend_selected,
        )

        ns = normalize_paths(build_parser().parse_args(argv))
        # Redmine #13729: the predicate takes the facade-resolved repo root + target scalar.
        return herdr_effective_backend_selected(
            repo_root=repo_root_from_args(ns), target=getattr(ns, "target", None)
        )

    def test_pinned_repo_resolves_herdr_from_a_divergent_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            ext = self._herdr_external_project(tmp)
            # A divergent cwd that resolves to its OWN (non-herdr) repo root.
            other = Path(tmp) / "other_cwd"
            (other / ".git").mkdir(parents=True)

            seen: list[list] = []

            class FakeParser:
                def parse_args(self, argv):
                    seen.append(list(argv))
                    return Namespace(func=lambda a: 0)

            ops = HerdrWorkerDispatchOps(
                repo_root=ext, lane_label=LANE_LABEL, issue=ISSUE
            )
            out, err = io.StringIO(), io.StringIO()
            with patch(
                "mozyo_bridge.application.cli.build_parser", return_value=FakeParser()
            ), patch(
                "mozyo_bridge.application.cli.normalize_paths", side_effect=lambda a: a
            ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                ops.dispatch_to_worker(
                    issue=ISSUE,
                    journal="73729",
                    worker_pane="wS:p3",
                    lane_label=LANE_LABEL,
                    gateway_callback_target="wS:p2",
                    target_repo="auto",
                )
            argv = seen[0]
            # The pinned --repo is the ops' repo_root (the outer-selected herdr root).
            self.assertEqual(argv[:2], ["--repo", str(ext)])

            # The REAL send-path predicate resolves herdr from that argv even while
            # cwd is the non-herdr `other` dir (would be False without the pin).
            old = os.getcwd()
            try:
                os.chdir(other)
                self.assertTrue(self._effective_backend_from_argv(argv))
                # Guard the harness: the same argv WITHOUT the pin re-derives tmux
                # from this cwd, proving the pin (not the cwd) carries the selection.
                self.assertFalse(self._effective_backend_from_argv(argv[2:]))
            finally:
                os.chdir(old)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
