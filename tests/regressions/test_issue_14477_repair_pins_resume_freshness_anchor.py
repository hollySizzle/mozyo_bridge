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
2. **acceptance 2** — a REAL pre-hibernate survivor (attested before T0) still fails closed
   ``stale_generation`` after the very same repair;
3. **acceptance 3** — metadata-only writes (declared-pin repair, release request / outcome)
   never move the boundary;
4. **acceptance 4** — resume still atomically adopts the exact fresh declared pin snapshot,
   and the provider-binding fence still blocks;
5. **acceptance 5** — a pre-v8 row (no anchor) stays readable WITHOUT migrating the store, and
   gets NO substitute boundary: the freshness half fails CLOSED. Review j#94515 F1 / verdict
   j#94520 measured the alternative — standing ``updated_at`` in for the boundary admitted a
   genuine pre-hibernate survivor, because ``updated_at`` is not monotonic (no writer on this
   component validates its caller-supplied ``now`` against the row's prior stamp). That exact
   ordering is pinned here, as is the absence of ANY substitute column;
6. the anchor's own lifecycle: cleared on rehydrate, re-stamped on the next hibernation, and
   its inbound-edge enumeration DERIVED from the public transition policy rather than recalled,
   so a future edge into ``hibernated`` cannot quietly bypass the stamp;
7. the stamp's own TRUST boundary, which is still OPEN — review j#94531 R2-F1. A backdated
   ``now`` on the hibernate CAS stores a boundary earlier than a survivor's attestation and
   admits it. ``PostV8BackdatedStampTest`` states the correct refusal as an expected failure
   (not inverted into a pin of the defect) plus a fact-pin on the missing stamp validation, so
   the hole is durable and visible in the suite. Scope disposition: design_consultation j#94543.

Everything is synthetic: a temp store path, a fake herdr inventory and fake attestation reads.
No pane / process / route / worktree mutation. Hermeticity is itself CHECKED rather than
intended (``OperatorHomeHermeticityTest``, j#94504 item 4): the v8 bump forward-migrated the
operator's shared ``state.sqlite`` from inside a full-suite run, because a store built without
an explicit ``path=`` resolves ``MOZYO_BRIDGE_HOME`` / ``~/.mozyo_bridge`` and writing there
takes the write-MIGRATING gate.
"""

from __future__ import annotations

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
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DISPOSITION_RETIRED,
    DISPOSITIONS,
    RELEASE_RELEASED,
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
from mozyo_bridge.core.state.lane_pin_repair import LanePinRepairStore  # noqa: E402
from mozyo_bridge.core.state.lane_pin_role import read_declared_pin_pair  # noqa: E402
from mozyo_bridge.core.state.lane_released_locator_fence import (  # noqa: E402
    FENCE_EVIDENCE_ABSENT,
    FENCE_EVIDENCE_INCOMPLETE,
    FENCE_EVIDENCE_UNREADABLE,
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


def _attest(provider: str, locator: str, observed_at: str) -> IdentityAttestationRecord:
    return IdentityAttestationRecord(
        assigned_name=encode_assigned_name(_WS, provider, _LANE),
        workspace_id=_WS,
        role=provider,
        lane_id=_LANE,
        locator=locator,
        verdict=VERDICT_PRESENT,
        observed_at=observed_at,
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
    ):
        self._rows = [
            {"name": _gw_name(), "pane_id": gw_locator},
            {"name": _wk_name(), "pane_id": wk_locator},
        ]
        self._attest = {
            _gw_name(): _attest(_GW_PROVIDER, gw_locator, observed_at),
            _wk_name(): _attest(_WK_PROVIDER, wk_locator, observed_at),
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
            pins=[
                ReleasePin("gateway", _gw_name(), f"{_WS}:pOLD_G"),
                ReleasePin("worker", _wk_name(), f"{_WS}:pOLD_W"),
            ],
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
        outcome = self._resume(ops=_FakeOps(observed_at=T_SURVIVOR))
        self.assertTrue(outcome.is_blocked)
        self.assertIn(BLOCK_PAIR_ATTESTATION, outcome.preflight.blocked_reasons)
        self.assertIn("stale_generation", outcome.preflight.pair_attestation_detail)
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_HIBERNATED)
        self.assertIsNone(outcome.transition)

    def test_a_pane_attesting_exactly_at_the_boundary_is_not_fresh(self) -> None:
        """``strictly after`` stays strict: equality is a survivor, not a relaunch."""
        outcome = self._resume(ops=_FakeOps(observed_at=T_HIBERNATE))
        self.assertTrue(outcome.is_blocked)
        self.assertIn("stale_generation", outcome.preflight.pair_attestation_detail)


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

    def test_an_unresolvable_boundary_fails_the_freshness_half_closed(self) -> None:
        """An absent threshold is not a proof of freshness.

        ``evaluate_pair_attestation`` skips the freshness comparison on an empty
        ``fresh_after``, so a row with no boundary would otherwise be admitted on the locator
        pin alone — precisely the survivor hole the gate exists to close.
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
        self.assertTrue(outcome.is_blocked)
        self.assertIn(BLOCK_PAIR_ATTESTATION, outcome.preflight.blocked_reasons)
        self.assertIn(ANCHOR_UNAVAILABLE, outcome.preflight.pair_attestation_detail)
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_HIBERNATED)

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
                pins=[
                    ReleasePin("gateway", _gw_name(), released[0]),
                    ReleasePin("worker", _wk_name(), released[1]),
                ],
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

        self.assertTrue(outcome.is_blocked)  # ...and the locator half refuses anyway
        self.assertIn(BLOCK_PAIR_ATTESTATION, outcome.preflight.blocked_reasons)
        self.assertIn(FENCE_LOCATOR_REUSED, outcome.preflight.pair_attestation_detail)
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_HIBERNATED)
        self.assertIsNone(outcome.transition)

    def test_a_relaunch_on_new_locators_still_resumes(self) -> None:
        """Not a blanket refusal: different pane-ids are exactly what a real relaunch has."""
        self._resumed_once()
        self._hibernate_again(released=(_GW_LOC, _WK_LOC), now=T_RESUME)
        fresh_gw, fresh_wk = f"{_WS}:p9A", f"{_WS}:p9B"
        outcome = self._resume(
            ops=_FakeOps(observed_at=T_LATER, gw_locator=fresh_gw, wk_locator=fresh_wk)
        )
        self.assertFalse(
            outcome.is_blocked,
            f"blocked: {outcome.preflight.blocked_reasons} "
            f"({outcome.preflight.pair_attestation_detail})",
        )
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_ACTIVE)

    def test_absent_release_evidence_refuses(self) -> None:
        """j#94544 A.2: the row cannot tell "no process existed" from "a survivor was never
        recorded", so absence of evidence is never read as freshness."""
        self._resumed_once()
        self._hibernate_again(released=None, now=T_RESUME)  # release never requested
        outcome = self._resume(ops=_FakeOps(observed_at=T_LATER, gw_locator=f"{_WS}:p9A",
                                           wk_locator=f"{_WS}:p9B"))
        self.assertTrue(outcome.is_blocked)
        self.assertIn(FENCE_EVIDENCE_ABSENT, outcome.preflight.pair_attestation_detail)
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_HIBERNATED)


class ReleasedLocatorVerdictUnitTest(unittest.TestCase):
    """The pure predicate's own boundaries (no store, no clock)."""

    def _rec(self, *, release=RELEASE_RELEASED, pins_raw=None, locators=(_GW_LOC, _WK_LOC)):
        raw = pins_raw
        if raw is None:
            raw = encode_release_pins(
                tuple(ReleasePin("gateway", _gw_name(), loc) for loc in locators)
            )
        return LaneLifecycleRecord(
            repo_workspace_id=_WS, lane_id=_LANE, process_release=release, release_pins=raw
        )

    def test_no_record_is_absent(self) -> None:
        self.assertEqual(released_locator_verdict(None, [_GW_LOC]), (False, FENCE_EVIDENCE_ABSENT))

    def test_an_unreleased_generation_is_absent(self) -> None:
        ok, reason = released_locator_verdict(self._rec(release="not_requested"), [_GW_LOC])
        self.assertEqual((ok, reason), (False, FENCE_EVIDENCE_ABSENT))

    def test_corrupt_pins_are_unreadable_not_empty(self) -> None:
        ok, reason = released_locator_verdict(self._rec(pins_raw="{not json"), [_GW_LOC])
        self.assertEqual((ok, reason), (False, FENCE_EVIDENCE_UNREADABLE))

    def test_an_observed_locator_in_the_released_set_is_reuse(self) -> None:
        ok, reason = released_locator_verdict(self._rec(), [_GW_LOC, f"{_WS}:pNEW"])
        self.assertEqual((ok, reason), (False, FENCE_LOCATOR_REUSED))

    def test_fewer_released_locators_than_observed_slots_is_incomplete(self) -> None:
        rec = self._rec(locators=(f"{_WS}:pOLD_ONLY",))
        ok, reason = released_locator_verdict(rec, [f"{_WS}:pN1", f"{_WS}:pN2"])
        self.assertEqual((ok, reason), (False, FENCE_EVIDENCE_INCOMPLETE))

    def test_all_different_locators_with_complete_evidence_pass(self) -> None:
        ok, reason = released_locator_verdict(self._rec(), [f"{_WS}:pN1", f"{_WS}:pN2"])
        self.assertEqual((ok, reason), (True, FENCE_OK))

    def test_the_stamp_itself_is_still_unvalidated_by_design(self) -> None:
        """A does NOT validate the hibernate stamp; it makes the stamp non-load-bearing.

        Recorded so nobody later reads the green suite as "the stamp is now trustworthy". The
        authority-grade epoch that would make it trustworthy is Redmine #14756 (disposition B).
        """
        store_pins = encode_release_pins((ReleasePin("gateway", _gw_name(), _GW_LOC),))
        rec = LaneLifecycleRecord(
            repo_workspace_id=_WS,
            lane_id=_LANE,
            process_release=RELEASE_RELEASED,
            release_pins=store_pins,
            hibernated_at=T_BACKDATED,
        )
        # The anchor is happily a backdated value — nothing here rejects it...
        self.assertEqual(resume_freshness_anchor(rec), (T_BACKDATED, ANCHOR_HIBERNATE_TRANSITION))
        # ...and the locator fence is what actually refuses the survivor.
        self.assertEqual(
            released_locator_verdict(rec, [_GW_LOC]), (False, FENCE_LOCATOR_REUSED)
        )


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


class SchemaVersionTest(unittest.TestCase):
    def test_the_anchor_landed_as_schema_v8(self) -> None:
        self.assertEqual(LANE_LIFECYCLE_SCHEMA_VERSION, 8)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
