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
5. **acceptance 5** — a pre-v8 row (no anchor) stays readable WITHOUT migrating the store and
   keeps its pre-#14477 ``updated_at`` boundary under an explicitly surfaced compatibility
   token; an anchor that resolves to nothing at all fails the freshness half CLOSED rather
   than skipping it;
6. the anchor's own lifecycle: cleared on rehydrate, re-stamped on the next hibernation, and
   its inbound-edge enumeration DERIVED from the public transition policy rather than recalled,
   so a future edge into ``hibernated`` cannot quietly bypass the stamp.

Everything is synthetic: a temp store path, a fake herdr inventory and fake attestation reads.
No pane / process / route / worktree mutation, and never the shared ``$HOME/.mozyo_bridge``.
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
    ANCHOR_LIFECYCLE_UPDATED_AT,
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
)
from mozyo_bridge.core.state.lane_lifecycle_schema import (  # noqa: E402
    LANE_LIFECYCLE_COMPONENT,
    LANE_LIFECYCLE_SCHEMA_VERSION,
)
from mozyo_bridge.core.state.lane_pin_repair import LanePinRepairStore  # noqa: E402
from mozyo_bridge.core.state.lane_pin_role import read_declared_pin_pair  # noqa: E402
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

    def __init__(self, *, observed_at: str = T_FRESH, providers=(_GW_PROVIDER, _WK_PROVIDER)):
        self._rows = [
            {"name": _gw_name(), "pane_id": _GW_LOC},
            {"name": _wk_name(), "pane_id": _WK_LOC},
        ]
        self._attest = {
            _gw_name(): _attest(_GW_PROVIDER, _GW_LOC, observed_at),
            _wk_name(): _attest(_WK_PROVIDER, _WK_LOC, observed_at),
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

    def _repair_pins(self, now: str = T_REPAIR):
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

    def test_a_pre_v8_row_reads_without_migrating_and_names_its_authority(self) -> None:
        self.assertTrue(self._repair_pins().applied)
        before = self._rewind_to_v7()

        rec = self.store.get(self.key)
        anchor, authority = resume_freshness_anchor(rec)

        self.assertEqual(rec.hibernated_at, "")  # the padded additive default, not a guess
        self.assertEqual(anchor, T_REPAIR)  # its own ``updated_at``, the pre-#14477 boundary
        self.assertEqual(authority, ANCHOR_LIFECYCLE_UPDATED_AT)
        # The read is non-migrating (#13844): not one byte moved, the version is still 7.
        self.assertEqual(self.path.read_bytes(), before)

    def test_the_pre_v8_fallback_is_stricter_never_weaker(self) -> None:
        """``updated_at >= hibernated_at`` always, so the legacy threshold can only refuse
        MORE. The old false-stale outcome is preserved verbatim on an old row — surfaced with
        its authority token — rather than being repaired by a guessed boundary."""
        self.assertTrue(self._repair_pins().applied)
        self._rewind_to_v7()
        outcome = self._resume()
        self.assertTrue(outcome.is_blocked)
        self.assertIn("stale_generation", outcome.preflight.pair_attestation_detail)
        self.assertIn(
            f"freshness anchor: {ANCHOR_LIFECYCLE_UPDATED_AT}",
            outcome.preflight.pair_attestation_detail,
        )

    def test_a_pre_v8_row_with_no_later_metadata_write_still_resumes(self) -> None:
        """The fallback is a threshold, not a blanket refusal: an untouched legacy row whose
        ``updated_at`` still predates the fresh attestation resumes exactly as it always did."""
        self._rewind_to_v7()  # no repair -> updated_at is still the hibernate-side stamp
        outcome = self._resume()
        self.assertFalse(
            outcome.is_blocked,
            f"blocked: {outcome.preflight.blocked_reasons} "
            f"({outcome.preflight.pair_attestation_detail})",
        )

    def test_an_unresolvable_boundary_fails_the_freshness_half_closed(self) -> None:
        """An absent threshold is not a proof of freshness.

        ``evaluate_pair_attestation`` skips the freshness comparison on an empty
        ``fresh_after``, so a row carrying NEITHER stamp would otherwise be admitted on the
        locator pin alone — precisely the survivor hole the gate exists to close.
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


class SchemaVersionTest(unittest.TestCase):
    def test_the_anchor_landed_as_schema_v8(self) -> None:
        self.assertEqual(LANE_LIFECYCLE_SCHEMA_VERSION, 8)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
