"""Update evidence is discharged after a VERIFIED relaunch, and only then (#14741 j#97131).

Three layers, because the defect this closes lives between them:

* the completion adapter itself -- what it refuses before it opens a store at all;
* the actuator step -- WHERE the consume happens, and what a crash either side leaves
  behind;
* the five compositions -- that the port is actually wired, bound to the same home the
  planner reads.

Everything runs against a temp home. Nothing touches the operator's shared state.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.launch_identity_receipt import (  # noqa: E402
    CONSUME_OK,
    CONSUME_REPLAY,
    EVIDENCE_BOUND,
    EVIDENCE_CONSUMED,
    GenerationKey,
    LaunchIdentityReceiptStore,
)
from mozyo_bridge.core.state.replacement_transaction import (  # noqa: E402
    ReplacementTransactionKey,
)
from mozyo_bridge.core.state.replacement_transaction_model import (  # noqa: E402
    ParticipantPin,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_evidence_completion import (  # noqa: E402,E501
    COMPLETION_CAUSE_MISMATCH,
    COMPLETION_FOREIGN_WORKSPACE,
    COMPLETION_INCOMPLETE,
    COMPLETION_UNAVAILABLE,
    build_update_evidence_completion,
)

WORKSPACE = "ws"
LANE = "issue_14741"
PROVIDER = "codex"
ASSIGNED = "mzb1_ws_codex_lane"
LOCATOR = "ws:p1"
ACTION = "startup-ir1-" + "a" * 64
REPLACEMENT_ACTION = "refresh-gateway:issue_14741:codex:codex:gw:ws:p1:r4"
CAUSE = "update_relaunch"
DIGEST = "sha256:" + "c" * 64


def _pin(**kw) -> ParticipantPin:
    base = dict(
        lane_id=LANE,
        role="gateway",
        provider=PROVIDER,
        assigned_name=ASSIGNED,
        old_locator=LOCATOR,
        lane_revision="7",
        lane_generation="lane-gen-1",
        evidence_workspace_id=WORKSPACE,
        evidence_startup_action_id=ACTION,
        evidence_cause=CAUSE,
    )
    base.update(kw)
    return ParticipantPin(**base)


def _legacy_pin() -> ParticipantPin:
    return ParticipantPin(
        lane_id=LANE, role="gateway", provider=PROVIDER, assigned_name=ASSIGNED,
        old_locator=LOCATOR, lane_revision="7", lane_generation="lane-gen-1",
    )


class _CountingStore(LaunchIdentityReceiptStore):
    """The real store, counting how many times it was actually opened."""

    opens = 0

    def _connect(self, *, create: bool):  # noqa: D102 - see the base
        type(self).opens += 1
        return super()._connect(create=create)


class CompletionAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.key = ReplacementTransactionKey(WORKSPACE, REPLACEMENT_ACTION)
        self.complete = build_update_evidence_completion(self.home)
        self.generation_key = GenerationKey(
            workspace_id=WORKSPACE, lane_id=LANE, provider=PROVIDER,
            assigned_name=ASSIGNED, startup_action_id=ACTION,
        )

    def _bind_live_evidence(self) -> None:
        store = LaunchIdentityReceiptStore(home=self.home)
        store.reserve(self.generation_key, identity_digest=DIGEST)
        store.finalize(
            self.generation_key, identity_digest=DIGEST, locator=LOCATOR,
            lane_generation="lane-gen-1", lifecycle_revision="7", composite_proof=True,
        )
        store.bind_evidence(
            self.generation_key,
            blocker_id="update_prompt_available",
            identity_digest=DIGEST,
        )

    def _phase(self) -> str:
        found = LaunchIdentityReceiptStore(home=self.home).read_bound_evidence(
            workspace_id=WORKSPACE, lane_id=LANE, provider=PROVIDER,
            lane_generation="lane-gen-1", lifecycle_revision="7",
        )
        return EVIDENCE_BOUND if found is not None else EVIDENCE_CONSUMED

    def test_a_bound_receipt_is_consumed_exactly_once_and_replays_after(self) -> None:
        self._bind_live_evidence()
        self.assertEqual(self._phase(), EVIDENCE_BOUND)
        first = self.complete(
            self.key, _pin(), replacement_action_id=REPLACEMENT_ACTION
        )
        self.assertEqual(first, CONSUME_OK)
        self.assertEqual(self._phase(), EVIDENCE_CONSUMED)
        again = self.complete(
            self.key, _pin(), replacement_action_id=REPLACEMENT_ACTION
        )
        self.assertEqual(again, CONSUME_REPLAY, "the SAME action replays, it does not fail")

    def test_a_different_action_may_not_consume_the_same_evidence(self) -> None:
        self._bind_live_evidence()
        self.complete(self.key, _pin(), replacement_action_id=REPLACEMENT_ACTION)
        outcome = self.complete(
            self.key, _pin(), replacement_action_id="refresh-gateway:SOMEONE-ELSE"
        )
        self.assertNotIn(outcome, (CONSUME_OK, CONSUME_REPLAY))

    def test_a_foreign_or_malformed_triplet_never_opens_the_store(self) -> None:
        """Fail-closed BEFORE the authority is touched, not by asking it."""
        cases = (
            ("another workspace", _pin(evidence_workspace_id="OTHER"), COMPLETION_FOREIGN_WORKSPACE),
            ("a non-update cause", _pin(evidence_cause="generic_fresh"), COMPLETION_CAUSE_MISMATCH),
            ("no triplet at all", _legacy_pin(), COMPLETION_INCOMPLETE),
        )
        for label, pin, expected in cases:
            with self.subTest(label=label):
                _CountingStore.opens = 0
                complete = build_update_evidence_completion(self.home)
                import mozyo_bridge.core.state.launch_identity_receipt as receipt

                original = receipt.LaunchIdentityReceiptStore
                receipt.LaunchIdentityReceiptStore = _CountingStore
                try:
                    outcome = complete(
                        self.key, pin, replacement_action_id=REPLACEMENT_ACTION
                    )
                finally:
                    receipt.LaunchIdentityReceiptStore = original
                self.assertEqual(outcome, expected)
                self.assertEqual(_CountingStore.opens, 0, "zero store calls")

    def test_a_padded_triplet_is_refused_rather_than_repaired(self) -> None:
        """`ParticipantPin` already refuses padding; this module states it independently."""
        padded = SimpleNamespace(
            lane_id=LANE, provider=PROVIDER, assigned_name=ASSIGNED,
            evidence_workspace_id=" " + WORKSPACE + " ",
            evidence_startup_action_id=ACTION,
            evidence_cause=CAUSE,
        )
        self.assertEqual(
            self.complete(self.key, padded, replacement_action_id=REPLACEMENT_ACTION),
            COMPLETION_INCOMPLETE,
        )

    def test_an_absent_authority_is_a_typed_refusal_not_an_exception(self) -> None:
        """The actuator is mid-transaction; an exception there abandons a participant."""
        empty = build_update_evidence_completion(Path(tempfile.mkdtemp()))
        self.assertEqual(
            empty(self.key, _pin(), replacement_action_id=REPLACEMENT_ACTION),
            COMPLETION_UNAVAILABLE,
        )


class ActuatorConsumePositionTest(unittest.TestCase):
    """WHERE the consume happens, and what a crash on either side of it leaves behind."""

    GEN = 7

    def setUp(self) -> None:
        from mozyo_bridge.core.state.replacement_transaction import (
            ReplacementTransactionStore,
        )
        from mozyo_bridge.core.state.replacement_transaction_model import (
            ContinuationPointer,
            DecisionPointer,
        )

        self.home = Path(tempfile.mkdtemp())
        self.store = ReplacementTransactionStore(home=self.home)
        self.key = ReplacementTransactionKey(WORKSPACE, REPLACEMENT_ACTION)
        self.pin = _pin()
        self.store.plan_transaction(
            self.key,
            action_generation=self.GEN,
            decision=DecisionPointer(source="redmine", issue_id="14741", journal_id="97131"),
            continuation=ContinuationPointer(
                source="redmine", issue_id="14741", journal_id="97131",
                expected_gate="review_request", next_semantic_action="dispatch_once",
            ),
            participants=[self.pin],
        )
        self.calls: list = []

    def _port(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E501
            ATTEST_BOUND,
            CLOSE_DONE,
            LAUNCH_DONE,
            OLD_SLOT_PRESENT,
        )
        from mozyo_bridge.core.state.replacement_preservation import (
            PreservationObservation,
        )

        calls = self.calls

        class _Port:
            def observe_old_slot(self, pin):
                return OLD_SLOT_PRESENT

            def observe_preservation(self, pin):
                return PreservationObservation(
                    identity_matches=True, attestation_fresh=True,
                )

            def close_exact_generation(self, pin):
                calls.append("close")
                return CLOSE_DONE

            def launch_action_bound(self, action_id, pin):
                calls.append("launch")
                return LAUNCH_DONE

            def verify_attestation(self, action_id, pin):
                return ATTEST_BOUND

        return _Port()

    def _actuator(self, completion):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E501
            ReplacementActuatorUseCase,
        )

        return ReplacementActuatorUseCase(
            self.store, self._port(), clock=lambda: "2026-08-02T00:00:00+00:00",
            evidence_completion=completion,
        )

    def _drive(self, completion):
        return self._actuator(completion).drive_worker_recovery(
            self.key, holder="H", expected_action_generation=self.GEN,
        )

    def _phase(self):
        return self.store.get(self.key).find_participant(self.pin.identity).phase

    def test_the_consume_happens_before_the_replaced_cas(self) -> None:
        seen = []

        def completion(key, pin, *, replacement_action_id):
            seen.append(self._phase())
            return CONSUME_OK

        self._drive(completion)
        self.assertEqual(seen, ["verify_owed"], "consumed while still owed, not after")
        self.assertEqual(self._phase(), "replaced")

    def test_a_crash_between_consume_and_cas_replays_without_relaunching(self) -> None:
        """Item 6: the exact window the position was chosen to make recoverable."""
        state = {"consumed": False}

        def crashing(key, pin, *, replacement_action_id):
            state["consumed"] = True
            raise KeyboardInterrupt("the process died right after the consume")

        with self.assertRaises(KeyboardInterrupt):
            self._drive(crashing)
        self.assertTrue(state["consumed"])
        self.assertEqual(self._phase(), "verify_owed", "still owed, so still replayable")
        launches_before = self.calls.count("launch")

        def replaying(key, pin, *, replacement_action_id):
            return CONSUME_REPLAY

        self._drive(replaying)
        self.assertEqual(self._phase(), "replaced")
        self.assertEqual(
            self.calls.count("launch"), launches_before, "zero additional launches"
        )

    def test_an_undischargeable_evidence_stays_owed_with_no_cas(self) -> None:
        for outcome in ("absent", "foreign", COMPLETION_UNAVAILABLE, "something_new"):
            with self.subTest(outcome=outcome):
                self.setUp()
                result = self._drive(lambda *a, **k: outcome)
                self.assertEqual(result.status, "effect_failed")
                self.assertEqual(self._phase(), "verify_owed")

    def test_a_missing_completion_port_fails_closed(self) -> None:
        """Item 5 / 7: an evidenceful pin at a port-less actuator never reaches replaced."""
        result = self._drive(None)
        self.assertEqual(result.status, "effect_failed")
        self.assertIn("no update-evidence completion port", result.detail)
        self.assertEqual(self._phase(), "verify_owed")

    def test_a_legacy_participant_never_calls_the_port(self) -> None:
        """Item 7: the pre-#14741 path is byte-exact, including its cost."""
        from mozyo_bridge.core.state.replacement_transaction import (
            ReplacementTransactionStore,
        )
        from mozyo_bridge.core.state.replacement_transaction_model import (
            ContinuationPointer,
            DecisionPointer,
        )

        self.home = Path(tempfile.mkdtemp())
        self.store = ReplacementTransactionStore(home=self.home)
        self.pin = _legacy_pin()
        self.store.plan_transaction(
            self.key,
            action_generation=self.GEN,
            decision=DecisionPointer(source="redmine", issue_id="14741", journal_id="97131"),
            continuation=ContinuationPointer(
                source="redmine", issue_id="14741", journal_id="97131",
                expected_gate="review_request", next_semantic_action="dispatch_once",
            ),
            participants=[self.pin],
        )
        seen = []
        self._drive(lambda *a, **k: seen.append(1) or CONSUME_OK)
        self.assertEqual(seen, [], "the completion port was never consulted")
        self.assertEqual(self._phase(), "replaced")


class FiveSiteWiringTest(unittest.TestCase):
    """Every planner composition also wires the completion port, at the same home."""

    SITES = (
        "sublane_gateway_recovery",
        "sublane_stale_worker_recovery",
        "sublane_worker_refresh",
        "sublane_hibernated_bound_pair_convergence_live",
        "sublane_hibernated_bound_pair_composer_discard_live",
    )

    def _source(self, module: str) -> str:
        path = (
            ROOT
            / "src/mozyo_bridge/e_110_execution_platform"
            / "f_140_delegated_coordinator_nested_handoff/application"
            / f"{module}.py"
        )
        return path.read_text()

    def test_every_planner_site_also_discharges(self) -> None:
        for module in self.SITES:
            with self.subTest(module=module):
                source = self._source(module)
                self.assertIn("plan_participants_with_evidence(", source)
                self.assertIn("evidence_completion=build_update_evidence_completion(", source)

    def test_the_completion_home_is_the_stores_own_home_not_a_guess(self) -> None:
        """`path.parent` of the injected transaction store -- never cwd or a repo root."""
        for module in self.SITES:
            with self.subTest(module=module):
                source = self._source(module)
                start = source.index("evidence_completion=build_update_evidence_completion(")
                snippet = source[start : start + 200]
                self.assertIn(".path.parent", snippet)
                for guess in ("Path.cwd()", "repo_root", "os.getcwd"):
                    self.assertNotIn(guess, snippet)

    def test_the_self_close_executor_wires_no_completion_port(self) -> None:
        """A self-replacement discharges nothing; an evidenceful pin there fails closed."""
        self.assertNotIn("evidence_completion", self._source("self_close_executor"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
