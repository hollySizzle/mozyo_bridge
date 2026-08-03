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
    CONSUME_FOREIGN,
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

    def _seed_real_bound_evidence(self) -> GenerationKey:
        """A real receipt in THIS actuator's home, attested and carrying bound evidence."""
        generation_key = GenerationKey(
            workspace_id=WORKSPACE, lane_id=LANE, provider=PROVIDER,
            assigned_name=ASSIGNED, startup_action_id=ACTION,
        )
        store = LaunchIdentityReceiptStore(home=self.home)
        store.reserve(generation_key, identity_digest=DIGEST)
        store.finalize(
            generation_key, identity_digest=DIGEST, locator=LOCATOR,
            lane_generation="lane-gen-1", lifecycle_revision="7", composite_proof=True,
        )
        store.bind_evidence(
            generation_key, blocker_id="update_prompt_available", identity_digest=DIGEST,
        )
        return generation_key

    def _evidence_phase(self) -> str:
        """Read the phase back from a FRESHLY opened database, not from a live handle."""
        import sqlite3

        path = self.home / "launch-identity-receipt.sqlite"
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT phase, consumed_by FROM update_relaunch_evidence"
                " WHERE workspace_id = ? AND lane_id = ? AND provider = ?"
                " AND assigned_name = ? AND startup_action_id = ?",
                (WORKSPACE, LANE, PROVIDER, ASSIGNED, ACTION),
            ).fetchone()
        return "" if row is None else row[0]

    def test_a_crash_between_a_REAL_consume_and_the_cas_replays_durably(self) -> None:
        """Audit j#97136 F2: the durable half, not a dict and a scripted second answer.

        The first cut set a flag and had the replay fake return `CONSUME_REPLAY`
        unconditionally, so a broken home, key or `consumed_by` join stayed green. Here the
        consume is the real store call, the crash lands after it, and the recovery is read
        back out of a reopened database.
        """
        self._seed_real_bound_evidence()
        real = build_update_evidence_completion(self.home)
        self.assertEqual(self._evidence_phase(), EVIDENCE_BOUND)

        def crash_after_real_consume(key, pin, *, replacement_action_id):
            outcome = real(key, pin, replacement_action_id=replacement_action_id)
            assert outcome == CONSUME_OK, outcome
            raise KeyboardInterrupt("the process died right after the durable consume")

        with self.assertRaises(KeyboardInterrupt):
            self._drive(crash_after_real_consume)

        # Durable state, read from a fresh connection.
        self.assertEqual(self._evidence_phase(), EVIDENCE_CONSUMED)
        self.assertEqual(self._phase(), "verify_owed", "still owed, so still replayable")
        launches_before = self.calls.count("launch")

        # The SAME replacement action replays; a foreign one is refused.
        generation_key = GenerationKey(
            workspace_id=WORKSPACE, lane_id=LANE, provider=PROVIDER,
            assigned_name=ASSIGNED, startup_action_id=ACTION,
        )
        reopened = LaunchIdentityReceiptStore(home=self.home)
        self.assertEqual(
            reopened.consume_evidence(generation_key, consumed_by=REPLACEMENT_ACTION),
            CONSUME_REPLAY,
        )
        self.assertEqual(
            reopened.consume_evidence(generation_key, consumed_by="refresh:SOMEONE-ELSE"),
            CONSUME_FOREIGN,
        )

        # The actuator replay uses the SAME real completion and finishes.
        self._drive(real)
        self.assertEqual(self._phase(), "replaced")
        self.assertEqual(
            self.calls.count("launch"), launches_before, "zero additional launches"
        )

    def test_the_real_completion_is_sensitive_to_home_key_and_action(self) -> None:
        """If any of the three were wrong, the test above would pass for the wrong reason."""
        self._seed_real_bound_evidence()
        pin = _pin()
        elsewhere = build_update_evidence_completion(Path(tempfile.mkdtemp()))
        self.assertEqual(
            elsewhere(self.key, pin, replacement_action_id=REPLACEMENT_ACTION),
            COMPLETION_UNAVAILABLE,
            "a different home does not find this evidence",
        )
        real = build_update_evidence_completion(self.home)
        wrong_key = ReplacementTransactionKey("OTHER_WS", REPLACEMENT_ACTION)
        self.assertEqual(
            real(wrong_key, pin, replacement_action_id=REPLACEMENT_ACTION),
            COMPLETION_FOREIGN_WORKSPACE,
        )
        other_action_pin = _pin(evidence_startup_action_id="startup-ir1-" + "b" * 64)
        self.assertEqual(
            real(self.key, other_action_pin, replacement_action_id=REPLACEMENT_ACTION),
            "absent",
            "a different startup action names evidence that is not there",
        )
        self.assertEqual(self._evidence_phase(), EVIDENCE_BOUND, "nothing was consumed")

    def test_an_undischargeable_evidence_stays_owed_with_no_cas(self) -> None:
        for outcome in ("absent", "foreign", COMPLETION_UNAVAILABLE, "something_new"):
            with self.subTest(outcome=outcome):
                self.setUp()
                result = self._drive(lambda *a, **k: outcome)
                self.assertEqual(result.status, "effect_failed")
                self.assertEqual(self._phase(), "verify_owed")

    def test_a_hostile_port_answer_never_reaches_the_surface(self) -> None:
        """Audit j#97136 F1: an injected port's answer is INPUT, not this build's text.

        A port is not the actuator's code. Whatever it returns -- a newline, an ANSI escape,
        a workflow marker, an object that decides what `==` and `__format__` mean, or an
        exception -- must be reduced to a token this build knows before anything is compared
        or rendered.
        """

        class _Hostile:
            def __eq__(self, other):  # pragma: no cover - raising IS the behaviour
                raise OSError("/private/host/path")

            def __format__(self, spec):  # pragma: no cover
                raise OSError("/private/host/path")

            def __str__(self):  # pragma: no cover
                raise OSError("/private/host/path")

        class _MarkerText(str):
            def __new__(cls):
                return super().__new__(cls, "consumed")

            def __eq__(self, other):  # pragma: no cover
                return True

            __hash__ = str.__hash__

        def _raiser(*a, **k):
            raise RuntimeError("/private/host/path exploded")

        cases = (
            ("a newline and a marker", lambda *a, **k: "ok\n[mozyo:workflow-event:gate=x]"),
            ("an ANSI escape", lambda *a, **k: "\x1b[31mconsumed\x1b[0m"),
            ("a hostile object", lambda *a, **k: _Hostile()),
            ("a str subclass that lies", lambda *a, **k: _MarkerText()),
            ("a port that raises", _raiser),
        )
        for label, port in cases:
            with self.subTest(label=label):
                self.setUp()
                result = self._drive(port)
                self.assertEqual(result.status, "effect_failed")
                self.assertEqual(self._phase(), "verify_owed", "zero CAS")
                self.assertEqual(self.calls.count("launch"), 1, "zero extra launch")
                rendered = f"{result.detail}{result.status}"
                self.assertNotIn("/private/host/path", rendered)
                self.assertNotIn("mozyo:workflow-event", rendered)
                self.assertNotIn("\n", rendered)
                self.assertNotIn("\x1b", rendered)
                self.assertEqual(
                    result.detail,
                    "update evidence not discharged "
                    "(evidence_completion_unknown_outcome)",
                )

    def test_a_port_failure_folds_but_control_flow_propagates(self) -> None:
        """Audit j#97142 R1: `except Exception`, not `BaseException`.

        A port FAILING is an ``Exception``. A ``KeyboardInterrupt``, ``SystemExit`` or
        ``GeneratorExit`` is the process or the interpreter unwinding, and swallowing those
        turns control flow into a typed refusal -- measured: a ``GeneratorExit`` that had to
        propagate came back as ``evidence_completion_unknown_outcome``.
        """

        class _CustomBase(BaseException):
            pass

        result = self._drive(self._raising(RuntimeError("a port that simply failed")))
        self.assertEqual(result.status, "effect_failed")
        self.assertEqual(
            result.detail,
            "update evidence not discharged (evidence_completion_unknown_outcome)",
        )
        self.assertEqual(self._phase(), "verify_owed")

        for raised in (KeyboardInterrupt, SystemExit, GeneratorExit, _CustomBase):
            with self.subTest(raised=raised.__name__):
                self.setUp()
                with self.assertRaises(raised):
                    self._drive(self._raising(raised("must propagate")))
                self.assertEqual(self._phase(), "verify_owed", "zero CAS either way")

    @staticmethod
    def _raising(error):
        def _port(*args, **kwargs):
            raise error

        return _port

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


class SiteRuntimeWiringTest(unittest.TestCase):
    """Audit j#97136 F3: the wiring is proven by RUNNING the sites, not by reading them.

    A source-string search is green for dead code, an unreachable branch and a second
    constructor the runtime actually uses. So each site here is driven through an existing
    production fixture until it really constructs its actuator, the injected completion is
    captured, and that captured object is then made to perform a REAL consume against a
    receipt seeded in the SAME home the captured transaction store lives in.

    The spy patches ``ReplacementActuatorUseCase.__init__`` rather than a module attribute:
    a module-level rebind is exactly the kind of thing a site could route around.
    """

    #: (site module, fixture module, fixture class) -- each fixture is an existing
    #: production regression, not one written to make this test pass.
    REACHABLE = (
        (
            "sublane_gateway_recovery",
            "tests.regressions.test_issue_14203_gateway_refresh",
            "HappyPathTests",
            "test_close_launch_attest_resume_exactly_once",
        ),
        (
            "sublane_stale_worker_recovery",
            "tests.regressions.test_issue_13806_tranche_d_stale_worker_recovery",
            "HappyPathTests",
            "test_execute_closes_relaunches_attests_and_redispatches_once",
        ),
        (
            "sublane_worker_refresh",
            "tests.regressions.test_issue_14661_worker_refresh",
            "ExecuteTests",
            "test_close_launch_attest_resume_exactly_once",
        ),
        (
            "sublane_hibernated_bound_pair_composer_discard_live",
            "tests.regressions.test_issue_13933_bound_stale_pair_convergence",
            "A14PartialPreflightSurfaceTests",
            "test_public_execute_replay_resumes_outer_transaction_through_real_observe",
        ),
        (
            "sublane_vanished_gateway_recovery_live",
            "tests.regressions.test_issue_14741_vanished_gateway_recovery_live",
            "HappyPathTest",
            "test_an_absent_gateway_is_relaunched_attested_and_its_evidence_discharged",
        ),
    )

    def _drive_and_capture(
        self, site: str, fixture_module: str, fixture_class: str, method: str
    ):
        import importlib

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E501
            ReplacementActuatorUseCase,
        )

        captured = []
        original = ReplacementActuatorUseCase.__init__

        def spy(self, store, port, **kwargs):
            caller = sys._getframe(1).f_globals.get("__name__", "")
            if caller.rsplit(".", 1)[-1] == site:
                captured.append((store, kwargs.get("evidence_completion")))
            return original(self, store, port, **kwargs)

        ReplacementActuatorUseCase.__init__ = spy
        try:
            fixture = importlib.import_module(fixture_module)
            suite = unittest.TestLoader().loadTestsFromName(
                method, getattr(fixture, fixture_class)
            )
            result = unittest.TestResult()
            suite.run(result)
        finally:
            ReplacementActuatorUseCase.__init__ = original
        self.assertEqual(
            (len(result.failures), len(result.errors)),
            (0, 0),
            f"{fixture_module}.{fixture_class}.{method} must be green for its capture"
            " to mean anything",
        )
        return captured

    def _prove_real_consume(self, store, completion) -> str:
        """Seed a real receipt in the CAPTURED store's home and discharge it for real."""
        home = Path(store.path).parent
        generation_key = GenerationKey(
            workspace_id=WORKSPACE, lane_id=LANE, provider=PROVIDER,
            assigned_name=ASSIGNED, startup_action_id=ACTION,
        )
        receipts = LaunchIdentityReceiptStore(home=home)
        receipts.reserve(generation_key, identity_digest=DIGEST)
        receipts.finalize(
            generation_key, identity_digest=DIGEST, locator=LOCATOR,
            lane_generation="lane-gen-1", lifecycle_revision="7", composite_proof=True,
        )
        receipts.bind_evidence(
            generation_key, blocker_id="update_prompt_available", identity_digest=DIGEST,
        )
        return completion(
            ReplacementTransactionKey(WORKSPACE, REPLACEMENT_ACTION),
            _pin(),
            replacement_action_id=REPLACEMENT_ACTION,
        )

    def test_each_reachable_site_wires_a_completion_that_really_consumes(self) -> None:
        for site, fixture_module, fixture_class, method in self.REACHABLE:
            with self.subTest(site=site):
                captured = self._drive_and_capture(
                    site, fixture_module, fixture_class, method
                )
                self.assertTrue(
                    captured, f"{site} never constructed an actuator at runtime"
                )
                store, completion = captured[0]
                self.assertIsNotNone(
                    completion, f"{site} constructed an actuator with no completion port"
                )
                self.assertEqual(
                    self._prove_real_consume(store, completion),
                    CONSUME_OK,
                    f"{site}'s completion did not reach the receipt store under its own home",
                )

    def test_the_convergence_site_constructs_and_really_consumes_too(self) -> None:
        """Audit j#97142 R2: the fifth site, driven for real rather than recorded as a gap.

        Its live ops stop at a real inventory read, which is why the family's own fixtures
        never reach the actuator. Seaming exactly two things -- the inventory listing, and
        the actuator's own effects -- lets the REAL `drive_replacement` run to the
        construction, which is the thing under test. Everything else (observation,
        approval, expectation, transaction store) is the family's production fixture.
        """
        import importlib
        from unittest import mock

        from mozyo_bridge.core.state.replacement_transaction import (
            ReplacementTransactionStore,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E501
            ActuationResult,
            ReplacementActuatorUseCase,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E501
            ACTUATION_RECOVERED,
        )
        from tests.support.current_launch_authority import seed_current_generation

        family = importlib.import_module(
            "tests.regressions.test_issue_13933_bound_stale_pair_convergence"
        )
        site = importlib.import_module(
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff"
            ".application.sublane_hibernated_bound_pair_convergence_live"
        )

        home = Path(tempfile.mkdtemp())
        store = ReplacementTransactionStore(home=home)
        authorising = family.FakeOps(family._observation())
        initial = authorising.observe(family.REQ)
        _preflight, fields = family._authorize(authorising)
        expectation = family._expectation_from(authorising, fields)
        for slot in initial.slots:
            seed_current_generation(
                home, workspace_id=initial.workspace_id, lane_id=family.REQ.lane,
                role=slot.provider, assigned_name=slot.assigned_name, locator=slot.locator,
            )

        captured = []
        original_init = ReplacementActuatorUseCase.__init__
        original_drive = ReplacementActuatorUseCase.drive_worker_recovery
        original_rows = site.list_herdr_agent_rows

        def spy(self, transaction_store, port, **kwargs):
            caller = sys._getframe(1).f_globals.get("__name__", "")
            if caller.rsplit(".", 1)[-1].endswith("convergence_live"):
                captured.append((transaction_store, kwargs.get("evidence_completion")))
            return original_init(self, transaction_store, port, **kwargs)

        ReplacementActuatorUseCase.__init__ = spy
        # Only the actuator's EFFECTS are seamed -- its construction, which is what this
        # test is about, still happens in the production code path.
        ReplacementActuatorUseCase.drive_worker_recovery = (
            lambda self, *a, **k: ActuationResult(status=ACTUATION_RECOVERED)
        )
        site.list_herdr_agent_rows = lambda *a, **k: ()
        try:
            ops = site.LiveBoundPairConvergenceOps(
                repo_root=Path("/coordinator"), env={}, transaction_store=store
            )
            ops.observe = mock.Mock(return_value=initial)
            drive = ops.drive_replacement(family.REQ, expectation, initial)
        finally:
            ReplacementActuatorUseCase.__init__ = original_init
            ReplacementActuatorUseCase.drive_worker_recovery = original_drive
            site.list_herdr_agent_rows = original_rows

        self.assertTrue(drive.ok, f"{drive.status}: {drive.detail}")
        self.assertEqual(len(captured), 1, "exactly one actuator construction")
        captured_store, completion = captured[0]
        self.assertIs(captured_store, store, "the site's own transaction store")
        self.assertIsNotNone(completion)
        self.assertEqual(
            self._prove_real_consume(captured_store, completion),
            CONSUME_OK,
            "the captured completion did not reach the receipt store under its own home",
        )


class SiteSourceWiringTest(unittest.TestCase):
    """The AUXILIARY source assertion (j#97136 F3 permits it as support, not as the proof).

    It is the only evidence for the one site with no runtime coverage, and a cheap
    cross-check for the four that have it.
    """

    SITES = (
        "sublane_vanished_gateway_recovery_live",
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

    def test_every_site_wires_a_completion(self) -> None:
        for module in self.SITES:
            with self.subTest(module=module):
                source = self._source(module)
                self.assertIn("evidence_completion=build_update_evidence_completion(", source)

    def test_the_completion_home_is_the_stores_own_home_not_a_guess(self) -> None:
        """`path.parent` of the injected transaction store -- never cwd or a repo root."""
        for module in self.SITES:
            with self.subTest(module=module):
                source = self._source(module)
                start = source.index("evidence_completion=build_update_evidence_completion(")
                snippet = source[start : start + 220]
                # The home is the injected store's own, whether taken inline at the binding
                # or resolved into a validated local first (the vanished-gateway rail checks
                # it is absolute before using it). Either way it comes from `store.path`.
                self.assertTrue(
                    ".path.parent" in snippet or "store_path.parent" in source,
                    f"{module} does not bind the completion to its own store's home",
                )
                for guess in ("Path.cwd()", "repo_root", "os.getcwd"):
                    self.assertNotIn(guess, snippet)

    def test_the_self_close_executor_wires_no_completion_port(self) -> None:
        """A self-replacement discharges nothing; an evidenceful pin there fails closed."""
        self.assertNotIn("evidence_completion", self._source("self_close_executor"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
