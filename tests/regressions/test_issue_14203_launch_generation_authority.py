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


class R8GenerationProtocolCapability(unittest.TestCase):
    """F1 (j#87479): generation protocol is an INDEPENDENT launcher capability, preflighted
    before the first Herdr side effect — not inferred from the attestation schema."""

    def _help(self, *, attest=True, stores=True, generation=None):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
            build_attest_capability_contract_line,
            build_attest_capability_stores_line,
            build_generation_protocol_capability_line,
        )

        parts = ["usage: mozyo-bridge herdr agent-attest [--assigned-name NAME]"]
        if attest:
            parts.append(build_attest_capability_contract_line(2))
        if stores:
            parts.append(build_attest_capability_stores_line({1, 2}))
        if generation is not None:
            parts.append(build_generation_protocol_capability_line(generation))
        return "\n".join(parts) + "\n"

    def test_the_canonical_epilog_advertises_the_generation_marker(self):
        # The real launcher auto-advertises from the const, so a released launcher is capable.
        from mozyo_bridge.core.state.herdr_launch_generation import (
            HERDR_LAUNCH_GENERATION_PROTOCOL_VERSION as V,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
            build_agent_attest_epilog,
            decide_generation_protocol_capability,
            parse_launcher_capability_output,
        )

        obs = parse_launcher_capability_output(
            "--assigned-name\n" + build_agent_attest_epilog()
        )
        self.assertEqual(obs.advertised_generation_protocol_version, V)
        self.assertTrue(
            decide_generation_protocol_capability(obs, required_version=V).ok
        )

    def test_an_installed_launcher_with_attestation_but_no_generation_is_refused(self):
        # The exact skew j#87479 F1 names: a launcher whose attestation schema/store contract
        # landed (`450c77dc`) but predates the generation event (`69764b7e`) advertises the
        # attest markers and NOT the generation one — attestation-capable, generation-incapable.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
            GENERATION_PROTOCOL_CONTRACT_ABSENT,
            LAUNCHER_CAPABILITY_OK,
            decide_generation_protocol_capability,
            decide_launcher_capability,
            parse_launcher_capability_output,
        )

        obs = parse_launcher_capability_output(self._help(generation=None))
        # It PASSES the attestation decision (schema v2 matches) ...
        self.assertEqual(
            decide_launcher_capability(obs, required_schema_version=2).reason,
            LAUNCHER_CAPABILITY_OK,
        )
        # ... but is refused fail-closed by the independent generation decision.
        verdict = decide_generation_protocol_capability(obs, required_version=1)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, GENERATION_PROTOCOL_CONTRACT_ABSENT)

    def test_a_version_mismatch_is_refused(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
            GENERATION_PROTOCOL_VERSION_MISMATCH,
            decide_generation_protocol_capability,
            parse_launcher_capability_output,
        )

        obs = parse_launcher_capability_output(self._help(generation=2))
        verdict = decide_generation_protocol_capability(obs, required_version=1)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, GENERATION_PROTOCOL_VERSION_MISMATCH)

    def test_a_malformed_or_conflicting_marker_is_unprovable(self):
        # Strict token discipline (#13847 j#80000 finding 3): a malformed spelling is not
        # salvaged, and two conflicting versions arbitrate to neither — both fail closed.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
            GENERATION_PROTOCOL_CONTRACT_ABSENT,
            decide_generation_protocol_capability,
            parse_launcher_capability_output,
        )

        for bad in (
            "--assigned-name\nmozyo_generation_protocol_capability=1x\n",  # trailing garbage
            "--assigned-name\nmozyo_generation_protocol_capability=1\n"
            "mozyo_generation_protocol_capability=2\n",  # conflicting
            "--assigned-name\nxmozyo_generation_protocol_capability=1\n",  # not whole token
        ):
            obs = parse_launcher_capability_output(bad)
            self.assertIsNone(obs.advertised_generation_protocol_version, bad)
            self.assertEqual(
                decide_generation_protocol_capability(obs, required_version=1).reason,
                GENERATION_PROTOCOL_CONTRACT_ABSENT,
                bad,
            )


class R9RebuildableCacheRecovery(unittest.TestCase):
    """F2 (j#87479): a corrupt store degrades (rebuildable_cache) with a PUBLIC backup-first
    rebuild — it never bricks future launches, and never repairs implicitly."""

    def _view(self, *, live=(), ok=True, backend=True):
        agents = tuple(SimpleNamespace(name=n) for n in live)
        return SimpleNamespace(
            backend_selected=backend, ok=ok, managed_agents=agents,
            reason="unreadable", detail="probe failed",
        )

    def _corrupt(self, home):
        herdr_launch_generation_path(home).parent.mkdir(parents=True, exist_ok=True)
        herdr_launch_generation_path(home).write_bytes(b"not a sqlite database")

    def test_a_corrupt_store_refuses_every_launch_operation_fail_closed(self):
        home = _tmp()
        self._corrupt(home)
        # binding/recovery authority degrades to no token (never a stale generation) ...
        self.assertEqual(_token_for(home), "")
        # ... and reserve/read raise a typed error (not a silent repair).
        with self.assertRaises(HerdrLaunchGenerationError):
            HerdrLaunchGenerationStore(home=home).read(NAME)

    def test_status_classifies_absent_healthy_corrupt(self):
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_launch_generation_store_maintenance import (  # noqa: E501
            run_launch_generation_store_status,
        )
        from mozyo_bridge.core.state.herdr_launch_generation import (
            GENERATION_STORE_ABSENT,
            GENERATION_STORE_CORRUPT,
            GENERATION_STORE_HEALTHY,
        )

        home = _tmp()
        self.assertEqual(
            run_launch_generation_store_status(home=home).store_state,
            GENERATION_STORE_ABSENT,
        )
        _seed_generation(home, _seed_fence_success(home, nonce="A"))
        self.assertEqual(
            run_launch_generation_store_status(home=home).store_state,
            GENERATION_STORE_HEALTHY,
        )
        self._corrupt(_tmp2 := _tmp())
        r = run_launch_generation_store_status(home=_tmp2)
        self.assertEqual(r.store_state, GENERATION_STORE_CORRUPT)
        self.assertTrue(r.ok)  # status always reports
        self.assertTrue(any("rebuild --write" in n for n in r.notes))

    def test_backup_first_rebuild_recovers_and_only_relaunch_mints_a_new_generation(self):
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_launch_generation_store_maintenance import (  # noqa: E501
            APPLIED,
            PLANNED,
            run_launch_generation_store_rebuild,
        )

        home = _tmp()
        self._corrupt(home)
        path = herdr_launch_generation_path(home)

        # A dry-run plan performs no removal.
        plan = run_launch_generation_store_rebuild(home=home, view=self._view(), write=False)
        self.assertEqual(plan.state, PLANNED)
        self.assertTrue(path.exists())

        # --write: backup-first, then the corrupt store is removed. The rebuild itself does
        # NOT mint a generation — the store is simply absent again (fail-closed) ...
        applied = run_launch_generation_store_rebuild(
            home=home, view=self._view(), write=True
        )
        self.assertEqual(applied.state, APPLIED)
        self.assertTrue(applied.executed)
        self.assertIsNotNone(applied.backup_dir)
        self.assertTrue(Path(applied.backup_dir).exists())  # the prior bytes are preserved
        self.assertFalse(path.exists())
        self.assertEqual(_token_for(home), "")  # still fail-closed, no fabricated generation

        # ... only the NEXT managed launch (reserve + finalize) re-creates a live generation.
        token = _seed_fence_success(home, nonce="A")
        _seed_generation(home, token)
        self.assertEqual(_token_for(home), token)

    def test_rebuild_is_refused_while_a_live_consumer_holds_a_generation(self):
        # A healthy store IS enumerable, so a live agent holding a row blocks a (misdirected)
        # rebuild — discarding a live agent's generation is refused.
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_launch_generation_store_maintenance import (  # noqa: E501
            BLOCKED_CONSUMERS_UNMEASURABLE,
            BLOCKED_STORE_HEALTHY,
            run_launch_generation_store_rebuild,
        )

        home = _tmp()
        _seed_generation(home, _seed_fence_success(home, nonce="A"))  # gw holds a generation
        # Healthy store -> refused outright (nothing corrupt; rebuild would discard rows).
        healthy = run_launch_generation_store_rebuild(
            home=home, view=self._view(live=["gw"]), write=True
        )
        self.assertEqual(healthy.state, BLOCKED_STORE_HEALTHY)

        # Corrupt store WITH a live agent -> rows unmeasurable, so it cannot be proven the
        # agent does not consume it: refused fail-closed with a public next action.
        self._corrupt(home)
        blocked = run_launch_generation_store_rebuild(
            home=home, view=self._view(live=["gw"]), write=True
        )
        self.assertEqual(blocked.state, BLOCKED_CONSUMERS_UNMEASURABLE)
        self.assertFalse(blocked.ok)
        self.assertIn("Retire / close", blocked.detail)  # public next action
        self.assertTrue(herdr_launch_generation_path(home).exists())  # not removed

    def test_an_unreadable_inventory_refuses_the_rebuild(self):
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_launch_generation_store_maintenance import (  # noqa: E501
            BLOCKED_INVENTORY_UNREADABLE,
            run_launch_generation_store_rebuild,
        )

        home = _tmp()
        self._corrupt(home)
        r = run_launch_generation_store_rebuild(
            home=home, view=self._view(ok=False), write=True
        )
        self.assertEqual(r.state, BLOCKED_INVENTORY_UNREADABLE)
        self.assertTrue(herdr_launch_generation_path(home).exists())  # untouched


class R10RebuildAtomicityAndLock(unittest.TestCase):
    """R12 (j#87488 P1): the rebuild pins the generation under an exclusive lock, proves
    backup-first, and reports side-effect truth — a mature rail, not a raw quarantine."""

    def _view(self, *, live=(), ok=True, backend=True):
        agents = tuple(SimpleNamespace(name=n) for n in live)
        return SimpleNamespace(
            backend_selected=backend, ok=ok, managed_agents=agents,
            reason="unreadable", detail="probe failed",
        )

    def _corrupt(self, home):
        herdr_launch_generation_path(home).parent.mkdir(parents=True, exist_ok=True)
        herdr_launch_generation_path(home).write_bytes(b"not a sqlite database")

    def _maint(self):
        import mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_launch_generation_store_maintenance as m  # noqa: E501
        return m

    def test_vanished_after_probe_is_blocked_never_a_fabricated_backup(self):
        # The store disappears between probe and quarantine: quarantine returns None (nothing
        # preserved). Reporting APPLIED with backup_dir=null would fabricate a recovery point.
        from unittest.mock import patch

        m = self._maint()
        home = _tmp()
        self._corrupt(home)
        with patch.object(m, "quarantine_attestation_store_artifacts", lambda p: None):
            r = m.run_launch_generation_store_rebuild(
                home=home, view=self._view(), write=True
            )
        self.assertEqual(r.state, m.BLOCKED_FAILED)
        self.assertFalse(r.ok)
        self.assertIsNone(r.backup_dir)
        self.assertFalse(r.executed)
        self.assertIn("disappeared", r.detail)

    def test_partial_unlink_is_a_structured_removal_interruption_not_a_raw_oserror(self):
        # The backup published, but removal is interrupted (an OSError mid-unlink). The result
        # is structured (backup_dir + executed=true + "NOT untouched"), never a raw traceback.
        from unittest.mock import patch

        m = self._maint()
        home = _tmp()
        self._corrupt(home)

        def _boom(path):
            raise OSError("disk gone mid-unlink")

        with patch.object(m, "remove_attestation_store_artifacts", _boom):
            r = m.run_launch_generation_store_rebuild(
                home=home, view=self._view(), write=True
            )  # must NOT raise
        self.assertEqual(r.state, m.BLOCKED_FAILED)
        self.assertTrue(r.executed)
        self.assertIsNotNone(r.backup_dir)
        self.assertIn("NOT untouched", r.detail)

    def test_rebuild_is_blocked_while_a_managed_write_holds_the_lock(self):
        # An in-flight managed-launch write holds the store lock SHARED; the rebuild's
        # exclusive non-blocking acquire fails, so it does not start — the store is untouched.
        from mozyo_bridge.core.state.herdr_launch_generation import (
            launch_generation_store_lock,
        )

        m = self._maint()
        home = _tmp()
        self._corrupt(home)
        with launch_generation_store_lock(home, exclusive=False, blocking=True):
            r = m.run_launch_generation_store_rebuild(
                home=home, view=self._view(), write=True
            )
        self.assertEqual(r.state, m.BLOCKED_FAILED)
        self.assertIn("in use", r.detail)
        self.assertTrue(herdr_launch_generation_path(home).exists())  # untouched

    def test_the_exclusive_lock_excludes_a_peer_write_across_the_whole_rotation(self):
        # The path-ABA closure, proven with the REAL lock: while the rebuild holds the store
        # EXCLUSIVE and is mid-quarantine, a peer managed-launch write (SHARED) cannot start —
        # so no peer can replace the store between the probe and the removal.
        from unittest.mock import patch

        from mozyo_bridge.core.state.herdr_launch_generation import (
            LaunchGenerationStoreLockBusy,
            launch_generation_store_lock,
        )

        m = self._maint()
        home = _tmp()
        self._corrupt(home)
        real_quarantine = m.quarantine_attestation_store_artifacts
        peer_write_admitted = {}

        def _spy(path):
            # A peer managed-launch write takes the SHARED lock; while rebuild holds EXCLUSIVE
            # it must be refused.
            try:
                with launch_generation_store_lock(home, exclusive=False, blocking=False):
                    peer_write_admitted["v"] = True
            except LaunchGenerationStoreLockBusy:
                peer_write_admitted["v"] = False
            return real_quarantine(path)

        with patch.object(m, "quarantine_attestation_store_artifacts", _spy):
            r = m.run_launch_generation_store_rebuild(
                home=home, view=self._view(), write=True
            )
        self.assertEqual(r.state, m.APPLIED)
        self.assertIn("v", peer_write_admitted)  # the spy actually ran
        self.assertFalse(peer_write_admitted["v"])  # the peer write was excluded


if __name__ == "__main__":
    unittest.main()
