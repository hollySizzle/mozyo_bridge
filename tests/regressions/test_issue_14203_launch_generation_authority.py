"""Redmine #14203 — launch-generation authority (design consultation answer j#87472).

The collision-free per-launch generation authority is a home-scoped, 2-phase
(``pending`` -> ``attested``) current-generation pointer keyed by ``assigned_name``
(:mod:`mozyo_bridge.core.state.herdr_launch_generation`), established by the parent launcher
and read by both the queue-enter binding and the gateway recovery. These regressions pin the
seven properties the Design Answer required, each a defect the weaker designs (option b /
token-only sidecar / seconds-precision ``observed_at``) would have re-admitted.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mozyo_bridge.core.state.herdr_identity_attestation import (
    VERDICT_PRESENT,
    herdr_identity_attestation_path,
)
from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
    STORE_RECOGNIZED,
    probe_store_schema,
)
from mozyo_bridge.core.state.herdr_launch_generation import (
    GENERATION_ATTESTED,
    GENERATION_PENDING,
    HerdrLaunchGenerationError,
    HerdrLaunchGenerationStore,
    herdr_launch_generation_path,
    verified_generation_token,
)
from mozyo_bridge.core.state.startup_execution_events import (
    STAGE_ATTESTATION_WRITE_SUCCEEDED,
    STAGE_SELF_LOOKUP_SUCCEEDED,
    append_execution_event,
)
from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_COMPLETED_SUCCESS,
    PHASE_HEALTH_CHECK,
    Participant,
    StartupTransactionFence,
    StartupUnit,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_generation_binding import (  # noqa: E501
    finalize_launch_generations,
    reserve_launch_generations,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
    _norm_lane,
)

WS = "wsA"
ROLE = "codex"
LANE = "issue_x_lane"
LOCATOR = "w:3"
NAME = "gw"


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _seed_fence_success(
    home: Path, *, nonce: str, name=NAME, role=ROLE, lane=LANE, locator=LOCATOR,
    workspace=WS, closed=False, terminal_success=True, receipt="rcpt",
) -> str:
    """Reserve a startup transaction, record this gateway as its participant, drive it to
    ``completed_success`` (or a non-terminal phase). Returns the fence action id (token)."""
    fence = StartupTransactionFence(home=home)
    action = fence.reserve(
        StartupUnit(workspace_id=workspace, lane_id=lane, providers=(role,)),
        f"nonce-{nonce}",
    )
    token = action.action_id
    fence.record_participant(
        token,
        Participant(role=role, assigned_name=name, locator=locator, receipt=receipt,
                    closed=closed),
    )
    fence.set_phase(
        token, PHASE_COMPLETED_SUCCESS if terminal_success else PHASE_HEALTH_CHECK
    )
    return token


def _seed_generation(
    home: Path, token: str, *, name=NAME, role=ROLE, lane=LANE, locator=LOCATOR,
    workspace=WS, verdict=VERDICT_PRESENT, finalize=True,
):
    store = HerdrLaunchGenerationStore(home=home)
    store.reserve_pending(
        assigned_name=name, startup_action_id=token, workspace_id=workspace,
        role=role, lane_id=lane,
    )
    if finalize:
        store.finalize(
            assigned_name=name, startup_action_id=token, workspace_id=workspace,
            role=role, lane_id=lane, locator=locator, verdict=verdict,
            observed_at="2026-07-24T17:00:00+00:00",
        )


def _token_for(home: Path, *, name=NAME, role=ROLE, lane=LANE, locator=LOCATOR,
               workspace=WS) -> str:
    return verified_generation_token(
        home, assigned_name=name, workspace_id=workspace, role=role, lane_id=lane,
        locator=locator, norm=_norm, norm_lane=_norm_lane,
    )


def _seed_v1_attestation_store(home: Path) -> Path:
    """A genuine pre-#13806 (v1) main attestation store — the shape a real shared home may
    still carry. The generation mechanism must never force this to migrate (j#87472)."""
    path = herdr_identity_attestation_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "CREATE TABLE herdr_identity_attestations ("
            "assigned_name TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, "
            "role TEXT NOT NULL, lane_id TEXT NOT NULL, locator TEXT NOT NULL, "
            "verdict TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', "
            "observed_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO herdr_identity_attestations VALUES "
            "('mzb1_ws1_claude_default','ws1','claude','default','wY:p2','present','','t')"
        )
        conn.commit()
    finally:
        conn.close()
    return path


class R1UnmigratedAttestationHome(unittest.TestCase):
    """1. A real v1/v2 attestation home keeps admitting normal launches — NO migration."""

    def test_a_v1_attestation_home_is_untouched_by_the_generation_store(self):
        home = _tmp()
        att_path = _seed_v1_attestation_store(home)
        before = att_path.read_bytes()
        self.assertEqual(probe_store_schema(att_path).version, 1)

        # Running the whole generation lifecycle in the SAME home never touches the
        # attestation file — the generation store is a separate home-scoped file.
        token = _seed_fence_success(home, nonce="A")
        _seed_generation(home, token)
        self.assertTrue(herdr_launch_generation_path(home).exists())

        observation = probe_store_schema(att_path)
        self.assertEqual(observation.state, STORE_RECOGNIZED)
        self.assertEqual(observation.version, 1)  # still v1 — never migrated
        self.assertEqual(att_path.read_bytes(), before)  # byte-for-byte untouched

    def test_a_v1_attestation_home_still_admits_a_normal_launch_write(self):
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            IdentityAttestationRecord,
            record_identity_attestation,
        )

        home = _tmp()
        att_path = _seed_v1_attestation_store(home)
        # A normal launch (empty replacement_action_id) writes a v1-shaped row with no
        # migration — the design's whole premise for rejecting option (b).
        record_identity_attestation(
            IdentityAttestationRecord(
                assigned_name="mzb1_ws1_codex_default", workspace_id="ws1", role="codex",
                lane_id="default", locator="wY:p3", verdict=VERDICT_PRESENT,
                detail="", observed_at="t2",
            ),
            home=home,
        )
        self.assertEqual(probe_store_schema(att_path).version, 1)


class R2PendingSupersedesAttested(unittest.TestCase):
    """2. A pre-launch pending reservation invalidates the old attested current pointer."""

    def test_a_new_reservation_supersedes_the_prior_attested_generation(self):
        home = _tmp()
        token_a = _seed_fence_success(home, nonce="A")
        _seed_generation(home, token_a)
        self.assertEqual(_token_for(home), token_a)  # A is the live authority

        # A relaunch reserves a NEW pending generation BEFORE it attests. The moment it is
        # reserved the old attested pointer is gone — the authority reads pending => no token
        # (recovery fails closed during the relaunch window), never the stale generation A.
        token_b = _seed_fence_success(home, nonce="B")
        HerdrLaunchGenerationStore(home=home).reserve_pending(
            assigned_name=NAME, startup_action_id=token_b, workspace_id=WS,
            role=ROLE, lane_id=LANE,
        )
        row = HerdrLaunchGenerationStore(home=home).read(NAME)
        self.assertEqual(row.phase, GENERATION_PENDING)
        self.assertEqual(row.startup_action_id, token_b)
        self.assertEqual(_token_for(home), "")  # pending => fail-closed

        # Only once B attests does the authority return B — and never A again.
        _seed_generation(home, token_b, finalize=True)
        self.assertEqual(_token_for(home), token_b)


class R3FailClosed(unittest.TestCase):
    """3. Missing finalize / absent / corrupt / pending => authority fails closed."""

    def test_a_pending_generation_yields_no_token(self):
        home = _tmp()
        token = _seed_fence_success(home, nonce="A")
        _seed_generation(home, token, finalize=False)  # reserved, never finalized
        self.assertEqual(_token_for(home), "")

    def test_an_absent_store_yields_no_token(self):
        self.assertEqual(_token_for(_tmp()), "")  # fresh home, nothing seeded

    def test_a_corrupt_store_reads_fail_closed(self):
        home = _tmp()
        path = herdr_launch_generation_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"this is not a sqlite database")
        with self.assertRaises(HerdrLaunchGenerationError):
            HerdrLaunchGenerationStore(home=home).read(NAME)
        self.assertEqual(_token_for(home), "")  # authority swallows it => no token

    def test_an_attested_row_whose_transaction_is_not_success_yields_no_token(self):
        # The attested row alone is not enough — its token must name a completed-success
        # startup transaction with this exact participant (j#87472). A non-terminal (or
        # rolled-back) transaction, or a closed / foreign participant, yields no token.
        home = _tmp()
        token = _seed_fence_success(home, nonce="A", terminal_success=False)
        _seed_generation(home, token)
        self.assertEqual(_token_for(home), "")

        home2 = _tmp()
        token2 = _seed_fence_success(home2, nonce="A", closed=True)  # participant closed
        _seed_generation(home2, token2)
        self.assertEqual(_token_for(home2), "")

        home3 = _tmp()
        token3 = _seed_fence_success(home3, nonce="A", locator="w:OTHER")  # foreign locator
        _seed_generation(home3, token3)  # generation row locator is w:3
        self.assertEqual(_token_for(home3), "")


class R4LateFinalizeCasRejected(unittest.TestCase):
    """4. An older launch's late finalize after a newer pending reservation is refused."""

    def test_an_old_action_finalize_after_a_newer_reservation_is_cas_refused(self):
        home = _tmp()
        store = HerdrLaunchGenerationStore(home=home)
        store.reserve_pending(assigned_name=NAME, startup_action_id="startup-A",
                              workspace_id=WS, role=ROLE, lane_id=LANE)
        store.reserve_pending(assigned_name=NAME, startup_action_id="startup-B",
                              workspace_id=WS, role=ROLE, lane_id=LANE)
        # The row now holds pending B. A's delayed finalize matches zero rows => refused.
        with self.assertRaises(HerdrLaunchGenerationError):
            store.finalize(assigned_name=NAME, startup_action_id="startup-A",
                           workspace_id=WS, role=ROLE, lane_id=LANE, locator=LOCATOR,
                           verdict=VERDICT_PRESENT, observed_at="t")
        row = store.read(NAME)
        self.assertEqual((row.phase, row.startup_action_id), (GENERATION_PENDING, "startup-B"))

    def test_a_finalize_with_a_mismatched_identity_is_cas_refused(self):
        home = _tmp()
        store = HerdrLaunchGenerationStore(home=home)
        store.reserve_pending(assigned_name=NAME, startup_action_id="startup-A",
                              workspace_id=WS, role=ROLE, lane_id=LANE)
        with self.assertRaises(HerdrLaunchGenerationError):
            store.finalize(assigned_name=NAME, startup_action_id="startup-A",
                           workspace_id="OTHER-WS", role=ROLE, lane_id=LANE,
                           locator=LOCATOR, verdict=VERDICT_PRESENT, observed_at="t")


class R5SameSecondAba(unittest.TestCase):
    """5. Same-second / ABA launches (token-only difference) never join — both directions."""

    def test_a_delivery_time_recycle_never_lets_the_old_binding_join(self):
        # "delivery中recycle": the binding captured token A; a same-second recycle to B makes
        # B the current generation. A != B, so the old binding never joins the new gen.
        home = _tmp()
        token_a = _seed_fence_success(home, nonce="A")
        _seed_generation(home, token_a)
        binding_token = token_a  # captured at delivery
        token_b = _seed_fence_success(home, nonce="B")
        _seed_generation(home, token_b)  # recycle => current is B
        self.assertEqual(_token_for(home), token_b)
        self.assertNotEqual(binding_token, token_b)  # each launch's nonce differs

    def test_a_delivery_to_recovery_recycle_is_symmetric(self):
        # "delivery→recovery間recycle": same as above but the recycle happens between the
        # binding capture and the recovery read — the authority still reflects only the
        # current generation, so a stale-token binding fails closed either way.
        home = _tmp()
        token_a = _seed_fence_success(home, nonce="A")
        _seed_generation(home, token_a)
        self.assertEqual(_token_for(home), token_a)
        # ... time passes, gateway recycles ...
        token_aprime = _seed_fence_success(home, nonce="Aprime")
        _seed_generation(home, token_aprime)
        self.assertEqual(_token_for(home), token_aprime)
        self.assertNotEqual(token_a, token_aprime)


class R6ReturnEqualsDb(unittest.TestCase):
    """6. reserve / finalize / readback return values equal the persisted DB row exactly."""

    def test_reserve_and_finalize_returns_equal_the_stored_row(self):
        home = _tmp()
        store = HerdrLaunchGenerationStore(home=home)
        reserved = store.reserve_pending(
            assigned_name=NAME, startup_action_id="startup-A", workspace_id=WS,
            role=ROLE, lane_id=LANE,
        )
        self.assertEqual(reserved.phase, GENERATION_PENDING)
        self.assertEqual(store.read(NAME), reserved)  # reserve return == DB

        attested = store.finalize(
            assigned_name=NAME, startup_action_id="startup-A", workspace_id=WS,
            role=ROLE, lane_id=LANE, locator=LOCATOR, verdict=VERDICT_PRESENT,
            observed_at="2026-07-24T17:00:00+00:00",
        )
        self.assertEqual(attested.phase, GENERATION_ATTESTED)
        self.assertEqual(store.read(NAME), attested)  # finalize return == DB
        self.assertEqual(attested.as_payload(), store.read(NAME).as_payload())


class R7DiagnosticOnlyEvents(unittest.TestCase):
    """7. A startup execution event is diagnostic-only — never a close authority alone."""

    def _slot(self, **over):
        base = dict(assigned_name=NAME, provider=ROLE, locator=LOCATOR)
        base.update(over)
        return SimpleNamespace(**base)

    def _attestation(self, **over):
        base = dict(
            assigned_name=NAME, role=ROLE, workspace_id=WS, lane_id=LANE,
            locator=LOCATOR, verdict=VERDICT_PRESENT, observed_at="obs-1",
        )
        base.update(over)
        return SimpleNamespace(**base)

    def _reserve(self, home, token):
        HerdrLaunchGenerationStore(home=home).reserve_pending(
            assigned_name=NAME, startup_action_id=token, workspace_id=WS,
            role=ROLE, lane_id=LANE,
        )

    def test_reserve_finalize_helpers_bind_only_on_the_full_composite(self):
        # The happy path: participant (with receipt) + attestation_write_succeeded event +
        # exact attestation all present => the parent finalizes the reservation to attested.
        home = _tmp()
        token = _seed_fence_success(home, nonce="A")  # participant + receipt + success
        reserve_launch_generations(
            store_home=home, startup_action_id=token,
            launch_plans=[self._slot()], workspace_id=WS, lane_id=LANE,
        )
        append_execution_event(
            StartupTransactionFence(home=home), token,
            STAGE_ATTESTATION_WRITE_SUCCEEDED, participant=NAME,
        )
        finalize_launch_generations(
            store_home=home, startup_action_id=token, slots=[self._slot()],
            workspace_id=WS, lane_id=LANE, attestation_read=lambda n: self._attestation(),
        )
        self.assertEqual(HerdrLaunchGenerationStore(home=home).read(NAME).phase,
                         GENERATION_ATTESTED)

    def test_the_write_succeeded_event_alone_never_finalizes(self):
        # A wrapper execution event WITHOUT the main attestation is diagnostic-only — it must
        # not, on its own, flip the phase to attested (regression 7 / j#87472).
        home = _tmp()
        token = _seed_fence_success(home, nonce="A")
        self._reserve(home, token)
        append_execution_event(
            StartupTransactionFence(home=home), token,
            STAGE_ATTESTATION_WRITE_SUCCEEDED, participant=NAME,
        )
        finalize_launch_generations(
            store_home=home, startup_action_id=token, slots=[self._slot()],
            workspace_id=WS, lane_id=LANE, attestation_read=lambda n: None,  # NO attestation
        )
        self.assertEqual(HerdrLaunchGenerationStore(home=home).read(NAME).phase,
                         GENERATION_PENDING)

    def test_a_missing_write_succeeded_event_never_finalizes(self):
        # The attestation + participant present but NO attestation_write_succeeded event (only
        # an unrelated stage) => still pending. The event is a required composite member.
        home = _tmp()
        token = _seed_fence_success(home, nonce="A")
        self._reserve(home, token)
        append_execution_event(
            StartupTransactionFence(home=home), token,
            STAGE_SELF_LOOKUP_SUCCEEDED, participant=NAME,  # a DIFFERENT stage
        )
        finalize_launch_generations(
            store_home=home, startup_action_id=token, slots=[self._slot()],
            workspace_id=WS, lane_id=LANE, attestation_read=lambda n: self._attestation(),
        )
        self.assertEqual(HerdrLaunchGenerationStore(home=home).read(NAME).phase,
                         GENERATION_PENDING)

    def test_a_missing_participant_never_finalizes(self):
        # No startup-transaction participant recorded (an adopt-only slot) => pending, even
        # with the event and attestation present.
        home = _tmp()
        # Reserve the fence action but record NO participant, drive to success.
        fence = StartupTransactionFence(home=home)
        action = fence.reserve(
            StartupUnit(workspace_id=WS, lane_id=LANE, providers=(ROLE,)), "nonce-A"
        )
        token = action.action_id
        fence.set_phase(token, PHASE_COMPLETED_SUCCESS)
        self._reserve(home, token)
        append_execution_event(fence, token, STAGE_ATTESTATION_WRITE_SUCCEEDED,
                               participant=NAME)
        finalize_launch_generations(
            store_home=home, startup_action_id=token, slots=[self._slot()],
            workspace_id=WS, lane_id=LANE, attestation_read=lambda n: self._attestation(),
        )
        self.assertEqual(HerdrLaunchGenerationStore(home=home).read(NAME).phase,
                         GENERATION_PENDING)

    def test_reserve_failure_is_zero_actuation_typed(self):
        # A store that cannot be written (its home is a FILE, not a dir) raises — the caller
        # turns that into a typed zero-actuation launch refusal before any Herdr side effect.
        bad_home = _tmp() / "not-a-dir"
        bad_home.write_text("x")  # a regular file where the store dir must be
        with self.assertRaises(HerdrLaunchGenerationError):
            reserve_launch_generations(
                store_home=bad_home, startup_action_id="startup-A",
                launch_plans=[self._slot()], workspace_id=WS, lane_id=LANE,
            )


if __name__ == "__main__":
    unittest.main()
