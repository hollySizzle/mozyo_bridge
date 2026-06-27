"""Tests for the workflow-contract reference payload (Redmine #12700).

GK3500 smoke #12698 / its #12700 rerun surfaced the third ticketless blocker: a
Redmine-anchor-free ticketless prompt carried no pointer to the workflow contract
docs, so the receiver could classify a request but had no normal-operation way to
know the lane / work-item / callback / child-dispatch contract, and (j#66929) even
raw sender-repo-relative paths did not resolve in the GK3500 monorepo workspace.
The fix carries a resolvable workflow-contract reference bundle (catalog
``contract_id`` + canonical + monorepo-nested ``resolvable_paths`` + read/callback
obligations) on the standard transition payload and the durable delivery record.
Unknown / malformed bundles fail closed; omitting the bundle is the explicit
fallback; the bundle carries pointers, never doc bodies.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    RedmineAnchor,
    build_delivery_record,
    build_notification_body,
    make_outcome,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.workflow_contract import (
    CALLBACK_OBLIGATION_TICKETLESS,
    MOZYO_BRIDGE_PROJECT_SUBDIR,
    READ_OBLIGATION_ALL_BEFORE_ACTING,
    WORKFLOW_CONTRACT_BUNDLES,
    WORKFLOW_CONTRACT_SET_VERSION,
    WORKFLOW_CONTRACT_TOKENS,
    WorkflowContractBundle,
    WorkflowContractError,
    WorkflowContractRef,
    make_ref,
    resolve_workflow_contract,
    workflow_contract_from_payload,
)

# The four ticketless workflow contract docs #12698 needed; the #12700 fix must
# carry resolvable refs to exactly these (description's required-docs list).
REQUIRED_TICKETLESS_DOCS = (
    "vibes/docs/logics/ticketless-project-gateway-runtime-ux.md",
    "vibes/docs/logics/delegated-coordinator-real-machine-acceptance.md",
    "vibes/docs/logics/delegated-coordinator-smoke-test-frame.md",
    "vibes/docs/logics/coordinator-sublane-development-flow.md",
)


class ResolveWorkflowContractTest(unittest.TestCase):
    def test_every_token_resolves_to_a_bundle(self) -> None:
        for token in WORKFLOW_CONTRACT_TOKENS:
            bundle = resolve_workflow_contract(token)
            self.assertEqual(bundle.current_role, token)
            self.assertTrue(bundle.refs)
            self.assertTrue(bundle.read_obligation)
            self.assertTrue(bundle.callback_obligation)

    def test_unknown_role_fails_closed(self) -> None:
        with self.assertRaises(WorkflowContractError):
            resolve_workflow_contract("definitely_not_a_role")

    def test_grandparent_bundle_lists_the_four_required_docs(self) -> None:
        bundle = resolve_workflow_contract(ROLE_GRANDPARENT_COORDINATOR)
        self.assertEqual(
            tuple(ref.canonical_path for ref in bundle.refs),
            REQUIRED_TICKETLESS_DOCS,
        )
        self.assertEqual(bundle.read_obligation, READ_OBLIGATION_ALL_BEFORE_ACTING)
        self.assertEqual(bundle.callback_obligation, CALLBACK_OBLIGATION_TICKETLESS)

    def test_ticketless_callback_obligation_names_the_return_primitives(self) -> None:
        # #12737: the ticketless callback obligation is no longer just "callback the
        # result"; it names the product return path so the gateway returns it via
        # `ticketless-callback` or `q-enter consultation_callback`, not a local pane
        # answer. The token stays a fixed snake_case (durable-record safe) string.
        self.assertIn("ticketless_callback", CALLBACK_OBLIGATION_TICKETLESS)
        self.assertIn("q_enter_consultation_callback", CALLBACK_OBLIGATION_TICKETLESS)
        self.assertEqual(
            CALLBACK_OBLIGATION_TICKETLESS, CALLBACK_OBLIGATION_TICKETLESS.strip()
        )
        self.assertNotIn(" ", CALLBACK_OBLIGATION_TICKETLESS)

    def test_builtin_bundles_pin_the_current_set_version(self) -> None:
        # The #12737 obligation-semantics change bumped the set version to 2; every
        # builtin bundle must carry the current version so a pinning receiver detects
        # the drift and re-reads the obligation.
        self.assertEqual(WORKFLOW_CONTRACT_SET_VERSION, 2)
        for bundle in WORKFLOW_CONTRACT_BUNDLES.values():
            self.assertEqual(bundle.contract_set_version, WORKFLOW_CONTRACT_SET_VERSION)

    def test_project_gateway_bundle_equips_the_delegated_child(self) -> None:
        bundle = resolve_workflow_contract(ROLE_PROJECT_GATEWAY)
        canonical = [ref.canonical_path for ref in bundle.refs]
        self.assertIn(
            "vibes/docs/logics/coordinator-sublane-development-flow.md", canonical
        )
        # A distinct callback obligation from the ticketless bundle (child -> parent).
        self.assertNotEqual(
            bundle.callback_obligation,
            resolve_workflow_contract(ROLE_GRANDPARENT_COORDINATOR).callback_obligation,
        )

    def test_every_builtin_ref_carries_the_monorepo_resolvable_form(self) -> None:
        # #12700 j#66929: raw sender-repo-relative paths did not resolve in GK3500;
        # every ref must also carry the project-nested form the receiver resolved.
        for bundle in WORKFLOW_CONTRACT_BUNDLES.values():
            for ref in bundle.refs:
                nested = f"{MOZYO_BRIDGE_PROJECT_SUBDIR}/{ref.canonical_path}"
                self.assertIn(ref.canonical_path, ref.resolvable_paths)
                self.assertIn(nested, ref.resolvable_paths)

    def test_every_builtin_canonical_path_resolves_to_a_real_doc(self) -> None:
        # The refs must point at docs that actually exist in the repo, so the
        # bundle never ships a dangling contract pointer.
        for bundle in WORKFLOW_CONTRACT_BUNDLES.values():
            for ref in bundle.refs:
                self.assertTrue(
                    (ROOT / ref.canonical_path).is_file(),
                    f"missing workflow contract doc: {ref.canonical_path}",
                )


class WorkflowContractValidationTest(unittest.TestCase):
    def _ref(self) -> WorkflowContractRef:
        return make_ref("logic-x", "vibes/docs/logics/x.md")

    def test_make_ref_derives_canonical_plus_nested(self) -> None:
        ref = make_ref("logic-x", "vibes/docs/logics/x.md")
        self.assertEqual(
            ref.resolvable_paths,
            (
                "vibes/docs/logics/x.md",
                f"{MOZYO_BRIDGE_PROJECT_SUBDIR}/vibes/docs/logics/x.md",
            ),
        )

    def test_blank_contract_id_fails_closed(self) -> None:
        with self.assertRaises(WorkflowContractError):
            WorkflowContractRef(
                contract_id="  ",
                canonical_path="vibes/docs/logics/x.md",
                resolvable_paths=("vibes/docs/logics/x.md",),
            )

    def test_empty_resolvable_paths_fails_closed(self) -> None:
        with self.assertRaises(WorkflowContractError):
            WorkflowContractRef(
                contract_id="logic-x",
                canonical_path="vibes/docs/logics/x.md",
                resolvable_paths=(),
            )

    def test_empty_refs_fails_closed(self) -> None:
        with self.assertRaises(WorkflowContractError):
            WorkflowContractBundle(
                current_role="r",
                contract_set_version=1,
                read_obligation="read",
                callback_obligation="callback",
                refs=(),
            )

    def test_blank_obligation_fails_closed(self) -> None:
        with self.assertRaises(WorkflowContractError):
            WorkflowContractBundle(
                current_role="r",
                contract_set_version=1,
                read_obligation="  ",
                callback_obligation="callback",
                refs=(self._ref(),),
            )

    def test_non_int_version_fails_closed(self) -> None:
        with self.assertRaises(WorkflowContractError):
            WorkflowContractBundle(
                current_role="r",
                contract_set_version="1",  # type: ignore[arg-type]
                read_obligation="read",
                callback_obligation="callback",
                refs=(self._ref(),),
            )

    def test_bool_version_fails_closed(self) -> None:
        # bool is an int subclass; a stray True must not pass as a version.
        with self.assertRaises(WorkflowContractError):
            WorkflowContractBundle(
                current_role="r",
                contract_set_version=True,  # type: ignore[arg-type]
                read_obligation="read",
                callback_obligation="callback",
                refs=(self._ref(),),
            )

    def test_duplicate_contract_id_fails_closed(self) -> None:
        with self.assertRaises(WorkflowContractError):
            WorkflowContractBundle(
                current_role="r",
                contract_set_version=1,
                read_obligation="read",
                callback_obligation="callback",
                refs=(self._ref(), self._ref()),
            )


class WorkflowContractPayloadTest(unittest.TestCase):
    def test_structured_dict_round_trips(self) -> None:
        for token in WORKFLOW_CONTRACT_TOKENS:
            bundle = resolve_workflow_contract(token)
            self.assertEqual(
                workflow_contract_from_payload(bundle.to_structured_dict()), bundle
            )

    def test_structured_dict_is_free_text_free_tokens(self) -> None:
        payload = resolve_workflow_contract(
            ROLE_GRANDPARENT_COORDINATOR
        ).to_structured_dict()
        self.assertEqual(
            set(payload),
            {
                "current_role",
                "contract_set_version",
                "read_obligation",
                "callback_obligation",
                "refs",
            },
        )
        self.assertIsInstance(payload["refs"], list)
        self.assertEqual(
            set(payload["refs"][0]),
            {"contract_id", "canonical_path", "resolvable_paths"},
        )

    def test_payload_missing_field_fails_closed(self) -> None:
        with self.assertRaises(WorkflowContractError):
            workflow_contract_from_payload(
                {
                    "current_role": "r",
                    "contract_set_version": 1,
                    "read_obligation": "read",
                    # callback_obligation missing
                    "refs": [],
                }
            )

    def test_payload_non_sequence_refs_fail_closed(self) -> None:
        with self.assertRaises(WorkflowContractError):
            workflow_contract_from_payload(
                {
                    "current_role": "r",
                    "contract_set_version": 1,
                    "read_obligation": "read",
                    "callback_obligation": "callback",
                    "refs": "not-a-list",
                }
            )

    def test_payload_ref_missing_field_fails_closed(self) -> None:
        with self.assertRaises(WorkflowContractError):
            workflow_contract_from_payload(
                {
                    "current_role": "r",
                    "contract_set_version": 1,
                    "read_obligation": "read",
                    "callback_obligation": "callback",
                    "refs": [{"contract_id": "logic-x"}],
                }
            )

    def test_pointer_clause_is_single_line(self) -> None:
        for token in WORKFLOW_CONTRACT_TOKENS:
            clause = resolve_workflow_contract(token).pointer_clause()
            self.assertNotIn("\n", clause)
            self.assertIn(token, clause)


class WorkflowContractHandoffExpansionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.anchor = RedmineAnchor(issue="12700", journal="67060")
        self.bundle = resolve_workflow_contract(ROLE_GRANDPARENT_COORDINATOR)

    def test_notification_body_appends_single_line_pointer(self) -> None:
        body = build_notification_body(
            self.anchor,
            "design_consultation",
            "consult gateway",
            "codex",
            workflow_contract=self.bundle,
        )
        self.assertIn(self.bundle.pointer_clause(), body)
        # The body is delivered via one `send-keys -l`; it must stay single-line,
        # and must NOT inline a doc body (only the pointer clause).
        self.assertNotIn("\n", body)

    def test_notification_body_without_bundle_is_unchanged(self) -> None:
        without = build_notification_body(
            self.anchor, "design_consultation", "consult gateway", "codex"
        )
        self.assertNotIn("workflow contracts for", without)

    def test_make_outcome_carries_structured_bundle(self) -> None:
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="codex",
            target="%73",
            anchor=self.anchor,
            mode="standard",
            kind="design_consultation",
            notification_marker="m",
            workflow_contract=self.bundle,
        )
        self.assertEqual(
            outcome.workflow_contract, self.bundle.to_structured_dict()
        )
        self.assertIn("workflow_contract", outcome.to_json())

    def test_make_outcome_without_bundle_is_none(self) -> None:
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="codex",
            target="%73",
            anchor=self.anchor,
            mode="standard",
            kind="design_consultation",
            notification_marker="m",
        )
        self.assertIsNone(outcome.workflow_contract)

    def test_delivery_record_renders_resolvable_refs_and_obligations(self) -> None:
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="codex",
            target="%73",
            anchor=self.anchor,
            mode="standard",
            kind="design_consultation",
            notification_marker="m",
            workflow_contract=self.bundle,
        )
        record = build_delivery_record(outcome)
        self.assertIn("- Workflow contracts: `grandparent_coordinator`", record)
        self.assertIn(self.bundle.read_obligation, record)
        self.assertIn(self.bundle.callback_obligation, record)
        # Each required doc renders with its catalog id, canonical path, AND the
        # monorepo-resolvable form (the #12700 j#66929 fix).
        for ref in self.bundle.refs:
            self.assertIn(ref.contract_id, record)
            self.assertIn(ref.canonical_path, record)
            self.assertIn(
                f"{MOZYO_BRIDGE_PROJECT_SUBDIR}/{ref.canonical_path}", record
            )

    def test_delivery_record_dash_when_no_bundle(self) -> None:
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="codex",
            target="%73",
            anchor=self.anchor,
            mode="standard",
            kind="design_consultation",
            notification_marker="m",
        )
        self.assertIn("- Workflow contracts: —", build_delivery_record(outcome))


if __name__ == "__main__":
    unittest.main()
