"""Fake-port truth table for the handoff target-resolution preflight (Redmine #13729 tranche 5).

Exercises :class:`TargetResolutionUseCase` with a synthetic fake port — no live tmux / herdr /
Redmine — pinning the slice's resolution truth table and, in particular, its fail-closed policy
conditions:

- **herdr resolution** succeeds against the injected inventory record, or fails closed: a
  :class:`HerdrSendEntryError` with ``reason == "invalid_args"`` emits ``blocked`` /
  ``invalid_args`` (#13884), any other reason emits ``blocked`` / ``target_unavailable``, and both
  ``die`` — never a silent tmux fallback;
- **tmux resolution** succeeds against ``pane_info``, or on a resolver ``SystemExit`` emits
  ``blocked`` / ``target_unavailable``, fires the best-effort ``<session>:codex`` gateway
  diagnostic, and re-raises the ``SystemExit`` (the original resolver failure is unchanged);
- **same-lane duplicate diagnostics** thread the resolved rows through onto the result under tmux,
  and are an explicit no-op (empty) under the herdr backend;
- **``--target-repo auto``** resolves to the sender's own repo root under herdr; under tmux it
  requires an explicit ``%pane`` (else ``blocked`` / ``invalid_args``) and a cwd that reaches a
  workspace/repo marker (else ``blocked`` / ``target_repo_mismatch``), and on success prints the
  audit diagnostic and hands the concrete root back; a non-auto ``--target-repo`` value passes
  through untouched;
- the canonical ``preflight_target`` projection is threaded onto the result.

The live composition (``run_target_resolution`` over ``LiveTargetResolutionOps``, routing every
effect through the ``commands`` module) is covered end-to-end by the ``orchestrate_handoff``
handoff-routing integration tests.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    PreflightTarget,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_target_resolution import (
    TargetResolutionOps,
    TargetResolutionRequest,
    TargetResolutionResult,
    TargetResolutionUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    AsanaAnchor,
    DeliveryOutcome,
    NormalizedAnchor,
    RedmineAnchor,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_send_entry import (
    HerdrSendEntryError,
)

_AUTO = "auto"


def _preflight(role: str = "claude") -> PreflightTarget:
    """A minimal canonical projection; the slice only threads it, so the fields are placeholders."""
    return PreflightTarget(
        pane_id="%pT",
        role=role,
        role_source="pane_option",
        confidence="strong",
        ambiguous=False,
        view_kind="cockpit_pane",
        workspace_id="ws-1",
        lane_id="default",
        window_name="claude",
        pane_option_role=role,
    )


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


@dataclass
class _FakeOps:
    """A typed fake :class:`TargetResolutionOps` recording the side-effect calls in order.

    The result-shaping inputs (the herdr / tmux resolution outcome, the duplicate rows, the auto
    repo roots, the projection) are set by the caller so a single fake drives every truth-table
    cell without live tmux / herdr.
    """

    #: herdr resolution: the synthesized record, or an error to raise.
    herdr_target_info: Optional[Dict[str, str]] = None
    herdr_error: Optional[HerdrSendEntryError] = None
    #: tmux resolution: the pane record, or ``None`` to raise ``SystemExit``.
    pane_info_result: Optional[Dict[str, str]] = None
    duplicate_rows: List[str] = field(default_factory=list)
    herdr_auto_repo: str = "/repo/herdr-root"
    workspace_root: Optional[str] = "/repo/tmux-root"
    projection: PreflightTarget = field(default_factory=_preflight)

    events: List[str] = field(default_factory=list)
    emitted: List[_EmitCall] = field(default_factory=list)
    died: List[str] = field(default_factory=list)
    codex_diag: List[str] = field(default_factory=list)
    dup_calls: List[tuple] = field(default_factory=list)
    auto_diag: List[tuple] = field(default_factory=list)
    workspace_root_calls: List[str] = field(default_factory=list)

    def resolve_herdr_send_target(
        self,
        *,
        repo_root: Path,
        target: Optional[str],
        target_repo: Optional[str],
        target_lane: Optional[str],
        receiver: str,
    ) -> Dict[str, str]:
        self.events.append("herdr_resolve")
        if self.herdr_error is not None:
            raise self.herdr_error
        assert self.herdr_target_info is not None
        return self.herdr_target_info

    def pane_info(self, target_arg: str) -> Dict[str, str]:
        self.events.append("pane_info")
        if self.pane_info_result is None:
            raise SystemExit(f"unresolved: {target_arg}")
        return self.pane_info_result

    def emit_codex_diagnostic(self, target_arg: str) -> None:
        self.events.append("codex_diag")
        self.codex_diag.append(target_arg)

    def resolve_duplicate_lane_panes(
        self, target_info: Dict[str, str], receiver: str
    ) -> List[str]:
        self.events.append("duplicates")
        self.dup_calls.append((target_info, receiver))
        return list(self.duplicate_rows)

    def herdr_auto_target_repo(self, repo_root: Path) -> str:
        self.events.append("herdr_auto")
        return self.herdr_auto_repo

    def resolve_workspace_root(self, cwd: str) -> Optional[str]:
        self.events.append("workspace_root")
        self.workspace_root_calls.append(cwd)
        return self.workspace_root

    def print_auto_repo_diagnostic(self, *, target: str, cwd: str, root: str) -> None:
        self.events.append("auto_diag")
        self.auto_diag.append((target, cwd, root))

    def project_preflight_target(self, target_info: Dict[str, str]) -> PreflightTarget:
        self.events.append("project")
        return self.projection

    def emit(
        self,
        outcome: DeliveryOutcome,
        *,
        record_format: str,
        command: Optional[str],
    ) -> None:
        self.events.append("emit")
        self.emitted.append(_EmitCall(outcome, record_format, command))

    def die(self, message: str) -> None:
        self.events.append("die")
        self.died.append(message)
        raise _FakeDie(message)


# Structural-conformance gate (mypy island, review j#79040 F1' precedent): assigning the fake to
# the port type makes any fake signature drift a STATIC error, not a silent runtime-only skip.
_PORT_CONFORMS: TargetResolutionOps = _FakeOps()


def _request(
    *,
    herdr_send: bool,
    target: Optional[str] = "%pT",
    resolved_target_repo: Optional[str] = None,
    anchor: Optional[NormalizedAnchor] = None,
) -> TargetResolutionRequest:
    return TargetResolutionRequest(
        repo_root=Path("/repo"),
        target=target,
        target_repo=resolved_target_repo,
        target_lane=None,
        receiver="claude",
        anchor=anchor,
        mode="queue-enter",
        kind="implementation_request",
        source="redmine",
        record_format="both",
        record_command=None,
        resolved_target_repo=resolved_target_repo,
        herdr_send=herdr_send,
    )


def _run(
    ops: _FakeOps, request: TargetResolutionRequest
) -> tuple[Optional[TargetResolutionResult], Optional[BaseException]]:
    result: Optional[TargetResolutionResult] = None
    raised: Optional[BaseException] = None
    try:
        result = TargetResolutionUseCase(ops).execute(request)
    except (_FakeDie, SystemExit) as exc:
        raised = exc
    return result, raised


class HerdrResolutionTest(unittest.TestCase):
    def test_herdr_success_resolves_target_no_duplicates_and_projects(self) -> None:
        ops = _FakeOps(herdr_target_info={"id": "herdr:claude:live"})
        result, raised = _run(ops, _request(herdr_send=True))
        self.assertIsNone(raised)
        assert result is not None
        # herdr resolve -> project; the same-lane duplicate diagnostic is a no-op under herdr.
        self.assertEqual(ops.events, ["herdr_resolve", "project"])
        self.assertEqual(result.target, "herdr:claude:live")
        self.assertEqual(result.target_info, {"id": "herdr:claude:live"})
        self.assertEqual(result.duplicate_lane_panes, [])
        self.assertIs(result.preflight_target, ops.projection)
        self.assertEqual(ops.dup_calls, [])
        self.assertEqual(ops.emitted, [])

    def test_herdr_error_target_unavailable_emits_and_dies(self) -> None:
        ops = _FakeOps(herdr_error=HerdrSendEntryError("no live agent", reason="no_single_agent"))
        result, raised = _run(ops, _request(herdr_send=True))
        self.assertIsNone(result)
        self.assertIsInstance(raised, _FakeDie)
        self.assertEqual(ops.events, ["herdr_resolve", "emit", "die"])
        self.assertEqual(ops.emitted[0].outcome.status, "blocked")
        self.assertEqual(ops.emitted[0].outcome.reason, "target_unavailable")
        self.assertIsNone(ops.emitted[0].outcome.target)
        self.assertEqual(ops.died, ["no live agent"])

    def test_herdr_error_invalid_args_maps_to_invalid_args_reason(self) -> None:
        # Redmine #13884: an inconsistent `--target` argument projects onto `invalid_args`, not
        # `target_unavailable`.
        ops = _FakeOps(herdr_error=HerdrSendEntryError("bad --target", reason="invalid_args"))
        result, raised = _run(ops, _request(herdr_send=True))
        self.assertIsNone(result)
        self.assertIsInstance(raised, _FakeDie)
        self.assertEqual(ops.emitted[0].outcome.reason, "invalid_args")
        self.assertEqual(ops.died, ["bad --target"])


class TmuxResolutionTest(unittest.TestCase):
    def test_tmux_success_resolves_and_threads_duplicates(self) -> None:
        ops = _FakeOps(
            pane_info_result={"id": "%14", "cwd": "/repo/work"},
            duplicate_rows=["%16 duplicate-row"],
        )
        result, raised = _run(ops, _request(herdr_send=False))
        self.assertIsNone(raised)
        assert result is not None
        self.assertEqual(ops.events, ["pane_info", "duplicates", "project"])
        self.assertEqual(result.target, "%14")
        self.assertEqual(result.duplicate_lane_panes, ["%16 duplicate-row"])
        self.assertEqual(ops.dup_calls, [({"id": "%14", "cwd": "/repo/work"}, "claude")])

    def test_tmux_resolver_systemexit_emits_diagnostic_and_reraises(self) -> None:
        ops = _FakeOps(pane_info_result=None)  # -> raises SystemExit
        result, raised = _run(
            ops, _request(herdr_send=False, target="mycockpit:codex")
        )
        self.assertIsNone(result)
        self.assertIsInstance(raised, SystemExit)
        # emit blocked -> best-effort codex diagnostic -> re-raise (no die, no duplicates, no project).
        self.assertEqual(ops.events, ["pane_info", "emit", "codex_diag"])
        self.assertEqual(ops.emitted[0].outcome.status, "blocked")
        self.assertEqual(ops.emitted[0].outcome.reason, "target_unavailable")
        self.assertIsNone(ops.emitted[0].outcome.target)
        self.assertEqual(ops.codex_diag, ["mycockpit:codex"])
        self.assertEqual(ops.died, [])

    def test_tmux_implicit_target_uses_receiver_as_target_arg(self) -> None:
        ops = _FakeOps(pane_info_result=None)  # -> raises SystemExit
        result, raised = _run(ops, _request(herdr_send=False, target=None))
        self.assertIsInstance(raised, SystemExit)
        # target_arg fell back to the receiver; the diagnostic sees that literal.
        self.assertEqual(ops.codex_diag, ["claude"])


class TargetRepoAutoTest(unittest.TestCase):
    def test_non_auto_target_repo_passes_through_untouched(self) -> None:
        ops = _FakeOps(pane_info_result={"id": "%14"})
        result, raised = _run(
            ops, _request(herdr_send=False, resolved_target_repo="/explicit/root")
        )
        self.assertIsNone(raised)
        assert result is not None
        self.assertEqual(result.resolved_target_repo, "/explicit/root")
        self.assertNotIn("workspace_root", ops.events)
        self.assertNotIn("herdr_auto", ops.events)

    def test_auto_under_herdr_resolves_to_sender_repo_root(self) -> None:
        ops = _FakeOps(
            herdr_target_info={"id": "herdr:claude:live"},
            herdr_auto_repo="/repo/herdr-root",
        )
        result, raised = _run(
            ops, _request(herdr_send=True, resolved_target_repo=_AUTO)
        )
        self.assertIsNone(raised)
        assert result is not None
        self.assertEqual(result.resolved_target_repo, "/repo/herdr-root")
        self.assertIn("herdr_auto", ops.events)
        self.assertNotIn("workspace_root", ops.events)

    def test_auto_under_tmux_non_explicit_pane_blocks_invalid_args(self) -> None:
        ops = _FakeOps(pane_info_result={"id": "w1", "cwd": "/repo/work"})
        result, raised = _run(
            ops,
            _request(
                herdr_send=False, target="mycockpit:claude", resolved_target_repo=_AUTO
            ),
        )
        self.assertIsNone(result)
        self.assertIsInstance(raised, _FakeDie)
        self.assertEqual(ops.emitted[0].outcome.status, "blocked")
        self.assertEqual(ops.emitted[0].outcome.reason, "invalid_args")
        # Blocked outcome carries the resolved locator (not None) at this stage.
        self.assertEqual(ops.emitted[0].outcome.target, "w1")
        self.assertNotIn("workspace_root", ops.events)
        assert isinstance(raised, _FakeDie)
        self.assertIn("requires an explicit `%pane` target", raised.message)

    def test_auto_under_tmux_no_workspace_root_blocks_target_repo_mismatch(self) -> None:
        ops = _FakeOps(
            pane_info_result={"id": "%14", "cwd": "/nowhere"}, workspace_root=None
        )
        result, raised = _run(
            ops, _request(herdr_send=False, resolved_target_repo=_AUTO)
        )
        self.assertIsNone(result)
        self.assertIsInstance(raised, _FakeDie)
        self.assertEqual(ops.emitted[0].outcome.reason, "target_repo_mismatch")
        self.assertEqual(ops.emitted[0].outcome.target, "%14")
        self.assertEqual(ops.workspace_root_calls, ["/nowhere"])
        self.assertEqual(ops.auto_diag, [])
        assert isinstance(raised, _FakeDie)
        self.assertIn("could not infer a workspace/repo root", raised.message)

    def test_auto_under_tmux_resolves_root_prints_diagnostic_and_hands_back(self) -> None:
        ops = _FakeOps(
            pane_info_result={"id": "%14", "cwd": "/repo/work"},
            workspace_root="/repo",
        )
        result, raised = _run(
            ops, _request(herdr_send=False, resolved_target_repo=_AUTO)
        )
        self.assertIsNone(raised)
        assert result is not None
        self.assertEqual(result.resolved_target_repo, "/repo")
        self.assertEqual(ops.workspace_root_calls, ["/repo/work"])
        self.assertEqual(ops.auto_diag, [("%14", "/repo/work", "/repo")])
        # duplicates before the auto step, project after it.
        self.assertEqual(
            ops.events,
            ["pane_info", "duplicates", "workspace_root", "auto_diag", "project"],
        )

    def test_auto_under_tmux_missing_cwd_defaults_to_empty_string(self) -> None:
        ops = _FakeOps(pane_info_result={"id": "%14"}, workspace_root=None)
        result, raised = _run(
            ops, _request(herdr_send=False, resolved_target_repo=_AUTO)
        )
        self.assertIsInstance(raised, _FakeDie)
        # No `cwd` key -> the auto step probes the empty string, not a KeyError.
        self.assertEqual(ops.workspace_root_calls, [""])


class AnchorThreadingTest(unittest.TestCase):
    def test_redmine_anchor_threads_onto_blocked_outcome(self) -> None:
        ops = _FakeOps(herdr_error=HerdrSendEntryError("boom", reason="unavailable"))
        _run(
            ops,
            _request(
                herdr_send=True, anchor=RedmineAnchor(issue="13729", journal="83883")
            ),
        )
        anchor = ops.emitted[0].outcome.anchor
        self.assertIsInstance(anchor, dict)
        self.assertEqual((anchor or {}).get("source"), "redmine")

    def test_asana_anchor_threads_onto_blocked_outcome(self) -> None:
        ops = _FakeOps(herdr_error=HerdrSendEntryError("boom", reason="unavailable"))
        _run(
            ops,
            _request(herdr_send=True, anchor=AsanaAnchor(task_id="T1", comment_id="C1")),
        )
        anchor = ops.emitted[0].outcome.anchor
        self.assertIsInstance(anchor, dict)
        self.assertEqual((anchor or {}).get("source"), "asana")


if __name__ == "__main__":
    unittest.main()
