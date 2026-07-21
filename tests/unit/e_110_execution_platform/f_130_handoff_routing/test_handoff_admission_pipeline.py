"""Fake-port truth table for the handoff admission pipeline (Redmine #13729 tranche 6).

Exercises :class:`AdmissionPipelineUseCase` with a synthetic fake port — no live tmux / herdr /
Redmine / repo-local config — pinning the slice's fixed-order gate truth table and, in particular,
its fail-closed conditions and herdr no-op guards:

- **main-lane guard**: a blocked guard emits ``blocked`` / ``main_lane_implementation_blocked`` and
  ``die``\\ s; a passing guard falls through;
- **receiver binding**: under queue-enter (or ``require_receiver_binding`` in any mode) a target
  that does not ``binds_receiver`` emits ``blocked`` / ``invalid_args`` and ``die``\\ s, with the
  mode-specific ``gate_label``;
- **session binding**: under queue-enter a same-session target passes; a cross-session target is
  admitted only with an explicit ``--target`` + ``--target-repo``, else ``blocked`` /
  ``invalid_args``; an explicit no-op under herdr;
- **cross-workspace ``--to claude``**: a cross-session ``--to claude`` emits ``blocked`` /
  ``cross_session_claude`` with the best-effort gateway diagnostic appended, then ``die``\\ s; an
  explicit no-op under herdr;
- **gateway-route enforcement**: the f_140 gate is called with the request context and the
  herdr-derived ``sender_lane_unit`` (``None`` under tmux);
- **``--target-repo`` identity gate**: a ``None`` observed root and an identity mismatch both emit
  ``blocked`` / ``target_repo_mismatch`` and ``die`` (with their distinct setup hints); a
  Unicode-normalized match passes;
- **project-scope gate**: a missing ``--target-repo`` emits ``invalid_args``; a stamped-scope
  cwd-under-project trust path and the re-derived ``project_scope_for_cwd`` fallback both feed the
  ``expected_project`` comparison; a mismatch (or a discovery error, fail-closed) emits ``blocked``
  / ``target_project_mismatch``;
- **standard_target_admission**: under queue-enter to an inactive split, an admitted pane +
  activate policy **plans** the activation (``activate_inactive_target`` True; no ``select-pane``
  here); a non-admitted pane or a disabled policy emits ``blocked`` / ``invalid_args`` with the
  recovery command;
- **foreground binding**: under queue-enter a non-agent process emits ``blocked`` /
  ``target_not_agent`` and ``die``\\ s; otherwise the generic ``ensure_agent_target`` gate runs and
  its ``SystemExit`` is re-raised after a ``target_not_agent`` emit;
- the result threads the resolved ``admission_policy`` and the ``activate_inactive_target`` plan.

The live composition (``run_admission_pipeline`` over ``LiveAdmissionPipelineOps``, routing the
``current_session_name`` / ``ensure_agent_target`` seams through ``commands``) is covered end-to-end
by the ``orchestrate_handoff`` handoff-routing integration tests.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.project_scope import (
    ProjectScope,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    PreflightTarget,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_admission_pipeline import (
    AdmissionPipelineOps,
    AdmissionPipelineRequest,
    AdmissionPipelineResult,
    AdmissionPipelineUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    DeliveryOutcome,
    RedmineAnchor,
    StandardTargetAdmissionPolicy,
)


_ANCHOR = RedmineAnchor(issue="13729", journal="83990")


def _preflight(*, role: str = "codex", ambiguous: bool = False) -> PreflightTarget:
    """A canonical projection that (by default) strongly, non-ambiguously binds ``role``."""
    return PreflightTarget(
        pane_id="%pT",
        role=role,
        role_source="pane_option",
        confidence="strong",
        ambiguous=ambiguous,
        view_kind="cockpit_pane",
        workspace_id="ws-1",
        lane_id="default",
        window_name=role,
        pane_option_role=role,
    )


def _scope(scope: str, path: str) -> ProjectScope:
    """A minimal :class:`ProjectScope`; the slice only reads ``.scope`` / ``.path``."""
    return ProjectScope(
        scope=scope,
        path=path,
        label=scope,
        workdir=path,
        parent_workspace=None,
        source="project.yaml",
        fingerprint="fp",
    )


def _target_info(**overrides: str) -> Dict[str, str]:
    info: Dict[str, str] = {
        "id": "%884",
        "location": "sess:1.0",
        "command": "codex",
        "cwd": "/repo",
        "window_name": "codex",
        "pane_active": "1",
        "workspace_id": "ws-1",
        "lane_id": "default",
    }
    info.update(overrides)
    return info


class _FakeDie(Exception):
    """Stand-in for ``commands.die`` — raises so the use case's control flow terminates."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class _EmitCall:
    outcome: DeliveryOutcome
    record_format: str
    command: Optional[str]
    recovery_command: Optional[str]


@dataclass
class _GatewayCall:
    kwargs: Dict[str, Any]


@dataclass
class _FakeOps:
    """A typed fake :class:`AdmissionPipelineOps` recording the side-effect calls in order.

    Every effect is configurable so a single test drives one branch: ``session_name`` is what the
    two ``current_session_name`` reads return; ``guard_blocked`` toggles the main-lane guard;
    ``gateway_raises`` lets the gateway gate simulate a block (the real gate emits + dies, so a
    ``_FakeDie`` here stands in); ``workspace_root`` / ``project_scope`` drive the identity gates;
    ``diagnostic`` is the cross-session hint; ``agent_raises`` makes the generic agent gate raise
    ``SystemExit``.
    """

    session_name: Optional[str] = "sess"
    guard_blocked: bool = False
    gateway_raises: bool = False
    workspace_root: Optional[str] = "/repo"
    project_scope: Optional[ProjectScope] = None
    project_scope_raises: bool = False
    diagnostic: str = ""
    agent_raises: bool = False

    emits: List[_EmitCall] = field(default_factory=list)
    dies: List[str] = field(default_factory=list)
    gateway_calls: List[_GatewayCall] = field(default_factory=list)
    session_name_calls: int = 0
    guard_calls: List[Dict[str, Any]] = field(default_factory=list)
    workspace_root_calls: List[str] = field(default_factory=list)
    project_scope_calls: List[Tuple[str, str]] = field(default_factory=list)
    diagnostic_calls: List[str] = field(default_factory=list)
    ensure_calls: List[Tuple[Dict[str, str], str, bool]] = field(default_factory=list)

    def current_session_name(self) -> Optional[str]:
        self.session_name_calls += 1
        return self.session_name

    def main_lane_guard_blocked(
        self,
        *,
        receiver: str,
        kind: Optional[str],
        preflight_target: PreflightTarget,
        has_main_lane_exception: bool,
    ) -> bool:
        self.guard_calls.append(
            {
                "receiver": receiver,
                "kind": kind,
                "preflight_target": preflight_target,
                "has_main_lane_exception": has_main_lane_exception,
            }
        )
        return self.guard_blocked

    def enforce_gateway_route(
        self,
        *,
        kind: Optional[str],
        receiver: str,
        preflight_target: PreflightTarget,
        source: str,
        mode: str,
        anchor: Any,
        target: str,
        record_format: str,
        record_command: Optional[str],
        allow_direct_worker: bool,
        sender_lane_unit: Optional[Tuple[Optional[str], Optional[str]]],
    ) -> None:
        self.gateway_calls.append(
            _GatewayCall(
                kwargs={
                    "kind": kind,
                    "receiver": receiver,
                    "preflight_target": preflight_target,
                    "source": source,
                    "mode": mode,
                    "anchor": anchor,
                    "target": target,
                    "record_format": record_format,
                    "record_command": record_command,
                    "allow_direct_worker": allow_direct_worker,
                    "sender_lane_unit": sender_lane_unit,
                }
            )
        )
        if self.gateway_raises:
            raise _FakeDie("gateway_route_blocked")

    def resolve_workspace_root(self, cwd: str) -> Optional[str]:
        self.workspace_root_calls.append(cwd)
        return self.workspace_root

    def project_scope_for_cwd(self, cwd: str, git_root: str) -> Optional[ProjectScope]:
        self.project_scope_calls.append((cwd, git_root))
        if self.project_scope_raises:
            raise RuntimeError("discovery blew up")
        return self.project_scope

    def cross_session_gateway_diagnostic(self, target_session: str) -> str:
        self.diagnostic_calls.append(target_session)
        return self.diagnostic

    def ensure_agent_target(
        self, target_info: Dict[str, str], receiver: str, *, force: bool
    ) -> None:
        self.ensure_calls.append((target_info, receiver, force))
        if self.agent_raises:
            raise SystemExit(1)

    def emit(
        self,
        outcome: DeliveryOutcome,
        *,
        record_format: str,
        command: Optional[str],
        recovery_command: Optional[str] = None,
    ) -> None:
        self.emits.append(
            _EmitCall(
                outcome=outcome,
                record_format=record_format,
                command=command,
                recovery_command=recovery_command,
            )
        )

    def die(self, message: str) -> None:
        self.dies.append(message)
        raise _FakeDie(message)


# Structural conformance: a fake that fails to implement the port fails the typed island (review
# j#79040 F1'). The assignment is a real gate against fake signature drift.
_conformance: AdmissionPipelineOps = _FakeOps()


def _request(**overrides: Any) -> AdmissionPipelineRequest:
    """A happy-path standard send that passes every gate (each test overrides one axis)."""
    base: Dict[str, Any] = dict(
        receiver="codex",
        kind="review_result",
        mode="standard",
        source="redmine",
        anchor=_ANCHOR,
        target="%884",
        target_info=_target_info(),
        preflight_target=_preflight(role="codex"),
        herdr_send=False,
        resolved_target_repo=None,
        record_format="both",
        record_command=None,
        raw_target="%884",
        require_receiver_binding=False,
        has_main_lane_exception=False,
        allow_direct_worker=False,
        target_project=None,
        no_target_activation=False,
        restore_previous_active=False,
        force=False,
    )
    base.update(overrides)
    return AdmissionPipelineRequest(**base)


def _run(ops: _FakeOps, request: AdmissionPipelineRequest) -> AdmissionPipelineResult:
    return AdmissionPipelineUseCase(ops).execute(request)


class HappyPathTest(unittest.TestCase):
    def test_standard_send_passes_every_gate(self) -> None:
        ops = _FakeOps()
        result = _run(ops, _request())
        self.assertEqual(ops.emits, [])
        self.assertEqual(ops.dies, [])
        self.assertFalse(result.activate_inactive_target)
        self.assertIsInstance(result.admission_policy, StandardTargetAdmissionPolicy)
        # standard rail runs the generic agent gate, not the queue-enter foreground check.
        self.assertEqual(len(ops.ensure_calls), 1)
        # gateway gate runs with sender_lane_unit=None (tmux).
        self.assertEqual(len(ops.gateway_calls), 1)
        self.assertIsNone(ops.gateway_calls[0].kwargs["sender_lane_unit"])


class MainLaneGuardTest(unittest.TestCase):
    def test_blocked_guard_emits_and_dies(self) -> None:
        ops = _FakeOps(guard_blocked=True)
        with self.assertRaises(_FakeDie):
            _run(ops, _request(receiver="claude", kind="implementation_request"))
        self.assertEqual(len(ops.emits), 1)
        self.assertEqual(ops.emits[0].outcome.status, "blocked")
        self.assertEqual(ops.emits[0].outcome.reason, "main_lane_implementation_blocked")
        self.assertIn("default/main lane", ops.dies[0])
        # nothing past the first gate ran.
        self.assertEqual(ops.gateway_calls, [])

    def test_passing_guard_threads_context(self) -> None:
        ops = _FakeOps()
        _run(ops, _request(has_main_lane_exception=True))
        self.assertEqual(len(ops.guard_calls), 1)
        self.assertTrue(ops.guard_calls[0]["has_main_lane_exception"])


class ReceiverBindingTest(unittest.TestCase):
    def test_queue_enter_mismatch_blocks_invalid_args(self) -> None:
        ops = _FakeOps()
        # preflight binds codex, but the receiver is claude -> not bound.
        with self.assertRaises(_FakeDie):
            _run(ops, _request(mode="queue-enter", receiver="claude"))
        self.assertEqual(ops.emits[-1].outcome.reason, "invalid_args")
        self.assertIn("--mode queue-enter requires the explicit --target", ops.dies[-1])

    def test_require_receiver_binding_standard_mode_uses_generic_label(self) -> None:
        ops = _FakeOps()
        with self.assertRaises(_FakeDie):
            _run(ops, _request(mode="standard", receiver="claude", require_receiver_binding=True))
        self.assertIn("this handoff primitive requires the explicit --target", ops.dies[-1])

    def test_queue_enter_bound_receiver_passes(self) -> None:
        ops = _FakeOps()
        # receiver == preflight role and same tmux session + active agent pane.
        result = _run(ops, _request(mode="queue-enter", receiver="codex"))
        self.assertFalse(result.activate_inactive_target)
        self.assertEqual(ops.dies, [])


class SessionBindingTest(unittest.TestCase):
    def test_cross_session_not_admitted_blocks(self) -> None:
        ops = _FakeOps(session_name="sender")  # target lives in "sess"
        with self.assertRaises(_FakeDie):
            _run(
                ops,
                _request(mode="queue-enter", raw_target=None, resolved_target_repo=None),
            )
        self.assertEqual(ops.emits[-1].outcome.reason, "invalid_args")
        self.assertIn("requires the target pane to live in the", ops.dies[-1])

    def test_cross_session_admitted_with_explicit_target_and_repo(self) -> None:
        ops = _FakeOps(session_name="sender", workspace_root="/repo")
        # cross-session admitted -> continues; repo gate matches -> passes.
        result = _run(
            ops,
            _request(
                mode="queue-enter",
                raw_target="%884",
                resolved_target_repo="/repo",
                target_info=_target_info(cwd="/repo"),
            ),
        )
        self.assertEqual(ops.dies, [])
        self.assertFalse(result.activate_inactive_target)

    def test_herdr_send_skips_session_binding(self) -> None:
        ops = _FakeOps()
        _run(ops, _request(mode="queue-enter", herdr_send=True))
        # herdr no-op: current_session_name is never read (neither the session gate nor the
        # cross-session gate call it under herdr).
        self.assertEqual(ops.session_name_calls, 0)


class CrossWorkspaceClaudeTest(unittest.TestCase):
    def test_cross_session_claude_blocks_with_diagnostic(self) -> None:
        ops = _FakeOps(session_name="sender", diagnostic="try %codex")
        with self.assertRaises(_FakeDie):
            _run(
                ops,
                _request(
                    mode="standard",
                    receiver="claude",
                    preflight_target=_preflight(role="claude"),
                    target_info=_target_info(location="sess:1.0", command="claude", window_name="claude"),
                ),
            )
        self.assertEqual(ops.emits[-1].outcome.reason, "cross_session_claude")
        self.assertIn("cross-session handoff to Claude is not allowed", ops.dies[-1])
        # the best-effort diagnostic hint is appended to the boundary message.
        self.assertIn("try %codex", ops.dies[-1])
        self.assertEqual(ops.diagnostic_calls, ["sess"])

    def test_herdr_send_skips_cross_session_claude(self) -> None:
        ops = _FakeOps(session_name="sender")
        # under herdr sender_session_xw is empty -> gate is a no-op even for --to claude.
        _run(
            ops,
            _request(
                mode="standard",
                herdr_send=True,
                receiver="claude",
                preflight_target=_preflight(role="claude"),
                target_info=_target_info(command="claude", window_name="claude"),
            ),
        )
        self.assertEqual(ops.diagnostic_calls, [])
        self.assertEqual(ops.dies, [])


class GatewayRouteTest(unittest.TestCase):
    def test_gateway_gate_gets_herdr_sender_lane_unit(self) -> None:
        ops = _FakeOps()
        _run(
            ops,
            _request(
                herdr_send=True,
                mode="standard",
                target_info=_target_info(
                    herdr_sender_workspace_id="ws-9",
                    herdr_sender_lane_id="lane-9",
                    command="codex",
                ),
            ),
        )
        self.assertEqual(
            ops.gateway_calls[0].kwargs["sender_lane_unit"], ("ws-9", "lane-9")
        )

    def test_gateway_block_terminates(self) -> None:
        ops = _FakeOps(gateway_raises=True)
        with self.assertRaises(_FakeDie):
            _run(ops, _request())
        # the gateway gate is the terminal; no later gate ran.
        self.assertEqual(ops.ensure_calls, [])


class TargetRepoGateTest(unittest.TestCase):
    def test_identity_mismatch_blocks(self) -> None:
        ops = _FakeOps(workspace_root="/other/repo")
        with self.assertRaises(_FakeDie):
            _run(ops, _request(resolved_target_repo="/repo", target_info=_target_info(cwd="/repo")))
        self.assertEqual(ops.emits[-1].outcome.reason, "target_repo_mismatch")
        self.assertIn("target pane resolves to repo root", ops.dies[-1])

    def test_unresolvable_identity_blocks_with_scaffold_hint(self) -> None:
        ops = _FakeOps(workspace_root=None)
        with self.assertRaises(_FakeDie):
            _run(ops, _request(resolved_target_repo="/repo", target_info=_target_info(cwd="/nowhere")))
        self.assertEqual(ops.emits[-1].outcome.reason, "target_repo_mismatch")
        self.assertIn("has no identity marker reachable", ops.dies[-1])

    def test_matching_identity_passes(self) -> None:
        ops = _FakeOps(workspace_root="/repo")
        result = _run(
            ops, _request(resolved_target_repo="/repo", target_info=_target_info(cwd="/repo"))
        )
        self.assertEqual(ops.dies, [])
        self.assertIsInstance(result.admission_policy, StandardTargetAdmissionPolicy)


class ProjectScopeGateTest(unittest.TestCase):
    def test_project_without_repo_gate_blocks_invalid_args(self) -> None:
        ops = _FakeOps()
        with self.assertRaises(_FakeDie):
            _run(ops, _request(target_project="proj-a", resolved_target_repo=None))
        self.assertEqual(ops.emits[-1].outcome.reason, "invalid_args")
        self.assertIn("`--target-project` requires an explicit `--target-repo`", ops.dies[-1])

    def test_scope_mismatch_blocks(self) -> None:
        ops = _FakeOps(
            workspace_root="/repo",
            project_scope=_scope("proj-b", "proj-b"),
        )
        with self.assertRaises(_FakeDie):
            _run(
                ops,
                _request(
                    target_project="proj-a",
                    resolved_target_repo="/repo",
                    target_info=_target_info(cwd="/repo/proj-b"),
                ),
            )
        self.assertEqual(ops.emits[-1].outcome.reason, "target_project_mismatch")

    def test_scope_match_passes(self) -> None:
        ops = _FakeOps(
            workspace_root="/repo",
            project_scope=_scope("proj-a", "proj-a"),
        )
        result = _run(
            ops,
            _request(
                target_project="proj-a",
                resolved_target_repo="/repo",
                target_info=_target_info(cwd="/repo/proj-a"),
            ),
        )
        self.assertEqual(ops.dies, [])
        self.assertIsInstance(result.admission_policy, StandardTargetAdmissionPolicy)

    def test_discovery_error_fails_closed(self) -> None:
        ops = _FakeOps(workspace_root="/repo", project_scope_raises=True)
        with self.assertRaises(_FakeDie):
            _run(
                ops,
                _request(
                    target_project="proj-a",
                    resolved_target_repo="/repo",
                    target_info=_target_info(cwd="/repo/proj-a"),
                ),
            )
        # observed_scope stays None (!= expected) -> fail closed.
        self.assertEqual(ops.emits[-1].outcome.reason, "target_project_mismatch")

    def test_stamped_scope_trusted_when_cwd_under_project(self) -> None:
        # A stamped pane-option scope whose cwd is under the stamped path is trusted directly,
        # so the live project.yaml discovery is never consulted.
        ops = _FakeOps(workspace_root="/repo")
        result = _run(
            ops,
            _request(
                target_project="proj-a",
                resolved_target_repo="/repo",
                target_info=_target_info(
                    cwd="/repo/proj-a",
                    project_scope="proj-a",
                    project_path="proj-a",
                ),
            ),
        )
        self.assertEqual(ops.dies, [])
        self.assertEqual(ops.project_scope_calls, [])
        self.assertIsInstance(result.admission_policy, StandardTargetAdmissionPolicy)


class StandardTargetAdmissionTest(unittest.TestCase):
    def test_inactive_admitted_plans_activation(self) -> None:
        ops = _FakeOps()
        result = _run(
            ops,
            _request(
                mode="queue-enter",
                receiver="codex",
                target_info=_target_info(pane_active="0", workspace_id="ws-1"),
            ),
        )
        self.assertTrue(result.activate_inactive_target)
        # planned only: no die, and the policy is the default (activate_inactive True).
        self.assertEqual(ops.dies, [])
        self.assertTrue(result.admission_policy.activate_inactive)

    def test_inactive_not_admitted_blocks_with_recovery(self) -> None:
        ops = _FakeOps()
        with self.assertRaises(_FakeDie):
            _run(
                ops,
                _request(
                    mode="queue-enter",
                    receiver="codex",
                    # missing workspace_id -> admission not met.
                    target_info=_target_info(pane_active="0", workspace_id=""),
                ),
            )
        self.assertEqual(ops.emits[-1].outcome.reason, "invalid_args")
        self.assertIsNotNone(ops.emits[-1].recovery_command)
        self.assertIn("standard_target_admission did not admit", ops.dies[-1])

    def test_inactive_activation_disabled_blocks(self) -> None:
        ops = _FakeOps()
        with self.assertRaises(_FakeDie):
            _run(
                ops,
                _request(
                    mode="queue-enter",
                    receiver="codex",
                    no_target_activation=True,
                    target_info=_target_info(pane_active="0", workspace_id="ws-1"),
                ),
            )
        self.assertEqual(ops.emits[-1].outcome.reason, "invalid_args")
        self.assertIn("activation is disabled by policy", ops.dies[-1])

    def test_active_pane_no_activation_plan(self) -> None:
        ops = _FakeOps()
        result = _run(
            ops,
            _request(
                mode="queue-enter",
                receiver="codex",
                target_info=_target_info(pane_active="1"),
            ),
        )
        self.assertFalse(result.activate_inactive_target)


class ForegroundBindingTest(unittest.TestCase):
    def test_queue_enter_non_agent_blocks(self) -> None:
        ops = _FakeOps()
        with self.assertRaises(_FakeDie):
            _run(
                ops,
                _request(
                    mode="queue-enter",
                    receiver="codex",
                    target_info=_target_info(command="bash"),
                ),
            )
        self.assertEqual(ops.emits[-1].outcome.reason, "target_not_agent")
        self.assertIn("requires the foreground process to match", ops.dies[-1])

    def test_queue_enter_agent_passes(self) -> None:
        ops = _FakeOps()
        result = _run(
            ops,
            _request(mode="queue-enter", receiver="codex", target_info=_target_info(command="codex")),
        )
        self.assertEqual(ops.dies, [])
        # queue-enter uses the strict foreground check, never the generic agent gate.
        self.assertEqual(ops.ensure_calls, [])
        self.assertIsInstance(result.admission_policy, StandardTargetAdmissionPolicy)

    def test_standard_agent_gate_reraises_systemexit(self) -> None:
        ops = _FakeOps(agent_raises=True)
        with self.assertRaises(SystemExit):
            _run(ops, _request(mode="standard"))
        # the SystemExit is re-raised after a target_not_agent emit.
        self.assertEqual(ops.emits[-1].outcome.reason, "target_not_agent")

    def test_standard_agent_gate_passes(self) -> None:
        ops = _FakeOps()
        _run(ops, _request(mode="standard", force=True))
        self.assertEqual(len(ops.ensure_calls), 1)
        self.assertTrue(ops.ensure_calls[0][2])  # force threaded through


class ResultTest(unittest.TestCase):
    def test_result_threads_policy_overrides(self) -> None:
        ops = _FakeOps()
        result = _run(ops, _request(restore_previous_active=True))
        self.assertTrue(result.admission_policy.restore_previous_active)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
