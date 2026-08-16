"""Regression pins for the #14477 immutable resume-freshness anchor.

Redmine #14477 (parent #13490), derived from the #14476 j#88614-j#88618 live run. ``sublane
resume`` (#13682) proves a relaunched pair is a genuine post-hibernate generation by requiring
each slot's startup self-attestation (#13637) to be observed strictly AFTER the lane
hibernated. Before this change that boundary was read from the lifecycle row's generic
``updated_at`` — the column EVERY write moves — so the metadata-only ``repair-pins`` CAS
(#13879), which fills an empty declared-pin snapshot and launches / closes / resumes / sends
NOTHING, pushed the boundary PAST the self-attestation of the exact live pair it had just
verified. ``recover-pair`` / ``resume`` then refused that pair ``stale_generation`` forever and
the lane could only move through an action-specific glass-break.

The measured ordering is the whole defect, so it is the shape these pins reproduce:

    T0 hibernate  <  T1 fresh pair self-attests  <  T2 metadata-only repair-pins

with ``hibernated_at`` (schema v8) stamped ONLY at T0. Pinned here:

1. **acceptance 1** — that exact ordering resumes through the standard semantic rail, with no
   glass-break, and the mechanism is pinned directly (the repair moves ``updated_at`` and
   leaves ``hibernated_at`` byte-equal) rather than only its outcome;
2. **acceptance 2** — a REAL pre-hibernate survivor still fails closed. Redmine #14756 /
   #14955 supersede the timestamp reason at the resume integration boundary: the survivor's
   immutable process env lacks the epoch minted at T0, so the epoch verdict now refuses it;
3. **acceptance 3** — metadata-only writes (declared-pin repair, release request / outcome)
   never move the boundary;
4. **acceptance 4** — resume still atomically adopts the exact fresh declared pin snapshot,
   and the provider-binding fence still blocks;
5. **acceptance 5** — a pre-v8 row (no anchor) stays readable WITHOUT migrating the store, and
   gets NO substitute boundary. Review j#94515 F1 / verdict
   j#94520 measured the alternative — standing ``updated_at`` in for the boundary admitted a
   genuine pre-hibernate survivor, because ``updated_at`` is not monotonic (no writer on this
   component validates its caller-supplied ``now`` against the row's prior stamp). That exact
   ordering is pinned here, as is the absence of ANY substitute column;
6. the anchor's own lifecycle: cleared on rehydrate, re-stamped on the next hibernation, and
   its inbound-edge enumeration DERIVED from the public transition policy rather than recalled,
   so a future edge into ``hibernated`` cannot quietly bypass the stamp;
7. the historical CLOCK-INDEPENDENT survivor fence (``ReleasedLocatorFenceTest``) — review j#94531 R2-F1,
   disposition j#94544 A. A backdated stamp defeats the timestamp, so the generation is
   discriminated by the locators hibernate's release closed: the same locator refuses,
   all-different resumes, absent evidence refuses. The timestamp is a liveness boundary only;
   it is never called the generation proof anywhere in this module. #14756/#14955 replace
   this resume authority with lane epoch while retaining these pure helper characterizations.
8. the fence's own REMAINING hole (``ReleaseObservationBindingTest``) — review j#94570 R3-F1.
   ``release_pins`` is driver-enumerated on the public hibernate rail but the store-level
   ``request_release`` accepts arbitrary pins, so a direct store caller can record locators
   other than the live ones and a survivor is admitted. Stated as an expected failure rather
   than inverted into a pin of the defect; scope disposition requested in j#94581.

Everything is synthetic: a temp store path, a fake herdr inventory and fake attestation reads.
No pane / process / route / worktree mutation. Hermeticity is itself CHECKED rather than
intended (``OperatorHomeHermeticityTest``, j#94504 item 4): the v8 bump forward-migrated the
operator's shared ``state.sqlite`` from inside a full-suite run, because a store built without
an explicit ``path=`` resolves ``MOZYO_BRIDGE_HOME`` / ``~/.mozyo_bridge`` and writing there
takes the write-MIGRATING gate.
"""

from __future__ import annotations

import ast
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.core.state.lane_release_observation import (  # noqa: E402
    build_release_observation,
)
from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    IdentityAttestationRecord,
    VERDICT_PRESENT,
)
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore  # noqa: E402
from mozyo_bridge.core.state.lane_hibernation_anchor import (  # noqa: E402
    ANCHOR_HIBERNATE_TRANSITION,
    ANCHOR_UNAVAILABLE,
    hibernation_anchor_on_transition,
    resume_freshness_anchor,
)
from mozyo_bridge.core.state.lane_lifecycle import (  # noqa: E402
    BINDING_KIND_ISSUE,
    CAS_FORBIDDEN_TRANSITION,
    CAS_UNEXPECTED_STATE,
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DISPOSITION_RETIRED,
    DISPOSITIONS,
    RELEASE_NOT_REQUESTED,
    RELEASE_PARTIAL,
    RELEASE_RELEASED,
    RELEASE_REQUESTED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleRecord,
    LaneLifecycleStore,
    ProcessGenerationPin,
    ReleasePin,
    disposition_transition_allowed,
    encode_release_pins,
)
from mozyo_bridge.core.state.lane_lifecycle_schema import (  # noqa: E402
    LANE_LIFECYCLE_COMPONENT,
    LANE_LIFECYCLE_SCHEMA_VERSION,
)
from mozyo_bridge.core.state.lane_lifecycle_shapes import _COLUMN_DEFS  # noqa: E402
from mozyo_bridge.core.state.lane_epoch import EPOCH_NOT_NEWER  # noqa: E402
from mozyo_bridge.core.state.lane_pin_repair import LanePinRepairStore  # noqa: E402
from mozyo_bridge.core.state.lane_pin_role import read_declared_pin_pair  # noqa: E402
from mozyo_bridge.core.state.lane_release import (  # noqa: E402
    OBSERVATION_ABSENT,
    OBSERVATION_GENERATION_NOT_COMPLETED,
    OBSERVATION_PIN_MISMATCH,
    OBSERVATION_RELEASE_STATE_UNKNOWN,
    OBSERVATION_STALE_AFTER_RESET,
    OBSERVATION_UNREADABLE,
    open_release_generation,
    verify_release_observation,
)
from mozyo_bridge.core.state.lane_release_observation import (  # noqa: E402
    ReleaseObservationError,
    build_release_observation,
    encode_release_observation,
)
from mozyo_bridge.core.state.lane_released_locator_fence import (  # noqa: E402
    FENCE_COMPLETE_EMPTY,
    FENCE_EVIDENCE_ABSENT,
    FENCE_LOCATOR_REUSED,
    FENCE_OK,
    released_locator_verdict,
)
from mozyo_bridge.core.state.state_store import state_store_path  # noqa: E402
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_resume import (  # noqa: E402,E501
    BLOCK_PAIR_ATTESTATION,
    ResumeRequest,
    SublaneResumeUseCase,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
)

_WS = "a71f4c93b2e84d16"
_LANE = "issue_14477_repair_pins_resume_freshness_anchor"
_ISSUE = "14477"
_JOURNAL = "94484"
_RESUME_JOURNAL = "94490"
_BOUND_WT = "wt_14477a1b2c3d4e"
_GW_PROVIDER = "codex"
_WK_PROVIDER = "claude"

#: The measured ordering (#14476 j#88614-j#88618), as fixed-width UTC ISO-seconds so a lexical
#: compare is a time compare — exactly what the freshness gate does.
T_DECLARE = "2026-07-26T19:40:00+00:00"
T_SURVIVOR = "2026-07-26T19:50:00+00:00"  # a pane that predates the hibernation
T_HIBERNATE = "2026-07-26T20:00:00+00:00"  # T0 — the immutable boundary
T_RELEASE = "2026-07-26T20:00:05+00:00"  # still hibernate-side, but a LATER metadata write
T_FRESH = "2026-07-26T20:10:00+00:00"  # T1 — the relaunched pair self-attests
T_REPAIR = "2026-07-26T20:20:00+00:00"  # T2 — the metadata-only pin repair
T_RESUME = "2026-07-26T20:30:00+00:00"
#: A metadata write whose stamp REGRESSES below the hibernation (review j#94515 F1). No writer
#: on this component validates ``now`` against the row's prior stamp, so this is reachable from
#: a backdated programmatic caller or a regressed wall clock — not a contrived value.
T_BACKDATED = "2026-07-26T19:40:00+00:00"
#: A relaunch attested after the SECOND hibernation (stamped ``T_RESUME``).
T_LATER = "2026-07-26T20:40:00+00:00"

_GW_LOC = f"{_WS}:p4A"
_WK_LOC = f"{_WS}:p4B"
_STARTUP_ACTION = "startup-14477-current"


def _terminal_id(locator: str) -> str:
    """Stable synthetic server-owned terminal identity for one fake live locator."""
    return f"terminal:{locator}"


def _decision(journal: str = _JOURNAL) -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=_ISSUE, journal_id=journal)


def _gw_name() -> str:
    return encode_assigned_name(_WS, _GW_PROVIDER, _LANE)


def _wk_name() -> str:
    return encode_assigned_name(_WS, _WK_PROVIDER, _LANE)


def _live_pins() -> tuple[ProcessGenerationPin, ...]:
    """The exact live pair a ``repair-pins`` run fills the empty snapshot with."""
    return (
        ProcessGenerationPin(
            role="gateway",
            provider=_GW_PROVIDER,
            assigned_name=_gw_name(),
            locator=_GW_LOC,
            attested_at=T_FRESH,
        ),
        ProcessGenerationPin(
            role="worker",
            provider=_WK_PROVIDER,
            assigned_name=_wk_name(),
            locator=_WK_LOC,
            attested_at=T_FRESH,
        ),
    )


#: The epoch a lane that has hibernated once has minted (#14756). These fixtures drive
#: exactly one hibernate transition before resuming.
_CURRENT_EPOCH = "1"
#: ...and after a SECOND hibernate, which several fixtures below drive.
_SECOND_EPOCH = "2"


def _attest(
    provider: str, locator: str, observed_at: str, lane_epoch: str = _CURRENT_EPOCH
) -> IdentityAttestationRecord:
    """A startup self-attestation for this fixture's lane.

    ``lane_epoch`` defaults to the epoch a lane hibernated ONCE has minted (Redmine #14756),
    because that is what a genuine relaunch of these fixtures' pairs would have received. It
    is a parameter rather than a constant so a test can attest a PRE-hibernate epoch, which
    is what a survivor carries. A minted exact epoch now replaces timestamp/released-locator
    generation authority at the resume boundary (#14955), so tests that describe a survivor
    must supply its actual pre-hibernate epoch rather than an impossible current one.
    """
    return IdentityAttestationRecord(
        assigned_name=encode_assigned_name(_WS, provider, _LANE),
        workspace_id=_WS,
        role=provider,
        lane_id=_LANE,
        locator=locator,
        verdict=VERDICT_PRESENT,
        observed_at=observed_at,
        lane_epoch=lane_epoch,
        terminal_id=_terminal_id(locator),
    )


class _FakeOps:
    """The resume IO port: a canned live inventory + attestation reads (read-only)."""

    def __init__(
        self,
        *,
        observed_at: str = T_FRESH,
        providers=(_GW_PROVIDER, _WK_PROVIDER),
        gw_locator: str = _GW_LOC,
        wk_locator: str = _WK_LOC,
        lane_epoch: str = _CURRENT_EPOCH,
    ):
        self._rows = [
            {
                "name": _gw_name(),
                "pane_id": gw_locator,
                "terminal_id": _terminal_id(gw_locator),
            },
            {
                "name": _wk_name(),
                "pane_id": wk_locator,
                "terminal_id": _terminal_id(wk_locator),
            },
        ]
        self._attest = {
            _gw_name(): _attest(_GW_PROVIDER, gw_locator, observed_at, lane_epoch),
            _wk_name(): _attest(_WK_PROVIDER, wk_locator, observed_at, lane_epoch),
        }
        self._providers = providers

    def workspace_id(self) -> str:
        return _WS

    def live_rows(self):
        return list(self._rows)

    def read_attestation(self, assigned_name):
        return self._attest.get(assigned_name)

    def provider_pair(self):
        return tuple(self._providers)


class _Fixture(unittest.TestCase):
    """A hibernated / released BOUND row with the #13879 pins-only gap, on a temp store."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "state.sqlite"
        self.key = LaneLifecycleKey(_WS, _LANE)
        self.store = LaneLifecycleStore(path=self.path)
        self._seed()

    def _seed(self) -> None:
        """Drive the row to hibernated / released through the REAL store transitions.

        Every seed CAS is asserted: a silently refused seed would leave the row in a shape
        other than the one this test names, and the assertion under test would then pass or
        fail for a reason the test never states.
        """
        out = LaneDeclarationStore(path=self.path).declare_lane(
            self.key,
            decision=_decision(),
            issue_id=_ISSUE,
            declared_slots=(),  # the #13879 pins-only gap
            worktree_identity=_BOUND_WT,
            now=T_DECLARE,
        )
        self.assertTrue(out.applied, f"seed declare_lane refused: {out.reason}")
        out = self.store.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=self._rec().revision,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
            now=T_HIBERNATE,
        )
        self.assertTrue(out.applied, f"seed hibernate refused: {out.reason}")
        out = self.store.request_release(
            self.key,
            expected_revision=self._rec().revision,
            action_id="rel-14477",
            observation=build_release_observation([
                ReleasePin("gateway", _gw_name(), f"{_WS}:pOLD_G", "startup-old"),
                ReleasePin("worker", _wk_name(), f"{_WS}:pOLD_W", "startup-old"),
            ]),
            now=T_RELEASE,
        )
        self.assertTrue(out.applied, f"seed request_release refused: {out.reason}")
        out = self.store.record_release_outcome(
            self.key,
            action_id="rel-14477",
            expected_revision=self._rec().revision,
            target=RELEASE_RELEASED,
            now=T_RELEASE,
        )
        self.assertTrue(out.applied, f"seed release outcome refused: {out.reason}")

    def _rec(self) -> LaneLifecycleRecord:
        return self.store.get(self.key)

    def _repair_pins(self, *, now: str = T_REPAIR):
        """The metadata-only #13879 declared-pin repair at ``now``."""
        rec = self._rec()
        return LanePinRepairStore(path=self.path).repair_hibernated_bound_pins(
            self.key,
            expected_revision=rec.revision,
            expected_generation=rec.lane_generation,
            issue_id=_ISSUE,
            worktree_identity=_BOUND_WT,
            declared_slots=_live_pins(),
            decision=_decision(),
            now=now,
        )

    def _resume(self, ops=None, *, execute: bool = True):
        return SublaneResumeUseCase(ops=ops or _FakeOps(), store=self.store).run(
            ResumeRequest(issue=_ISSUE, lane=_LANE, journal=_RESUME_JOURNAL),
            execute=execute,
        )


class RepairThenResumeTest(_Fixture):
    """Acceptance 1 + 3: the metadata-only repair no longer invalidates the fresh pair."""

    def test_the_measured_ordering_resumes_through_the_standard_rail(self) -> None:
        """T0 hibernate < T1 fresh attestation < T2 repair-pins -> resume, no glass-break."""
        self.assertTrue(self._repair_pins().applied)
        outcome = self._resume()
        self.assertFalse(
            outcome.is_blocked,
            f"blocked: {outcome.preflight.blocked_reasons} "
            f"({outcome.preflight.pair_attestation_detail})",
        )
        self.assertTrue(outcome.transition.applied)
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_ACTIVE)

    def test_the_repair_moves_updated_at_and_leaves_the_boundary_byte_equal(self) -> None:
        """The MECHANISM, not only the outcome: the two timestamps are now separate axes.

        Pinning the outcome alone would still pass if some later change re-coupled the
        boundary to a column that merely happens not to move in this fixture.
        """
        before = self._rec()
        self.assertEqual(before.hibernated_at, T_HIBERNATE)
        # The release generation already moved ``updated_at`` past the hibernation while the
        # boundary stayed put, so the split is load-bearing before the repair even runs.
        self.assertEqual(before.updated_at, T_RELEASE)

        self.assertTrue(self._repair_pins().applied)

        after = self._rec()
        self.assertEqual(after.updated_at, T_REPAIR)  # the metadata write DID move
        self.assertEqual(after.hibernated_at, T_HIBERNATE)  # the boundary did NOT
        self.assertGreater(after.updated_at, T_FRESH)  # the exact pre-#14477 false-stale shape
        self.assertLess(after.hibernated_at, T_FRESH)  # ...which the boundary is immune to

    def test_the_freshness_authority_is_the_hibernate_transition(self) -> None:
        self.assertTrue(self._repair_pins().applied)
        anchor, authority = resume_freshness_anchor(self._rec())
        self.assertEqual(anchor, T_HIBERNATE)
        self.assertEqual(authority, ANCHOR_HIBERNATE_TRANSITION)

    def test_resume_still_adopts_the_exact_fresh_declared_pin_snapshot(self) -> None:
        """Acceptance 4: the fix is a threshold change, not a relaxed adoption."""
        self.assertTrue(self._repair_pins().applied)
        self.assertFalse(self._resume().is_blocked)
        pair = read_declared_pin_pair(self._rec())
        self.assertTrue(pair.ok, pair.reason)
        self.assertEqual(pair.gateway.locator, _GW_LOC)
        self.assertEqual(pair.worker.locator, _WK_LOC)
        self.assertEqual(
            (pair.gateway.provider, pair.worker.provider), (_GW_PROVIDER, _WK_PROVIDER)
        )

    def test_the_provider_binding_fence_still_blocks_after_the_repair(self) -> None:
        """Acceptance 4: no existing fence is loosened by moving the threshold."""
        self.assertTrue(self._repair_pins().applied)
        drifted = _FakeOps(providers=("claude", "codex"))  # the stored pair is codex/claude
        outcome = self._resume(ops=drifted)
        self.assertTrue(outcome.is_blocked)
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_HIBERNATED)


class SurvivorStaysFailClosedTest(_Fixture):
    """Acceptance 2: the gate still refuses what it exists to refuse."""

    def test_a_pre_hibernate_survivor_is_still_stale_after_the_same_repair(self) -> None:
        self.assertTrue(self._repair_pins().applied)
        # Same live locators, same repair — only the self-attestation predates T0. This is the
        # survivor the locator pin alone cannot tell from a relaunch.
        outcome = self._resume(ops=_FakeOps(observed_at=T_SURVIVOR, lane_epoch=""))
        self.assertTrue(outcome.is_blocked)
        self.assertIn(BLOCK_PAIR_ATTESTATION, outcome.preflight.blocked_reasons)
        self.assertIn(
            "lane_epoch_attestation_absent",
            outcome.preflight.pair_attestation_detail,
        )
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_HIBERNATED)
        self.assertIsNone(outcome.transition)

    def test_exact_epoch_ignores_timestamp_equal_to_legacy_boundary(self) -> None:
        """#14955: a caller-controlled clock no longer vetoes exact epoch authority."""
        outcome = self._resume(ops=_FakeOps(observed_at=T_HIBERNATE))
        self.assertFalse(outcome.is_blocked)


class BoundaryLifecycleTest(_Fixture):
    """The anchor's own write lifecycle — stamped, cleared, re-stamped; never drifting."""

    def test_a_fresh_active_lane_holds_no_boundary(self) -> None:
        store = LaneLifecycleStore(path=Path(self._tmp.name) / "fresh.sqlite")
        key = LaneLifecycleKey(_WS, "issue_14477_fresh")
        self.assertTrue(
            store.declare_active(
                key, decision=_decision(), issue_id=_ISSUE, now=T_DECLARE
            ).applied
        )
        self.assertEqual(store.get(key).hibernated_at, "")

    def test_rehydrate_clears_the_boundary_and_the_next_hibernation_restamps_it(self) -> None:
        self.assertTrue(self._repair_pins().applied)
        self.assertFalse(self._resume().is_blocked)
        # Awake: no boundary is in force, so a stale one can never be read as a threshold.
        self.assertEqual(self._rec().hibernated_at, "")

        out = self.store.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=self._rec().revision,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
            now=T_RESUME,
        )
        self.assertTrue(out.applied, out.reason)
        self.assertEqual(self._rec().hibernated_at, T_RESUME)

    def test_a_terminal_transition_preserves_the_boundary_as_an_audit_fact(self) -> None:
        out = self.store.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_HIBERNATED,
            expected_revision=self._rec().revision,
            target=DISPOSITION_RETIRED,
            decision=_decision(),
            now=T_RESUME,
        )
        self.assertTrue(out.applied, out.reason)
        self.assertEqual(self._rec().hibernated_at, T_HIBERNATE)

    def test_only_the_active_disposition_can_enter_hibernation(self) -> None:
        """DERIVE the inbound-edge set from the public policy; never recall it.

        The stamp lives on one CAS because ``active -> hibernated`` is the only way in. If a
        later version adds another inbound edge, this fails and forces that writer to stamp
        the boundary too — instead of silently inheriting a stale one.
        """
        inbound = {
            d
            for d in DISPOSITIONS
            if disposition_transition_allowed(d, DISPOSITION_HIBERNATED)
        }
        self.assertEqual(inbound, {DISPOSITION_ACTIVE})

    def test_the_transition_rule_is_total_over_every_disposition(self) -> None:
        """No disposition falls through to an unhandled branch (also derived, not listed)."""
        for target in DISPOSITIONS:
            value = hibernation_anchor_on_transition(
                T_HIBERNATE, target=target, stamp=T_RESUME
            )
            if target == DISPOSITION_HIBERNATED:
                self.assertEqual(value, T_RESUME)
            elif target == DISPOSITION_ACTIVE:
                self.assertEqual(value, "")
            else:
                self.assertEqual(value, T_HIBERNATE)


class PreV8CompatibilityTest(_Fixture):
    """Acceptance 5: an old row is read, labelled, and never guessed at."""

    def _rewind_to_v7(self) -> bytes:
        """Drop the v8 column and record v7 — a genuine pre-#14477 store shape."""
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("ALTER TABLE lane_lifecycle_records DROP COLUMN hibernated_at")
            # v9 (#14477 j#94582) added release_observation; a faithful pre-v9 rewind drops it.
            conn.execute("ALTER TABLE lane_lifecycle_records DROP COLUMN release_observation")
            # v10 (#14756) added lane_epoch; a faithful pre-v10 rewind drops it too.
            conn.execute("ALTER TABLE lane_lifecycle_records DROP COLUMN lane_epoch")
            # v11 (#15227) added reconcile_close_pin; retaining it would be a partial/newer
            # shape merely stamped v7, not a genuine historical v7 store.
            conn.execute("ALTER TABLE lane_lifecycle_records DROP COLUMN reconcile_close_pin")
            conn.execute(
                "UPDATE state_schema_components SET schema_version = 7 WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            )
            conn.commit()
        finally:
            conn.close()
        return self.path.read_bytes()

    def test_a_pre_v8_row_reads_without_migrating_and_has_no_boundary(self) -> None:
        self.assertTrue(self._repair_pins().applied)
        before = self._rewind_to_v7()

        rec = self.store.get(self.key)

        self.assertEqual(rec.hibernated_at, "")  # the padded additive default, not a guess
        # No substitute is invented from any other column on the row — notably NOT its own
        # ``updated_at``, which is present and non-empty here.
        self.assertEqual(rec.updated_at, T_REPAIR)
        self.assertEqual(resume_freshness_anchor(rec), ("", ANCHOR_UNAVAILABLE))
        # The read is non-migrating (#13844): not one byte moved, the version is still 7.
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_regressed_updated_at_cannot_admit_a_pre_hibernate_survivor(self) -> None:
        """Review j#94515 F1 / verdict j#94520 — the exact measured ordering.

        ``updated_at`` is NOT monotonic: every CAS on this component takes a caller-supplied
        ``now`` and no writer validates it against the row's prior stamp, so a backdated caller
        or a regressed wall clock (NTP step-back, skewed host) leaves it EARLIER than the true
        hibernation. Substituting it as the boundary therefore produced

            updated_at (19:40)  <  survivor attestation (19:50)  <  true hibernate (20:00)

        and admitted a genuine pre-hibernate survivor, flipping the lane to ``active``. The
        boundary must be absent-and-refused here, never reconstructed from a mutable column.
        """
        self.assertEqual(self._rec().hibernated_at, T_HIBERNATE)  # the TRUE boundary
        # A public metadata-only repair whose stamp REGRESSES below the hibernation.
        self.assertTrue(self._repair_pins(now=T_BACKDATED).applied)
        self._rewind_to_v7()

        rec = self.store.get(self.key)
        self.assertEqual(rec.updated_at, T_BACKDATED)
        self.assertLess(rec.updated_at, T_SURVIVOR)  # the ordering that used to admit
        self.assertLess(T_SURVIVOR, T_HIBERNATE)  # ...a REAL pre-hibernate survivor

        outcome = self._resume(ops=_FakeOps(observed_at=T_SURVIVOR))

        self.assertTrue(outcome.is_blocked)
        self.assertIn(BLOCK_PAIR_ATTESTATION, outcome.preflight.blocked_reasons)
        self.assertIn(ANCHOR_UNAVAILABLE, outcome.preflight.pair_attestation_detail)
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_HIBERNATED)
        self.assertIsNone(outcome.transition)

    def test_a_legacy_row_fails_closed_even_with_a_genuinely_fresh_pair(self) -> None:
        """The refusal is unconditional on a row with no boundary — the safe direction.

        A legacy lane cannot prove freshness at all, so even a truly fresh pair is refused
        rather than admitted on the locator pin alone. This is a deliberate, stated functional
        regression for pre-v8 rows (verdict j#94520): such a lane resumes only after passing
        through a v8 hibernate transition. The alternative — a substitute threshold — is what
        the test above measures as unsafe.
        """
        self._rewind_to_v7()  # no repair: updated_at is still the hibernate-side stamp
        outcome = self._resume()  # a genuinely fresh pair, attested after the hibernation
        self.assertTrue(outcome.is_blocked)
        self.assertIn(ANCHOR_UNAVAILABLE, outcome.preflight.pair_attestation_detail)
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_HIBERNATED)

    def test_an_unresolvable_boundary_does_not_veto_minted_epoch(self) -> None:
        """An absent timestamp is irrelevant once the exact epoch proves the generation.

        The hibernate transition mints the epoch that a fresh launch receives in its immutable
        process environment.  A survivor cannot acquire that value, so an exact attestation
        distinguishes the generation without substituting another clock when the legacy
        timestamp is absent.  This test admits only that minted-exact shape.
        """
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "UPDATE lane_lifecycle_records SET updated_at = '', hibernated_at = ''"
            )
            conn.commit()
        finally:
            conn.close()
        rec = self.store.get(self.key)
        self.assertEqual(resume_freshness_anchor(rec), ("", ANCHOR_UNAVAILABLE))

        outcome = self._resume()
        self.assertFalse(outcome.is_blocked)
        self.assertNotIn(ANCHOR_UNAVAILABLE, outcome.preflight.pair_attestation_detail)
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_ACTIVE)

    def test_an_absent_row_resolves_to_no_boundary_rather_than_a_guess(self) -> None:
        self.assertEqual(resume_freshness_anchor(None), ("", ANCHOR_UNAVAILABLE))

    def test_no_column_on_the_row_is_ever_used_as_a_substitute_boundary(self) -> None:
        """Derived, not recalled: NO stored string field may stand in for the anchor.

        Enumerates the record's own text fields rather than naming ``updated_at``, so a future
        "helpful" fallback to ``created_at`` or any other timestamp-bearing column is caught
        by this pin instead of shipping as a fresh survivor hole.
        """
        self.assertTrue(self._repair_pins().applied)
        self._rewind_to_v7()
        rec = self.store.get(self.key)
        anchor, authority = resume_freshness_anchor(rec)
        self.assertEqual((anchor, authority), ("", ANCHOR_UNAVAILABLE))
        candidates = {
            name: value
            for name, value in vars(rec).items()
            if isinstance(value, str) and value and name != "hibernated_at"
        }
        self.assertIn("updated_at", candidates)  # the fixture really does offer a temptation
        self.assertIn("created_at", candidates)
        for name, value in candidates.items():
            self.assertNotEqual(
                anchor, value, f"{name} was used as a substitute freshness boundary"
            )

class ReleasedLocatorFenceTest(_Fixture):
    """The clock-independent survivor fence — Redmine #14477 review j#94531 R2-F1,
    coordinator disposition j#94544 A.

    A timestamp cannot carry the generation proof: a backdated CAS stamp, a regressed host clock
    and a self-written ``observed_at`` each defeat it. So the proof is that hibernate's release
    recorded the exact locators it closed — a survivor keeps its pane-id and is in that set, a
    relaunch is not. These pins are ordinary red->green regressions (the earlier known-hole
    marker is retired), covering the three boundaries the disposition names: the SAME locator
    refuses, ALL-DIFFERENT locators still resume, and ABSENT evidence refuses.
    """

    def _hibernate_again(self, *, released, now):
        """Drive active -> hibernated -> released again, recording ``released`` as closed."""
        out = self.store.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=self._rec().revision,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
            now=now,
        )
        self.assertTrue(out.applied, out.reason)
        if released is None:  # leave the release generation unrequested (no evidence at all)
            return
        self.assertTrue(
            self.store.request_release(
                self.key,
                expected_revision=self._rec().revision,
                action_id="rel-again",
                observation=build_release_observation([
                    ReleasePin("gateway", _gw_name(), released[0], _STARTUP_ACTION),
                    ReleasePin("worker", _wk_name(), released[1], _STARTUP_ACTION),
                ]),
                now=now,
            ).applied
        )
        self.assertTrue(
            self.store.record_release_outcome(
                self.key,
                action_id="rel-again",
                expected_revision=self._rec().revision,
                target=RELEASE_RELEASED,
                now=now,
            ).applied
        )

    def _resumed_once(self) -> None:
        self.assertTrue(self._repair_pins().applied)
        self.assertFalse(self._resume().is_blocked)
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_ACTIVE)

    def test_a_backdated_stamp_no_longer_admits_a_survivor_on_a_released_locator(self) -> None:
        """The R2-F1 scenario, now REFUSED: the timestamp is defeated, the locator is not."""
        self._resumed_once()
        # Hibernate again with a stamp EARLIER than the survivor's attestation, closing exactly
        # the panes that are live now — so a survivor keeps one of those locators.
        self._hibernate_again(released=(_GW_LOC, _WK_LOC), now=T_BACKDATED)
        rec = self._rec()
        self.assertEqual(rec.hibernated_at, T_BACKDATED)
        self.assertLess(rec.hibernated_at, T_SURVIVOR)  # the timestamp half is defeated...

        outcome = self._resume(ops=_FakeOps(observed_at=T_SURVIVOR))

        self.assertTrue(outcome.is_blocked)  # ...and the immutable old epoch refuses
        self.assertIn(BLOCK_PAIR_ATTESTATION, outcome.preflight.blocked_reasons)
        self.assertIn(EPOCH_NOT_NEWER, outcome.preflight.pair_attestation_detail)
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_HIBERNATED)
        self.assertIsNone(outcome.transition)

    def test_a_relaunch_on_new_locators_still_resumes(self) -> None:
        """Not a blanket refusal: different pane-ids are exactly what a real relaunch has."""
        self._resumed_once()
        self._hibernate_again(released=(_GW_LOC, _WK_LOC), now=T_RESUME)
        fresh_gw, fresh_wk = f"{_WS}:p9A", f"{_WS}:p9B"
        outcome = self._resume(
            # Second hibernate -> the counter is at 2, so a genuine relaunch is handed 2
            # (#14756). Spelling the generation out is the point of the fixture, not noise.
            ops=_FakeOps(
                observed_at=T_LATER,
                gw_locator=fresh_gw,
                wk_locator=fresh_wk,
                lane_epoch=_SECOND_EPOCH,
            )
        )
        self.assertFalse(
            outcome.is_blocked,
            f"blocked: {outcome.preflight.blocked_reasons} "
            f"({outcome.preflight.pair_attestation_detail})",
        )
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_ACTIVE)

    def test_exact_epoch_replaces_released_locator_reuse_fence(self) -> None:
        """#14955 Acceptance 2: locator reuse cannot veto exact epoch authority."""
        self._resumed_once()
        self._hibernate_again(released=(_GW_LOC, _WK_LOC), now=T_BACKDATED)
        fence_ok, fence_reason = released_locator_verdict(
            self._rec(), (_GW_LOC, _WK_LOC)
        )
        self.assertFalse(fence_ok)
        self.assertEqual(fence_reason, FENCE_LOCATOR_REUSED)

        outcome = self._resume(
            ops=_FakeOps(observed_at=T_SURVIVOR, lane_epoch=_SECOND_EPOCH)
        )

        self.assertFalse(outcome.is_blocked)
        self.assertNotIn(FENCE_LOCATOR_REUSED, outcome.preflight.pair_attestation_detail)
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_ACTIVE)

    def test_exact_epoch_replaces_absent_release_evidence(self) -> None:
        """#14955: exact epoch answers what absent release evidence could not."""
        self._resumed_once()
        self._hibernate_again(released=None, now=T_RESUME)  # release never requested
        outcome = self._resume(ops=_FakeOps(observed_at=T_LATER, gw_locator=f"{_WS}:p9A",
                                           wk_locator=f"{_WS}:p9B",
                                           lane_epoch=_SECOND_EPOCH))
        self.assertFalse(outcome.is_blocked)
        self.assertNotIn(FENCE_EVIDENCE_ABSENT, outcome.preflight.pair_attestation_detail)
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_ACTIVE)


class ReleaseObservationBindingTest(_Fixture):
    """What the v9 observation contract closes, and the residual it does NOT.

    Redmine #14477 review j#94570 R3-F1 showed that ``release_pins`` was caller-supplied, so a
    caller could record locators that were never live and a survivor passed the disjointness
    test. Disposition j#94582 closed that seam: ``request_release`` no longer accepts ``pins`` at
    all, the store DERIVES them from a single :class:`ReleaseObservation`, and the two fields must
    match at the writer and read gates.

    The residual, acknowledged in j#94581 and accepted in j#94582: a writer inside the trust
    boundary can still hand over a FABRICATED observation. What changed is that it must now do so
    explicitly through one auditable seam instead of being accepted implicitly wherever ``pins=``
    was passed. The cryptographic/epoch replacement is Redmine #14756 — so the fabrication case
    below is pinned as a KNOWN, DOCUMENTED residual rather than as an expected failure that
    #14477 could retire.
    """

    def _rehibernate(self) -> None:
        """Put the lane back in the one state from which a release may be requested."""
        self.assertTrue(self._repair_pins().applied)
        self.assertFalse(self._resume().is_blocked)
        out = self.store.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=self._rec().revision,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
            now=T_RESUME,
        )
        self.assertTrue(out.applied, out.reason)

    def test_the_hybrid_observation_plus_pins_call_is_refused_outright(self) -> None:
        """j#94582 item 6: no backward compatibility, no silent fallback.

        This is the shape that passes BOTH keywords. It is kept as the positive control for the
        ``pins is not None`` refusal; the literal legacy shape is pinned separately below,
        because review j#94707 R4-F2 showed the two do NOT fail the same way.
        """
        self._rehibernate()
        with self.assertRaises(ReleaseObservationError):
            self.store.request_release(
                self.key,
                expected_revision=self._rec().revision,
                action_id="rel-legacy",
                observation=build_release_observation(()),
                pins=[ReleasePin("gateway", _gw_name(), f"{_WS}:pRAW")],
            )

    def test_the_literal_legacy_pins_only_call_is_refused_with_a_typed_error(self) -> None:
        """Review j#94707 R4-F2: the shape an actual legacy caller writes.

        Before this fix ``observation`` was a REQUIRED keyword, so ``pins=`` alone died of
        ``TypeError: missing a required argument`` at argument binding and never reached the
        typed refusal that this seam exists to give. A refusal that cannot state its reason is
        not the authority seam j#94582 item 6 specified, so the exception TYPE is pinned, not
        just the fact that something was raised.
        """
        self._rehibernate()
        for label, call in (
            (
                "public delegator",
                lambda: self.store.request_release(
                    self.key,
                    expected_revision=self._rec().revision,
                    action_id="rel-legacy-literal",
                    pins=[ReleasePin("gateway", _gw_name(), f"{_WS}:pRAW")],
                ),
            ),
            (
                "axis function",
                lambda: open_release_generation(
                    self.store,
                    self.key,
                    expected_revision=self._rec().revision,
                    action_id="rel-legacy-literal",
                    pins=[ReleasePin("gateway", _gw_name(), f"{_WS}:pRAW")],
                ),
            ),
        ):
            with self.subTest(seam=label):
                with self.assertRaises(ReleaseObservationError) as caught:
                    call()
                # A TypeError would satisfy "it refused" while telling the caller nothing about
                # the authority contract. ReleaseObservationError does not subclass it.
                self.assertNotIsInstance(caught.exception, TypeError)
                self.assertIn("pins", str(caught.exception))

    def test_an_omitted_observation_is_refused_with_a_typed_error(self) -> None:
        """The other half of making ``observation`` defaultable: forgetting it must still be
        loud, and loud in the same typed vocabulary rather than as an arity error."""
        self._rehibernate()
        with self.assertRaises(ReleaseObservationError) as caught:
            self.store.request_release(
                self.key,
                expected_revision=self._rec().revision,
                action_id="rel-no-observation",
            )
        self.assertNotIsInstance(caught.exception, TypeError)
        self.assertIn("ReleaseObservation", str(caught.exception))
        # Refused BEFORE any write: no generation was opened, so the axis is still the one the
        # rehydrate left behind (reset), not ``requested``.
        self.assertEqual(self._rec().process_release, RELEASE_NOT_REQUESTED)
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_HIBERNATED)

    def test_a_fabricated_observation_is_the_documented_residual(self) -> None:
        """The trust-boundary residual #14756 owns — pinned as a FACT, not as a fixed hole.

        A writer that fabricates the observation is still believed. This is asserted so the
        boundary is visible and so a future change that closes it (an epoch bound into the
        attestation) makes this pin FAIL and forces the reader here to be updated.
        """
        self.assertTrue(self._repair_pins().applied)
        self.assertFalse(self._resume().is_blocked)
        out = self.store.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=self._rec().revision,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
            now=T_BACKDATED,
        )
        self.assertTrue(out.applied, out.reason)
        fabricated = build_release_observation(
            (
                ReleasePin(
                    "gateway", _gw_name(), f"{_WS}:pOTHER_G", _STARTUP_ACTION
                ),
                ReleasePin(
                    "worker", _wk_name(), f"{_WS}:pOTHER_W", _STARTUP_ACTION
                ),
            )
        )
        self.assertTrue(
            self.store.request_release(
                self.key,
                expected_revision=self._rec().revision,
                action_id="rel-fabricated",
                observation=fabricated,
                now=T_BACKDATED,
            ).applied
        )
        self.assertTrue(
            self.store.record_release_outcome(
                self.key,
                action_id="rel-fabricated",
                expected_revision=self._rec().revision,
                target=RELEASE_RELEASED,
                now=T_BACKDATED,
            ).applied
        )
        # INVERTED by Redmine #14756, not deleted. This assertion previously read
        # ``assertFalse(outcome.is_blocked)`` and documented the residual in its own message:
        # "a fabricated observation is still trusted; Redmine #14756 replaces this
        # trust-boundary authority with an epoch bound into the attestation". That is exactly
        # what happened, so the pin now records the CLOSED state and keeps the history
        # visible — a deleted pin would leave no trace that the hole ever existed or when it
        # stopped existing.
        #
        # Why the epoch closes it: a fabricated observation is a lie about which locators the
        # release closed, and every locator-based fence is downstream of that lie. The epoch
        # is not — it is minted by the store from its own row and delivered to the process
        # through an environment a writer inside this boundary cannot forge into a LIVE
        # process. The survivor here holds the pre-hibernate epoch, so it is refused whatever
        # the observation claims.
        outcome = self._resume(ops=_FakeOps(observed_at=T_SURVIVOR))
        self.assertTrue(
            outcome.is_blocked,
            "a fabricated observation must no longer admit a survivor: Redmine #14756 "
            "replaced this trust-boundary authority with an epoch bound into the attestation",
        )
        self.assertIn(BLOCK_PAIR_ATTESTATION, outcome.preflight.blocked_reasons)
        self.assertIn(
            EPOCH_NOT_NEWER, outcome.preflight.pair_attestation_detail
        )


class ReleaseAxisResetClearsObservationTest(_Fixture):
    """Every writer that resets the release axis clears the OBSERVATION with it.

    Review j#94707 R4-F1: the v9 field was added to exactly one writer
    (``open_release_generation``) and to none of the three that reset the axis, so a rehydrated /
    promoted / re-incarnated lane kept the previous generation's observation. The declared
    contract is `vibes/docs/logics/managed-state-model.md` — "``active`` へ戻る際は release 一式と
    共に clear される" — and "release 一式" includes the authority field that decides freshness.

    The seed's observation is NON-EMPTY (``pOLD_G`` / ``pOLD_W``), so a leftover value is
    distinguishable from a correctly cleared one: an empty string cannot be mistaken for it.
    Each pin asserts the WHOLE set at once, because the defect was precisely that three of four
    fields were reset and the fourth was forgotten.
    """

    def _assert_release_set_cleared(self, rec: LaneLifecycleRecord, *, path: str) -> None:
        self.assertEqual(
            (
                rec.process_release,
                rec.release_action_id,
                rec.release_pins,
                rec.release_observation,
            ),
            (RELEASE_NOT_REQUESTED, "", "", ""),
            f"{path}: the release set must clear as ONE set, in one CAS",
        )
        # And the read gate agrees the row holds no usable proof, by the same evidence.
        observation, reason = verify_release_observation(rec)
        self.assertIsNone(observation)
        self.assertEqual(reason, "release_observation_absent")

    def _seeded_observation_is_non_empty(self) -> None:
        self.assertNotEqual(self._rec().release_observation, "")
        self.assertEqual(self._rec().process_release, RELEASE_RELEASED)

    def test_rehydrate_to_active_clears_the_release_observation(self) -> None:
        """``transition_disposition`` hibernated -> active — the resume path of this issue."""
        self._seeded_observation_is_non_empty()
        out = self.store.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_HIBERNATED,
            expected_revision=self._rec().revision,
            target=DISPOSITION_ACTIVE,
            decision=_decision(),
            now=T_RESUME,
        )
        self.assertTrue(out.applied, out.reason)
        self._assert_release_set_cleared(self._rec(), path="rehydrate")

    def test_supersede_promotion_clears_the_recovery_lanes_release_observation(self) -> None:
        """``supersede_and_activate`` promoting an EXISTING recovery lane.

        The promoted lane is the one whose row is rewritten, so the observation that must not
        survive is the recovery lane's own — seeded here by driving it through a full release
        generation before the handover.
        """
        recovery = LaneLifecycleKey(_WS, f"{_LANE}_recovery")
        # An UNBOUND recovery lane: it owns no issue yet, which is what makes it promotable
        # (a lane already bound to a different issue is refused with CAS_OWNER_CONFLICT).
        out = self.store.declare_active(
            recovery, decision=_decision(), issue_id="", now=T_DECLARE
        )
        self.assertTrue(out.applied, out.reason)
        out = self.store.transition_disposition(
            recovery,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=self.store.get(recovery).revision,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
            now=T_HIBERNATE,
        )
        self.assertTrue(out.applied, out.reason)
        self.assertTrue(
            self.store.request_release(
                recovery,
                expected_revision=self.store.get(recovery).revision,
                action_id="rel-recovery",
                observation=build_release_observation([
                    ReleasePin(
                        "gateway", _gw_name(), f"{_WS}:pREC_G", _STARTUP_ACTION
                    ),
                ]),
                now=T_RELEASE,
            ).applied
        )
        self.assertTrue(
            self.store.record_release_outcome(
                recovery,
                action_id="rel-recovery",
                expected_revision=self.store.get(recovery).revision,
                target=RELEASE_RELEASED,
                now=T_RELEASE,
            ).applied
        )
        self.assertNotEqual(self.store.get(recovery).release_observation, "")

        # The superseded lane must be ACTIVE to hand ownership over.
        out = self.store.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_HIBERNATED,
            expected_revision=self._rec().revision,
            target=DISPOSITION_ACTIVE,
            decision=_decision(),
            now=T_RESUME,
        )
        self.assertTrue(out.applied, out.reason)
        out = self.store.supersede_and_activate(
            superseded=self.key,
            expected_revision=self._rec().revision,
            recovery=recovery,
            decision=_decision(),
            recovery_expected_disposition=DISPOSITION_HIBERNATED,
            recovery_expected_revision=self.store.get(recovery).revision,
            now=T_LATER,
        )
        self.assertTrue(out.applied, out.reason)
        promoted = self.store.get(recovery)
        self.assertEqual(promoted.lane_disposition, DISPOSITION_ACTIVE)
        self._assert_release_set_cleared(promoted, path="supersede promotion")

    def test_open_next_generation_clears_the_release_observation(self) -> None:
        """``open_next_generation`` — a re-incarnated generation inherits no release evidence."""
        self._seeded_observation_is_non_empty()
        out = self.store.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_HIBERNATED,
            expected_revision=self._rec().revision,
            target=DISPOSITION_RETIRED,
            decision=_decision(),
            now=T_RESUME,
        )
        self.assertTrue(out.applied, out.reason)
        # Retiring does not rewrite the release axis, so the evidence is still there to inherit.
        self.assertNotEqual(self._rec().release_observation, "")
        rec = self._rec()
        out = LaneDeclarationStore(path=self.path).open_next_generation(
            self.key,
            expected_revision=rec.revision,
            expected_generation=rec.lane_generation,
            decision=_decision(),
            declared_slots=_live_pins(),
            now=T_LATER,
        )
        self.assertTrue(out.applied, out.reason)
        self.assertEqual(self._rec().lane_generation, rec.lane_generation + 1)
        self._assert_release_set_cleared(self._rec(), path="open_next_generation")

    def test_the_read_gate_refuses_an_in_flight_generation_as_not_completed(self) -> None:
        """An in-flight generation's observation is ITS OWN, and is refused for being unfinished.

        Review j#94727 R5-F1: ``record_release_outcome`` advances ``requested -> partial ->
        released`` WITHOUT rewriting the observation, so a ``requested`` / ``partial`` row holds
        the current generation's observation — it is not stale, it is incomplete. The earlier
        version of this pin called it "not the current generation" and built the states by hand
        with ``lane_disposition=active``, a shape no writer can produce, which hid that meaning.

        Both states are therefore reached HERE THROUGH THE REAL TRANSITIONS, so the pin proves
        what a caller can actually observe rather than what a fabricated record can hold.
        """
        # Rehydrate the seeded lane, then hibernate it so a fresh generation may be opened.
        for expected, target in (
            (DISPOSITION_HIBERNATED, DISPOSITION_ACTIVE),
            (DISPOSITION_ACTIVE, DISPOSITION_HIBERNATED),
        ):
            out = self.store.transition_disposition(
                self.key,
                expected_disposition=expected,
                expected_revision=self._rec().revision,
                target=target,
                decision=_decision(),
                now=T_RESUME,
            )
            self.assertTrue(out.applied, out.reason)
        observation = build_release_observation([
            ReleasePin("gateway", _gw_name(), _GW_LOC, _STARTUP_ACTION),
            ReleasePin("worker", _wk_name(), _WK_LOC, _STARTUP_ACTION),
        ])
        self.assertTrue(
            self.store.request_release(
                self.key,
                expected_revision=self._rec().revision,
                action_id="rel-inflight",
                observation=observation,
                now=T_RESUME,
            ).applied
        )
        # `requested`: the generation is open and owns this observation.
        rec = self._rec()
        self.assertEqual(rec.process_release, RELEASE_REQUESTED)
        self.assertNotEqual(rec.release_observation, "")
        got, reason = verify_release_observation(rec)
        self.assertIsNone(got)
        self.assertEqual(reason, OBSERVATION_GENERATION_NOT_COMPLETED)

        # `partial`: one slot closed, the run may still close more — still not a proof.
        self.assertTrue(
            self.store.record_release_outcome(
                self.key,
                action_id="rel-inflight",
                expected_revision=rec.revision,
                target=RELEASE_PARTIAL,
                now=T_RESUME,
            ).applied
        )
        partial = self._rec()
        self.assertEqual(partial.process_release, RELEASE_PARTIAL)
        # The advance did NOT rewrite the observation — that is why it is "not completed" and
        # never "not the current generation".
        self.assertEqual(partial.release_observation, rec.release_observation)
        got, reason = verify_release_observation(partial)
        self.assertIsNone(got)
        self.assertEqual(reason, OBSERVATION_GENERATION_NOT_COMPLETED)

        # Completing the SAME generation makes the SAME observation readable: the gate
        # discriminates on completion, not by refusing everything.
        self.assertTrue(
            self.store.record_release_outcome(
                self.key,
                action_id="rel-inflight",
                expected_revision=partial.revision,
                target=RELEASE_RELEASED,
                now=T_RESUME,
            ).applied
        )
        done = self._rec()
        self.assertEqual(done.release_observation, rec.release_observation)
        got, reason = verify_release_observation(done)
        self.assertIsNotNone(got)
        self.assertEqual(reason, "release_observation_ok")

    def test_the_read_gate_names_a_reset_invariant_violation_as_its_own_reason(self) -> None:
        """`not_requested` + a residual observation is a VIOLATION, not an in-flight state.

        The reset writers pinned above make this shape unreachable through any transition, which
        is exactly why it is built by hand here: an unreachable shape is the honest way to pin an
        invariant violation, whereas building a *reachable* state by hand (the R5-F1 mistake)
        misrepresents what the code does. If a future writer forgets its reset, or an older
        build's row is read, this is the reason an operator sees — distinct from the in-flight
        case so the two are never confused.
        """
        pins = (ReleasePin("gateway", _gw_name(), _GW_LOC, _STARTUP_ACTION),)
        violated = LaneLifecycleRecord(
            repo_workspace_id=_WS,
            lane_id=_LANE,
            issue_id=_ISSUE,
            lane_disposition=DISPOSITION_ACTIVE,
            process_release=RELEASE_NOT_REQUESTED,
            release_pins=encode_release_pins(pins),
            release_observation=encode_release_observation(build_release_observation(pins)),
        )
        got, reason = verify_release_observation(violated)
        self.assertIsNone(got)
        self.assertEqual(reason, OBSERVATION_STALE_AFTER_RESET)
        self.assertNotEqual(reason, OBSERVATION_GENERATION_NOT_COMPLETED)

    def _row_with_release_state(self, token: str) -> LaneLifecycleRecord:
        pins = (ReleasePin("gateway", _gw_name(), _GW_LOC, _STARTUP_ACTION),)
        return LaneLifecycleRecord(
            repo_workspace_id=_WS,
            lane_id=_LANE,
            issue_id=_ISSUE,
            lane_disposition=DISPOSITION_HIBERNATED,
            process_release=token,
            release_pins=encode_release_pins(pins),
            release_observation=encode_release_observation(build_release_observation(pins)),
        )

    def test_a_non_canonical_release_state_is_typed_unknown_not_a_deterministic_class(self):
        """Review j#94738 R6-F1: an unknown release state is OUTCOME-UNKNOWN.

        ``process_release`` is ``TEXT NOT NULL`` with no CHECK constraint and the row decoder
        passes the string through, so a legacy / corrupted / hand-edited row can hold anything.
        The standing ruling for the same storage fact on the hibernate rail is that such a state
        is *uncertain* and must never be folded into a deterministic classification
        (``release_state_unknown``, review j#86776 R5-F5 / j#87226). Collapsing it into
        ``stale_after_reset`` claimed a specific invariant violation the row does not evidence.

        Hand-built on purpose: the canonical store refuses to persist a non-canonical token, so a
        readable-invalid storage row is exactly what has to be simulated — the same reasoning the
        #14219 regression uses for this shape.
        """
        for token in ("weird_unknown_token", "", "RELEASED", "requeste", "not_requested "):
            with self.subTest(process_release=token):
                got, reason = verify_release_observation(self._row_with_release_state(token))
                self.assertIsNone(got)
                self.assertEqual(reason, OBSERVATION_RELEASE_STATE_UNKNOWN)
                # Never a deterministic class, and never a pass.
                self.assertNotIn(
                    reason,
                    (
                        OBSERVATION_STALE_AFTER_RESET,
                        OBSERVATION_GENERATION_NOT_COMPLETED,
                        "release_observation_ok",
                    ),
                )

    def test_a_release_state_that_only_normalises_to_canonical_is_not_a_proof(self) -> None:
        """A padded token used to be ADMITTED — worse than being misclassified.

        Found while reproducing R6-F1 (recorded in verdict j#94739). The gate compared
        ``norm(process_release)``, so ``"released "`` normalised to the canonical token and the
        observation was returned as ``release_observation_ok``. A closed vocabulary is compared as
        STORED — the same discipline the ``lane_kind`` vocabulary was given in review j#85852 F1.
        """
        got, reason = verify_release_observation(self._row_with_release_state("released "))
        self.assertIsNone(got, "a padded release state must never yield a survivor proof")
        self.assertEqual(reason, OBSERVATION_RELEASE_STATE_UNKNOWN)
        # Control: the byte-exact token still passes, so this is a canonicality check and not a
        # blanket refusal of everything that contains "released".
        got, reason = verify_release_observation(self._row_with_release_state(RELEASE_RELEASED))
        self.assertIsNotNone(got)
        self.assertEqual(reason, "release_observation_ok")

    def test_the_fence_reports_one_reason_for_every_non_canonical_spelling(self) -> None:
        """The fence's typed detail no longer depends on HOW the invalid value is spelled.

        Review j#94750 R7-F2. R7 left the fence's own ``norm``-ed precheck in place, so
        ``weird_unknown_token`` was folded into ``release_evidence_absent`` while ``"released "``
        surfaced the component reason — the same non-canonical class reported two ways. I had
        argued that fixing it would flip the existing ``not_requested`` pin; that was wrong, and I
        had not read which state that pin uses. Classifying the RAW state first fixes the
        asymmetry AND leaves canonical non-released rows on their long-standing generic reason.
        """
        for token in ("weird_unknown_token", "released ", "", " not_requested"):
            with self.subTest(process_release=token):
                ok, reason = released_locator_verdict(
                    self._row_with_release_state(token), [_WK_LOC]
                )
                self.assertFalse(ok)
                self.assertEqual(reason, OBSERVATION_RELEASE_STATE_UNKNOWN)
        # Canonical non-released states keep the generic reason they have always had.
        for token in (RELEASE_NOT_REQUESTED, RELEASE_REQUESTED, RELEASE_PARTIAL):
            with self.subTest(canonical=token):
                ok, reason = released_locator_verdict(
                    self._row_with_release_state(token), [_WK_LOC]
                )
                self.assertFalse(ok)
                self.assertEqual(reason, FENCE_EVIDENCE_ABSENT)

    def test_an_unknown_state_is_classified_before_the_observation_is_decoded(self) -> None:
        """The state diagnosis must not depend on another field's shape (j#94750 R7-F2).

        Before this fix the observation was decoded first, so the SAME unknown token reported
        ``absent`` when the observation was empty and ``unreadable`` when it was malformed — the
        state-specific reason only appeared when the observation happened to be valid.
        """
        pins = (ReleasePin("gateway", _gw_name(), _GW_LOC, _STARTUP_ACTION),)
        for label, raw in (
            ("valid", encode_release_observation(build_release_observation(pins))),
            ("absent", ""),
            ("malformed", "{not-json"),
        ):
            with self.subTest(observation=label):
                rec = LaneLifecycleRecord(
                    repo_workspace_id=_WS,
                    lane_id=_LANE,
                    issue_id=_ISSUE,
                    lane_disposition=DISPOSITION_HIBERNATED,
                    process_release="weird_unknown_token",
                    release_pins=encode_release_pins(pins),
                    release_observation=raw,
                )
                got, reason = verify_release_observation(rec)
                self.assertIsNone(got)
                self.assertEqual(reason, OBSERVATION_RELEASE_STATE_UNKNOWN)
        # Control: a CANONICAL state still gets the observation-shape reasons, so the reordering
        # did not swallow the absent / unreadable diagnoses.
        for raw, expected in (("", OBSERVATION_ABSENT), ("{not-json", OBSERVATION_UNREADABLE)):
            with self.subTest(canonical_observation=expected):
                rec = LaneLifecycleRecord(
                    repo_workspace_id=_WS,
                    lane_id=_LANE,
                    issue_id=_ISSUE,
                    lane_disposition=DISPOSITION_HIBERNATED,
                    process_release=RELEASE_RELEASED,
                    release_pins=encode_release_pins(pins),
                    release_observation=raw,
                )
                got, reason = verify_release_observation(rec)
                self.assertIsNone(got)
                self.assertEqual(reason, expected)

    def test_a_future_release_state_added_to_the_vocabulary_never_yields_a_proof(self) -> None:
        """Growing `RELEASE_STATES` must not hand an unclassified state a survivor proof.

        Review j#94750 R7-F1, and a direct refutation of what I claimed in review request j#94742
        observation 4. The R7 gate refused the states it knew and let everything else FALL THROUGH
        to the ``released`` pin check, so a fifth vocabulary member returned
        ``release_observation_ok`` — measured. The gate now classifies against its OWN literal set,
        so a state it has no rule for fails closed and the omission is visible as a refusal.

        The vocabulary is patched on the module under test and restored, with identity asserted
        after the run so a leak cannot silently widen any other pin in this process.
        """
        from mozyo_bridge.core.state import lane_release as module

        original = module.RELEASE_STATES
        module.RELEASE_STATES = frozenset(set(original) | {"future_settling_state"})
        try:
            got, reason = verify_release_observation(
                self._row_with_release_state("future_settling_state")
            )
        finally:
            module.RELEASE_STATES = original
        self.assertIs(module.RELEASE_STATES, original)
        self.assertIsNone(got, "an unclassified state must never yield a survivor proof")
        self.assertEqual(reason, OBSERVATION_RELEASE_STATE_UNKNOWN)

    def test_the_hibernate_enumeration_classifies_the_stored_state_byte_exact(self) -> None:
        """The precedent rail this issue cited had the same defect (j#94750 R7-F3).

        ``enumerate_hibernated_redrives`` stripped ``process_release`` before classifying it, so a
        padded value impersonated a canonical token against that function's own documented
        contract: ``"released "`` vanished as a completed generation, and — worse, which I measured
        while verifying the finding — ``" not_requested"`` was admitted to the REDRIVE path, which
        actuates. Pinned here, in the issue whose review found it, for both spellings.
        """
        from types import SimpleNamespace
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_supervisor_wiring import (  # noqa: E501
            enumerate_hibernated_redrives,
        )

        def _row(token: str):
            return SimpleNamespace(
                binding_kind="issue",
                lane_disposition=DISPOSITION_HIBERNATED,
                repo_workspace_id=_WS,
                issue_id=_ISSUE,
                lane_id=_LANE,
                lane_generation=1,
                revision=3,
                process_release=token,
            )

        for token in ("released ", " not_requested", "RELEASED", "weird_unknown_token"):
            with self.subTest(process_release=token):
                out = enumerate_hibernated_redrives(
                    [_row(token)], workspace_id=_WS, live_slot_fn=lambda _row: True
                )
                self.assertEqual(
                    (len(out.redrives), len(out.unknown_release)),
                    (0, 1),
                    "a non-canonical stored state is typed uncertain and never actuated",
                )
        # Controls: the canonical tokens keep their existing classification.
        out = enumerate_hibernated_redrives(
            [_row(RELEASE_RELEASED)], workspace_id=_WS, live_slot_fn=lambda _row: True
        )
        self.assertEqual((len(out.redrives), len(out.unknown_release)), (0, 0))
        out = enumerate_hibernated_redrives(
            [_row(RELEASE_NOT_REQUESTED)], workspace_id=_WS, live_slot_fn=lambda _row: True
        )
        self.assertEqual((len(out.redrives), len(out.unknown_release)), (1, 0))


class ReleasedLocatorVerdictUnitTest(unittest.TestCase):
    """The pure predicate's edge matrix over the v9 observation contract (no store, no clock).

    Enumerates the adversarial edges j#94582 lists: absent, unreadable, pin-mismatch (partial /
    extra), complete-empty, locator reuse, and the disjoint pass. Duplicate and empty-locator
    enumerations are refused at construction, so they are pinned on the builder.
    """

    def _rec(self, *, release=RELEASE_RELEASED, observation_raw=None, pins=None, locators=(_GW_LOC,)):
        identities = (
            ("gateway", _gw_name()),
            ("worker", _wk_name()),
        )
        obs = build_release_observation(
            tuple(
                ReleasePin(role, name, locator, _STARTUP_ACTION)
                for (role, name), locator in zip(identities, locators)
            )
        )
        raw = encode_release_observation(obs) if observation_raw is None else observation_raw
        stored = obs.slots if pins is None else pins
        return LaneLifecycleRecord(
            repo_workspace_id=_WS,
            lane_id=_LANE,
            process_release=release,
            # v2 complete-empty is a PRESENT envelope. Collapsing it to the legacy/absent
            # empty string would turn positive zero-slot evidence into a pin mismatch.
            release_pins=encode_release_pins(stored),
            release_observation=raw,
        )

    def test_no_record_is_absent(self) -> None:
        self.assertEqual(
            released_locator_verdict(None, [_GW_LOC]), (False, FENCE_EVIDENCE_ABSENT)
        )

    def test_an_unreleased_generation_is_absent(self) -> None:
        rec = self._rec(release="not_requested")
        self.assertEqual(released_locator_verdict(rec, [_GW_LOC]), (False, FENCE_EVIDENCE_ABSENT))

    def test_a_pre_v9_row_with_no_observation_is_absent_not_empty(self) -> None:
        """The v9 distinction: an ABSENT observation must never read as complete-empty."""
        rec = self._rec(observation_raw="")
        self.assertEqual(released_locator_verdict(rec, [f"{_WS}:pN"]), (False, FENCE_EVIDENCE_ABSENT))

    def test_a_corrupt_observation_is_unreadable_not_empty(self) -> None:
        rec = self._rec(observation_raw="{not json")
        self.assertEqual(released_locator_verdict(rec, [f"{_WS}:pN"]), (False, OBSERVATION_UNREADABLE))

    def test_a_future_envelope_version_is_unreadable(self) -> None:
        rec = self._rec(observation_raw='{"v": 999, "slots": []}')
        self.assertEqual(released_locator_verdict(rec, [f"{_WS}:pN"]), (False, OBSERVATION_UNREADABLE))

    def test_pins_missing_a_slot_the_observation_has_is_a_mismatch(self) -> None:
        rec = self._rec(locators=(_GW_LOC, _WK_LOC), pins=())
        self.assertEqual(released_locator_verdict(rec, [f"{_WS}:pN"]), (False, OBSERVATION_PIN_MISMATCH))

    def test_pins_claiming_an_extra_slot_is_a_mismatch(self) -> None:
        extra = (
            ReleasePin("gateway", _gw_name(), _GW_LOC, _STARTUP_ACTION),
            ReleasePin("worker", _wk_name(), _WK_LOC, _STARTUP_ACTION),
        )
        rec = self._rec(locators=(_GW_LOC,), pins=extra)
        self.assertEqual(released_locator_verdict(rec, [f"{_WS}:pN"]), (False, OBSERVATION_PIN_MISMATCH))

    def test_a_complete_empty_observation_is_positive_evidence(self) -> None:
        rec = self._rec(locators=())
        self.assertEqual(
            released_locator_verdict(rec, [f"{_WS}:pN1", f"{_WS}:pN2"]),
            (True, FENCE_COMPLETE_EMPTY),
        )

    def test_an_observed_locator_in_the_observation_is_reuse(self) -> None:
        rec = self._rec(locators=(_GW_LOC, _WK_LOC))
        self.assertEqual(
            released_locator_verdict(rec, [_GW_LOC, f"{_WS}:pNEW"]), (False, FENCE_LOCATOR_REUSED)
        )

    def test_all_different_locators_pass(self) -> None:
        rec = self._rec(locators=(_GW_LOC, _WK_LOC))
        self.assertEqual(
            released_locator_verdict(rec, [f"{_WS}:pN1", f"{_WS}:pN2"]), (True, FENCE_OK)
        )

    def test_a_duplicate_locator_enumeration_is_refused_at_construction(self) -> None:
        with self.assertRaises(ReleaseObservationError):
            build_release_observation(
                (
                    ReleasePin("gateway", _gw_name(), _GW_LOC, _STARTUP_ACTION),
                    ReleasePin("worker", _wk_name(), _GW_LOC, _STARTUP_ACTION),
                )
            )

    def test_a_slot_without_a_locator_is_refused_at_construction(self) -> None:
        with self.assertRaises(Exception):
            build_release_observation(
                (ReleasePin("gateway", _gw_name(), "", _STARTUP_ACTION),)
            )



class DriverDerivedObservationE2ETest(_Fixture):
    """The AUTHORITY SUCCESS PATH, driven end to end from the public hibernate rail.

    Redmine #14477 j#94582 requires the success path to run observation-generation → request →
    outcome → resume through ``drive_process_release`` with a fake inventory, and NOT through a
    store-level fixture that hands over arbitrary pins. That is the whole point of the contract:
    the observation must come from the driver's own enumeration of the live inventory, so no test
    may establish the success path by asserting what it wants the observation to be.

    Two orderings are pinned against that same driver-derived observation:

    - the j#94570 adversarial one — a survivor keeps a locator the driver actually observed, so
      it is REFUSED (this is the red→green regression the disposition asked for);
    - a genuine relaunch on new pane-ids, which resumes.
    """

    class _ReleaseOps:
        """Fake release IO: a canned live inventory plus a close that always succeeds."""

        def __init__(self, rows):
            self._rows = list(rows)

        def live_rows(self):
            return list(self._rows)

        def execute_close(self, plan):
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
                HerdrRetireCloseResult,
            )
            closed_locators = {locator for _role, locator in plan.close_targets}
            # The production rail performs a fresh full-inventory read after close. Model
            # the successful close in that authoritative observation instead of returning
            # the same pre-close rows forever.
            self._rows = [
                row for row in self._rows if row.get("pane_id") not in closed_locators
            ]
            return HerdrRetireCloseResult(
                workspace_id=plan.workspace_id,
                lane_id=plan.lane_id,
                closed=tuple(plan.close_targets),
                failed=(),
                foreign_names=(),
            )

    def _drive_release(self, live):
        """Run the real driver so the observation is DERIVED, never supplied by this test."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_process_release import (  # noqa: E501
            drive_process_release,
        )
        from tests.support.current_launch_authority import (
            seed_completed_current_launch_authority,
        )

        rows = [
            {
                "name": _gw_name(),
                "pane_id": live[0],
                "terminal_id": _terminal_id(live[0]),
            },
            {
                "name": _wk_name(),
                "pane_id": live[1],
                "terminal_id": _terminal_id(live[1]),
            },
        ]
        for role, assigned_name, locator in (
            (_GW_PROVIDER, _gw_name(), live[0]),
            (_WK_PROVIDER, _wk_name(), live[1]),
        ):
            seed_completed_current_launch_authority(
                self.path.parent,
                workspace_id=_WS,
                lane_id=_LANE,
                role=role,
                assigned_name=assigned_name,
                locator=locator,
                terminal_id=_terminal_id(locator),
                # Receipt topology is an explicit Herdr container identity. It is separate
                # from the logical repo-workspace segment used in the managed name.
                target_workspace="w28",
                target_tab="w28:t1",
            )
        return drive_process_release(
            store=self.store,
            ops=self._ReleaseOps(rows),
            key=self.key,
            lane_id=_LANE,
            workspace_id=_WS,
            action_id=f"hibernate:{_LANE}",
            rows=rows,
        )

    def _hibernate_then_drive(self, live, *, now):
        out = self.store.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=self._rec().revision,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
            now=now,
        )
        self.assertTrue(out.applied, out.reason)
        released = self._drive_release(live)
        self.assertEqual(released.process_release, RELEASE_RELEASED, released.detail)
        # The observation came from the driver's enumeration of exactly those live rows.
        rec = self._rec()
        self.assertNotEqual(rec.release_observation, "")
        return rec

    def test_a_survivor_on_a_driver_observed_locator_is_refused(self) -> None:
        """j#94570 ordering, now red->green: the driver saw the pane, so a survivor is caught."""
        self.assertTrue(self._repair_pins().applied)
        self.assertFalse(self._resume().is_blocked)
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_ACTIVE)

        # Backdate the stamp too, so the timestamp conjunct cannot be what refuses.
        self._hibernate_then_drive((_GW_LOC, _WK_LOC), now=T_BACKDATED)
        self.assertLess(self._rec().hibernated_at, T_SURVIVOR)

        outcome = self._resume(ops=_FakeOps(observed_at=T_SURVIVOR))

        self.assertTrue(outcome.is_blocked)
        self.assertIn(BLOCK_PAIR_ATTESTATION, outcome.preflight.blocked_reasons)
        self.assertIn(EPOCH_NOT_NEWER, outcome.preflight.pair_attestation_detail)
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_HIBERNATED)
        self.assertIsNone(outcome.transition)

    def test_a_relaunch_on_new_pane_ids_resumes(self) -> None:
        """Not a blanket refusal: the same driver-derived evidence admits a real relaunch."""
        self.assertTrue(self._repair_pins().applied)
        self.assertFalse(self._resume().is_blocked)
        self._hibernate_then_drive((_GW_LOC, _WK_LOC), now=T_RESUME)
        outcome = self._resume(
            ops=_FakeOps(
                observed_at=T_LATER,
                gw_locator=f"{_WS}:p9A",
                wk_locator=f"{_WS}:p9B",
                # Second hibernate -> the counter is at 2, which is what a genuine relaunch
                # is handed (#14756).
                lane_epoch=_SECOND_EPOCH,
            )
        )
        self.assertFalse(
            outcome.is_blocked,
            f"blocked: {outcome.preflight.blocked_reasons} "
            f"({outcome.preflight.pair_attestation_detail})",
        )
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_ACTIVE)


class ReleasedIsNotDeadnessProofTest(_Fixture):
    """`released` is generation completion, never a proof of process absence.

    Redmine #14477 j#94596 item 1 / j#94653 item 3. Since a live-zero release now records a
    COMPLETE-EMPTY observation and reaches `released`, a consumer could be tempted to read
    `released` as "no process exists". It does not mean that, and this pins it directly: the
    liveness authority is the live inventory, and a `released` row can coexist with a fully live
    pair. Any consumer that promoted `released` alone to a deadness/empty proof would fail here.
    """

    def test_a_released_row_coexists_with_a_live_pair(self) -> None:
        rec = self._rec()
        self.assertEqual(rec.process_release, RELEASE_RELEASED)
        # The lane's release generation completed, yet a full live pair is observable right now.
        live = _FakeOps().live_rows()
        self.assertEqual(len(live), 2)
        self.assertEqual(
            {row["pane_id"] for row in live},
            {_GW_LOC, _WK_LOC},
            "`released` says a release generation completed; it says nothing about what is live",
        )

    def test_a_complete_empty_observation_is_not_a_claim_about_now(self) -> None:
        """Complete-empty is a statement about hibernate time, not about the present."""
        from mozyo_bridge.core.state.lane_release import verify_release_observation

        # Drive a complete-empty generation, then observe a live pair afterwards.
        self.assertTrue(self._repair_pins().applied)
        self.assertFalse(self._resume().is_blocked)
        out = self.store.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=self._rec().revision,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
            now=T_RESUME,
        )
        self.assertTrue(out.applied, out.reason)
        self.assertTrue(
            self.store.request_release(
                self.key,
                expected_revision=self._rec().revision,
                action_id="rel-empty",
                observation=build_release_observation(()),
                now=T_RESUME,
            ).applied
        )
        self.assertTrue(
            self.store.record_release_outcome(
                self.key,
                action_id="rel-empty",
                expected_revision=self._rec().revision,
                target=RELEASE_RELEASED,
                now=T_RESUME,
            ).applied
        )
        observation, reason = verify_release_observation(self._rec())
        self.assertIsNotNone(observation)
        self.assertTrue(observation.is_complete_empty)
        # ...and a live pair is still observable. The observation describes the release
        # generation's instant, not the current world.
        self.assertEqual(len(_FakeOps().live_rows()), 2)


class OperatorHomeHermeticityTest(_Fixture):
    """This module never resolves — let alone migrates — the operator's shared home.

    Redmine #14477 j#94504 item 4. The v8 bump forward-migrated the shared operator
    ``state.sqlite`` at ``2026-07-29T21:46:28Z`` from inside a full-suite run, because a store
    constructed WITHOUT an explicit ``home=``/``path=`` resolves ``MOZYO_BRIDGE_HOME`` /
    ``~/.mozyo_bridge`` and any write there takes the write-MIGRATING gate. That is a shared
    authority store other lanes read with older-schema CLIs, and migrating it read-fail-closes
    every one of them. These pins make the hermeticity of THIS module a checked property rather
    than an author's intention.
    """

    def test_every_store_this_module_touches_is_under_its_temp_dir(self) -> None:
        tmp = Path(self._tmp.name).resolve()
        for store in (
            self.store,
            LaneDeclarationStore(path=self.path),
            LanePinRepairStore(path=self.path),
        ):
            resolved = Path(store.path).resolve()
            self.assertTrue(
                str(resolved).startswith(str(tmp)),
                f"{type(store).__name__} resolved {resolved}, outside the temp dir {tmp}",
            )

    def test_the_fixture_path_is_not_the_resolved_default_home_store(self) -> None:
        """A path comparison only — the operator store is never opened, read, or stat-ed."""
        default_store = state_store_path(None).resolve()
        self.assertNotEqual(Path(self.path).resolve(), default_store)
        self.assertNotEqual(
            Path(self.path).resolve().parent, default_store.parent
        )

    def test_no_store_in_this_module_is_constructed_without_an_explicit_path(self) -> None:
        """Derived from this file's own AST: the defect shape must not reappear here.

        A store built with NO ``home=``/``path=`` resolves the default home — the shape that
        migrated the operator's shared store. Banned in this module by a check rather than by
        review attention. Parsed as an AST rather than grepped so the ban survives its own
        mention in prose (a text search matches this docstring and fails on itself).
        """
        import ast

        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        stores = {"LaneLifecycleStore", "LaneDeclarationStore", "LanePinRepairStore"}
        offenders = [
            f"{node.func.id}() at line {node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in stores
            and not node.args
            and not node.keywords
        ]
        self.assertEqual(
            offenders,
            [],
            "these resolve MOZYO_BRIDGE_HOME / ~/.mozyo_bridge (the operator's shared authority "
            f"store); always pass an explicit temp path=: {offenders}",
        )


class UnclassifiedStoredReleaseStateTest(_Fixture):
    """A stored release state nobody has a rule for must not become authority or success.

    Redmine #14477 review j#94778. R6–R8 made the READ surfaces byte-exact, and R8-F1 / R8-F2
    showed that was not enough because two WRITE-side surfaces still classified the stored value
    by something other than its exact bytes:

    - the release driver's ``else`` reported every unclassifiable state as ``released`` with zero
      panes closed, which ``HibernateOutcome.is_success`` accepts as a clean actuation (R8-F1);
    - the core CAS policy predicates normalised the stored value, so a padded row was ADMITTED and
      then rewritten to the canonical token — laundering an invalid value into real authority that
      every byte-exact reader downstream would then correctly trust (R8-F2).

    The rows here are seeded with raw SQL on the fixture's TEMP store, which is the only way to
    hold a value the canonical writers refuse to persist — the same justification the reviewer
    accepted for the R7 hand-built pins, and the reason these are storage-boundary pins.
    """

    NON_CANONICAL = ("weird_unknown_token", "released ", " not_requested", "", "RELEASED")

    def _force_release_state(self, token: str) -> None:
        conn = sqlite3.connect(str(self.path))
        try:
            conn.execute(
                "UPDATE lane_lifecycle_records SET process_release = ? "
                "WHERE repo_workspace_id = ? AND lane_id = ?",
                (token, _WS, _LANE),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self._rec().process_release, token, "raw seed did not stick")

    # ---------------------------------------------------------------- R8-F1
    def test_the_driver_refuses_an_unclassified_state_instead_of_reporting_released(self):
        """The driver carries the RAW state back, closes nothing, and is not a success."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_process_release import (  # noqa: E501
            RELEASE_STATE_UNKNOWN,
            drive_process_release,
        )

        class _Ops:
            def __init__(self) -> None:
                self.close_calls: list = []

            def live_rows(self):
                return []

            def execute_close(self, plan):
                self.close_calls.append(plan)
                return []

        for token in self.NON_CANONICAL:
            with self.subTest(process_release=token):
                self._force_release_state(token)
                ops = _Ops()
                outcome = drive_process_release(
                    store=self.store, ops=ops, key=self.key, lane_id=_LANE,
                    workspace_id=_WS, action_id="probe",
                )
                self.assertEqual(outcome.process_release, token, "the raw state must survive")
                self.assertIn(RELEASE_STATE_UNKNOWN, outcome.detail)
                self.assertEqual(ops.close_calls, [], "zero close")
                self.assertEqual(self._rec().process_release, token, "zero write")
                # The exact condition HibernateOutcome.is_success applies to a release.
                self.assertNotIn(
                    outcome.process_release,
                    (RELEASE_RELEASED, RELEASE_NOT_REQUESTED),
                    "an unclassified state must never satisfy the completed-release condition",
                )

    def test_the_driver_still_reports_a_genuinely_released_generation(self) -> None:
        """Positive control: the ``released`` branch is a real classification, not a fallback."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_process_release import (  # noqa: E501
            drive_process_release,
        )

        class _Ops:
            def live_rows(self):
                return []

            def execute_close(self, plan):  # pragma: no cover - must not be reached
                raise AssertionError("a completed generation closes nothing")

        # The seed already left this row hibernated / released.
        self.assertEqual(self._rec().process_release, RELEASE_RELEASED)
        outcome = drive_process_release(
            store=self.store, ops=_Ops(), key=self.key, lane_id=_LANE,
            workspace_id=_WS, action_id="probe",
        )
        self.assertEqual(outcome.process_release, RELEASE_RELEASED)
        self.assertIn("already released", outcome.detail)

    # ---------------------------------------------------------------- R8-F2
    def test_a_padded_state_cannot_open_a_release_generation(self) -> None:
        """The release CAS refuses it zero-write instead of laundering it to ``requested``."""
        for token in ("not_requested ", " not_requested", "weird_unknown_token"):
            with self.subTest(process_release=token):
                self._force_release_state(token)
                out = self.store.request_release(
                    self.key,
                    expected_revision=self._rec().revision,
                    action_id="probe-open",
                    observation=build_release_observation(()),
                )
                self.assertFalse(out.applied)
                self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)
                self.assertEqual(
                    self._rec().process_release,
                    token,
                    "the refusal must not rewrite the invalid value into a canonical one",
                )

    def test_a_padded_in_flight_state_cannot_advance_to_an_outcome(self) -> None:
        """The same policy guards ``record_release_outcome``."""
        for token in ("requested ", " partial"):
            with self.subTest(process_release=token):
                self._force_release_state(token)
                rec = self._rec()
                out = self.store.record_release_outcome(
                    self.key,
                    action_id=rec.release_action_id,
                    expected_revision=rec.revision,
                    target=RELEASE_RELEASED,
                )
                self.assertFalse(out.applied)
                self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)
                self.assertEqual(self._rec().process_release, token)

    def test_a_padded_released_state_cannot_rehydrate_the_lane(self) -> None:
        """A lane whose release state cannot be classified must not come back ``active``."""
        for token in ("released ", "RELEASED", "weird_unknown_token"):
            with self.subTest(process_release=token):
                self._force_release_state(token)
                out = self.store.transition_disposition(
                    self.key,
                    expected_disposition=DISPOSITION_HIBERNATED,
                    expected_revision=self._rec().revision,
                    target=DISPOSITION_ACTIVE,
                    decision=_decision(),
                )
                self.assertFalse(out.applied)
                self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)
                rec = self._rec()
                self.assertEqual(rec.lane_disposition, DISPOSITION_HIBERNATED)
                self.assertEqual(rec.process_release, token)

    def test_the_canonical_states_still_pass_the_same_policies(self) -> None:
        """Controls: the byte-exact tightening refuses ONLY the unclassifiable values."""
        # `released` rehydrates.
        self.assertEqual(self._rec().process_release, RELEASE_RELEASED)
        out = self.store.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_HIBERNATED,
            expected_revision=self._rec().revision,
            target=DISPOSITION_ACTIVE,
            decision=_decision(),
        )
        self.assertTrue(out.applied, out.reason)
        # ...and a canonical `not_requested` hibernated row opens a generation.
        out = self.store.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=self._rec().revision,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
        )
        self.assertTrue(out.applied, out.reason)
        self.assertEqual(self._rec().process_release, RELEASE_NOT_REQUESTED)
        out = self.store.request_release(
            self.key,
            expected_revision=self._rec().revision,
            action_id="probe-canonical",
            observation=build_release_observation(()),
        )
        self.assertTrue(out.applied, out.reason)
        self.assertEqual(self._rec().process_release, RELEASE_REQUESTED)


class StoredAuthorityIsNeverNormalisedTest(_Fixture):
    """No policy predicate may normalise a STORED authority value (review j#94805 R9-F3).

    R8-F2 fixed the release axis. The replacement axis had the identical hole, and it was a REAL
    write path — measured through the public store on an isolated temp DB before the fix:

    - stored ``" not_requested"`` -> ``request_replacement`` applied, row rewritten to ``requested``
    - stored ``"requested "`` -> ``record_replacement_outcome`` applied, row rewritten to ``pending``
    - stored ``"replaced "`` -> the settled gate passed, so the lane rehydrated to ``active``

    The disposition axis is included on the reviewer's ruling as a CONTRACT fix: its pure predicate
    normalised too, but the real ``transition_disposition`` already refuses a padded stored
    disposition at its exact expected-state guard (``unexpected_state``, zero-write) — pinned below
    so the two layers of defence are both recorded rather than one being assumed.
    """

    def _force(self, field: str, value: str) -> None:
        conn = sqlite3.connect(str(self.path))
        try:
            conn.execute(
                f"UPDATE lane_lifecycle_records SET {field} = ? "
                "WHERE repo_workspace_id = ? AND lane_id = ?",
                (value, _WS, _LANE),
            )
            conn.commit()
        finally:
            conn.close()

    def _replacement_store(self):
        from mozyo_bridge.core.state.lane_replacement import LaneReplacementStore

        return LaneReplacementStore(path=self.path)

    def test_the_pure_predicates_reject_every_padded_stored_value(self) -> None:
        from mozyo_bridge.core.state.lane_lifecycle_model import (
            disposition_transition_allowed,
            replacement_open_allowed,
            replacement_settled,
            replacement_transition_allowed,
        )

        self.assertFalse(disposition_transition_allowed("hibernated ", DISPOSITION_ACTIVE))
        self.assertFalse(replacement_transition_allowed("requested ", "pending"))
        self.assertFalse(replacement_open_allowed(" not_requested"))
        self.assertFalse(replacement_settled("replaced "))
        # Controls: the canonical spellings still pass.
        self.assertTrue(
            disposition_transition_allowed(DISPOSITION_HIBERNATED, DISPOSITION_ACTIVE)
        )
        self.assertTrue(replacement_transition_allowed("requested", "pending"))
        self.assertTrue(replacement_open_allowed("not_requested"))
        self.assertTrue(replacement_settled("replaced"))

    def _activate(self) -> None:
        """Rehydrate the seeded lane to ACTIVE — a replacement only happens on an active lane.

        Without this the replacement CAS refuses at its earlier disposition guard
        (``unexpected_state``) and the pin would pass without ever reaching the predicate under
        test — green for a reason it does not state.
        """
        self.assertTrue(
            self.store.transition_disposition(
                self.key,
                expected_disposition=DISPOSITION_HIBERNATED,
                expected_revision=self._rec().revision,
                target=DISPOSITION_ACTIVE,
                decision=_decision(),
                now=T_RESUME,
            ).applied
        )
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_ACTIVE)

    def test_a_padded_replacement_state_cannot_open_a_generation(self) -> None:
        self._activate()
        for token in (" not_requested", "not_requested ", "weird_unknown_token"):
            with self.subTest(replacement_state=token):
                self._force("replacement_state", token)
                out = self._replacement_store().request_replacement(
                    self.key,
                    expected_revision=self._rec().revision,
                    action_id="probe-open",
                    pins=[
                        ReleasePin(
                            "worker", _wk_name(), f"{_WS}:pOLD", _STARTUP_ACTION
                        )
                    ],
                    decision=_decision(),
                )
                self.assertFalse(out.applied)
                self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)
                self.assertEqual(
                    self._rec().replacement_state,
                    token,
                    "the refusal must not rewrite the invalid value into a canonical one",
                )

    def test_a_padded_replacement_state_cannot_advance_to_an_outcome(self) -> None:
        """Reached through a REAL open generation, then padding the state it left behind."""
        self._activate()
        self._force("replacement_state", "not_requested")
        opened = self._replacement_store().request_replacement(
            self.key,
            expected_revision=self._rec().revision,
            action_id="probe-advance",
            pins=[
                ReleasePin("worker", _wk_name(), f"{_WS}:pOLD", _STARTUP_ACTION)
            ],
            decision=_decision(),
        )
        self.assertTrue(opened.applied, opened.reason)
        self.assertEqual(self._rec().replacement_state, "requested")
        action = self._rec().replacement_action_id
        self.assertTrue(action)

        for token in ("requested ", " requested", "weird_unknown_token"):
            with self.subTest(replacement_state=token):
                self._force("replacement_state", token)
                out = self._replacement_store().record_replacement_outcome(
                    self.key,
                    action_id=action,
                    expected_revision=self._rec().revision,
                    target="pending",
                )
                self.assertFalse(out.applied)
                self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)
                self.assertEqual(self._rec().replacement_state, token)
        # Control: restoring the canonical spelling lets the SAME generation advance.
        self._force("replacement_state", "requested")
        out = self._replacement_store().record_replacement_outcome(
            self.key,
            action_id=action,
            expected_revision=self._rec().revision,
            target="pending",
        )
        self.assertTrue(out.applied, out.reason)
        self.assertEqual(self._rec().replacement_state, "pending")

    def test_a_padded_settled_replacement_cannot_be_consumed_by_a_rehydrate(self) -> None:
        """The settled gate is a conjunct of BOTH rehydrate paths — pin them both."""
        self._force("replacement_state", "replaced ")
        out = self.store.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_HIBERNATED,
            expected_revision=self._rec().revision,
            target=DISPOSITION_ACTIVE,
            decision=_decision(),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)
        rec = self._rec()
        self.assertEqual(rec.lane_disposition, DISPOSITION_HIBERNATED)
        self.assertEqual(rec.replacement_state, "replaced ")

        # ...and the supersede promotion of an EXISTING recovery lane applies the same gate.
        recovery = LaneLifecycleKey(_WS, f"{_LANE}_recovery_r10")
        self.assertTrue(
            self.store.declare_active(
                recovery, decision=_decision(), issue_id="", now=T_DECLARE
            ).applied
        )
        conn = sqlite3.connect(str(self.path))
        try:
            conn.execute(
                "UPDATE lane_lifecycle_records SET lane_disposition = ?, replacement_state = ? "
                "WHERE lane_id = ?",
                (DISPOSITION_HIBERNATED, "replaced ", recovery.lane_id),
            )
            conn.commit()
        finally:
            conn.close()
        # The superseded lane must be ACTIVE to hand ownership over; rehydrate it first with a
        # canonical replacement state so the ONLY unsettled input is the recovery lane's.
        self._force("replacement_state", "replaced")
        self.assertTrue(
            self.store.transition_disposition(
                self.key,
                expected_disposition=DISPOSITION_HIBERNATED,
                expected_revision=self._rec().revision,
                target=DISPOSITION_ACTIVE,
                decision=_decision(),
                now=T_RESUME,
            ).applied
        )
        out = self.store.supersede_and_activate(
            superseded=self.key,
            expected_revision=self._rec().revision,
            recovery=recovery,
            decision=_decision(),
            recovery_expected_disposition=DISPOSITION_HIBERNATED,
            recovery_expected_revision=self.store.get(recovery).revision,
            now=T_LATER,
        )
        self.assertFalse(out.applied, "a non-canonical replacement state is never settled")
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)
        self.assertEqual(self.store.get(recovery).replacement_state, "replaced ")

    def test_a_padded_stored_disposition_is_refused_by_the_expected_state_guard(self) -> None:
        """The measured second line of defence — recorded, not assumed (j#94805 ruling)."""
        self._force("lane_disposition", "hibernated ")
        out = self.store.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_HIBERNATED,
            expected_revision=self._rec().revision,
            target=DISPOSITION_ACTIVE,
            decision=_decision(),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self._rec().lane_disposition, "hibernated ")

    def test_the_canonical_replacement_lifecycle_still_works(self) -> None:
        """Control: the tightening refuses ONLY the unclassifiable values."""
        self._force("replacement_state", "not_requested")
        self.assertTrue(
            self.store.transition_disposition(
                self.key,
                expected_disposition=DISPOSITION_HIBERNATED,
                expected_revision=self._rec().revision,
                target=DISPOSITION_ACTIVE,
                decision=_decision(),
            ).applied,
            "a canonical settled replacement state rehydrates",
        )
        out = self._replacement_store().request_replacement(
            self.key,
            expected_revision=self._rec().revision,
            action_id="probe-canonical",
            pins=[
                ReleasePin("worker", _wk_name(), f"{_WS}:pOLD", _STARTUP_ACTION)
            ],
            decision=_decision(),
        )
        self.assertTrue(out.applied, out.reason)
        self.assertEqual(self._rec().replacement_state, "requested")


class RawAuthorityRenderingTest(unittest.TestCase):
    """A raw stored state may not forge lines or inject terminal control bytes (j#94805 R9-F2).

    The domain deliberately keeps the raw value (R8-F1). The presentation boundary is what has to
    be safe, and it was not: measured before the fix, a stored value of
    ``"weird\\n  commit: applied=True\\x1b[31m"`` reached the hibernate text output with the newline
    and the ANSI ESC intact, forging a line that read ``commit: applied=True``.
    """

    HOSTILE = "weird\n  commit: applied=True\x1b[31m"

    def _release(self, state: str):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_process_release import (  # noqa: E501
            ReleaseOutcome,
        )

        return ReleaseOutcome(action_id="a", process_release=state, detail="unknown state")

    def test_the_helper_escapes_every_control_byte_and_leaves_canonical_alone(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_process_release import (  # noqa: E501
            render_release_state,
        )

        for token in (RELEASE_RELEASED, RELEASE_NOT_REQUESTED, RELEASE_REQUESTED, RELEASE_PARTIAL):
            self.assertEqual(render_release_state(token), token, "canonical renders verbatim")
        for hostile in (self.HOSTILE, "a\rb", "a\tb", "x\x00y", "released ", ""):
            with self.subTest(value=hostile):
                out = render_release_state(hostile)
                self.assertTrue(out.isprintable(), f"control byte survived: {out!r}")
                self.assertNotIn("\n", out)
                self.assertNotIn("\x1b", out)
                # Reversible: the escaped form still names the exact stored bytes.
                self.assertEqual(ast.literal_eval(out), hostile)

    def _hibernate_text(self, state: str) -> str:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_cli import (  # noqa: E501
            format_hibernate_text,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate import (  # noqa: E501
            HibernateOutcome,
            HibernatePreflight,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_assertions import (  # noqa: E501
            HibernateAssertions,
        )

        return format_hibernate_text(
            HibernateOutcome(
                executed=True,
                preflight=HibernatePreflight(
                    original_identity_known=True, park_satisfied=True,
                    obligations_satisfied=True, lane_idle=True, boundary_ok=True,
                    inventory_readable=True, project_generation_matched=True,
                    project_attestation_ok=True, action_generation_current=True,
                    action_revision_current=True, action_identity_current=True,
                    assertions=HibernateAssertions(),
                ),
                issue=_ISSUE, lane=_LANE, release=self._release(state),
            )
        )

    def _supersede_text(self, state: str) -> str:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_supersede import (  # noqa: E501
            SupersedeOutcome,
            SupersedePreflight,
            format_supersede_text,
        )

        return format_supersede_text(
            SupersedeOutcome(
                executed=True,
                preflight=SupersedePreflight(
                    original_identity_known=True, recovery_both_slots_live=True,
                    recovery_attested=True, original_idle=True,
                ),
                issue=_ISSUE, original_lane=_LANE, recovery_lane=f"{_LANE}_recovery",
                release=self._release(state),
            )
        )

    def test_neither_text_surface_can_be_line_forged(self) -> None:
        """BOTH public renderers, each actually executed (review j#94840 R10-F2).

        The earlier version of this pin claimed "both text surfaces" but only ever called
        `format_hibernate_text`; the supersede renderer — which carries the identical line — was
        never run. Reading the shared helper and inferring the second surface is not the same as
        executing it, and the name asserted a coverage the body did not have.
        """
        for surface, render in (
            ("hibernate", self._hibernate_text),
            ("supersede", self._supersede_text),
        ):
            for state in (RELEASE_RELEASED, self.HOSTILE):
                with self.subTest(surface=surface, process_release=state):
                    text = render(state)
                    self.assertNotIn("\x1b", text)
                    release_lines = [
                        ln for ln in text.splitlines() if ln.startswith("  release: ")
                    ]
                    self.assertEqual(len(release_lines), 1, "exactly one release line")
                    # The forged text must not appear as its own line.
                    self.assertNotIn("  commit: applied=True", text.splitlines())
                    if state == RELEASE_RELEASED:
                        # Canonical rendering is unchanged: verbatim, unquoted.
                        self.assertEqual(
                            release_lines[0],
                            f"  release: {RELEASE_RELEASED} (unknown state)",
                        )

    def test_the_json_payload_keeps_the_raw_value_verbatim(self) -> None:
        """Escaping is presentation-only: the machine surface stays exact and reversible."""
        import json as _json

        rel = self._release(self.HOSTILE)
        payload = _json.loads(_json.dumps({"process_release": rel.process_release}))
        self.assertEqual(payload["process_release"], self.HOSTILE)


class StoredBindingKindIsNeverNormalisedTest(_Fixture):
    """`binding_kind` is a stored authority too (review j#94840 R10-F1).

    The census that finding names: 19 ``norm(...binding_kind)`` sites across 14 modules, of which
    exactly ONE — ``LaneDeclarationStore.declare_lane``'s ``kind = norm(binding_kind)`` — is
    ingress (a caller's argument). The other 18 classified what the ROW already held, so a padded
    value was read as a canonical kind. Measured through the public API before the fix:

    - ``backfill_active_binding`` applied on a stored ``"issue "`` row (revision 1 -> 2)
    - ``open_next_generation`` applied on one (generation 1 -> 2, back to ``active``)
    - ``record_matches_binding``'s issue branch returned True for a stored ``"project_gateway"``
      row, because it compared the issue id ALONE and never looked at the kind

    Byte-exact classification alone was not sufficient for ``open_next_generation``: with an
    unclassifiable kind every branch simply failed to fire and control fell through to the write,
    so that CAS now refuses an out-of-vocabulary kind outright — the j#94750 R7-F1 discipline.
    """

    # "" IS non-canonical (Redmine #14477 review j#94992 R11-F1). I previously excluded it,
    # calling the decoder's ``or BINDING_KIND_ISSUE`` a "pre-v5 legacy default"; that was wrong.
    # A v4 row has no ``binding_kind`` COLUMN and the migration is
    # ``ADD COLUMN binding_kind TEXT NOT NULL DEFAULT 'issue'``, which backfills the literal
    # token — so a migrated row reads ``'issue'`` and an empty one is not a legacy artifact.
    NON_CANONICAL = ("issue ", " issue", "ISSUE", "weird_kind", "")

    def _seed(self) -> None:  # noqa: D401 - the fixture's own seed is not needed here
        super()._seed()

    def _decl(self):
        return LaneDeclarationStore(path=self.path)

    def _force_kind(self, token: str, *, lane: str = _LANE) -> None:
        conn = sqlite3.connect(str(self.path))
        try:
            conn.execute(
                "UPDATE lane_lifecycle_records SET binding_kind = ? WHERE lane_id = ?",
                (token, lane),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self._rec().binding_kind, token, "raw seed did not stick")

    def _legacy_owner(self, token: str):
        """An ACTIVE issue owner whose worktree binding is MISSING — the backfill precondition."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "state.sqlite"
        key = LaneLifecycleKey(_WS, _LANE)
        store = LaneLifecycleStore(path=path)
        self.assertTrue(
            LaneDeclarationStore(path=path).declare_lane(
                key, decision=_decision(), issue_id=_ISSUE,
                declared_slots=(), worktree_identity="",
            ).applied
        )
        conn = sqlite3.connect(str(path))
        try:
            conn.execute(
                "UPDATE lane_lifecycle_records SET binding_kind = ? WHERE lane_id = ?",
                (token, _LANE),
            )
            conn.commit()
        finally:
            conn.close()
        return store, key, path

    # ------------------------------------------------- public boundary: backfill
    def test_backfill_refuses_a_non_canonical_stored_binding_kind(self) -> None:
        for token in self.NON_CANONICAL + ("project_gateway",):
            with self.subTest(binding_kind=token):
                store, key, path = self._legacy_owner(token)
                rec0 = store.get(key)
                out = LaneDeclarationStore(path=path).backfill_active_binding(
                    key, expected_revision=rec0.revision,
                    issue_id=_ISSUE, worktree_identity="wt-new",
                )
                self.assertFalse(out.applied)
                rec1 = store.get(key)
                self.assertEqual(rec1.revision, rec0.revision, "zero write")
                self.assertEqual(rec1.worktree_identity, "", "zero write")
                self.assertEqual(rec1.binding_kind, token, "the invalid value is not laundered")

    def test_backfill_still_applies_for_the_canonical_kind(self) -> None:
        store, key, path = self._legacy_owner(BINDING_KIND_ISSUE)
        rec0 = store.get(key)
        out = LaneDeclarationStore(path=path).backfill_active_binding(
            key, expected_revision=rec0.revision, issue_id=_ISSUE, worktree_identity="wt-new",
        )
        self.assertTrue(out.applied, out.reason)
        self.assertEqual(store.get(key).worktree_identity, "wt-new")

    # ------------------------------------- public boundary: open_next_generation
    def _retired(self, token: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "state.sqlite"
        key = LaneLifecycleKey(_WS, _LANE)
        store = LaneLifecycleStore(path=path)
        self.assertTrue(
            LaneDeclarationStore(path=path).declare_lane(
                key, decision=_decision(), issue_id=_ISSUE,
                declared_slots=(), worktree_identity=_BOUND_WT,
            ).applied
        )
        conn = sqlite3.connect(str(path))
        try:
            conn.execute(
                "UPDATE lane_lifecycle_records SET binding_kind = ?, lane_disposition = ? "
                "WHERE lane_id = ?",
                (token, DISPOSITION_RETIRED, _LANE),
            )
            conn.commit()
        finally:
            conn.close()
        return store, key, path

    def test_open_next_generation_refuses_a_non_canonical_stored_binding_kind(self) -> None:
        for token in self.NON_CANONICAL:
            with self.subTest(binding_kind=token):
                store, key, path = self._retired(token)
                rec0 = store.get(key)
                out = LaneDeclarationStore(path=path).open_next_generation(
                    key, expected_revision=rec0.revision,
                    expected_generation=rec0.lane_generation, decision=_decision(),
                )
                self.assertFalse(out.applied)
                self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
                rec1 = store.get(key)
                self.assertEqual(rec1.lane_generation, rec0.lane_generation, "zero write")
                self.assertEqual(rec1.lane_disposition, DISPOSITION_RETIRED, "zero write")
                self.assertEqual(rec1.binding_kind, token)

    def test_open_next_generation_still_applies_for_the_canonical_kind(self) -> None:
        store, key, path = self._retired(BINDING_KIND_ISSUE)
        rec0 = store.get(key)
        out = LaneDeclarationStore(path=path).open_next_generation(
            key, expected_revision=rec0.revision,
            expected_generation=rec0.lane_generation, decision=_decision(),
        )
        self.assertTrue(out.applied, out.reason)
        rec1 = store.get(key)
        self.assertEqual(rec1.lane_generation, rec0.lane_generation + 1)
        self.assertEqual(rec1.lane_disposition, DISPOSITION_ACTIVE)

    # ------------------------------------------ representative retire / repair
    def test_a_retire_and_a_repair_refuse_a_non_canonical_stored_binding_kind(self) -> None:
        """Two of the 18 sites, driven through their own public stores."""
        from mozyo_bridge.core.state.lane_bound_retire import LaneBoundRetireStore
        from mozyo_bridge.core.state.lane_pin_repair import LanePinRepairStore

        for token in ("issue ", "weird_kind"):
            with self.subTest(binding_kind=token):
                self._force_kind(token)
                rec = self._rec()
                retire = LaneBoundRetireStore(path=self.path).retire_released_hibernated_bound(
                    self.key,
                    expected_revision=rec.revision,
                    issue_id=_ISSUE,
                    worktree_identity=_BOUND_WT,
                    decision=_decision(),
                )
                self.assertFalse(retire.applied)
                self.assertEqual(self._rec().lane_disposition, DISPOSITION_HIBERNATED)

                repair = LanePinRepairStore(path=self.path).repair_hibernated_bound_pins(
                    self.key,
                    expected_revision=rec.revision,
                    expected_generation=rec.lane_generation,
                    issue_id=_ISSUE,
                    worktree_identity=_BOUND_WT,
                    declared_slots=_live_pins(),
                    decision=_decision(),
                )
                self.assertFalse(repair.applied)
                self.assertEqual(self._rec().revision, rec.revision, "zero write")

    # -------------------------------------------- pure / preflight classifiers
    def test_the_identity_predicate_requires_the_canonical_issue_kind(self) -> None:
        from mozyo_bridge.core.state.lane_binding import record_matches_binding

        for token in self.NON_CANONICAL + ("project_gateway",):
            with self.subTest(binding_kind=token):
                self._force_kind(token)
                self.assertFalse(
                    record_matches_binding(self._rec(), issue_id=_ISSUE),
                    "an issue binding requires an issue-kind row, not just a matching issue id",
                )
        self._force_kind(BINDING_KIND_ISSUE)
        self.assertTrue(record_matches_binding(self._rec(), issue_id=_ISSUE))

    def test_the_pure_classifiers_reject_a_non_canonical_stored_kind(self) -> None:
        from mozyo_bridge.core.state.lane_lifecycle_model import stored_binding_kind_is
        from mozyo_bridge.core.state.lane_worktree_binding_signature import (
            SIGNATURE_OK,
            classify_repair_signature,
        )

        self.assertTrue(stored_binding_kind_is(BINDING_KIND_ISSUE, BINDING_KIND_ISSUE))
        for token in self.NON_CANONICAL:
            with self.subTest(binding_kind=token):
                self.assertFalse(stored_binding_kind_is(token, BINDING_KIND_ISSUE))
                self._force_kind(token)
                verdict = classify_repair_signature(self._rec(), issue_id=_ISSUE)
                self.assertNotEqual(
                    verdict, SIGNATURE_OK,
                    "the preflight signature must not accept a non-issue-kind row",
                )

    def test_the_decoder_does_not_promote_an_empty_stored_kind_to_canonical(self) -> None:
        """Review j#94992 R11-F1 — the inversion of what this pin used to assert.

        The decoder read ``str(row[17] or BINDING_KIND_ISSUE)``, so an empty stored value became
        the canonical ``issue`` BEFORE any classifier saw it: every byte-exact predicate and the
        vocabulary guard added in R11 were handed an owner authority the storage never carried,
        and a raw ``''`` row reopened its generation (1 -> 2). I defended that default as a
        "pre-v5 legacy" mapping and pinned it as a decision; the defence was wrong and is
        retracted here in the pin itself, not only in the journal.
        """
        conn = sqlite3.connect(str(self.path))
        try:
            conn.execute(
                "UPDATE lane_lifecycle_records SET binding_kind = '' WHERE lane_id = ?", (_LANE,)
            )
            conn.commit()
            stored = conn.execute(
                "SELECT binding_kind FROM lane_lifecycle_records WHERE lane_id = ?", (_LANE,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(stored, "", "the STORED bytes are empty")
        self.assertEqual(self._rec().binding_kind, "", "...and the decode keeps them empty")
        from mozyo_bridge.core.state.lane_lifecycle_model import stored_binding_kind_is

        self.assertFalse(stored_binding_kind_is(self._rec().binding_kind, BINDING_KIND_ISSUE))

    def test_an_empty_stored_kind_cannot_reopen_a_generation(self) -> None:
        """The exact write the finding measured, now refused zero-write."""
        store, key, path = self._retired("")
        rec0 = store.get(key)
        self.assertEqual(rec0.binding_kind, "")
        out = LaneDeclarationStore(path=path).open_next_generation(
            key, expected_revision=rec0.revision,
            expected_generation=rec0.lane_generation, decision=_decision(),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        conn = sqlite3.connect(str(path))
        try:
            raw = conn.execute(
                "SELECT binding_kind, lane_disposition, lane_generation, revision "
                "FROM lane_lifecycle_records WHERE lane_id = ?",
                (_LANE,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(
            raw,
            ("", DISPOSITION_RETIRED, rec0.lane_generation, rec0.revision),
            "zero write: the raw bytes, disposition, generation and revision are untouched",
        )

    def test_a_genuine_v4_migration_yields_the_canonical_token(self) -> None:
        """The positive compatibility control the finding requires (j#94992 item 4).

        A v4 row has no ``binding_kind`` column at all. Rewinding to that shape and re-opening
        the store runs the real migration, whose
        ``ADD COLUMN binding_kind TEXT NOT NULL DEFAULT 'issue'`` backfills the LITERAL token —
        so a genuinely migrated legacy row is canonical and keeps working. That is what makes the
        empty value above a corruption rather than a legacy artifact.
        """
        conn = sqlite3.connect(str(self.path))
        try:
            # The partial owner index references the column, so it goes first.
            conn.execute("DROP INDEX IF EXISTS idx_lane_lifecycle_active_project_owner")
            # A genuine v4 shape lacks every column added after it, not only the v5 tranche —
            # the schema gate matches the FULL signature and refuses a partial rewind.
            for column in (
                "reconcile_close_pin",  # v11
                "lane_epoch",  # v10
                "release_observation",  # v9
                "hibernated_at",  # v8
                "lane_kind",  # v7
                "reconcile_phase",  # v6
                "declared_slots", "lane_generation", "project_scope", "binding_kind",  # v5
            ):
                conn.execute(f"ALTER TABLE lane_lifecycle_records DROP COLUMN {column}")
            conn.execute(
                "UPDATE state_schema_components SET schema_version = 4 WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            )
            conn.commit()
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM pragma_table_info('lane_lifecycle_records') "
                    "WHERE name = 'binding_kind'"
                ).fetchone(),
                "the v4 shape genuinely has no binding_kind column",
            )
        finally:
            conn.close()

        # A WRITE-path open runs the backfilling migration.
        migrated = LaneDeclarationStore(path=self.path).declare_lane(
            LaneLifecycleKey(_WS, f"{_LANE}_v4probe"),
            decision=_decision(), issue_id=_ISSUE,
            declared_slots=(), worktree_identity=_BOUND_WT,
        )
        self.assertTrue(migrated.applied, migrated.reason)
        conn = sqlite3.connect(str(self.path))
        try:
            raw = conn.execute(
                "SELECT binding_kind FROM lane_lifecycle_records WHERE lane_id = ?", (_LANE,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(raw, BINDING_KIND_ISSUE, "the migration backfills the literal token")
        self.assertEqual(self._rec().binding_kind, BINDING_KIND_ISSUE)
        from mozyo_bridge.core.state.lane_binding import record_matches_binding

        self.assertTrue(record_matches_binding(self._rec(), issue_id=_ISSUE))

    def test_only_the_declaring_surface_normalises_its_argument(self) -> None:
        """Ingress stays normalised: `declare_lane` still accepts a padded ARGUMENT."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "state.sqlite"
        key = LaneLifecycleKey(_WS, f"{_LANE}_ingress")
        out = LaneDeclarationStore(path=path).declare_lane(
            key, decision=_decision(), binding_kind=" issue ", issue_id=_ISSUE,
            declared_slots=(), worktree_identity=_BOUND_WT,
        )
        self.assertTrue(out.applied, out.reason)
        # ...and what it STORES is the canonical token, so no reader ever sees the padding.
        self.assertEqual(
            LaneLifecycleStore(path=path).get(key).binding_kind, BINDING_KIND_ISSUE
        )


class SchemaVersionTest(unittest.TestCase):
    def test_the_anchor_and_observation_are_still_present_after_later_bumps(self) -> None:
        # Redmine #14756 moved the component to v10. This assertion is deliberately NOT
        # re-pinned to a literal: what #14477 needs to stay true is that its two columns
        # survive, not that no later issue may add one. Pinning the number made a later
        # additive bump look like a #14477 regression.
        self.assertGreaterEqual(LANE_LIFECYCLE_SCHEMA_VERSION, 9)
        self.assertIn("hibernated_at", _COLUMN_DEFS)
        self.assertIn("release_observation", _COLUMN_DEFS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
