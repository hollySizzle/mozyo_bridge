"""Classical tests for the durable operator startup-gate schema (Redmine #13812/#13813).

Hermetic, no-side-effect tests for the pure gate schema
(:mod:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate`).
They pin what the projection + resume tranches must guarantee: the record is a faithful,
round-trippable model at every rung, it is pasteable (no absolute path, pane body,
credential, or login method; the repo identity is an opaque digest), and — the review
j#79003 Finding 2 discipline, now extended across the whole v2 lattice by #13813 — every
state carries its own ``(approval, resume)`` invariants so no contradictory durable
record can exist. #13812 realized only ``required``; #13813 realizes the append-only
transition lattice (owner_approved -> operator_reported_done -> verified_clear ->
consumed, with superseded as the invalidation branch), migration-aware over the v1 record.

Neutral placeholder identifiers only; no live tmux, no Redmine, no host paths.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (  # noqa: E402
    ALLOWED_ACTION_OPERATOR_UI,
    APPROVAL_SCOPE_ONE_TARGET,
    FENCE_DELIVERED,
    FENCE_RESERVED,
    FENCE_UNCERTAIN,
    FORBIDDEN_ACTIONS,
    OPERATOR_STARTUP_GATE_SCHEMA_VERSION,
    OPERATOR_STARTUP_GATE_SCHEMA_VERSION_V1,
    STATE_CONSUMED,
    STATE_OPERATOR_REPORTED_DONE,
    STATE_OWNER_APPROVED,
    STATE_REQUIRED,
    STATE_SUPERSEDED,
    STATE_VERIFIED_CLEAR,
    GateApproval,
    GateClassification,
    GateResume,
    GateTarget,
    OperatorStartupGate,
    OperatorStartupGateError,
    OriginalRequest,
    build_required_gate,
    reject_path_or_secret_shaped,
    repo_identity_digest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate_lattice import (  # noqa: E402
    approve_gate,
    consume_gate,
    operator_startup_gate_record_lines,
    operator_startup_resume_record_lines,
    report_operator_done,
    supersede_gate,
    verify_clear_gate,
)


def _target(**overrides) -> GateTarget:
    kwargs = dict(
        workspace_id="ws-alpha",
        repo_identity_digest=repo_identity_digest("repo-alpha"),
        execution_root=".",
        lane_id="lane-alpha",
        target_role="implementation_worker",
        target_assigned_name="worker-a",
        provider_id="claude",
        agent_generation=3,
        lane_revision=1,
    )
    kwargs.update(overrides)
    return GateTarget(**kwargs)


def _original() -> OriginalRequest:
    return OriginalRequest(
        source="redmine", issue="13760", journal="77948", delivery_id="deliv-1"
    )


def _classification(**overrides) -> GateClassification:
    kwargs = dict(
        blocker_id="first_run_theme",
        profile_version="2",
        classifier_version="1",
        observed_at="2026-07-16T00:00:00Z",
    )
    kwargs.update(overrides)
    return GateClassification(**kwargs)


class RepoIdentityDigestTests(unittest.TestCase):
    def test_deterministic_and_opaque(self) -> None:
        first = repo_identity_digest("repo-alpha")
        second = repo_identity_digest("repo-alpha")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("sha256:"))
        self.assertNotIn("repo-alpha", first)

    def test_distinct_inputs_distinct_digests(self) -> None:
        self.assertNotEqual(
            repo_identity_digest("repo-alpha"), repo_identity_digest("repo-beta")
        )

    def test_blank_token_fails_closed(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            repo_identity_digest("   ")


class RejectPathOrSecretShapedTests(unittest.TestCase):
    def test_absolute_path_rejected(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            reject_path_or_secret_shaped("/Users/me/x", field_name="f")

    def test_secret_token_rejected(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            reject_path_or_secret_shaped("my_api_key_here", field_name="f")

    def test_plain_identifier_ok(self) -> None:
        reject_path_or_secret_shaped("worker-a", field_name="f")  # no raise


class GateTargetTests(unittest.TestCase):
    def test_well_formed_round_trips(self) -> None:
        target = _target()
        self.assertEqual(GateTarget.from_record(target.to_record()), target)

    def test_path_shaped_workspace_rejected(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            _target(workspace_id="/Users/someone/ws")

    def test_repo_digest_must_be_opaque_digest_not_path(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            _target(repo_identity_digest="/home/me/repo")

    def test_repo_digest_bare_label_rejected(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            _target(repo_identity_digest="repo-alpha")

    def test_absolute_execution_root_rejected(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            _target(execution_root="/abs/root")

    def test_repo_relative_execution_root_allowed(self) -> None:
        self.assertEqual(_target(execution_root="projects/x").execution_root, "projects/x")

    def test_parent_traversal_execution_root_rejected(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            _target(execution_root="../escape")

    def test_non_positive_generation_rejected(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            _target(agent_generation=0)

    def test_bool_generation_rejected(self) -> None:
        # True is an int subclass; it must not silently read as generation 1.
        with self.assertRaises(OperatorStartupGateError):
            _target(agent_generation=True)

    def test_same_identity_ignores_generation(self) -> None:
        self.assertTrue(_target(agent_generation=3).same_identity(_target(agent_generation=9)))

    def test_same_identity_false_on_lane_change(self) -> None:
        self.assertFalse(_target().same_identity(_target(lane_id="lane-beta")))

    def test_same_identity_false_on_provider_change(self) -> None:
        self.assertFalse(_target().same_identity(_target(provider_id="codex")))


class OriginalRequestTests(unittest.TestCase):
    def test_non_redmine_source_rejected(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            OriginalRequest(source="asana", issue="1", journal="2", delivery_id="d")

    def test_blank_field_rejected(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            OriginalRequest(source="redmine", issue="", journal="2", delivery_id="d")

    def test_secret_shaped_delivery_id_rejected(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            OriginalRequest(
                source="redmine", issue="1", journal="2", delivery_id="api_key-xyz"
            )

    def test_round_trip(self) -> None:
        original = _original()
        self.assertEqual(OriginalRequest.from_record(original.to_record()), original)


class GateApprovalTests(unittest.TestCase):
    # GateApproval is the #13813 transition type; #13812 v1 never attaches it to a
    # gate, but it is a defined, validated schema part and is tested standalone.
    def test_default_shape_is_pinned(self) -> None:
        approval = GateApproval(source_journal="78412")
        self.assertEqual(approval.scope, APPROVAL_SCOPE_ONE_TARGET)
        self.assertEqual(approval.allowed_action, ALLOWED_ACTION_OPERATOR_UI)
        self.assertEqual(approval.forbidden, FORBIDDEN_ACTIONS)

    def test_widened_scope_rejected(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            GateApproval(source_journal="1", scope="global")

    def test_non_ui_action_rejected(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            GateApproval(source_journal="1", allowed_action="raw_key")

    def test_narrowed_forbidden_set_rejected(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            GateApproval(source_journal="1", forbidden=frozenset({"raw_key"}))

    def test_round_trip(self) -> None:
        approval = GateApproval(source_journal="78412")
        self.assertEqual(GateApproval.from_record(approval.to_record()), approval)


def _approval() -> GateApproval:
    return GateApproval(source_journal="78412")


class OperatorStartupGateRequiredStateTests(unittest.TestCase):
    def _gate(self, **overrides) -> OperatorStartupGate:
        kwargs = dict(
            gate_id="gate-1",
            action_generation=1,
            state=STATE_REQUIRED,
            original_request=_original(),
            target=_target(),
            classification=_classification(),
        )
        kwargs.update(overrides)
        return OperatorStartupGate(**kwargs)

    def test_required_gate_carries_no_approval(self) -> None:
        gate = build_required_gate(
            gate_id="gate-1",
            action_generation=1,
            original_request=_original(),
            target=_target(),
            classification=_classification(),
        )
        self.assertEqual(gate.state, STATE_REQUIRED)
        self.assertIsNone(gate.approval)
        self.assertEqual(gate.resume, GateResume())

    def test_required_with_approval_rejected(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            self._gate(approval=GateApproval(source_journal="78412"))

    def test_required_with_nondefault_resume_rejected(self) -> None:
        # Finding 2: a required gate with a reserved fence / consumed delivery is a
        # contradictory record and must not be constructible.
        with self.assertRaises(OperatorStartupGateError):
            self._gate(resume=GateResume(dispatch_fence_state="reserved"))
        with self.assertRaises(OperatorStartupGateError):
            self._gate(resume=GateResume(consumed_delivery_record="deliv-x"))
        with self.assertRaises(OperatorStartupGateError):
            self._gate(resume=GateResume(startup_clear_observed_at="2026-07-16T01:00:00Z"))

    def test_unknown_state_rejected(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            self._gate(state="frozen")

    def test_non_positive_action_generation_rejected(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            self._gate(action_generation=0)

    def test_round_trip_required(self) -> None:
        gate = self._gate()
        self.assertEqual(OperatorStartupGate.from_record(gate.to_record()), gate)
        self.assertEqual(gate.schema_version, OPERATOR_STARTUP_GATE_SCHEMA_VERSION)

    def test_public_projection_equals_to_record(self) -> None:
        gate = self._gate()
        self.assertEqual(gate.public_projection(), gate.to_record())

    def test_from_record_unsupported_version_rejected(self) -> None:
        record = self._gate().to_record()
        record["schema_version"] = 99
        with self.assertRaises(OperatorStartupGateError):
            OperatorStartupGate.from_record(record)

    def test_from_record_consumed_without_invariants_rejected(self) -> None:
        # A persisted v2 record naming `consumed` but carrying a required gate's shape
        # (no approval, default resume) fails the consumed rung invariant — a
        # contradictory record cannot be reconstructed.
        record = self._gate().to_record()
        record["state"] = STATE_CONSUMED
        with self.assertRaises(OperatorStartupGateError):
            OperatorStartupGate.from_record(record)


class LatticeTransitionTests(unittest.TestCase):
    """The #13813 append-only transition lattice and its per-state invariants."""

    def _required(self) -> OperatorStartupGate:
        return build_required_gate(
            gate_id="gate-1",
            action_generation=1,
            original_request=_original(),
            target=_target(),
            classification=_classification(),
        )

    def _done(self) -> OperatorStartupGate:
        return report_operator_done(approve_gate(self._required(), approval=_approval()))

    def test_forward_chain_round_trips_at_each_rung(self) -> None:
        approved = approve_gate(self._required(), approval=_approval())
        self.assertEqual(approved.state, STATE_OWNER_APPROVED)
        done = report_operator_done(approved)
        self.assertEqual(done.state, STATE_OPERATOR_REPORTED_DONE)
        cleared = verify_clear_gate(
            done,
            startup_clear_observed_at="2026-07-16T01:00:00Z",
            dispatch_fence_state=FENCE_RESERVED,
        )
        self.assertEqual(cleared.state, STATE_VERIFIED_CLEAR)
        consumed = consume_gate(cleared, consumed_delivery_record="deliv-1")
        self.assertEqual(consumed.state, STATE_CONSUMED)
        self.assertEqual(consumed.resume.dispatch_fence_state, FENCE_DELIVERED)
        for gate in (approved, done, cleared, consumed):
            self.assertEqual(OperatorStartupGate.from_record(gate.to_record()), gate)
        # The whole chain continues the SAME gate id / action generation.
        self.assertEqual(
            {g.gate_id for g in (approved, done, cleared, consumed)}, {"gate-1"}
        )
        self.assertEqual(
            {g.action_generation for g in (approved, done, cleared, consumed)}, {1}
        )

    def test_owner_approved_requires_approval(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            OperatorStartupGate(
                gate_id="g",
                action_generation=1,
                state=STATE_OWNER_APPROVED,
                original_request=_original(),
                target=_target(),
                classification=_classification(),
                approval=None,
            )

    def test_owner_approved_rejects_resume_evidence(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            OperatorStartupGate(
                gate_id="g",
                action_generation=1,
                state=STATE_OWNER_APPROVED,
                original_request=_original(),
                target=_target(),
                classification=_classification(),
                approval=_approval(),
                resume=GateResume(dispatch_fence_state=FENCE_RESERVED),
            )

    def test_verified_clear_requires_clear_and_reserved_fence(self) -> None:
        done = self._done()
        # missing clear timestamp
        with self.assertRaises(OperatorStartupGateError):
            OperatorStartupGate(
                gate_id="gate-1",
                action_generation=1,
                state=STATE_VERIFIED_CLEAR,
                original_request=_original(),
                target=_target(),
                classification=_classification(),
                approval=_approval(),
                resume=GateResume(dispatch_fence_state=FENCE_RESERVED),
            )
        # a delivered fence is the consumed rung, not verified_clear
        with self.assertRaises(OperatorStartupGateError):
            verify_clear_gate(
                done,
                startup_clear_observed_at="2026-07-16T01:00:00Z",
                dispatch_fence_state=FENCE_DELIVERED,
            )
        # uncertain fence is valid at verified_clear (reserve-but-unconfirmed rung)
        uncertain = verify_clear_gate(
            done,
            startup_clear_observed_at="2026-07-16T01:00:00Z",
            dispatch_fence_state=FENCE_UNCERTAIN,
        )
        self.assertEqual(uncertain.resume.dispatch_fence_state, FENCE_UNCERTAIN)

    def test_consumed_requires_delivered_fence_and_delivery_record(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            OperatorStartupGate(
                gate_id="g",
                action_generation=1,
                state=STATE_CONSUMED,
                original_request=_original(),
                target=_target(),
                classification=_classification(),
                approval=_approval(),
                resume=GateResume(
                    startup_clear_observed_at="2026-07-16T01:00:00Z",
                    dispatch_fence_state=FENCE_RESERVED,  # not delivered
                    consumed_delivery_record="deliv-1",
                ),
            )

    def test_backward_and_skipping_edges_rejected(self) -> None:
        done = self._done()
        # skip verified_clear
        with self.assertRaises(OperatorStartupGateError):
            consume_gate(done, consumed_delivery_record="deliv-1")
        # re-approve an already-approved gate
        with self.assertRaises(OperatorStartupGateError):
            approve_gate(approve_gate(self._required(), approval=_approval()), approval=_approval())

    def test_supersede_from_required_keeps_no_approval(self) -> None:
        superseded = supersede_gate(self._required())
        self.assertEqual(superseded.state, STATE_SUPERSEDED)
        self.assertIsNone(superseded.approval)
        self.assertEqual(OperatorStartupGate.from_record(superseded.to_record()), superseded)

    def test_supersede_terminal_rejected(self) -> None:
        cleared = verify_clear_gate(
            self._done(),
            startup_clear_observed_at="2026-07-16T01:00:00Z",
            dispatch_fence_state=FENCE_RESERVED,
        )
        consumed = consume_gate(cleared, consumed_delivery_record="deliv-1")
        with self.assertRaises(OperatorStartupGateError):
            supersede_gate(consumed)

    def test_invalid_fence_state_rejected(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            GateResume(dispatch_fence_state="teleported")


class V1MigrationTests(unittest.TestCase):
    def _required(self) -> OperatorStartupGate:
        return build_required_gate(
            gate_id="gate-1",
            action_generation=1,
            original_request=_original(),
            target=_target(),
            classification=_classification(),
        )

    def test_v1_required_record_still_reads_and_restamps_v2(self) -> None:
        record = self._required().to_record()
        record["schema_version"] = OPERATOR_STARTUP_GATE_SCHEMA_VERSION_V1
        gate = OperatorStartupGate.from_record(record)
        self.assertEqual(gate.state, STATE_REQUIRED)
        self.assertEqual(gate.schema_version, OPERATOR_STARTUP_GATE_SCHEMA_VERSION)

    def test_v1_record_naming_transition_state_rejected(self) -> None:
        # v1 only ever wrote `required`; a v1 record in any other state is malformed.
        record = self._required().to_record()
        record["schema_version"] = OPERATOR_STARTUP_GATE_SCHEMA_VERSION_V1
        record["state"] = STATE_CONSUMED
        with self.assertRaises(OperatorStartupGateError):
            OperatorStartupGate.from_record(record)


class ResumeRecordLinesTests(unittest.TestCase):
    def _consumed(self) -> OperatorStartupGate:
        required = build_required_gate(
            gate_id="gate-1",
            action_generation=2,
            original_request=_original(),
            target=_target(),
            classification=_classification(),
        )
        done = report_operator_done(approve_gate(required, approval=_approval()))
        cleared = verify_clear_gate(
            done,
            startup_clear_observed_at="2026-07-16T01:00:00Z",
            dispatch_fence_state=FENCE_RESERVED,
        )
        return consume_gate(cleared, consumed_delivery_record="deliv-1")

    def test_names_target_and_resume_tokens(self) -> None:
        blob = "\n".join(operator_startup_resume_record_lines(self._consumed()))
        self.assertIn("operator_startup_resume", blob)
        self.assertIn("state=consumed", blob)
        self.assertIn("dispatch_fence_state=delivered", blob)
        self.assertIn("sha256:", blob)
        self.assertIn("worker-a", blob)

    def test_states_ack_is_not_completion(self) -> None:
        blob = "\n".join(operator_startup_resume_record_lines(self._consumed()))
        self.assertIn("NOT a completion", blob)
        self.assertIn("#13813", blob)

    def test_carries_no_absolute_path_or_secret(self) -> None:
        for line in operator_startup_resume_record_lines(self._consumed()):
            self.assertNotIn("/Users/", line)
            self.assertNotIn("\\Users\\", line)
            self.assertNotIn("api_key", line)
            self.assertNotIn("password", line)

    def test_required_gate_rejected_by_resume_renderer(self) -> None:
        required = build_required_gate(
            gate_id="gate-1",
            action_generation=1,
            original_request=_original(),
            target=_target(),
            classification=_classification(),
        )
        with self.assertRaises(OperatorStartupGateError):
            operator_startup_resume_record_lines(required)


class RecordLinesRedactionTests(unittest.TestCase):
    def _lines(self) -> list[str]:
        gate = build_required_gate(
            gate_id="gate-1",
            action_generation=2,
            original_request=_original(),
            target=_target(),
            classification=_classification(),
        )
        return operator_startup_gate_record_lines(gate)

    def test_names_the_blocker_and_target_tokens(self) -> None:
        blob = "\n".join(self._lines())
        self.assertIn("operator_action_required", blob)
        self.assertIn("first_run_theme", blob)
        self.assertIn("ws-alpha", blob)
        self.assertIn("worker-a", blob)
        self.assertIn("sha256:", blob)

    def test_carries_no_absolute_path_or_secret(self) -> None:
        for line in self._lines():
            self.assertNotIn("/Users/", line)
            self.assertNotIn("\\Users\\", line)
            self.assertNotIn("api_key", line)
            self.assertNotIn("password", line)

    def test_states_operator_ui_boundary(self) -> None:
        blob = "\n".join(self._lines()).lower()
        self.assertIn("operator", blob)
        self.assertIn("read-only", blob)

    def test_does_not_promise_exactly_once_resume(self) -> None:
        # Finding 3: the formatter must not guarantee an exactly-once re-issue that
        # #13813 has not implemented; it must mark resume as pending.
        blob = "\n".join(self._lines()).lower()
        self.assertNotIn("lands exactly once", blob)
        self.assertIn("pending", blob)
        self.assertIn("#13813", "\n".join(self._lines()))


if __name__ == "__main__":
    unittest.main()
