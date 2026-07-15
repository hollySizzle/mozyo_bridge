"""Classical tests for the durable operator startup-gate schema (Redmine #13812).

Hermetic, no-side-effect tests for the pure gate schema
(:mod:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate`).
They pin what the projection tranche must guarantee: the record is a faithful,
round-trippable model of the required projection, it is pasteable (no absolute path,
pane body, credential, or login method; the repo identity is an opaque digest), and —
after review j#79003 — **v1 realizes only the ``required`` state**, with a zero-write
invariant (no approval, default resume) so no contradictory durable record can exist.

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
    FORBIDDEN_ACTIONS,
    STATE_CONSUMED,
    STATE_OWNER_APPROVED,
    STATE_REQUIRED,
    STATE_SUPERSEDED,
    GateApproval,
    GateClassification,
    GateResume,
    GateTarget,
    OperatorStartupGate,
    OperatorStartupGateError,
    OriginalRequest,
    build_required_gate,
    operator_startup_gate_record_lines,
    reject_path_or_secret_shaped,
    repo_identity_digest,
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


class OperatorStartupGateV1Tests(unittest.TestCase):
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

    def test_deferred_transition_states_rejected(self) -> None:
        # Finding 2: v1 realizes only `required`; the transition-bearing states are
        # #13813's (they carry owner-approval / resume evidence with invariants v1
        # does not define). Constructing one fails closed regardless of approval.
        for state in (STATE_OWNER_APPROVED, STATE_CONSUMED, STATE_SUPERSEDED):
            with self.assertRaises(OperatorStartupGateError):
                self._gate(state=state, approval=GateApproval(source_journal="78412"))
            with self.assertRaises(OperatorStartupGateError):
                self._gate(state=state)

    def test_unknown_state_rejected(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            self._gate(state="frozen")

    def test_non_positive_action_generation_rejected(self) -> None:
        with self.assertRaises(OperatorStartupGateError):
            self._gate(action_generation=0)

    def test_round_trip_required(self) -> None:
        gate = self._gate()
        self.assertEqual(OperatorStartupGate.from_record(gate.to_record()), gate)

    def test_public_projection_equals_to_record(self) -> None:
        gate = self._gate()
        self.assertEqual(gate.public_projection(), gate.to_record())

    def test_from_record_unsupported_version_rejected(self) -> None:
        record = self._gate().to_record()
        record["schema_version"] = 99
        with self.assertRaises(OperatorStartupGateError):
            OperatorStartupGate.from_record(record)

    def test_from_record_transition_state_rejected(self) -> None:
        # A persisted record naming a #13813 state fails closed under v1.
        record = self._gate().to_record()
        record["state"] = STATE_CONSUMED
        with self.assertRaises(OperatorStartupGateError):
            OperatorStartupGate.from_record(record)


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
