"""Fake-port specifications for the typed handoff application API (#15149 / #15156).

These drive :func:`run_handoff` through a synthetic
:class:`HandoffApplicationOps` — no tmux, no herdr, no receiver, no argv, no
subprocess — and pin the contract #15156 accepts:

- the API's input and output are typed value objects;
- the delivery verdict comes from the structured ``DeliveryOutcome`` the
  orchestration publishes, evaluated through the SHARED injection-stage
  authority, not from an exit code and not from printed text;
- a fail-closed gate becomes a typed ``fail_closed`` result carrying the gate's
  own message (via :class:`CommandAbort`), never a traceback and never something
  the caller must recover from stdout;
- the module itself depends on no ``argparse``, no TTY, and no subprocess.
"""

from __future__ import annotations

import ast
import inspect
import io
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Optional, get_args, get_type_hints

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application import (  # noqa: E402,E501
    handoff_application_service as service,
)
from mozyo_bridge.application.commands_target_select import SelectedTarget  # noqa: E402
from mozyo_bridge.application.handoff_delivery_command import (  # noqa: E402
    deliver_outcome,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_application_service import (  # noqa: E402,E501
    STATUS_COMPLETED,
    STATUS_FAIL_CLOSED,
    HandoffApplicationOps,
    HandoffEmission,
    HandoffRequest,
    HandoffResult,
    HandoffTargetSelection,
    run_handoff,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (  # noqa: E402,E501
    DeliveryOutcome,
    make_outcome,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_command_input import (  # noqa: E402,E501
    HandoffCommandInput,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_operation import (  # noqa: E402,E501
    HANDOFF_OPERATIONS,
    HandoffOperation,
    OP_CROSS_WORKSPACE_CONSULT,
    OP_REPLY,
    OP_SEND,
    OP_TICKETLESS_CALLBACK,
    UnknownHandoffOperation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_send_entry import (  # noqa: E402,E501
    ResolvedHerdrTargetCapability,
)
from mozyo_bridge.shared.errors import CommandAbort, die  # noqa: E402

_REPO = Path("/tmp/mozyo-15149-fake-repo")


def _sent_outcome(**overrides):
    """A positively-delivered transport outcome (marker observed)."""
    fields = dict(
        status="sent",
        reason="ok",
        receiver="claude",
        target="%7",
        anchor=None,
        mode="standard",
        kind="reply",
        notification_marker="[mozyo:handoff]",
        source="redmine",
    )
    fields.update(overrides)
    return make_outcome(**fields)


class _FakeOps:
    """A synthetic :class:`HandoffApplicationOps`."""

    def __init__(self, *, behaviour=None, selected=None) -> None:
        self._behaviour = behaviour or (lambda emit, publish: 0)
        self._selected = selected
        self.orchestrated: HandoffCommandInput | None = None
        self.orchestrate_kwargs: dict | None = None
        self.selection_kwargs: dict | None = None

    def orchestrate(
        self,
        inp,
        *,
        repo_root,
        publish,
        resolved_herdr_target_capability,
        emit_outcome,
    ) -> int:
        self.orchestrated = inp
        self.orchestrate_kwargs = {
            "repo_root": repo_root,
            "resolved_herdr_target_capability": resolved_herdr_target_capability,
        }

        def _emit(outcome, **context):
            publish(outcome)
            emit_outcome(outcome, **context)

        return self._behaviour(_emit, publish)

    def select_semantic_target(self, *, role, repo, session, project, sender_cwd):
        self.selection_kwargs = {
            "role": role,
            "repo": repo,
            "session": session,
            "project": project,
            "sender_cwd": sender_cwd,
        }
        return self._selected


class _Selected:
    def __init__(self, pane_id: str, repo_root: str | None) -> None:
        self.pane_id = pane_id
        self.repo_root = repo_root


def _request(operation=OP_REPLY, **input_fields) -> HandoffRequest:
    return HandoffRequest(
        operation=operation,
        input=HandoffCommandInput(**input_fields),
        repo_root=_REPO,
    )


class TypedResultTest(unittest.TestCase):
    def test_a_completed_run_carries_the_structured_outcome_and_exit_code(self) -> None:
        outcome = _sent_outcome()
        ops = _FakeOps(behaviour=lambda emit, publish: (emit(outcome), 0)[1])

        result = run_handoff(_request(to="claude", source="redmine"), ops=ops)

        self.assertEqual(STATUS_COMPLETED, result.status)
        self.assertFalse(result.fail_closed)
        self.assertEqual(0, result.exit_code)
        self.assertIs(outcome, result.outcome)
        self.assertIsNone(result.error_message)

    def test_delivery_is_the_shared_injection_stage_verdict_not_the_exit_code(self) -> None:
        """rc 0 is not proof of delivery; the pending rail returns 0 and lands nothing."""
        pending = _sent_outcome(status="pending_input")
        ops = _FakeOps(behaviour=lambda emit, publish: (emit(pending), 0)[1])

        result = run_handoff(_request(to="claude"), ops=ops)

        self.assertEqual(0, result.exit_code)
        self.assertEqual(STATUS_COMPLETED, result.status)
        self.assertFalse(result.delivered)

    def test_a_confirmed_send_is_delivered(self) -> None:
        ops = _FakeOps(behaviour=lambda emit, publish: (emit(_sent_outcome()), 0)[1])
        result = run_handoff(_request(to="claude"), ops=ops)
        self.assertTrue(result.delivered)

    def test_emissions_capture_the_record_context_as_named_typed_fields(self) -> None:
        outcome = _sent_outcome()
        ops = _FakeOps(
            behaviour=lambda emit, publish: (
                emit(
                    outcome,
                    record_format="json",
                    recovery_command="mozyo-bridge read %7",
                    duplicate_lane_panes=["%7", "%8"],
                ),
                0,
            )[1]
        )

        result = run_handoff(_request(to="claude"), ops=ops)

        self.assertEqual(1, len(result.emissions))
        emission = result.emissions[0]
        self.assertIs(outcome, emission.outcome)
        # Named fields, not string keys into a bag (review j#102080 finding_f2).
        self.assertEqual("json", emission.record_format)
        self.assertEqual("mozyo-bridge read %7", emission.recovery_command)
        # Sequence context is snapshotted immutably.
        self.assertEqual(("%7", "%8"), emission.duplicate_lane_panes)
        # Unsupplied context stays None; nothing unknown was dropped on the floor.
        self.assertIsNone(emission.activation)
        self.assertEqual({}, dict(emission.extra))

    def test_a_mutated_context_list_cannot_mutate_a_captured_emission(self) -> None:
        panes = ["%7"]
        ops = _FakeOps(
            behaviour=lambda emit, publish: (
                emit(_sent_outcome(), duplicate_lane_panes=panes),
                0,
            )[1]
        )
        result = run_handoff(_request(to="claude"), ops=ops)
        panes.append("%9")
        self.assertEqual(("%7",), result.emissions[0].duplicate_lane_panes)

    def test_an_unknown_emit_keyword_is_preserved_not_dropped(self) -> None:
        """A future emit keyword must not vanish, and must not break a done send."""
        ops = _FakeOps(
            behaviour=lambda emit, publish: (
                emit(_sent_outcome(), some_future_field="x"),
                0,
            )[1]
        )
        result = run_handoff(_request(to="claude"), ops=ops)
        self.assertEqual({"some_future_field": "x"}, dict(result.emissions[0].extra))

    def test_the_last_published_outcome_wins_and_all_are_kept(self) -> None:
        first = _sent_outcome(status="blocked", reason="invalid_args")
        second = _sent_outcome()
        ops = _FakeOps(
            behaviour=lambda emit, publish: (emit(first), emit(second), 0)[2]
        )

        result = run_handoff(_request(to="claude"), ops=ops)

        self.assertIs(second, result.outcome)
        self.assertEqual(2, len(result.emissions))


class FailClosedTest(unittest.TestCase):
    def test_a_gate_refusal_becomes_a_typed_fail_closed_result(self) -> None:
        blocked = _sent_outcome(status="blocked", reason="invalid_args")

        def _refuse(emit, publish):
            emit(blocked)
            die("--mode queue-enter refuses --force")

        ops = _FakeOps(behaviour=_refuse)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = run_handoff(_request(to="claude"), ops=ops)

        self.assertEqual(STATUS_FAIL_CLOSED, result.status)
        self.assertTrue(result.fail_closed)
        self.assertEqual(2, result.exit_code)
        # The reason is typed on BOTH channels: the gate's structured outcome and
        # the abort's carried message. Neither is parsed out of printed text.
        self.assertEqual("blocked", result.outcome.status)
        self.assertEqual("invalid_args", result.outcome.reason)
        self.assertEqual("--mode queue-enter refuses --force", result.error_message)
        self.assertEqual("", stdout.getvalue())

    def test_a_refusal_never_escapes_as_an_exception(self) -> None:
        ops = _FakeOps(behaviour=lambda emit, publish: die("nope"))
        with redirect_stderr(io.StringIO()):
            result = run_handoff(_request(to="claude"), ops=ops)
        self.assertEqual(STATUS_FAIL_CLOSED, result.status)
        self.assertEqual("nope", result.error_message)

    def test_a_bare_system_exit_still_fails_closed(self) -> None:
        """A gate that exits without the typed carrier must not read as success."""

        def _exit(emit, publish):
            raise SystemExit(3)

        result = run_handoff(_request(to="claude"), ops=_FakeOps(behaviour=_exit))

        self.assertEqual(STATUS_FAIL_CLOSED, result.status)
        self.assertEqual(3, result.exit_code)
        self.assertIsNone(result.error_message)
        self.assertFalse(result.delivered)

    def test_an_unknown_operation_is_rejected_before_anything_runs(self) -> None:
        ops = _FakeOps(behaviour=lambda emit, publish: 0)
        with self.assertRaises(UnknownHandoffOperation):
            run_handoff(
                HandoffRequest(
                    operation="raw_tmux_send",
                    input=HandoffCommandInput(to="claude"),
                    repo_root=_REPO,
                ),
                ops=ops,
            )
        self.assertIsNone(ops.orchestrated)


class EntryPolicyReachesTheOrchestrationTest(unittest.TestCase):
    def test_reply(self) -> None:
        ops = _FakeOps()
        run_handoff(_request(OP_REPLY, to="codex"), ops=ops)
        self.assertEqual("reply", ops.orchestrated.default_kind)
        self.assertFalse(ops.orchestrated.ticketless)

    def test_ticketless_callback(self) -> None:
        ops = _FakeOps()
        run_handoff(_request(OP_TICKETLESS_CALLBACK, to="codex"), ops=ops)
        self.assertEqual("reply", ops.orchestrated.default_kind)
        self.assertTrue(ops.orchestrated.ticketless)

    def test_cross_workspace_consult_pins_the_gateway_receiver(self) -> None:
        ops = _FakeOps()
        run_handoff(_request(OP_CROSS_WORKSPACE_CONSULT, to="claude"), ops=ops)
        self.assertEqual("codex", ops.orchestrated.to)
        self.assertEqual("design_consultation", ops.orchestrated.kind)
        self.assertTrue(ops.orchestrated.require_receiver_binding)

    def test_the_repo_root_and_capability_are_threaded_verbatim(self) -> None:
        ops = _FakeOps()
        capability = object()
        run_handoff(
            HandoffRequest(
                operation=OP_SEND,
                input=HandoffCommandInput(to="claude"),
                repo_root=_REPO,
                resolved_herdr_target_capability=capability,
            ),
            ops=ops,
        )
        self.assertEqual(_REPO, ops.orchestrate_kwargs["repo_root"])
        self.assertIs(
            capability, ops.orchestrate_kwargs["resolved_herdr_target_capability"]
        )


class SemanticSelectionTest(unittest.TestCase):
    def test_selection_narrows_the_target_and_the_repo_for_the_gates(self) -> None:
        ops = _FakeOps(selected=_Selected("%42", "/repos/target"))
        run_handoff(
            HandoffRequest(
                operation=OP_SEND,
                input=HandoffCommandInput(to="codex", target_project="proj"),
                repo_root=_REPO,
                selection=HandoffTargetSelection(sender_cwd="/repos/sender", session="s"),
            ),
            ops=ops,
        )

        self.assertEqual(
            {
                "role": "codex",
                "repo": None,
                "session": "s",
                "project": "proj",
                "sender_cwd": "/repos/sender",
            },
            ops.selection_kwargs,
        )
        # The selector NARROWS; the resolved pane + matched repo go to the unchanged
        # identity gates, which still enforce.
        self.assertEqual("%42", ops.orchestrated.target)
        self.assertEqual("/repos/target", ops.orchestrated.target_repo)

    def test_selection_is_mutually_exclusive_with_an_explicit_target(self) -> None:
        ops = _FakeOps(selected=_Selected("%42", None))
        result = run_handoff(
            HandoffRequest(
                operation=OP_SEND,
                input=HandoffCommandInput(to="codex", target="%9"),
                repo_root=_REPO,
                selection=HandoffTargetSelection(sender_cwd="/repos/sender"),
            ),
            ops=ops,
        )

        self.assertEqual(STATUS_FAIL_CLOSED, result.status)
        self.assertIn("mutually", result.error_message)
        self.assertIsNone(ops.orchestrated)  # nothing was sent
        self.assertIsNone(ops.selection_kwargs)  # and nothing was selected

    def test_operations_without_the_selection_policy_never_select(self) -> None:
        ops = _FakeOps(selected=_Selected("%42", None))
        run_handoff(
            HandoffRequest(
                operation=OP_REPLY,
                input=HandoffCommandInput(to="codex"),
                repo_root=_REPO,
                selection=HandoffTargetSelection(sender_cwd="/repos/sender"),
            ),
            ops=ops,
        )
        self.assertIsNone(ops.selection_kwargs)
        self.assertIsNone(ops.orchestrated.target)


class TypedBoundaryContractTest(unittest.TestCase):
    """#15149 review j#102080 finding_f2: the public boundary is type-checkable.

    The Acceptance ("入力・出力が型付きapplication APIになる") and the OOP-first
    static typing policy both ask a new public object boundary to be expressed in
    types, not in ``Any``. These pin that the result / emission / request fields
    resolve to concrete types, and that the two restatements which could silently
    drift — the ``Literal`` operation type and the emission's context field set —
    are checked against their sources.
    """

    def _hints(self, cls):
        return get_type_hints(cls, include_extras=True)

    def test_no_public_boundary_field_is_Any(self) -> None:
        for cls in (HandoffRequest, HandoffResult, HandoffEmission, HandoffTargetSelection):
            for name, hint in self._hints(cls).items():
                with self.subTest(cls=cls.__name__, field=name):
                    self.assertNotIn(
                        "typing.Any", str(hint), f"{cls.__name__}.{name} is Any-typed"
                    )

    def test_the_outcome_fields_resolve_to_delivery_outcome(self) -> None:
        self.assertEqual(
            Optional[DeliveryOutcome], self._hints(HandoffResult)["outcome"]
        )
        self.assertEqual(DeliveryOutcome, self._hints(HandoffEmission)["outcome"])

    def test_the_request_capability_field_resolves_to_its_concrete_type(self) -> None:
        self.assertEqual(
            Optional[ResolvedHerdrTargetCapability],
            self._hints(HandoffRequest)["resolved_herdr_target_capability"],
        )

    def test_the_operation_type_cannot_drift_from_the_policy_table(self) -> None:
        self.assertEqual(sorted(get_args(HandoffOperation)), sorted(HANDOFF_OPERATIONS))
        self.assertEqual(HandoffOperation, self._hints(HandoffRequest)["operation"])
        self.assertEqual(HandoffOperation, self._hints(HandoffResult)["operation"])

    def test_the_emission_context_fields_match_the_record_sink_signature(self) -> None:
        """The capture must name exactly the sink's keyword context, in its order."""
        params = list(inspect.signature(deliver_outcome).parameters.values())
        sink_keywords = tuple(
            p.name for p in params if p.kind is inspect.Parameter.KEYWORD_ONLY
        )
        self.assertEqual(sink_keywords, HandoffEmission.CONTEXT_FIELDS)
        hints = self._hints(HandoffEmission)
        for name in HandoffEmission.CONTEXT_FIELDS:
            self.assertIn(name, hints)

    def test_the_port_return_types_are_concrete(self) -> None:
        orchestrate = get_type_hints(HandoffApplicationOps.orchestrate)
        self.assertEqual(int, orchestrate["return"])
        self.assertEqual(
            Optional[ResolvedHerdrTargetCapability],
            orchestrate["resolved_herdr_target_capability"],
        )
        select = get_type_hints(HandoffApplicationOps.select_semantic_target)
        self.assertEqual(SelectedTarget, select["return"])


class NoCliDependencyTest(unittest.TestCase):
    """#15156: the API depends on no shell argv, no TTY, and no stdout parse."""

    def _module_source(self) -> str:
        return Path(service.__file__).read_text(encoding="utf-8")

    def test_the_module_imports_no_argparse_and_no_subprocess(self) -> None:
        tree = ast.parse(self._module_source())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for forbidden in ("argparse", "subprocess", "shlex", "os"):
            self.assertNotIn(forbidden, imported)

    def test_the_module_reads_no_stdout_stdin_or_tty(self) -> None:
        source = self._module_source()
        for forbidden in (
            r"sys\.stdout",
            r"sys\.stdin",
            r"\bisatty\b",
            r"\binput\(",
            r"\bprint\(",
            r"\bgetcwd\b",
        ):
            self.assertIsNone(
                re.search(forbidden, source), f"{forbidden} reached the API module"
            )

    def test_a_full_run_writes_nothing_to_stdout(self) -> None:
        ops = _FakeOps(behaviour=lambda emit, publish: (emit(_sent_outcome()), 0)[1])
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = run_handoff(_request(to="claude"), ops=ops)
        self.assertEqual("", stdout.getvalue())
        self.assertIsNotNone(result.outcome)

    def test_the_abort_carrier_is_a_system_exit_with_the_message(self) -> None:
        """The CLI's exit contract is unchanged; the message is additionally typed."""
        with redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as caught:
                die("refused")
        self.assertIsInstance(caught.exception, CommandAbort)
        self.assertEqual(2, caught.exception.code)
        self.assertEqual("refused", caught.exception.message)
        self.assertIn("error: refused", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
