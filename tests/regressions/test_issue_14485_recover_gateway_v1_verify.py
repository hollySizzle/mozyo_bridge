"""Redmine #14485 — post-launch verification reads the bound v1 replacement side record.

``recover-gateway`` and ``recover-stale`` share ONE actuation port
(:class:`...sublane_stale_worker_recovery_live.LiveRecoveryActuatorPort`), and its
``verify_attestation`` used to compare only ``record.replacement_action_id``.  Under a selected
v1 identity-attestation store that field is empty **by design** (#13882 holds the on-disk shape
while older installed launchers are live), because a replacement launch records its authority as
a normal v1 attestation PLUS a separate bound side record.  So a correctly bound v1 replacement
could never verify: #14484 measured it on installed 0.14.0a4 as an ``attestation_mismatch``
against a side record that was ``phase=bound`` on the exact action, name, fresh locator, and old
locator.  #14480 fixed that authority model on the LAUNCH side; these tests pin its post-launch
half.

Every negative here mutates ONE axis of a fixture the same test first asserts ``ATTEST_BOUND``
on.  Without that control the assertions would pass on a fixture that was already failing closed
for an unrelated reason — which is how a fail-closed default turns a blocking assertion vacuous.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# ``parents[2]`` is the repo root from ``tests/regressions/`` — ``parents[1]`` is ``tests/``,
# whose ``src`` does not exist, so the insert would be a silent no-op and the module would only
# import under an ambient ``PYTHONPATH``. Every test module self-inserts the repo-local ``src``
# so full, subpackage-scoped, and single-file discovery are each self-sufficient (``tests``
# is the discovery top-level dir and is never imported as a package, see ``tests/__init__.py``).
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state import (  # noqa: E402
    herdr_identity_attestation_replacement_binding as binding_mod,
)
from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
    herdr_identity_attestation_path,
)
from mozyo_bridge.core.state.herdr_identity_attestation_replacement_binding import (  # noqa: E402,E501
    HerdrIdentityReplacementBindingStore,
    herdr_identity_replacement_binding_path,
    replacement_action_bound_after_identity_join,
    selected_attestation_store_is_v1,
)
from mozyo_bridge.core.state.replacement_transaction import (  # noqa: E402
    ContinuationPointer,
    DecisionPointer,
    ParticipantPin,
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_convergence_live as convergence_live  # noqa: E402,E501
import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery_live as live  # noqa: E402,E501
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E402,E501
    FreshReceiverVerification,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery import (  # noqa: E402,E501
    RecoveryRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E402,E501
    ATTEST_BOUND,
    ATTEST_MISMATCH,
    ATTEST_PENDING,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
)

# The #14484 shape: the managed GATEWAY is the participant recover-gateway replaces, so role and
# provider are both the gateway provider (the action id carries them as ``:codex:codex:``).
WS = "ws14485"
LANE = "issue_14485_recover_gateway_v1_verify_r1"
ROLE = "codex"
OLD = "w3N:p2V"
FRESH = "w3N:p2X"
NAME = encode_assigned_name(WS, ROLE, LANE)
ACTION = f"refresh-gateway:{LANE}:{ROLE}:{ROLE}:{NAME}:{OLD}:r1"
STARTUP_NONCE = "nonce-14485"
STARTUP_ACTION = "startup-14485"
OBSERVED = "2026-07-27T03:00:00+00:00"

_V1_ATTESTATION_DDL = (
    "CREATE TABLE herdr_identity_attestations ("
    "assigned_name TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, role TEXT NOT NULL, "
    "lane_id TEXT NOT NULL, locator TEXT NOT NULL, verdict TEXT NOT NULL, "
    "detail TEXT NOT NULL DEFAULT '', observed_at TEXT NOT NULL)"
)


def _seed_v1_attestation_store(home: Path) -> Path:
    """Create the exact recognized v1 main store (the shape that cannot carry the action)."""
    path = herdr_identity_attestation_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA user_version=1")
        conn.execute(_V1_ATTESTATION_DDL)
        conn.commit()
    finally:
        conn.close()
    return path


def _request(**overrides) -> RecoveryRequest:
    base = dict(
        issue="14485", lane=LANE, role=ROLE, provider=ROLE, assigned_name=NAME,
        locator=OLD, journal="88808", action_id=ACTION, action_generation=1,
        worker_revision="1", lane_revision="1", lane_generation="1",
        expected_gate="implementation_request", next_semantic_action="dispatch_once",
    )
    base.update(overrides)
    return RecoveryRequest(**base)


def _pin(**overrides) -> ParticipantPin:
    base = dict(
        lane_id=LANE, role=ROLE, provider=ROLE, assigned_name=NAME, old_locator=OLD,
        lane_revision="1", lane_generation="1",
    )
    base.update(overrides)
    return ParticipantPin(**base)


class _FreshQ:
    """The identity join the port delegates to, stubbed at its exact public contract.

    It reports the FRESH locator, which is what the v1 side record must agree with — the old
    pinned locator lives in the side record's ``old_locator`` instead.
    """

    def __init__(self, locator: str = FRESH, ok: bool = True):
        self._locator, self._ok = locator, ok

    def verify_fresh_receiver(self, request, *, fresh_after):
        return FreshReceiverVerification(ok=self._ok, locator=self._locator if self._ok else "")


class _AttestCase(unittest.TestCase):
    def setUp(self):
        self._orig_ws = live.repo_scope_workspace_id
        live.repo_scope_workspace_id = lambda root: WS
        self.addCleanup(self._restore_ws)

    def _restore_ws(self):
        live.repo_scope_workspace_id = self._orig_ws

    def _home(self) -> Path:
        return Path(tempfile.mkdtemp())

    def _attest_v1(self, home: Path, *, locator: str = FRESH) -> IdentityAttestationRecord:
        _seed_v1_attestation_store(home)
        with sqlite3.connect(herdr_identity_attestation_path(home)) as conn:
            conn.execute(
                "INSERT INTO herdr_identity_attestations VALUES (?,?,?,?,?,?,?,?)",
                (NAME, WS, ROLE, LANE, locator, "present", "", OBSERVED),
            )
        record = HerdrIdentityAttestationStore(home=home).read(NAME)
        self.assertIsNotNone(record)
        # The premise the whole issue rests on: the v1 row is normal-shaped, so the direct
        # field is empty even though this launch WAS a replacement.
        self.assertEqual(record.replacement_action_id, "")
        self.assertTrue(selected_attestation_store_is_v1(home))
        return record

    def _bind_side_record(
        self, home: Path, record: IdentityAttestationRecord, *, action_id: str = ACTION,
        old_locator: str = OLD, workspace_id: str = WS, role: str = ROLE, lane_id: str = LANE,
        publish: bool = True,
    ):
        store = HerdrIdentityReplacementBindingStore(home=home)
        intent = store.reserve(
            action_id=action_id, assigned_name=NAME, workspace_id=workspace_id, role=role,
            lane_id=lane_id, old_locator=old_locator, startup_nonce=STARTUP_NONCE,
            startup_action_id=STARTUP_ACTION,
        )
        if not publish:
            return intent  # left at phase=reserved: a launch that never proved its receipt
        return store.bind(
            intent, attestation=record, receipt_startup_action_id=STARTUP_ACTION,
            receipt_role=role, receipt_assigned_name=NAME, receipt_locator=record.locator,
            receipt_present=True,
        )

    def _port(self, home: Path, *, q=None, request=None):
        store = ReplacementTransactionStore(home=self._home())
        key = ReplacementTransactionKey(WS, ACTION)
        store.plan_transaction(
            key,
            action_generation=1,
            decision=DecisionPointer(source="redmine", issue_id="14485", journal_id="88808"),
            continuation=ContinuationPointer(
                source="redmine", issue_id="14485", journal_id="88808",
                expected_gate="implementation_request", next_semantic_action="dispatch_once",
            ),
            participants=[_pin()],
        )
        port = live.LiveRecoveryActuatorPort(
            repo_root=ROOT, request=request or _request(), store=store, key=key,
            attestation_home=home,
        )
        port._q = lambda: (q or _FreshQ())  # type: ignore[method-assign]
        port._rows = lambda: ({"name": (request or _request()).assigned_name,
                               "pane_id": (q or _FreshQ())._locator,
                               "terminal_id": "terminal:fresh"},)  # type: ignore[method-assign]
        return port


class V1SideBindingVerificationTests(_AttestCase):
    """Legacy side records stay diagnostic and never become current authority."""

    def test_bound_v1_side_record_verifies(self):
        home = self._home()
        record = self._attest_v1(home)
        self._bind_side_record(home, record)
        self.assertEqual(
            self._port(home).verify_attestation(ACTION, _pin()), ATTEST_MISMATCH
        )

    def test_without_the_side_record_the_same_fixture_fails_closed(self):
        # The control for every negative below: absent the side record this fixture is
        # MISMATCH, and adding the exact record is the ONLY thing that turns it BOUND.  This
        # is also the exact pre-fix behaviour of a correctly bound launch.
        home = self._home()
        record = self._attest_v1(home)
        port = self._port(home)
        self.assertEqual(port.verify_attestation(ACTION, _pin()), ATTEST_MISMATCH)
        self._bind_side_record(home, record)
        self.assertEqual(port.verify_attestation(ACTION, _pin()), ATTEST_MISMATCH)

    def test_reserved_but_never_bound_fails_closed(self):
        home = self._home()
        record = self._attest_v1(home)
        self._bind_side_record(home, record, publish=False)
        self.assertEqual(
            self._port(home).verify_attestation(ACTION, _pin()), ATTEST_MISMATCH
        )
        self._bind_side_record(home, record)  # same reservation, now published
        self.assertEqual(
            self._port(home).verify_attestation(ACTION, _pin()), ATTEST_MISMATCH
        )

    def test_a_different_action_is_never_adopted(self):
        home = self._home()
        record = self._attest_v1(home)
        self._bind_side_record(home, record)
        port = self._port(home)
        self.assertEqual(port.verify_attestation(ACTION, _pin()), ATTEST_MISMATCH)
        self.assertEqual(
            port.verify_attestation(f"{ACTION}:other", _pin()), ATTEST_MISMATCH
        )

    def test_the_live_locator_must_be_the_fresh_one_the_binding_names(self):
        home = self._home()
        record = self._attest_v1(home)
        self._bind_side_record(home, record)
        self.assertEqual(
            self._port(home, q=_FreshQ(locator=FRESH)).verify_attestation(ACTION, _pin()),
            ATTEST_MISMATCH,
        )
        for foreign in (OLD, "w3N:p2Y", ""):
            self.assertEqual(
                self._port(home, q=_FreshQ(locator=foreign)).verify_attestation(ACTION, _pin()),
                ATTEST_MISMATCH,
                foreign,
            )

    def test_foreign_identity_axes_fail_closed(self):
        home = self._home()
        record = self._attest_v1(home)
        self._bind_side_record(home, record)
        self.assertEqual(self._port(home).verify_attestation(ACTION, _pin()), ATTEST_MISMATCH)
        # Each of these is the ONLY mutation against the same bound fixture.
        self.assertEqual(
            self._port(home, request=_request(role="claude")).verify_attestation(
                ACTION, _pin()
            ),
            ATTEST_MISMATCH,
            "role",
        )
        self.assertEqual(
            self._port(home, request=_request(lane="issue_14485_other")).verify_attestation(
                ACTION, _pin()
            ),
            ATTEST_MISMATCH,
            "lane",
        )
        self.assertEqual(
            self._port(home).verify_attestation(ACTION, _pin(old_locator="w3N:p2Q")),
            ATTEST_MISMATCH,
            "old_locator",
        )
        live.repo_scope_workspace_id = lambda root: "ws-foreign"
        self.assertEqual(
            self._port(home).verify_attestation(ACTION, _pin()), ATTEST_MISMATCH, "workspace"
        )

    def test_unresolvable_workspace_identity_never_joins(self):
        home = self._home()
        record = self._attest_v1(home)
        self._bind_side_record(home, record)
        self.assertEqual(self._port(home).verify_attestation(ACTION, _pin()), ATTEST_MISMATCH)

        def boom(root):
            raise RuntimeError("workspace identity unresolvable")

        live.repo_scope_workspace_id = boom
        self.assertEqual(self._port(home).verify_attestation(ACTION, _pin()), ATTEST_MISMATCH)

    def test_unreadable_binding_store_fails_closed(self):
        home = self._home()
        record = self._attest_v1(home)
        self._bind_side_record(home, record)
        port = self._port(home)
        self.assertEqual(port.verify_attestation(ACTION, _pin()), ATTEST_MISMATCH)
        path = herdr_identity_replacement_binding_path(home)
        self.assertTrue(path.exists())
        path.write_bytes(b"not a sqlite database at all\n" * 64)
        self.assertEqual(port.verify_attestation(ACTION, _pin()), ATTEST_MISMATCH)

    def test_a_foreign_assigned_name_has_no_record_at_all(self):
        # A different participant is not a mismatch on THIS action — there is no fresh
        # attestation to read, so the honest verdict is "nothing observed yet".
        home = self._home()
        record = self._attest_v1(home)
        self._bind_side_record(home, record)
        foreign = encode_assigned_name(WS, ROLE, "issue_14485_foreign")
        self.assertEqual(
            self._port(home, request=_request(assigned_name=foreign)).verify_attestation(
                ACTION, _pin()
            ),
            ATTEST_PENDING,
        )

    def test_not_fresh_is_still_pending_not_mismatch(self):
        # The boot race is a DIFFERENT branch from the binding verdict, and the fix must not
        # collapse them: a slot that has not attested fresh yet is pending, never mismatch.
        home = self._home()
        record = self._attest_v1(home)
        self._bind_side_record(home, record)
        self.assertEqual(
            self._port(home, q=_FreshQ(ok=False)).verify_attestation(ACTION, _pin()),
            ATTEST_PENDING,
        )


class V2DirectVerificationRegressionTests(_AttestCase):
    """The native-v2 direct field keeps verifying exactly as before (no regression)."""

    def _attest_v2(self, home: Path, *, action_id: str) -> IdentityAttestationRecord:
        record = HerdrIdentityAttestationStore(home=home).upsert(IdentityAttestationRecord(
            assigned_name=NAME, workspace_id=WS, role=ROLE, lane_id=LANE, locator=FRESH,
            verdict="present", observed_at=OBSERVED, replacement_action_id=action_id,
            terminal_id="terminal:fresh",
        ))
        self.assertFalse(selected_attestation_store_is_v1(home))
        return record

    def test_exact_direct_action_is_bound(self):
        home = self._home()
        self._attest_v2(home, action_id=ACTION)
        self.assertEqual(self._port(home).verify_attestation(ACTION, _pin()), ATTEST_BOUND)

    def test_different_direct_action_is_mismatch(self):
        home = self._home()
        self._attest_v2(home, action_id="some-other-action")
        self.assertEqual(self._port(home).verify_attestation(ACTION, _pin()), ATTEST_MISMATCH)

    def test_current_locator_terminal_and_identity_axes_fail_closed(self):
        home = self._home()
        self._attest_v2(home, action_id=ACTION)
        self.assertEqual(self._port(home).verify_attestation(ACTION, _pin()), ATTEST_BOUND)
        for locator in (OLD, "foreign", ""):
            self.assertEqual(self._port(home, q=_FreshQ(locator=locator)).verify_attestation(
                ACTION, _pin()), ATTEST_MISMATCH)
        port = self._port(home)
        port._rows = lambda: ({"name": NAME, "pane_id": FRESH,
                               "terminal_id": "different"},)  # type: ignore[method-assign]
        self.assertEqual(port.verify_attestation(ACTION, _pin()), ATTEST_MISMATCH)
        self.assertEqual(self._port(home, request=_request(role="claude")).verify_attestation(
            ACTION, _pin()), ATTEST_MISMATCH)
        self.assertEqual(self._port(home, request=_request(lane="foreign")).verify_attestation(
            ACTION, _pin()), ATTEST_MISMATCH)
        self.assertEqual(self._port(home).verify_attestation(
            ACTION, _pin(old_locator="foreign")), ATTEST_MISMATCH)
        live.repo_scope_workspace_id = lambda root: "foreign"
        self.assertEqual(self._port(home).verify_attestation(ACTION, _pin()), ATTEST_MISMATCH)

    def test_current_unreadable_foreign_name_and_boot_race_are_non_green(self):
        home = self._home()
        self._attest_v2(home, action_id=ACTION)
        self.assertEqual(self._port(home).verify_attestation(ACTION, _pin()), ATTEST_BOUND)
        foreign = encode_assigned_name(WS, ROLE, "foreign")
        self.assertEqual(self._port(home, request=_request(
            assigned_name=foreign)).verify_attestation(ACTION, _pin()), ATTEST_PENDING)
        self.assertEqual(self._port(home, q=_FreshQ(ok=False)).verify_attestation(
            ACTION, _pin()), ATTEST_PENDING)
        def boom(_root):
            raise RuntimeError("workspace identity unresolvable")
        live.repo_scope_workspace_id = boom
        self.assertEqual(self._port(home).verify_attestation(
            ACTION, _pin()), ATTEST_MISMATCH)
        live.repo_scope_workspace_id = lambda _root: WS
        herdr_identity_attestation_path(home).write_bytes(b"not sqlite")
        self.assertEqual(self._port(home).verify_attestation(ACTION, _pin()), ATTEST_PENDING)

    def test_a_v2_row_with_an_empty_direct_field_never_borrows_the_v1_side_record(self):
        # The v1 decomposition is a carve-out for the v1 store only.  On a migrated (v2) store
        # an empty direct field means the launch did NOT bind, and a stale side record left
        # over from the v1 era must not resurrect it.
        home = self._home()
        record = self._attest_v2(home, action_id="")
        HerdrIdentityReplacementBindingStore(home=home).reserve(
            action_id=ACTION, assigned_name=NAME, workspace_id=WS, role=ROLE, lane_id=LANE,
            old_locator=OLD, startup_nonce=STARTUP_NONCE, startup_action_id=STARTUP_ACTION,
        )
        self.assertEqual(record.replacement_action_id, "")
        self.assertEqual(self._port(home).verify_attestation(ACTION, _pin()), ATTEST_MISMATCH)


class SharedEvaluatorTests(unittest.TestCase):
    """One rule, two call sites — the whole point of hoisting the judgement."""

    def test_both_post_launch_verifications_read_the_same_function(self):
        canonical = binding_mod.replacement_action_bound_after_identity_join
        self.assertIs(live.replacement_action_bound_after_identity_join, canonical)
        self.assertIs(
            convergence_live.replacement_action_bound_after_identity_join, canonical
        )
        self.assertIs(replacement_action_bound_after_identity_join, canonical)

    def test_absent_record_is_never_a_binding(self):
        self.assertFalse(replacement_action_bound_after_identity_join(
            None, action_id=ACTION, live_locator=FRESH, live_terminal_id="terminal:fresh",
            workspace_id=WS, role=ROLE,
            lane=LANE, assigned_name=NAME, old_locator=OLD,
        ))

    def test_an_empty_action_is_never_a_binding(self):
        record = IdentityAttestationRecord(
            assigned_name=NAME, workspace_id=WS, role=ROLE, lane_id=LANE, locator=FRESH,
            verdict="present", observed_at=OBSERVED, terminal_id="terminal:fresh",
        )
        self.assertFalse(replacement_action_bound_after_identity_join(
            record, action_id="", live_locator=FRESH, live_terminal_id="terminal:fresh",
            workspace_id=WS, role=ROLE,
            lane=LANE, assigned_name=NAME, old_locator=OLD,
        ))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
