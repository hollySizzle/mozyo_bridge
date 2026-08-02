"""Redmine #14756 — the lane epoch as a clock-free, locator-free generation proof.

#14477 closed the resume freshness hole twice and both fixes rested on something a caller
could supply: a timestamp (defeated by a backdated CAS stamp, a regressed host clock, or a
self-written ``observed_at``) and then a released-locator fence (sound, but permanently
fail-closed when the release evidence is missing or a tmux pane-id is REUSED). This module
pins the replacement: a monotonic epoch the STORE mints inside the hibernate CAS from its own
stored value, injected into the launched processes' env, and self-attested by the process
that received it.

The tests are organised by what each one would let through if the code regressed, not by
which function it calls — a name like "epoch advances" says nothing about the defect it
guards. Every fixture reaches its state through REAL transitions (``declare_active`` ->
``transition_disposition``), never by hand-assembling a row: #14477 R5-F1 measured that a
hand-built, writer-unreachable shape hid a semantic error for several rounds.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "src"))

from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    HerdrIdentityAttestationError,
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
    VERDICT_PRESENT,
)
from mozyo_bridge.core.state.herdr_identity_attestation_schema import (  # noqa: E402
    HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
    migrate_attestation_store,
    write_drops_lane_epoch,
)
from mozyo_bridge.core.state.lane_epoch import (  # noqa: E402
    EPOCH_ATTESTATION_ABSENT,
    EPOCH_AUTHORITY_UNAVAILABLE,
    EPOCH_MALFORMED,
    EPOCH_NOT_NEWER,
    EPOCH_OK,
    LANE_EPOCH_UNMINTED,
    MOZYO_LANE_EPOCH_ENV,
    hibernate_boundary_epoch,
    lane_epoch_verdict,
    parse_attested_epoch,
    required_resume_epoch,
)
from mozyo_bridge.core.state.lane_lifecycle import (  # noqa: E402
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
)

WS = "wProj"
LANE = "issue_14756_lane"
ISSUE = "14756"


def _decision() -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=ISSUE, journal_id="96356")


def _hibernated_lane(tmp: str, *, cycles: int = 1):
    """A lane driven through ``cycles`` real hibernate/rehydrate round trips.

    Reached via the actual CAS both ways, so the epoch under test is one the production
    writer minted rather than one a fixture asserted into place.
    """
    store = LaneLifecycleStore(home=Path(tmp))
    key = LaneLifecycleKey(WS, LANE)
    store.declare_active(key, decision=_decision(), issue_id=ISSUE)
    for index in range(cycles):
        rec = store.get(key)
        store.transition_disposition(
            key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=rec.revision,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
        )
        if index < cycles - 1:
            rec = store.get(key)
            store.transition_disposition(
                key,
                expected_disposition=DISPOSITION_HIBERNATED,
                expected_revision=rec.revision,
                target=DISPOSITION_ACTIVE,
                decision=_decision(),
            )
    return store, key


def _lane_epoch_storage_section(markdown: str) -> str:
    """The `lane_epoch` storage paragraph of ``managed-state-model.md``, on its own.

    Assertions about a contract have to be made where the contract is stated. Checking the
    whole document let a positive match somewhere else stand in for the paragraph actually
    under review, which is how a section defining "malformed" as bare ``TEXT`` survived a
    green regression that asserted canonical TEXT is valid (review j#96997 F14).
    """
    start = markdown.index("**lane epoch (hibernate 世代の単調 counter)**")
    end = markdown.index("**release generation observation", start)
    return markdown[start:end]


class EpochIsMintedByTheStoreNotTheCaller(unittest.TestCase):
    """The defect class #14477 could never close: an authority a caller supplies."""

    def test_transition_disposition_takes_no_epoch_argument_at_all(self) -> None:
        # The strongest form of "a caller cannot supply it": there is no parameter to
        # supply. A future signature that accepted one would make every guarantee below
        # conditional on callers behaving, which is what `pins=` did before #14477 j#94582.
        import inspect

        from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore as Store

        params = inspect.signature(Store.transition_disposition).parameters
        self.assertNotIn("epoch", params)
        self.assertNotIn("lane_epoch", params)

    def test_epoch_is_computed_from_the_stored_row_under_the_same_cas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, key = _hibernated_lane(tmp)
            self.assertEqual(store.get(key).lane_epoch, "1")

    def test_a_refused_cas_mints_nothing(self) -> None:
        # A stale-revision caller must not advance the counter: if it did, a losing racer
        # could inflate the required epoch and strand the pair that legitimately won.
        with tempfile.TemporaryDirectory() as tmp:
            store, key = _hibernated_lane(tmp)
            before = store.get(key).lane_epoch
            outcome = store.transition_disposition(
                key,
                expected_disposition=DISPOSITION_ACTIVE,
                expected_revision=999,
                target=DISPOSITION_HIBERNATED,
                decision=_decision(),
            )
            self.assertFalse(outcome.applied)
            self.assertEqual(store.get(key).lane_epoch, before)


class EpochIsNeverResetOnTheWayBackToActive(unittest.TestCase):
    """Resetting the counter would re-mint epochs a released generation still holds.

    This is the inverse of #14477 R4-F1 (which enumerated the writers that must RESET a new
    field). Here the obligation runs the other way, and the sibling ``hibernated_at`` in the
    very same UPDATE *does* clear — so a reviewer reading only that line would conclude the
    epoch should clear too.
    """

    def test_rehydrate_clears_the_anchor_but_keeps_the_counter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, key = _hibernated_lane(tmp)
            rec = store.get(key)
            store.transition_disposition(
                key,
                expected_disposition=DISPOSITION_HIBERNATED,
                expected_revision=rec.revision,
                target=DISPOSITION_ACTIVE,
                decision=_decision(),
            )
            awake = store.get(key)
            self.assertEqual(awake.hibernated_at, "")  # boundary in force: cleared
            self.assertEqual(awake.lane_epoch, "1")  # counter: preserved

    def test_a_second_hibernate_advances_rather_than_restarting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, key = _hibernated_lane(tmp, cycles=2)
            rec = store.get(key)
            self.assertEqual(rec.lane_epoch, "2")
            # The exact regression a reset would cause: the FIRST generation's panes hold
            # epoch 1, and after a reset-and-climb the requirement would be 1 again, so a
            # long-dead survivor would satisfy it.
            self.assertEqual(lane_epoch_verdict(rec, "1"), (False, EPOCH_NOT_NEWER))
            self.assertEqual(lane_epoch_verdict(rec, "2"), (True, EPOCH_OK))


class UnmintedIsAbsenceNotAThresholdOfZero(unittest.TestCase):
    """``0`` must never behave as a boundary every positive epoch clears."""

    def test_a_lane_that_never_hibernated_states_no_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LaneLifecycleStore(home=Path(tmp))
            key = LaneLifecycleKey(WS, LANE)
            store.declare_active(key, decision=_decision(), issue_id=ISSUE)
            rec = store.get(key)
            self.assertEqual(rec.lane_epoch, "0")
            self.assertEqual(
                required_resume_epoch(rec),
                (LANE_EPOCH_UNMINTED, EPOCH_AUTHORITY_UNAVAILABLE),
            )

    def test_an_unminted_row_refuses_even_a_large_attested_epoch(self) -> None:
        # The fail-open shape this guards: `0` used as a threshold would admit anything.
        with tempfile.TemporaryDirectory() as tmp:
            store = LaneLifecycleStore(home=Path(tmp))
            key = LaneLifecycleKey(WS, LANE)
            store.declare_active(key, decision=_decision(), issue_id=ISSUE)
            self.assertEqual(
                lane_epoch_verdict(store.get(key), "9999"),
                (False, EPOCH_AUTHORITY_UNAVAILABLE),
            )

    def test_a_missing_row_states_no_requirement(self) -> None:
        self.assertEqual(
            lane_epoch_verdict(None, "1"), (False, EPOCH_AUTHORITY_UNAVAILABLE)
        )

    def test_the_authority_half_is_reported_before_the_process_half(self) -> None:
        # Precedence matters operationally: a lane that can prove nothing must not send an
        # operator off to relaunch panes that are already correct.
        self.assertEqual(lane_epoch_verdict(None, ""), (False, EPOCH_AUTHORITY_UNAVAILABLE))


class ForgedAndNonCanonicalTokensFailClosed(unittest.TestCase):
    """Acceptance 2: forgery / absence / old schema all refuse, each by its own name."""

    def test_absent_and_malformed_are_distinguished(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, key = _hibernated_lane(tmp)
            rec = store.get(key)
            self.assertEqual(
                lane_epoch_verdict(rec, ""), (False, EPOCH_ATTESTATION_ABSENT)
            )
            self.assertEqual(
                lane_epoch_verdict(rec, "nope"), (False, EPOCH_MALFORMED)
            )

    def test_non_canonical_spellings_of_a_passing_number_are_refused(self) -> None:
        # Each of these `int()`s to 1, which is exactly the required epoch. Coercing any of
        # them would admit a token no producer could have written — the laundering shape
        # #14477 R6-F1 / R8-F2 measured on the release-state vocabulary.
        with tempfile.TemporaryDirectory() as tmp:
            store, key = _hibernated_lane(tmp)
            rec = store.get(key)
            for token in ("01", " 1", "1 ", "+1", "1_0"[:2] + "", "\t1"):
                with self.subTest(token=token):
                    ok, reason = lane_epoch_verdict(rec, token)
                    self.assertFalse(ok)
                    self.assertEqual(reason, EPOCH_MALFORMED)

    def test_non_ascii_decimal_digits_are_refused(self) -> None:
        # `str.isdigit()` accepts these and `int()` parses them, so a digit-check delegated
        # to either would let a token the producer cannot render pass (the #14753 class).
        self.assertEqual(parse_attested_epoch("١")[1], EPOCH_MALFORMED)
        self.assertEqual(parse_attested_epoch("１")[1], EPOCH_MALFORMED)

    def test_zero_and_negative_tokens_are_refused(self) -> None:
        self.assertEqual(parse_attested_epoch("0")[1], EPOCH_MALFORMED)
        self.assertEqual(parse_attested_epoch("-1")[1], EPOCH_MALFORMED)

    def test_a_bool_is_not_the_epoch_one(self) -> None:
        self.assertEqual(parse_attested_epoch(True)[1], EPOCH_MALFORMED)

    def test_a_corrupt_stored_epoch_reads_as_unminted_not_as_a_threshold(self) -> None:
        # SQLite is typeless, so a foreign writer can leave any storage class in the column.
        # `int(2.5) == 2` would walk such a cell straight through the comparison (the #13689
        # trap), so the classifier accepts ONLY canonical decimal TEXT.
        #
        # `"3"` is deliberately NOT in this list any more: since j#96911 F2 the canonical
        # storage form IS text, so `"3"` is a legitimate counter of three. It sat here while
        # the column was INTEGER and would now assert the opposite of the contract.
        for corrupt in (2.5, "03", " 3", "3.0", None, True, 3, b"3"):
            with self.subTest(corrupt=corrupt):
                fake = type("Row", (), {"lane_epoch": corrupt})()
                self.assertEqual(
                    required_resume_epoch(fake)[1], EPOCH_AUTHORITY_UNAVAILABLE
                )


class StrictlyNewerIsStatedBothWaysAndAgrees(unittest.TestCase):
    """Acceptance 3's wording and the single stored counter must never diverge."""

    def test_boundary_and_requirement_are_derived_from_one_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, key = _hibernated_lane(tmp, cycles=3)
            rec = store.get(key)
            required, _ = required_resume_epoch(rec)
            boundary, _ = hibernate_boundary_epoch(rec)
            self.assertEqual(boundary, required - 1)
            # "strictly newer than the hibernate epoch" and "at least the required epoch"
            # must select the same tokens, or the acceptance text and the code have drifted.
            # Redmine #14756 review j#96949 F1 narrowed this from a lower bound to an
            # EQUALITY. "Strictly newer than the hibernate epoch" and "at least the required
            # epoch" used to select the same tokens; they also selected every LARGER token,
            # which admitted a forged future generation. The admissible set is the single
            # value the lifecycle has actually minted.
            for candidate in range(0, required + 3):
                with self.subTest(candidate=candidate):
                    admits = lane_epoch_verdict(rec, str(candidate))[0] if candidate else False
                    self.assertEqual(admits, candidate == required)
            self.assertEqual(required, boundary + 1)  # the two spellings still agree


class TheEpochSurvivesTheLaunchToAttestationRoundTrip(unittest.TestCase):
    """The epoch is only a proof if the exact token reaches the record and back."""

    def test_a_launch_injects_the_epoch_as_an_env_var_not_only_a_flag(self) -> None:
        # The env channel is what the immutability argument rests on (a live process's env
        # cannot be rewritten by anything else), so a wrapper flag alone would prove only
        # what the launcher intended.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (  # noqa: E501
            build_agent_start_argv,
        )
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (  # noqa: E501
            ResolvedProviderLaunch,
        )

        resolved = ResolvedProviderLaunch(
            provider_id="claude", executable="/usr/bin/true", argv0="/usr/bin/true"
        )
        argv = build_agent_start_argv(
            assigned_name="n",
            provider="claude",
            repo_root=Path("/tmp"),
            workspace_id=WS,
            lane=LANE,
            target_workspace="w",
            target_tab="",
            split="",
            focus=False,
            binary="/usr/bin/true",
            attest_launcher="/usr/bin/mozyo-bridge",
            store_home="/tmp/home",
            resolved=resolved,
            launch_argv_extra=(),
            lane_epoch="7",
        )
        self.assertIn(f"{MOZYO_LANE_EPOCH_ENV}=7", argv)
        self.assertIn("--lane-epoch", argv)

    def test_an_epochless_lane_keeps_the_launch_byte_invariant(self) -> None:
        # Staged rollout (acceptance 4): a lane with no minted epoch must launch exactly as
        # it did before #14756, or every pre-existing lane becomes unstartable.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (  # noqa: E501
            build_agent_start_argv,
        )
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (  # noqa: E501
            ResolvedProviderLaunch,
        )

        resolved = ResolvedProviderLaunch(
            provider_id="claude", executable="/usr/bin/true", argv0="/usr/bin/true"
        )
        kwargs = dict(
            assigned_name="n",
            provider="claude",
            repo_root=Path("/tmp"),
            workspace_id=WS,
            lane=LANE,
            target_workspace="w",
            target_tab="",
            split="",
            focus=False,
            binary="/usr/bin/true",
            attest_launcher="/usr/bin/mozyo-bridge",
            store_home="/tmp/home",
            resolved=resolved,
            launch_argv_extra=(),
        )
        self.assertEqual(
            build_agent_start_argv(**kwargs),
            build_agent_start_argv(**kwargs, lane_epoch=""),
        )
        self.assertNotIn(MOZYO_LANE_EPOCH_ENV, " ".join(build_agent_start_argv(**kwargs)))

    def test_a_disagreeing_epoch_is_recorded_as_neither_side(self) -> None:
        # Two properties at once, and the second is Redmine #14756 review j#96949 F1.
        #
        # (a) The record must NOT carry the launcher's expectation — that would attest the
        #     launcher rather than the process, reintroducing caller-supplied authority.
        # (b) It must not carry the unexplained observation either. The earlier contract
        #     stored the observed token raw and left the disagreement in the event stream
        #     only; the resume gate reads the identity RECORD, so the disagreement was
        #     invisible exactly where it had to be visible. Recording nothing is the honest
        #     answer: this launch established no epoch, and resume fails closed naming that.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            herdr_agent_attest as attest,
        )

        with tempfile.TemporaryDirectory() as tmp:
            seen: list[tuple[str, str]] = []
            record = attest.perform_self_attestation(
                assigned_name="n",
                workspace_id=WS,
                role="claude",
                lane=LANE,
                # A resolvable herdr binary so the bounded self-lookup reaches the
                # injected runner rather than short-circuiting on `binary_unresolved`.
                env={MOZYO_LANE_EPOCH_ENV: "4", "MOZYO_HERDR_BINARY": "/usr/bin/true"},
                lane_epoch="9",  # the launcher expected 9; only 4 landed
                home=Path(tmp),
                append_event=lambda stage, bounded_reason="": seen.append(
                    (stage, bounded_reason)
                ),
                runner=lambda *a, **k: type(
                    "P", (), {"returncode": 0, "stdout": '{"agents":[{"name":"n","pane_id":"%1"}]}'}
                )(),
            )
            self.assertIsNotNone(record)
            self.assertEqual(record.lane_epoch, "")  # neither 9 (expected) nor 4 (observed)
            self.assertIn(
                attest.ATTESTATION_REASON_LANE_EPOCH_NOT_INJECTED,
                [reason for _stage, reason in seen],
            )

    def test_the_raw_token_round_trips_through_the_store_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = HerdrIdentityAttestationStore(home=Path(tmp))
            for token in ("7", " 7", "007", "not-an-epoch"):
                with self.subTest(token=token):
                    store.upsert(
                        IdentityAttestationRecord(
                            assigned_name=f"n{len(token)}{token}",
                            workspace_id=WS,
                            role="claude",
                            lane_id=LANE,
                            locator="%1",
                            verdict=VERDICT_PRESENT,
                            lane_epoch=token,
                        )
                    )
                    back = store.read(f"n{len(token)}{token}")
                    # Byte-identical: the classifier is the only thing entitled to judge it.
                    self.assertEqual(back.lane_epoch, token)


class OldAttestationSchemasAreBlockedNotGuessed(unittest.TestCase):
    """R1 scope 5 / acceptance 4: compatibility is a typed block plus an explicit rail."""

    @staticmethod
    def _legacy_store(home: Path, version: int) -> Path:
        path = home / "herdr-identity-attestation.sqlite"
        columns = [
            "assigned_name TEXT PRIMARY KEY",
            "workspace_id TEXT NOT NULL",
            "role TEXT NOT NULL",
            "lane_id TEXT NOT NULL",
            "locator TEXT NOT NULL",
            "verdict TEXT NOT NULL",
            "detail TEXT NOT NULL DEFAULT ''",
            "observed_at TEXT NOT NULL",
        ]
        if version >= 2:
            columns.append("replacement_action_id TEXT NOT NULL DEFAULT ''")
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                f"CREATE TABLE herdr_identity_attestations ({', '.join(columns)})"
            )
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
        finally:
            conn.close()
        return path

    def test_a_legacy_row_reads_as_epoch_absent_never_as_a_padded_number(self) -> None:
        # The projection pads absent columns with their migration default. Padding an EPOCH
        # with any number would be a threshold claim the row never made.
        for version in (1, 2):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                path = self._legacy_store(home, version)
                conn = sqlite3.connect(path)
                try:
                    conn.execute(
                        "INSERT INTO herdr_identity_attestations "
                        "(assigned_name, workspace_id, role, lane_id, locator, verdict, "
                        "detail, observed_at) VALUES (?,?,?,?,?,?,?,?)",
                        ("n", WS, "claude", LANE, "%1", VERDICT_PRESENT, "", "2026-01-01"),
                    )
                    conn.commit()
                finally:
                    conn.close()
                back = HerdrIdentityAttestationStore(home=home).read("n")
                self.assertEqual(back.lane_epoch, "")
                self.assertEqual(parse_attested_epoch(back.lane_epoch)[1],
                                 EPOCH_ATTESTATION_ABSENT)

    def test_an_epoch_bearing_write_onto_an_old_store_is_refused_visibly(self) -> None:
        # Landing it shaped-to-fit would drop the epoch and leave a live, correctly launched
        # pair that resume can never admit — #13882's "live but unattested" on a new axis.
        for version in (1, 2):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                self._legacy_store(home, version)
                store = HerdrIdentityAttestationStore(home=home)
                with self.assertRaises(HerdrIdentityAttestationError) as caught:
                    store.upsert(
                        IdentityAttestationRecord(
                            assigned_name="n",
                            workspace_id=WS,
                            role="claude",
                            lane_id=LANE,
                            locator="%1",
                            verdict=VERDICT_PRESENT,
                            lane_epoch="7",
                        )
                    )
                # The refusal must name the operator's rail, not merely complain.
                self.assertIn("attestation-store migrate", str(caught.exception))

    def test_an_epochless_launch_still_writes_the_old_shape(self) -> None:
        # The mixed-runtime contract (#13882) must keep working: only the epoch-bearing
        # write is refused, never every write.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._legacy_store(home, 2)
            store = HerdrIdentityAttestationStore(home=home)
            store.upsert(
                IdentityAttestationRecord(
                    assigned_name="n",
                    workspace_id=WS,
                    role="claude",
                    lane_id=LANE,
                    locator="%1",
                    verdict=VERDICT_PRESENT,
                )
            )
            self.assertEqual(store.read("n").locator, "%1")

    def test_the_predicate_is_about_the_shape_not_the_version_number(self) -> None:
        self.assertTrue(write_drops_lane_epoch(1, "7"))
        self.assertTrue(write_drops_lane_epoch(2, "7"))
        self.assertFalse(write_drops_lane_epoch(3, "7"))
        self.assertFalse(write_drops_lane_epoch(1, ""))  # nothing to drop

    def test_migration_unblocks_the_rail_without_backfilling_an_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = self._legacy_store(home, 2)
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "INSERT INTO herdr_identity_attestations "
                    "(assigned_name, workspace_id, role, lane_id, locator, verdict, "
                    "detail, observed_at, replacement_action_id) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("old", WS, "claude", LANE, "%1", VERDICT_PRESENT, "", "2026-01-01", ""),
                )
                conn.commit()
            finally:
                conn.close()
            outcome = migrate_attestation_store(path)
            self.assertTrue(outcome.migrated)
            self.assertEqual(outcome.to_version, HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION)
            store = HerdrIdentityAttestationStore(home=home)
            # The pre-existing row is NOT given an epoch by the migration: it was written by
            # a runtime that never had one, and inventing one would fabricate a proof.
            self.assertEqual(store.read("old").lane_epoch, "")
            # But a fresh epoch-bearing write now lands.
            store.upsert(
                IdentityAttestationRecord(
                    assigned_name="new",
                    workspace_id=WS,
                    role="claude",
                    lane_id=LANE,
                    locator="%2",
                    verdict=VERDICT_PRESENT,
                    lane_epoch="7",
                )
            )
            self.assertEqual(store.read("new").lane_epoch, "7")


class OldLifecycleRowsAreBlockedNotGuessed(unittest.TestCase):
    """A pre-v10 hibernation cannot be reconstructed, so it is refused, not approximated."""

    def test_a_migrated_pre_v10_row_lands_unminted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, key = _hibernated_lane(tmp)
            path = store.path
            # Rewind to the v9 signature the way a real older build would have left it.
            conn = sqlite3.connect(path)
            try:
                conn.execute("ALTER TABLE lane_lifecycle_records DROP COLUMN lane_epoch")
                conn.execute(
                    "UPDATE state_schema_components SET schema_version = 9 "
                    "WHERE component = 'lane_lifecycle'"
                )
                conn.commit()
            finally:
                conn.close()
            # Re-opening migrates v9 -> v10 additively; the epoch must arrive UNMINTED.
            rec = LaneLifecycleStore(home=Path(tmp)).get(key)
            self.assertEqual(rec.lane_epoch, "0")
            self.assertEqual(
                lane_epoch_verdict(rec, "1"), (False, EPOCH_AUTHORITY_UNAVAILABLE)
            )

    def test_the_migration_does_not_seed_the_epoch_from_lane_generation(self) -> None:
        # `lane_generation` advances on re-incarnation, not hibernation. Seeding from it
        # would mint an epoch matching processes launched before this build existed.
        with tempfile.TemporaryDirectory() as tmp:
            store, key = _hibernated_lane(tmp)
            conn = sqlite3.connect(store.path)
            try:
                conn.execute("ALTER TABLE lane_lifecycle_records DROP COLUMN lane_epoch")
                conn.execute("UPDATE lane_lifecycle_records SET lane_generation = 5")
                conn.execute(
                    "UPDATE state_schema_components SET schema_version = 9 "
                    "WHERE component = 'lane_lifecycle'"
                )
                conn.commit()
            finally:
                conn.close()
            rec = LaneLifecycleStore(home=Path(tmp)).get(key)
            self.assertEqual(rec.lane_generation, 5)
            self.assertEqual(rec.lane_epoch, "0")


class TheEpochRefusalIsOnAnObservableBoundary(unittest.TestCase):
    """A fail-closed check inside a best-effort writer is not fail-closed at all.

    Found by self-review rather than by a failing test, which is why it is pinned here. The
    attestation write is best-effort by contract (#13637: a store failure must never block an
    agent boot), so ``record_identity_attestation`` swallows the epoch refusal. On its own,
    that refusal is therefore invisible: the pair boots live, correctly launched, with no
    epoch in its attestation, and ``sublane resume`` can never admit it — #13882's
    "live but unattested" regenerated on the epoch axis. The refusal has to be decided where
    an operator can see it, which is the pre-launch store preflight.
    """

    @staticmethod
    def _observation():
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
            LauncherCapabilityObservation,
        )

        return LauncherCapabilityObservation(
            subcommand_marker_present=True,
            advertised_schema_version=HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
            advertised_store_versions=frozenset({1, 2, 3}),
        )

    def _verdict(self, store_version: int, *, epoch_launch: bool):
        from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
            STORE_RECOGNIZED,
            StoreSchemaObservation,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
            decide_store_compatibility,
        )

        return decide_store_compatibility(
            self._observation(),
            StoreSchemaObservation(STORE_RECOGNIZED, store_version),
            required_schema_version=HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
            replacement_launch=False,
            epoch_launch=epoch_launch,
        )

    def test_an_epoch_launch_onto_an_old_store_is_refused_before_any_side_effect(
        self,
    ) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_epoch import (  # noqa: E501
            STORE_EPOCH_UNSUPPORTED,
        )

        for version in (1, 2):
            with self.subTest(version=version):
                verdict = self._verdict(version, epoch_launch=True)
                self.assertFalse(verdict.ok)
                self.assertEqual(verdict.reason, STORE_EPOCH_UNSUPPORTED)
                self.assertIn("migrate", verdict.detail)

    def test_an_epochless_launch_onto_the_same_store_is_still_admitted(self) -> None:
        # The #13882 mixed-runtime path must not be collateral damage: only the launch that
        # would LOSE something is refused.
        for version in (1, 2, 3):
            with self.subTest(version=version):
                self.assertTrue(self._verdict(version, epoch_launch=False).ok)

    def test_an_epoch_launch_onto_a_v3_store_is_admitted(self) -> None:
        self.assertTrue(self._verdict(3, epoch_launch=True).ok)

    def test_the_launch_boundary_actually_passes_the_flag(self) -> None:
        # The conjunct has to be SUPPLIED, not merely available (#14477 R9: fixing the
        # domain proves nothing until the boundary wires it).
        base = ROOT.parent / "src/mozyo_bridge/e_140_adapter_provider/f_130_terminal_runtime_provider/application"
        # The session composition root asks for the preflight; the preflight derives the flag
        # from the SAME predicate the per-slot launch uses. Both halves are pinned, because a
        # flag that is supplied but never derived (or vice versa) is not a fence.
        self.assertIn("preflight_managed_launch(", (base / "herdr_session_start.py").read_text())
        preflight = (base / "herdr_launch_preflight.py").read_text()
        self.assertIn("epoch_launch=", preflight)
        self.assertIn("launch_carries_lane_epoch", preflight)


class TheLegacyNextRailIsActuallyExecutable(unittest.TestCase):
    """Coordinator review j#96836: the named remedy has to be one the lane can perform.

    The epoch fence refuses an unminted row and points the operator at "resume after a real
    v10 hibernate transition". For the shape this issue exists to unblock — #14755, ALREADY
    ``hibernated`` under a pre-v10 build — that instruction is unperformable, and these pins
    measure the deadlock rather than asserting it:

    ``lane_epoch`` is minted only by the CAS INTO ``hibernated``; the legal edges out of
    ``hibernated`` are ``active`` and ``retired``; ``-> active`` IS the refused resume and
    ``-> retired`` discards the lane. A refusal whose remedy cannot be executed is not
    fail-closed, it is stuck.
    """

    @staticmethod
    def _legacy_already_hibernated(tmp: str):
        """The exact #14755 shape: hibernated + released, epoch never minted.

        Driven through the REAL transitions and then rewound on the single column a pre-v10
        build would not have had, rather than hand-assembling a row (#14477 R5-F1).
        """
        from mozyo_bridge.core.state.lane_lifecycle import RELEASE_RELEASED
        from mozyo_bridge.core.state.lane_release_observation import (
            build_release_observation,
        )

        store, key = _hibernated_lane(tmp)
        rec = store.get(key)
        store.request_release(
            key, expected_revision=rec.revision, action_id="legacy-rel",
            observation=build_release_observation(()),
        )
        rec = store.get(key)
        store.record_release_outcome(
            key, action_id="legacy-rel", expected_revision=rec.revision,
            target=RELEASE_RELEASED,
        )
        conn = sqlite3.connect(store.path)
        try:
            conn.execute("UPDATE lane_lifecycle_records SET lane_epoch = '0'")
            conn.commit()
        finally:
            conn.close()
        return store, key

    def test_the_only_minting_transition_is_forbidden_from_this_state(self) -> None:
        from mozyo_bridge.core.state.lane_lifecycle_model import (
            CAS_FORBIDDEN_TRANSITION,
            disposition_transition_allowed,
        )

        self.assertFalse(
            disposition_transition_allowed(DISPOSITION_HIBERNATED, DISPOSITION_HIBERNATED)
        )
        with tempfile.TemporaryDirectory() as tmp:
            store, key = self._legacy_already_hibernated(tmp)
            rec = store.get(key)
            self.assertEqual(
                lane_epoch_verdict(rec, "1"), (False, EPOCH_AUTHORITY_UNAVAILABLE)
            )
            outcome = store.transition_disposition(
                key,
                expected_disposition=DISPOSITION_HIBERNATED,
                expected_revision=rec.revision,
                target=DISPOSITION_HIBERNATED,
                decision=_decision(),
            )
            self.assertFalse(outcome.applied)
            self.assertEqual(outcome.reason, CAS_FORBIDDEN_TRANSITION)
            self.assertEqual(store.get(key).lane_epoch, "0")

    def test_adoption_mints_and_preserves_every_other_column(self) -> None:
        # WIP / identity / replay evidence must survive the rail intact (j#96836 action 3).
        from mozyo_bridge.core.state.lane_epoch_adoption import (
            ADOPTED_LEGACY_EPOCH,
            LaneEpochAdoptionStore,
        )

        preserved = (
            "lane_disposition", "process_release", "release_action_id", "release_pins",
            "release_observation", "replacement_state", "replacement_action_id",
            "replacement_pins", "worktree_identity", "declared_slots", "lane_kind",
            "reconcile_phase", "hibernated_at", "lane_generation", "issue_id",
            "binding_kind", "project_scope", "created_at",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store, key = self._legacy_already_hibernated(tmp)
            rec = store.get(key)
            before = {c: getattr(rec, c) for c in preserved}
            outcome = LaneEpochAdoptionStore(home=Path(tmp)).adopt_legacy_epoch(
                key, expected_revision=rec.revision, issue_id=ISSUE, decision=_decision()
            )
            self.assertTrue(outcome.applied, outcome.reason)
            after = store.get(key)
            self.assertEqual(after.lane_epoch, str(ADOPTED_LEGACY_EPOCH))
            self.assertEqual({c: getattr(after, c) for c in preserved}, before)

    def test_adoption_does_not_admit_a_survivor(self) -> None:
        # The property that makes this an unblock rather than a carve-out: adoption supplies
        # the AUTHORITY half only. A pre-#14756 survivor was launched by a build with no
        # epoch concept, so it attests nothing and is still refused.
        from mozyo_bridge.core.state.lane_epoch_adoption import LaneEpochAdoptionStore

        with tempfile.TemporaryDirectory() as tmp:
            store, key = self._legacy_already_hibernated(tmp)
            rec = store.get(key)
            LaneEpochAdoptionStore(home=Path(tmp)).adopt_legacy_epoch(
                key, expected_revision=rec.revision, issue_id=ISSUE, decision=_decision()
            )
            adopted = store.get(key)
            self.assertEqual(
                lane_epoch_verdict(adopted, ""), (False, EPOCH_ATTESTATION_ABSENT)
            )
            # ...while a process launched AFTER adoption reads the minted epoch from its own
            # env and clears the half that adoption unblocked.
            self.assertEqual(lane_epoch_verdict(adopted, "1"), (True, EPOCH_OK))

    def test_adoption_initialises_but_never_advances(self) -> None:
        # A second run must not walk 1 -> 2: that would strand the very pair the operator
        # launched by following this rail once.
        from mozyo_bridge.core.state.lane_epoch_adoption import LaneEpochAdoptionStore

        with tempfile.TemporaryDirectory() as tmp:
            store, key = self._legacy_already_hibernated(tmp)
            adopt = LaneEpochAdoptionStore(home=Path(tmp))
            rec = store.get(key)
            self.assertTrue(
                adopt.adopt_legacy_epoch(
                    key, expected_revision=rec.revision, issue_id=ISSUE,
                    decision=_decision(),
                ).applied
            )
            rec = store.get(key)
            again = adopt.adopt_legacy_epoch(
                key, expected_revision=rec.revision, issue_id=ISSUE, decision=_decision()
            )
            self.assertFalse(again.applied)
            self.assertEqual(store.get(key).lane_epoch, "1")

    def test_adoption_refuses_every_state_that_is_not_the_legacy_shape(self) -> None:
        from mozyo_bridge.core.state.lane_epoch_adoption import LaneEpochAdoptionStore
        from mozyo_bridge.core.state.lane_lifecycle_model import CAS_FORBIDDEN_TRANSITION

        with tempfile.TemporaryDirectory() as tmp:
            # An ACTIVE lane mints through the normal transition and must not shortcut it.
            store = LaneLifecycleStore(home=Path(tmp))
            key = LaneLifecycleKey(WS, LANE)
            store.declare_active(key, decision=_decision(), issue_id=ISSUE)
            rec = store.get(key)
            self.assertFalse(
                LaneEpochAdoptionStore(home=Path(tmp)).adopt_legacy_epoch(
                    key, expected_revision=rec.revision, issue_id=ISSUE,
                    decision=_decision(),
                ).applied
            )
        with tempfile.TemporaryDirectory() as tmp:
            # Hibernated but the release never completed: an actuator may still be closing
            # panes, so adoption must not race it.
            store, key = _hibernated_lane(tmp)
            conn = sqlite3.connect(store.path)
            try:
                conn.execute("UPDATE lane_lifecycle_records SET lane_epoch = '0'")
                conn.commit()
            finally:
                conn.close()
            rec = store.get(key)
            outcome = LaneEpochAdoptionStore(home=Path(tmp)).adopt_legacy_epoch(
                key, expected_revision=rec.revision, issue_id=ISSUE, decision=_decision()
            )
            self.assertFalse(outcome.applied)
            self.assertEqual(outcome.reason, CAS_FORBIDDEN_TRANSITION)
        with tempfile.TemporaryDirectory() as tmp:
            # A different issue is another lane's row.
            store, key = self._legacy_already_hibernated(tmp)
            rec = store.get(key)
            self.assertFalse(
                LaneEpochAdoptionStore(home=Path(tmp)).adopt_legacy_epoch(
                    key, expected_revision=rec.revision, issue_id="99999",
                    decision=_decision(),
                ).applied
            )
        with tempfile.TemporaryDirectory() as tmp:
            # A stale revision loses to the concurrent writer rather than clobbering it.
            store, key = self._legacy_already_hibernated(tmp)
            self.assertFalse(
                LaneEpochAdoptionStore(home=Path(tmp)).adopt_legacy_epoch(
                    key, expected_revision=999, issue_id=ISSUE, decision=_decision()
                ).applied
            )


class ConditionalCSplitsOnTheAuthorityFactNotCallerIntent(unittest.TestCase):
    """Design Answer j#96844: what may launch onto a v1 store is decided by ``lane_epoch``.

    The rejected proposal (mine) exempted the v1 heal by *intent* — "the operator is
    deliberately on the compatibility rail". The ruling splits on the stored authority fact
    instead, which is not the same thing and is strictly better: intent is a caller's claim,
    ``lane_epoch`` is the row's own. Only a launch that would actually LOSE an epoch is
    stopped, and a true-legacy lane keeps the recovery rail it has always had.

    - ``lane_epoch == 0`` — nothing to lose, so the v1 exact side-binding heal stays available
      (legacy recovery / pins repair / terminal convergence). It is NOT re-read as an epoch
      proof: ``sublane resume`` still refuses, so a successful v1 heal never implies resume.
    - ``lane_epoch > 0`` — the launch would drop a real epoch, so it is refused before any
      effect, and the named rail is the explicit backup-first store migration.
    """

    @staticmethod
    def _store_verdict(store_version: int, *, lane_epoch: int):
        """The pre-launch store admission for a lane at ``lane_epoch`` on a v-N store."""
        from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
            STORE_RECOGNIZED,
            StoreSchemaObservation,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
            LauncherCapabilityObservation,
            decide_store_compatibility,
        )

        return decide_store_compatibility(
            LauncherCapabilityObservation(
                subcommand_marker_present=True,
                advertised_schema_version=HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
                advertised_store_versions=frozenset({1, 2, 3}),
            ),
            StoreSchemaObservation(STORE_RECOGNIZED, store_version),
            required_schema_version=HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
            replacement_launch=False,
            # The flag the launch boundary derives from the row; `0` yields an empty token.
            epoch_launch=lane_epoch > 0,
        )

    def test_true_legacy_epoch_zero_keeps_the_v1_heal_rail(self) -> None:
        for version in (1, 2):
            with self.subTest(version=version):
                self.assertTrue(self._store_verdict(version, lane_epoch=0).ok)

    def test_but_a_successful_v1_heal_still_does_not_make_resume_possible(self) -> None:
        # Rule 1's second half, and the part most easily lost: keeping the heal available must
        # not be read as supplying the generation proof. The two are separate gates.
        with tempfile.TemporaryDirectory() as tmp:
            store = LaneLifecycleStore(home=Path(tmp))
            key = LaneLifecycleKey(WS, LANE)
            store.declare_active(key, decision=_decision(), issue_id=ISSUE)
            rec = store.get(key)
            self.assertEqual(rec.lane_epoch, "0")
            self.assertTrue(self._store_verdict(1, lane_epoch=0).ok)  # heal: allowed
            self.assertEqual(  # resume: still refused
                lane_epoch_verdict(rec, "1"), (False, EPOCH_AUTHORITY_UNAVAILABLE)
            )

    def test_an_issued_epoch_on_a_v1_store_is_refused_before_any_effect(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_epoch import (  # noqa: E501
            STORE_EPOCH_UNSUPPORTED,
        )

        for version in (1, 2):
            with self.subTest(version=version):
                verdict = self._store_verdict(version, lane_epoch=1)
                self.assertFalse(verdict.ok)
                self.assertEqual(verdict.reason, STORE_EPOCH_UNSUPPORTED)
                self.assertIn("migrate", verdict.detail)  # the rail is named

    def test_after_the_explicit_migration_the_modern_launch_is_admitted(self) -> None:
        # The named rail has to actually lead somewhere (the j#96836 lesson applied to THIS
        # refusal): migrate, then the same lane's epoch-bearing launch is admitted and a pair
        # attesting the minted epoch resumes.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = OldAttestationSchemasAreBlockedNotGuessed._legacy_store(home, 1)
            self.assertTrue(migrate_attestation_store(path).migrated)
            self.assertTrue(self._store_verdict(3, lane_epoch=1).ok)
            store, key = _hibernated_lane(tmp)
            rec = store.get(key)
            self.assertEqual(lane_epoch_verdict(rec, str(rec.lane_epoch)), (True, EPOCH_OK))

    def test_the_migration_itself_fails_closed_on_an_unknown_shape(self) -> None:
        # Rule 2's tail: migration ambiguity must not be resolved by guessing, or the rail
        # would silently swap a store nobody classified.
        from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
            HerdrIdentityAttestationSchemaError,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "herdr-identity-attestation.sqlite"
            conn = sqlite3.connect(path)
            try:
                conn.execute("CREATE TABLE herdr_identity_attestations (assigned_name TEXT)")
                conn.execute("PRAGMA user_version = 99")  # newer than anything recognised
                conn.commit()
            finally:
                conn.close()
            with self.assertRaises(HerdrIdentityAttestationSchemaError):
                migrate_attestation_store(path)

    def test_adoption_then_migrate_then_relaunch_is_the_whole_legacy_path(self) -> None:
        # End to end for #14755's shape: adoption supplies the authority half, the store
        # migration makes the attestation half storable, and only a RELAUNCH satisfies it.
        # The survivor never does, at any point along the path.
        from mozyo_bridge.core.state.lane_epoch_adoption import LaneEpochAdoptionStore

        with tempfile.TemporaryDirectory() as tmp:
            store, key = (
                TheLegacyNextRailIsActuallyExecutable._legacy_already_hibernated(tmp)
            )
            rec = store.get(key)
            self.assertEqual(
                lane_epoch_verdict(rec, "1"), (False, EPOCH_AUTHORITY_UNAVAILABLE)
            )
            LaneEpochAdoptionStore(home=Path(tmp)).adopt_legacy_epoch(
                key, expected_revision=rec.revision, issue_id=ISSUE, decision=_decision()
            )
            adopted = store.get(key)
            # The epoch is now issued, so the v1 store can no longer take this lane's launch.
            self.assertFalse(self._store_verdict(1, lane_epoch=1).ok)
            self.assertTrue(self._store_verdict(3, lane_epoch=1).ok)
            # Survivor: still refused. Relaunch: admitted.
            self.assertEqual(
                lane_epoch_verdict(adopted, ""), (False, EPOCH_ATTESTATION_ABSENT)
            )
            self.assertEqual(lane_epoch_verdict(adopted, "1"), (True, EPOCH_OK))


class TheReplacementRefusalHappensBeforeAnyIrreversibleEffect(unittest.TestCase):
    """Redmine #14756 j#96848: a typed refusal behind a close is not a pre-effect refusal.

    Measured before the fix: the epoch/store compatibility decision lived inside the LAUNCH
    step, so ``_step_close_owed`` had already closed the old slot and CAS'd the participant to
    ``launch_owed`` by the time the refusal was produced. herdr starts were zero, but closes
    and lifecycle mutations were not — and the contract this issue wrote down claimed all
    three. The seam now sits ahead of the owed-step dispatch, so a refusal costs nothing.
    """

    @staticmethod
    def _actuator(refusal, recorder):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E501
            ReplacementActuatorUseCase,
        )

        class _Port:
            def observe_old_slot(self, pin):
                recorder.append("observe_old_slot")
                return None

            def observe_preservation(self, pin):
                recorder.append("observe_preservation")
                return None

            def close_exact_generation(self, pin):
                recorder.append("close_exact_generation")
                return "close_done"

            def launch_action_bound(self, action_id, pin):
                recorder.append("launch_action_bound")
                return "launch_done"

        class _Store:
            def transition_participant(self, *a, **kw):
                recorder.append("transition_participant")
                raise AssertionError("a refused action must not CAS a participant")

        return ReplacementActuatorUseCase(
            _Store(), _Port(), store_admission=lambda _key, _pin: refusal
        )

    @staticmethod
    def _rec(phase, revision, pins):
        return SimpleNamespace(
            phase=phase, revision=revision, action_id="act-1", participants=pins
        )

    @staticmethod
    def _pin(phase, identity, lane="lane-a"):
        return SimpleNamespace(phase=phase, identity=identity, lane_id=lane)

    def test_a_refused_action_closes_nothing_and_cas_nothing(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E501
            PARTICIPANT_CLOSE_OWED,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_epoch import (  # noqa: E501
            STORE_EPOCH_UNSUPPORTED,
        )

        recorder: list = []
        actuator = self._actuator(STORE_EPOCH_UNSUPPORTED, recorder)
        pin = self._pin(PARTICIPANT_CLOSE_OWED, "gw")
        rec = self._rec("close_owed", 3, (pin,))

        outcome = actuator._actuate_participant(
            key=None, rec=rec, pin=pin, holder="h", gen=1, now="2026-01-01T00:00:00+00:00"
        )

        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.detail, STORE_EPOCH_UNSUPPORTED)
        # The whole point: not one observation, close, launch or CAS was reached.
        self.assertEqual(recorder, [])
        self.assertEqual(outcome.revision, 3)  # the transaction did not move

    def test_the_same_gate_is_reached_on_a_launch_owed_replay(self) -> None:
        # Crash replay must not slip past the refusal by re-entering at a later owed phase
        # (j#96848: "crash replay でも同じ preflight を通し partial transition を作らない").
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E501
            PARTICIPANT_LAUNCH_OWED,
        )

        recorder: list = []
        actuator = self._actuator("attestation_store_epoch_unsupported", recorder)
        pin = self._pin(PARTICIPANT_LAUNCH_OWED, "wk")
        rec = self._rec("launch_owed", 7, (pin,))

        outcome = actuator._actuate_participant(
            key=None, rec=rec, pin=pin, holder="h", gen=1, now="2026-01-01T00:00:00+00:00"
        )
        self.assertEqual(recorder, [])
        self.assertEqual(outcome.revision, 7)

    def test_one_inadmissible_participant_refuses_the_whole_action(self) -> None:
        """The gate is the OUTER action's, not the current participant's (j#96848).

        Without this, the first participant would be closed and relaunched before anyone
        discovered the second can never come back — the same half-destroyed pair the fence
        exists to prevent, reached one step later. Here the participant being actuated is
        itself admissible; only its sibling is not.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E501
            PARTICIPANT_CLOSE_OWED,
            ReplacementActuatorUseCase,
        )

        recorder: list = []
        actuating = self._pin(PARTICIPANT_CLOSE_OWED, "gw", lane="fine")
        sibling = self._pin(PARTICIPANT_CLOSE_OWED, "wk", lane="doomed")
        base = self._actuator(None, recorder)
        actuator = ReplacementActuatorUseCase(
            base._store,
            base._port,
            store_admission=lambda _key, pin: (
                "attestation_store_epoch_unsupported" if pin.lane_id == "doomed" else None
            ),
        )

        outcome = actuator._actuate_participant(
            key=None,
            rec=self._rec("close_owed", 11, (actuating, sibling)),
            pin=actuating,
            holder="h", gen=1, now="2026-01-01T00:00:00+00:00",
        )

        self.assertEqual(recorder, [])
        self.assertEqual(outcome.revision, 11)
        # It names the participant that is actually inadmissible, not the one being driven —
        # an operator reading `stopped_on` must be pointed at the lane that needs migrating.
        self.assertEqual(outcome.stopped_on, "wk")

    def test_an_already_replaced_participant_cannot_strand_the_transaction(self) -> None:
        """A participant with no remaining effect is not gated (the converse discipline).

        Refusing on behalf of one that is already ``replaced`` would withhold nothing and
        block a transaction that is past it — a fence with a cost and no protection.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E501
            PARTICIPANT_CLOSE_OWED,
            PARTICIPANT_REPLACED,
            ReplacementActuatorUseCase,
        )

        recorder: list = []
        base = self._actuator(None, recorder)
        actuator = ReplacementActuatorUseCase(
            base._store,
            base._port,
            store_admission=lambda _key, pin: (
                "attestation_store_epoch_unsupported" if pin.lane_id == "done" else None
            ),
        )
        actuating = self._pin(PARTICIPANT_CLOSE_OWED, "gw", lane="fine")

        actuator._actuate_participant(
            key=None,
            rec=self._rec(
                "close_owed", 4,
                (self._pin(PARTICIPANT_REPLACED, "old", lane="done"), actuating),
            ),
            pin=actuating,
            holder="h", gen=1, now="2026-01-01T00:00:00+00:00",
        )

        # It proceeded into the close step rather than being refused on the replaced pin.
        self.assertEqual(recorder[:1], ["observe_old_slot"])

    def test_no_admission_probe_leaves_the_pre_14756_flow_unchanged(self) -> None:
        # The seam is opt-in: a caller that injects nothing behaves exactly as before, so
        # adding it cannot alter the self-replacement path this issue never touched.
        import inspect

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E501
            ReplacementActuatorUseCase,
        )

        self.assertIsNone(
            inspect.signature(ReplacementActuatorUseCase.__init__)
            .parameters["store_admission"]
            .default
        )


class ThePreCloseFenceIsActuallyArmedOnEveryReplacementPath(unittest.TestCase):
    """Redmine #14756 j#96854/j#96859: a seam nobody injects is not a fence.

    The previous round built the pre-effect ``store_admission`` seam and measured that all
    six construction sites passed ``0`` of them — so the defect j#96848 named was still live
    end to end while the regression suite was green. These tests are written against that
    exact failure: the first group measures the joined decision on real stores, the second
    measures that each construction site hands it to the actuator.
    """

    @staticmethod
    def _admission(tmp: str, *, store_version: int | None, epoch: bool):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_epoch import (  # noqa: E501
            replacement_store_admission,
        )

        home = Path(tmp)
        if epoch:
            _hibernated_lane(tmp)  # a REAL hibernate CAS mints epoch 1
        else:
            LaneLifecycleStore(home=home).declare_active(
                LaneLifecycleKey(WS, LANE), decision=_decision(), issue_id=ISSUE
            )
        if store_version is not None:
            OldAttestationSchemasAreBlockedNotGuessed._legacy_store(home, store_version)
        return replacement_store_admission(
            WS, LANE, lifecycle_home=str(home), attestation_home=str(home)
        )

    def test_an_epoch_bearing_replacement_onto_a_v1_store_is_refused(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_epoch import (  # noqa: E501
            STORE_EPOCH_UNSUPPORTED,
        )

        for version in (1, 2):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(
                    self._admission(tmp, store_version=version, epoch=True),
                    STORE_EPOCH_UNSUPPORTED,
                )

    def test_a_true_legacy_lane_is_not_refused_by_the_pre_close_fence(self) -> None:
        # Conditional-C rule 1 (j#96844) at the fence itself, not only at the launch join:
        # a lane with no minted epoch keeps its v1 heal rail. Refusing it here would delete a
        # working recovery path without making a single epoch storable.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(self._admission(tmp, store_version=1, epoch=False))

    def test_a_store_that_can_hold_the_epoch_is_admitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = OldAttestationSchemasAreBlockedNotGuessed._legacy_store(home, 1)
            self.assertTrue(migrate_attestation_store(path).migrated)
            _hibernated_lane(tmp)
            from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_epoch import (  # noqa: E501
                replacement_store_admission,
            )

            self.assertIsNone(
                replacement_store_admission(
                    WS, LANE, lifecycle_home=str(home), attestation_home=str(home)
                )
            )

    def test_an_absent_store_is_admitted_because_the_first_write_creates_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(self._admission(tmp, store_version=None, epoch=True))

    def test_a_store_whose_shape_is_not_knowable_refuses_rather_than_reading_as_empty(
        self,
    ) -> None:
        # #13682 R1-F1 on this axis: an absence of measurement is not a measurement of
        # absence. The dangerous direction is admitting, because admitting means closing.
        #
        # The refusal token is asserted to be the PROBE'S OWN state rather than a token this
        # fence picks, so the two surfaces cannot come to describe one store two ways. (The
        # first version of this test asserted `store_unreadable` for the corrupt file below
        # and measured `store_unsupported` — the probe distinguishes "cannot open it" from
        # "opened it, and its recorded version and shape disagree". Pinning the literal would
        # have pinned my guess.)
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            herdr_identity_attestation_path,
        )
        from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
            STORE_ABSENT,
            STORE_RECOGNIZED,
            probe_store_schema,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_epoch import (  # noqa: E501
            replacement_store_admission,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _hibernated_lane(tmp)
            herdr_identity_attestation_path(home).write_bytes(b"not a database")
            state = probe_store_schema(herdr_identity_attestation_path(home)).state
            self.assertNotIn(state, (STORE_ABSENT, STORE_RECOGNIZED))  # unknowable
            self.assertEqual(
                replacement_store_admission(
                    WS, LANE, lifecycle_home=str(home), attestation_home=str(home)
                ),
                state,
            )

    def test_an_unknowable_store_is_only_refused_when_an_epoch_is_at_stake(self) -> None:
        # The scope line. A lane with no minted epoch loses nothing on any store shape, so
        # refusing it here would change behaviour on a path this issue never touched — and
        # would break the v1 mixed-runtime heal that conditional-C rule 1 preserves.
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            herdr_identity_attestation_path,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_epoch import (  # noqa: E501
            replacement_store_admission,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            LaneLifecycleStore(home=home).declare_active(
                LaneLifecycleKey(WS, LANE), decision=_decision(), issue_id=ISSUE
            )
            herdr_identity_attestation_path(home).write_bytes(b"not a database")
            self.assertIsNone(
                replacement_store_admission(
                    WS, LANE, lifecycle_home=str(home), attestation_home=str(home)
                )
            )

    def test_the_fence_reads_the_homes_it_is_given_not_the_ambient_ones(self) -> None:
        # The quietest way for this fence to be wrong: point one of its two reads at the
        # real shared home while the lane under test lives in an isolated one. It would then
        # answer confidently about a store that has nothing to do with the action.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_epoch import (  # noqa: E501
            STORE_EPOCH_UNSUPPORTED,
            replacement_store_admission,
        )

        with tempfile.TemporaryDirectory() as life, tempfile.TemporaryDirectory() as att:
            _hibernated_lane(life)  # the epoch lives HERE
            OldAttestationSchemasAreBlockedNotGuessed._legacy_store(Path(att), 1)  # v1 HERE
            self.assertEqual(
                replacement_store_admission(
                    WS, LANE, lifecycle_home=str(life), attestation_home=str(att)
                ),
                STORE_EPOCH_UNSUPPORTED,
            )
            # Swap only the lifecycle home: no epoch is minted there, so nothing is refused.
            # If the function ignored its arguments both calls would agree.
            self.assertIsNone(
                replacement_store_admission(
                    WS, LANE, lifecycle_home=str(att), attestation_home=str(att)
                )
            )

    # -- the census the previous round failed ---------------------------------

    #: Every place the replacement actuator is constructed in ``src``, with the reason each
    #: one either arms the fence or provably cannot. Held as data so a NEW construction site
    #: fails this test instead of quietly joining the un-gated majority.
    _SITES = {
        "sublane_hibernated_bound_pair_convergence_live.py": "store_admission=self.store_admission",
        "sublane_hibernated_bound_pair_composer_discard_live.py": "store_admission=self.store_admission",
        "sublane_stale_worker_recovery.py": "store_admission=self._ops.replacement_store_admission",
        "sublane_worker_refresh.py": "store_admission=self._ops.replacement_store_admission",
        "sublane_gateway_recovery.py": "store_admission=self._ops.replacement_store_admission",
        "self_close_executor.py": "store_admission=self._store_admission",
    }

    @staticmethod
    def _application_dir() -> Path:
        return (
            ROOT.parent
            / "src/mozyo_bridge/e_110_execution_platform"
            / "f_140_delegated_coordinator_nested_handoff/application"
        )

    def test_every_construction_site_in_src_is_accounted_for(self) -> None:
        found = {
            path.name
            for path in self._application_dir().glob("*.py")
            if "ReplacementActuatorUseCase(" in path.read_text()
            and "class ReplacementActuatorUseCase" not in path.read_text()
        }
        self.assertEqual(found, set(self._SITES))

    def test_every_construction_site_passes_the_admission_probe(self) -> None:
        for name, expected in self._SITES.items():
            with self.subTest(site=name):
                self.assertIn(expected, (self._application_dir() / name).read_text())

    def test_each_ops_protocol_requires_the_probe_so_omitting_it_is_an_error(self) -> None:
        # The three orchestrators reach the fence through their ops protocol. Declaring the
        # member there is what makes a missing implementation fail loudly instead of leaving
        # that path un-gated and green — the exact shape of the defect being fixed.
        for name in (
            "sublane_stale_worker_recovery.py",
            "sublane_worker_refresh.py",
            "sublane_gateway_recovery.py",
        ):
            with self.subTest(protocol=name):
                self.assertIn(
                    "def replacement_store_admission(self, key, pin)",
                    (self._application_dir() / name).read_text(),
                )

    def test_the_live_ops_resolve_the_probe_from_the_transaction_not_from_ambient(
        self,
    ) -> None:
        # Each live implementation must answer for the lane the ACTION pins, or the fence
        # measures one lane and the actuator closes another.
        for name in (
            "sublane_stale_worker_recovery_live.py",
            "sublane_worker_refresh_live.py",
            "sublane_gateway_recovery_live.py",
        ):
            with self.subTest(live=name):
                source = (self._application_dir() / name).read_text()
                self.assertIn("key.workspace_id", source)
                self.assertIn("pin.lane_id", source)

    def test_the_self_close_path_is_unarmed_by_a_written_decision_not_by_omission(
        self,
    ) -> None:
        # j#96859's discipline: a path left un-gated must SAY it is, and say why, so the next
        # reader cannot mistake a silence for coverage. The self-close executor forwards the
        # seam but has no composition root in src to resolve a lane from.
        source = (self._application_dir() / "self_close_executor.py").read_text()
        self.assertIn("no composition root in ``src``", source)
        self.assertIn("store_admission=self._store_admission", source)

    def test_the_self_close_executor_has_no_production_composition_root(self) -> None:
        # The premise the comment above rests on, measured rather than asserted — if a real
        # composition root lands, this fails and the comment must be re-decided with it.
        src = ROOT.parent / "src"
        builders = [
            path.name
            for path in src.rglob("*.py")
            if "SelfCloseExecutorUseCase(" in path.read_text()
        ]
        self.assertEqual(builders, [])


class TheLegacyRecoveryPlanRefusesBeforeItCloses(unittest.TestCase):
    """Redmine #14756 j#96861 + j#96866 + j#96881: ordering, blast radius, and authority.

    j#96861 corrected the sequence to close-first, because adopting the epoch before closing
    the old pair leaves a crash window in which the lane holds ``epoch=1`` on a v1 store and
    this issue's own pre-effect fence then refuses the close that would clear it.

    j#96866 then measured the real environment and found the sequence unreachable from inside
    the fleet at all: the attested-live intersection is 5 workspaces / 18 agents including the
    coordinator that would run the command. Hence: census first, plan only, no execute mode.

    j#96881 F1 then found the blocker was erasable by the party it blocked — the exclusion set
    came from a caller-supplied ``--target-slot``, so naming every consumer produced
    ``plan_ready``. Measured before fixing: four consumers, four names supplied, plan went
    green. The pair is now DERIVED from the lane's stored release observation and joined
    byte-exact against live inventory and startup attestation; the flag can only assert.
    """

    # -- fixtures --------------------------------------------------------------

    @staticmethod
    def _agent(name, *, role="claude", locator="%1", workspace=WS, lane=LANE):
        return SimpleNamespace(
            name=name, role=role, locator=locator, workspace_id=workspace,
            lane_id=lane, managed=True,
        )

    @classmethod
    def _view(cls, *, agents=(), ok=True, backend=True):
        return SimpleNamespace(
            backend_selected=backend, ok=ok, reason="", detail="",
            managed_agents=tuple(agents),
        )

    @staticmethod
    def _attest(home, name, *, role="claude", locator="%1", workspace=WS, lane=LANE):
        HerdrIdentityAttestationStore(home=home).upsert(
            IdentityAttestationRecord(
                assigned_name=name, workspace_id=workspace, role=role, lane_id=lane,
                locator=locator, verdict=VERDICT_PRESENT, detail="",
                observed_at="2026-01-01T00:00:00+00:00",
            )
        )

    @staticmethod
    def _legacy_lane_with_pins(tmp, pins):
        """The ACTUAL #14755 shape: declared pair present, release observation ABSENT.

        j#96895 read the real row: ``release_observation`` length 0, ``declared_slots``
        length 532. A fixture that carried an observation would have made the derivation look
        correct against a shape the named acceptance target does not have — which is exactly
        how the first version of this rail came to refuse the one lane it exists for.

        The declared pair is written through the real ``rehydrated_declared_slots`` writer, so
        the derivation reads a snapshot the production writer actually emits.
        """
        from mozyo_bridge.core.state.lane_lifecycle import RELEASE_RELEASED
        from mozyo_bridge.core.state.lane_release_observation import (
            build_release_observation,
        )

        store, key = _hibernated_lane(tmp)
        # hibernated -> active carrying the freshly observed pair, then back to hibernated:
        # the declaration snapshot survives, which is what makes it CURRENT-generation
        # authority rather than release-time evidence.
        rec = store.get(key)
        store.transition_disposition(
            key, expected_disposition=DISPOSITION_HIBERNATED,
            expected_revision=rec.revision, target=DISPOSITION_ACTIVE,
            decision=_decision(), rehydrated_declared_slots=pins,
        )
        rec = store.get(key)
        store.transition_disposition(
            key, expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=rec.revision, target=DISPOSITION_HIBERNATED,
            decision=_decision(),
        )
        rec = store.get(key)
        store.request_release(
            key, expected_revision=rec.revision, action_id="legacy-rel",
            observation=build_release_observation(()),
        )
        rec = store.get(key)
        store.record_release_outcome(
            key, action_id="legacy-rel", expected_revision=rec.revision,
            target=RELEASE_RELEASED,
        )
        conn = sqlite3.connect(store.path)
        try:
            # The single column a pre-v10 build would not have had, plus the absent release
            # observation the real row carries.
            conn.execute(
                "UPDATE lane_lifecycle_records SET lane_epoch = '0', release_observation = ''"
            )
            conn.commit()
        finally:
            conn.close()
        return store, key

    @classmethod
    def _pair_pins(cls):
        from mozyo_bridge.core.state.lane_declared_slots import ProcessGenerationPin

        # The ACTUAL #14755 shape (j#96911 F1): the declared `role` is the gateway/worker
        # SLOT, and `provider` is codex/claude. The live inventory row and the startup
        # attestation both spell the PROVIDER in their own `role` token. The previous fixture
        # set role == provider, which made a join of the wrong two axes look correct and hid
        # the defect that made the real dry-run unreachable.
        return (
            ProcessGenerationPin(
                role="worker", provider="claude", assigned_name="pair-worker",
                locator="%10",
            ),
            ProcessGenerationPin(
                role="gateway", provider="codex", assigned_name="pair-gateway",
                locator="%11",
            ),
        )

    def test_the_fixture_matches_the_real_14755_shape(self) -> None:
        """Guard the premise: observation absent, declared pair present.

        If this drifts, every derivation test below silently starts measuring a shape the
        acceptance target does not have — the failure j#96895 caught.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store, key = self._legacy_lane_with_pins(tmp, self._pair_pins())
            row = store.get(key)
            self.assertEqual(row.release_observation, "")
            self.assertTrue(row.declared_slots)
            self.assertEqual(row.lane_epoch, "0")

    def _ready_world(self, tmp):
        """A legacy lane whose own pair is live+attested and nothing else is."""
        home = pathlib.Path(tmp)
        pins = self._pair_pins()
        store, key = self._legacy_lane_with_pins(tmp, pins)
        agents = []
        for pin in pins:
            # live + attestation carry the PROVIDER in their `role` token, not the slot role.
            self._attest(home, pin.assigned_name, role=pin.provider, locator=pin.locator)
            agents.append(
                self._agent(pin.assigned_name, role=pin.provider, locator=pin.locator)
            )
        return home, store, key, pins, agents

    def _plan(self, home, store, key, view, *, asserted=()):
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.lane_epoch_legacy_recovery_plan import (  # noqa: E501
            plan_lane_epoch_legacy_recovery,
        )

        return plan_lane_epoch_legacy_recovery(
            home=home, view=view, workspace_id=WS, lane=LANE, issue_id=ISSUE,
            expected_revision=store.get(key).revision, decision=_decision(),
            asserted_slots=asserted,
        )

    # -- j#96881 F1: the exclusion set is authority, not input -------------------

    def test_naming_every_consumer_cannot_clear_the_global_blocker(self) -> None:
        """The exact laundering j#96881 F1 measured, as a regression.

        Before the fix this returned ``plan_ready`` with an empty foreign list. The caller
        could delete the blocker by describing the world as entirely its own.
        """
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.lane_epoch_legacy_recovery_plan import (  # noqa: E501
            OFFLINE_GLOBAL_RUNTIME_UPGRADE_REQUIRED,
            PLAN_READY,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home, store, key, pins, agents = self._ready_world(tmp)
            foreigners = ("other-coordinator", "another-workspace-agent")
            for name in foreigners:
                self._attest(home, name, locator="%99")
                agents.append(self._agent(name, locator="%99"))
            view = self._view(agents=agents)

            everything = tuple(a.name for a in agents)
            plan = self._plan(home, store, key, view, asserted=everything)

            self.assertNotEqual(plan.state, PLAN_READY)
            # It refuses on the assertion rather than silently ignoring the input, so a
            # caller who believed the old semantics is told, not quietly overruled.
            self.assertEqual(plan.state, "blocked_target_slot_assertion_failed")
            self.assertEqual(plan.target_slots, ("pair-gateway", "pair-worker"))

            # And with no assertion at all, the blocker fires on the authority-derived set.
            honest = self._plan(home, store, key, view)
            self.assertEqual(honest.state, OFFLINE_GLOBAL_RUNTIME_UPGRADE_REQUIRED)
            self.assertEqual(honest.foreign_consumers, tuple(sorted(foreigners)))

    def test_only_the_authority_derived_pair_is_excluded(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.lane_epoch_legacy_recovery_plan import (  # noqa: E501
            PLAN_READY,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home, store, key, pins, agents = self._ready_world(tmp)
            plan = self._plan(home, store, key, self._view(agents=agents))
            self.assertEqual(plan.state, PLAN_READY)
            self.assertEqual(plan.target_slots, ("pair-gateway", "pair-worker"))
            self.assertEqual(plan.foreign_consumers, ())

    def test_a_correct_assertion_is_accepted(self) -> None:
        # The flag has to be usable when it is TRUE, or it is not an assertion, just a trap.
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.lane_epoch_legacy_recovery_plan import (  # noqa: E501
            PLAN_READY,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home, store, key, pins, agents = self._ready_world(tmp)
            plan = self._plan(
                home, store, key, self._view(agents=agents),
                asserted=("pair-worker", "pair-gateway"),
            )
            self.assertEqual(plan.state, PLAN_READY)
            self.assertEqual(plan.asserted_slots, ("pair-gateway", "pair-worker"))

    def test_a_partial_or_padded_assertion_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, store, key, pins, agents = self._ready_world(tmp)
            view = self._view(agents=agents)
            for asserted in (
                ("pair-worker",),                          # one slot of two
                ("pair-worker", "pair-gateway", "extra"),  # padded with a foreign name
                ("pair-worker", "pair-worker"),            # duplicate
            ):
                with self.subTest(asserted=asserted):
                    plan = self._plan(home, store, key, view, asserted=asserted)
                    if sorted(set(asserted)) == ["pair-gateway", "pair-worker"]:
                        continue  # the duplicate case normalises to the true pair
                    self.assertEqual(
                        plan.state, "blocked_target_slot_assertion_failed"
                    )

    def test_a_duplicated_assertion_slot_is_refused_not_deduplicated(self) -> None:
        """Redmine #14756 review j#96949 F3.

        The assertion was normalised with `sorted(set(...))`, so `gateway, worker, worker` —
        three slots named for a two-slot pair — collapsed to the correct two and returned
        `plan_ready`. An assertion that edits itself into agreement is not an assertion, and
        the multiplicity is exactly what the caller got wrong.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home, store, key, pins, agents = self._ready_world(tmp)
            plan = self._plan(
                home, store, key, self._view(agents=agents),
                asserted=("pair-gateway", "pair-worker", "pair-worker"),
            )
            self.assertEqual(plan.state, "blocked_target_slot_assertion_failed")
            self.assertEqual(plan.target_slots, ("pair-gateway", "pair-worker"))

    def test_a_live_consumer_whose_locator_drifted_is_not_excluded(self) -> None:
        """Locator reuse is the failure mode this whole issue exists for.

        A stored pin names ``pair-worker`` at ``%10``. If a DIFFERENT process now answers to
        that name at another locator, excluding it from the census would exclude a stranger.
        """
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.lane_epoch_legacy_recovery_plan import (  # noqa: E501
            BLOCKED_PAIR_IDENTITY_MISMATCH,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home, store, key, pins, agents = self._ready_world(tmp)
            drifted = [
                self._agent("pair-worker", role="claude", locator="%77"),  # recycled pane
                agents[1],
            ]
            plan = self._plan(home, store, key, self._view(agents=drifted))
            self.assertEqual(plan.state, BLOCKED_PAIR_IDENTITY_MISMATCH)
            self.assertEqual(plan.foreign_consumers, ())  # it refuses, it does not guess

    def test_a_live_consumer_attested_to_another_lane_is_not_excluded(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.lane_epoch_legacy_recovery_plan import (  # noqa: E501
            BLOCKED_PAIR_IDENTITY_MISMATCH,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home, store, key, pins, agents = self._ready_world(tmp)
            # Same name, same locator, but the attestation says a different lane.
            self._attest(home, "pair-worker", role="claude", locator="%10", lane="other")
            plan = self._plan(home, store, key, self._view(agents=agents))
            self.assertEqual(plan.state, BLOCKED_PAIR_IDENTITY_MISMATCH)

    def test_a_declaration_that_is_not_an_exact_pair_refuses(self) -> None:
        # j#96890 §1 / j#96895: absent, corrupt, one slot, three slots, or two slots sharing
        # a role are all typed refusals. None of them may produce a partial exclusion set.
        from mozyo_bridge.core.state.lane_declared_slots import (
            ProcessGenerationPin,
            encode_declared_slots,
        )
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.lane_epoch_legacy_recovery_plan import (  # noqa: E501
            BLOCKED_PAIR_AUTHORITY_UNAVAILABLE,
            plan_lane_epoch_legacy_recovery,
        )

        def pin(role, name, locator, provider=None):
            return ProcessGenerationPin(
                role=role, provider=provider or role, assigned_name=name, locator=locator
            )

        cases = {
            "absent": "",
            "corrupt": "{not json",
            "one slot": encode_declared_slots((pin("claude", "solo", "%1"),)),
            "three slots": encode_declared_slots(
                (
                    pin("claude", "a", "%1"),
                    pin("codex", "b", "%2"),
                    pin("other", "c", "%3"),
                )
            ),
            "duplicate role": encode_declared_slots(
                (pin("claude", "a", "%1"), pin("claude", "b", "%2"))
            ),
        }
        for label, encoded in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                home = pathlib.Path(tmp)
                store, key = self._legacy_lane_with_pins(tmp, self._pair_pins())
                conn = sqlite3.connect(store.path)
                try:
                    conn.execute(
                        "UPDATE lane_lifecycle_records SET declared_slots = ?", (encoded,)
                    )
                    conn.commit()
                finally:
                    conn.close()
                plan = plan_lane_epoch_legacy_recovery(
                    home=home, view=self._view(agents=()), workspace_id=WS, lane=LANE,
                    issue_id=ISSUE, expected_revision=store.get(key).revision,
                    decision=_decision(),
                )
                self.assertEqual(plan.state, BLOCKED_PAIR_AUTHORITY_UNAVAILABLE)
                self.assertEqual(plan.target_slots, ())

    def test_release_evidence_is_never_a_fallback_authority(self) -> None:
        """j#96895: a release observation must not stand in for the current pair.

        A rail with two authorities answers from whichever happens to be populated, and the
        release snapshot is by construction the OLD generation's — using it after a bootstrap
        or pin repair would exclude processes that no longer exist while leaving the current
        pair in the census. So a lane with a rich release observation and NO declared slots
        must still refuse.
        """
        from mozyo_bridge.core.state.lane_release_observation import (
            build_release_observation,
            encode_release_observation,
        )
        from mozyo_bridge.core.state.lane_lifecycle_model import ReleasePin
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.lane_epoch_legacy_recovery_plan import (  # noqa: E501
            BLOCKED_PAIR_AUTHORITY_UNAVAILABLE,
            plan_lane_epoch_legacy_recovery,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            store, key = self._legacy_lane_with_pins(tmp, self._pair_pins())
            observation = encode_release_observation(
                build_release_observation(
                    (
                        ReleasePin(role="claude", assigned_name="pair-worker", locator="%10"),
                        ReleasePin(role="codex", assigned_name="pair-gateway", locator="%11"),
                    )
                )
            )
            conn = sqlite3.connect(store.path)
            try:
                conn.execute(
                    "UPDATE lane_lifecycle_records SET declared_slots = '', "
                    "release_observation = ?",
                    (observation,),
                )
                conn.commit()
            finally:
                conn.close()
            plan = plan_lane_epoch_legacy_recovery(
                home=home, view=self._view(agents=()), workspace_id=WS, lane=LANE,
                issue_id=ISSUE, expected_revision=store.get(key).revision,
                decision=_decision(),
            )
            self.assertEqual(plan.state, BLOCKED_PAIR_AUTHORITY_UNAVAILABLE)

    def test_a_lane_that_never_declared_a_pair_derives_nothing(self) -> None:
        # An absent declaration is not an empty pair. Without stored authority there is no
        # basis for excluding anyone, and the rail says so rather than excluding nobody
        # quietly and calling the result a measurement.
        #
        # (This test previously described itself as being about a missing release
        # OBSERVATION. It passed, but for the wrong reason — the lane it built had no
        # declared slots either — so the name asserted a behaviour the body never exercised.)
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.lane_epoch_legacy_recovery_plan import (  # noqa: E501
            BLOCKED_PAIR_AUTHORITY_UNAVAILABLE,
            plan_lane_epoch_legacy_recovery,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            store, key = TheLegacyNextRailIsActuallyExecutable._legacy_already_hibernated(
                tmp
            )
            self.assertEqual(store.get(key).declared_slots, "")  # the premise, measured
            plan = plan_lane_epoch_legacy_recovery(
                home=home, view=self._view(agents=()), workspace_id=WS, lane=LANE,
                issue_id=ISSUE, expected_revision=store.get(key).revision,
                decision=_decision(),
            )
            self.assertEqual(plan.state, BLOCKED_PAIR_AUTHORITY_UNAVAILABLE)

    def test_half_a_pair_is_never_excluded_as_if_it_were_the_pair(self) -> None:
        """j#96890 §1, and a correction of my own earlier reasoning.

        I first wrote this test the other way round: one slot live, the other gone, asserting
        ``plan_ready`` with a one-name exclusion set, on the argument that "excluding a dead
        slot changes nothing". That argument is true per-slot and wrong per-rail. The rail's
        step 2 is "terminally close BOTH slots of the exact old pair"; a derivation that
        answers with half a pair has not identified the pair, and subtracting it narrows the
        consumer census on an incomplete answer. Exactly two, or refuse.
        """
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.lane_epoch_legacy_recovery_plan import (  # noqa: E501
            BLOCKED_PAIR_IDENTITY_MISMATCH,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home, store, key, pins, agents = self._ready_world(tmp)
            # `pair-worker` live and attested, `pair-gateway` attested but not live.
            plan = self._plan(home, store, key, self._view(agents=[agents[0]]))
            self.assertEqual(plan.state, BLOCKED_PAIR_IDENTITY_MISMATCH)
            self.assertEqual(plan.target_slots, ())  # nothing partial is offered

    def test_a_pair_that_cannot_be_fully_located_refuses(self) -> None:
        """j#96911: both-absent is a refusal too, not an empty derivation.

        I first wrote this the other way round — neither slot live, therefore nothing to
        subtract, therefore ``plan_ready`` — on the argument that an empty exclusion set is
        harmless. It is harmless to the census and wrong for the rail: step 2 closes BOTH
        slots of the exact pair, so a pair the rail cannot locate is a pair it has not
        identified, and proceeding would plan a close against slots nobody has resolved.
        """
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.lane_epoch_legacy_recovery_plan import (  # noqa: E501
            BLOCKED_PAIR_IDENTITY_MISMATCH,
            plan_lane_epoch_legacy_recovery,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            store, key = self._legacy_lane_with_pins(tmp, self._pair_pins())
            plan = plan_lane_epoch_legacy_recovery(
                home=home, view=self._view(agents=()), workspace_id=WS, lane=LANE,
                issue_id=ISSUE, expected_revision=store.get(key).revision,
                decision=_decision(),
            )
            self.assertEqual(plan.state, BLOCKED_PAIR_IDENTITY_MISMATCH)
            self.assertEqual(plan.target_slots, ())

    def test_a_stale_attestation_locator_does_not_qualify_a_slot_as_ours(self) -> None:
        """j#96890 §2: the attestation locator is joined too, not just the live one.

        Without it, an attestation row left behind by a pane that no longer exists would
        still match on name/workspace/lane/role, and the live process now answering to that
        name — a stranger — would be removed from the census as though it were ours.
        """
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.lane_epoch_legacy_recovery_plan import (  # noqa: E501
            BLOCKED_PAIR_IDENTITY_MISMATCH,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home, store, key, pins, agents = self._ready_world(tmp)
            # Live row matches the declared pin exactly; only the ATTESTATION locator is stale.
            self._attest(home, "pair-worker", role="claude", locator="%stale")
            plan = self._plan(home, store, key, self._view(agents=agents))
            self.assertEqual(plan.state, BLOCKED_PAIR_IDENTITY_MISMATCH)
            self.assertIn("attested locator", plan.detail)

    # -- j#96866: the blocker itself -------------------------------------------

    def test_an_unmeasurable_fleet_refuses_rather_than_reading_as_drained(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.lane_epoch_legacy_recovery_plan import (  # noqa: E501
            BLOCKED_CONSUMERS_UNMEASURABLE,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home, store, key, pins, agents = self._ready_world(tmp)
            plan = self._plan(home, store, key, self._view(agents=(), ok=False))
            self.assertEqual(plan.state, BLOCKED_CONSUMERS_UNMEASURABLE)
            self.assertEqual(plan.steps, ())

    def test_the_census_is_evaluated_before_the_lanes_own_shape(self) -> None:
        # Order matters for what the operator is told. A lane that is NOT the legacy shape,
        # in a home with a live foreign fleet, must still hear about the fleet: that is the
        # fact deciding whether any of this is possible.
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.lane_epoch_legacy_recovery_plan import (  # noqa: E501
            OFFLINE_GLOBAL_RUNTIME_UPGRADE_REQUIRED,
            plan_lane_epoch_legacy_recovery,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            pins = self._pair_pins()
            store, key = self._legacy_lane_with_pins(tmp, pins)
            # Re-mint: epoch 1, so the lane is NOT the legacy shape any more.
            conn = sqlite3.connect(store.path)
            try:
                conn.execute("UPDATE lane_lifecycle_records SET lane_epoch = '1'")
                conn.commit()
            finally:
                conn.close()
            agents = []
            for pin in pins:  # the lane's own pair stays locatable
                self._attest(home, pin.assigned_name, role=pin.provider, locator=pin.locator)
                agents.append(
                    self._agent(pin.assigned_name, role=pin.provider, locator=pin.locator)
                )
            self._attest(home, "foreign", locator="%99")
            agents.append(self._agent("foreign", locator="%99"))
            plan = plan_lane_epoch_legacy_recovery(
                home=home, view=self._view(agents=agents),
                workspace_id=WS, lane=LANE, issue_id=ISSUE,
                expected_revision=store.get(key).revision, decision=_decision(),
            )
            self.assertEqual(plan.state, OFFLINE_GLOBAL_RUNTIME_UPGRADE_REQUIRED)

    def test_a_non_legacy_lane_in_a_drained_home_reports_the_cas_own_reason(self) -> None:
        # No second opinion about the row: the plan carries the lifecycle CAS's token, so a
        # plan and the write it predicts cannot describe the same row two ways.
        from mozyo_bridge.core.state.lane_lifecycle_model import CAS_UNEXPECTED_STATE
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.lane_epoch_legacy_recovery_plan import (  # noqa: E501
            BLOCKED_NOT_LEGACY_SHAPE,
            plan_lane_epoch_legacy_recovery,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            store, key = self._legacy_lane_with_pins(tmp, self._pair_pins())
            conn = sqlite3.connect(store.path)
            try:
                conn.execute("UPDATE lane_lifecycle_records SET lane_epoch = '1'")
                conn.commit()
            finally:
                conn.close()
            agents = []
            for pin in self._pair_pins():  # locatable pair, empty census
                self._attest(home, pin.assigned_name, role=pin.provider, locator=pin.locator)
                agents.append(
                    self._agent(pin.assigned_name, role=pin.provider, locator=pin.locator)
                )
            plan = plan_lane_epoch_legacy_recovery(
                home=home, view=self._view(agents=agents), workspace_id=WS, lane=LANE,
                issue_id=ISSUE, expected_revision=store.get(key).revision,
                decision=_decision(),
            )
            self.assertEqual(plan.state, BLOCKED_NOT_LEGACY_SHAPE)
            self.assertEqual(plan.lifecycle_reason, CAS_UNEXPECTED_STATE)

    def test_the_planned_order_closes_before_it_adopts(self) -> None:
        """j#96861's whole correction, as an ordering assertion.

        Adopt-before-close leaves ``epoch=1`` + v1 store + a live old pair on a crash, and
        the pre-effect fence added by this same issue then refuses the next close. The rail
        would deadlock on its own output.
        """
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.lane_epoch_legacy_recovery_plan import (  # noqa: E501
            CANONICAL_STEPS,
        )

        joined = "\n".join(CANONICAL_STEPS).lower()
        close_at = joined.index("terminally close")
        migrate_at = joined.index("migrate the attestation store")
        adopt_at = joined.index("adopt the lifecycle epoch")
        self.assertLess(close_at, migrate_at)
        self.assertLess(migrate_at, adopt_at)

    def test_the_plan_has_no_execute_mode_at_all(self) -> None:
        # j#96866 ruling 2: an implementation that closes the target pair and proceeds to a
        # global migration is forbidden. The strongest form of "it does not" is that there
        # is no parameter that could make it.
        import inspect

        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application import (  # noqa: E501
            lane_epoch_legacy_recovery_plan as module,
        )

        params = inspect.signature(module.plan_lane_epoch_legacy_recovery).parameters
        self.assertNotIn("write", params)
        self.assertNotIn("execute", params)
        with tempfile.TemporaryDirectory() as tmp:
            home, store, key, pins, agents = self._ready_world(tmp)
            plan = self._plan(home, store, key, self._view(agents=agents))
            self.assertFalse(plan.executed)
            self.assertFalse(plan.as_payload()["executed"])

    def test_planning_writes_nothing_to_either_store(self) -> None:
        # The claim "plan only" measured rather than asserted: both stores are byte-identical
        # across a planning run, including the ready path that offers the whole sequence.
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            herdr_identity_attestation_path,
        )
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.lane_epoch_legacy_recovery_plan import (  # noqa: E501
            PLAN_READY,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home, store, key, pins, agents = self._ready_world(tmp)
            before = {
                path: path.read_bytes()
                for path in (store.path, herdr_identity_attestation_path(home))
            }
            row_before = store.get(key)

            plan = self._plan(home, store, key, self._view(agents=agents))

            self.assertEqual(plan.state, PLAN_READY)  # the path that offers the most
            for path, blob in before.items():
                self.assertEqual(path.read_bytes(), blob, f"{path.name} was written")
            self.assertEqual(store.get(key).revision, row_before.revision)
            self.assertEqual(store.get(key).lane_epoch, row_before.lane_epoch)

    # -- j#96856: the intersection --------------------------------------------

    def test_a_live_sibling_that_never_attested_here_does_not_reach_the_gate(self) -> None:
        """Redmine #14756 j#96856: the gate is ``live & attested``, an INTERSECTION.

        The negative control that keeps every other consumer test from being vacuous. A live
        agent with no row in this store is correctly not a consumer of it.

        This also corrects my own earlier summary: j#96852 and j#96854 described the gate as
        "any live consumer blocks", which is looser than the code.
        """
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.lane_epoch_legacy_recovery_plan import (  # noqa: E501
            PLAN_READY,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home, store, key, pins, agents = self._ready_world(tmp)
            # Live, but holds NO row in this store.
            agents.append(self._agent("unrelated-lane-agent", locator="%99"))
            plan = self._plan(home, store, key, self._view(agents=agents))
            self.assertEqual(plan.state, PLAN_READY)
            self.assertEqual(plan.foreign_consumers, ())

    def test_the_same_live_sibling_blocks_once_it_holds_a_row_here(self) -> None:
        # The other half of the intersection, so the test above cannot pass by the gate
        # simply never firing: the ONLY difference between the two is the stored row.
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.lane_epoch_legacy_recovery_plan import (  # noqa: E501
            OFFLINE_GLOBAL_RUNTIME_UPGRADE_REQUIRED,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home, store, key, pins, agents = self._ready_world(tmp)
            agents.append(self._agent("unrelated-lane-agent", locator="%99"))
            self._attest(home, "unrelated-lane-agent", locator="%99")
            plan = self._plan(home, store, key, self._view(agents=agents))
            self.assertEqual(plan.state, OFFLINE_GLOBAL_RUNTIME_UPGRADE_REQUIRED)
            self.assertEqual(plan.foreign_consumers, ("unrelated-lane-agent",))

    def test_the_migration_this_plan_depends_on_really_does_refuse(self) -> None:
        """The premise j#96846 stated and two rounds carried forward unverified.

        Everything above assumes ``attestation-store migrate`` refuses while an attested live
        consumer exists. If that were false, the whole close-first sequence and the j#96866
        blocker would be solving a problem that does not exist.
        """
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_attestation_store_maintenance import (  # noqa: E501
            BLOCKED_CONSUMERS_LIVE,
            run_attestation_store_migrate,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            OldAttestationSchemasAreBlockedNotGuessed._legacy_store(home, 1)
            self._attest(home, "live-one")
            blocked = run_attestation_store_migrate(
                home=home, view=self._view(agents=[self._agent("live-one")]), write=True
            )
            self.assertEqual(blocked.state, BLOCKED_CONSUMERS_LIVE)
            self.assertFalse(blocked.executed)
            # And it is genuinely the intersection that blocks it: drain the fleet and the
            # same call proceeds, so the refusal above is not some unrelated precondition.
            drained = run_attestation_store_migrate(
                home=home, view=self._view(agents=()), write=True
            )
            self.assertTrue(drained.ok, drained.detail)

    # -- public surface --------------------------------------------------------

    def test_the_rail_is_reachable_from_the_public_cli_not_only_as_an_api(self) -> None:
        """j#96857: a private class and raw SQLite are not an operator surface."""
        import argparse
        import contextlib
        import io

        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.cli_herdr_attestation_store import (  # noqa: E501
            register_herdr_attestation_store_parser,
        )
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.lane_epoch_legacy_recovery_plan import (  # noqa: E501
            OFFLINE_GLOBAL_RUNTIME_UPGRADE_REQUIRED,
        )

        root = argparse.ArgumentParser()
        register_herdr_attestation_store_parser(root.add_subparsers(dest="cmd"))
        listing = io.StringIO()
        with contextlib.redirect_stdout(listing):
            with self.assertRaises(SystemExit):
                root.parse_args(["attestation-store", "--help"])
        self.assertIn("lane-epoch-recovery-plan", listing.getvalue())

        with tempfile.TemporaryDirectory() as tmp:
            home, store, key, pins, agents = self._ready_world(tmp)
            agents.append(self._agent("foreign", locator="%99"))
            self._attest(home, "foreign", locator="%99")
            args = root.parse_args([
                "attestation-store", "lane-epoch-recovery-plan",
                "--workspace", WS, "--lane", LANE, "--issue", ISSUE,
                "--revision", str(store.get(key).revision), "--journal", "96861",
                "--home", str(home), "--json",
            ])
            captured, errors = io.StringIO(), io.StringIO()
            with mock.patch.object(
                sys.modules[
                    "mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events"
                    ".application.cli_herdr_attestation_store"
                ],
                "_inventory_view",
                lambda _args: self._view(agents=agents),
            ):
                with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(errors):
                    code = args.func(args)

            payload = json.loads(captured.getvalue())
            self.assertEqual(payload["state"], OFFLINE_GLOBAL_RUNTIME_UPGRADE_REQUIRED)
            self.assertEqual(payload["foreign_consumers"], ["foreign"])
            self.assertFalse(payload["executed"])
            self.assertEqual(payload["steps"], [])
            self.assertEqual(code, 1)  # a blocker must not exit 0

    def test_the_public_command_offers_no_write_flag_and_no_target_slot(self) -> None:
        # j#96866 ruling 2 at the operator surface, plus j#96881 F1: `--target-slot` is gone
        # entirely. Leaving it as an accepted-but-ignored alias would let an operator who
        # learned the old semantics believe they were still narrowing the census.
        import argparse
        import contextlib
        import io

        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.cli_herdr_attestation_store import (  # noqa: E501
            register_herdr_attestation_store_parser,
        )

        root = argparse.ArgumentParser()
        register_herdr_attestation_store_parser(root.add_subparsers(dest="cmd"))
        for flag in ("--write", "--execute", "--target-slot"):
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    root.parse_args([
                        "attestation-store", "lane-epoch-recovery-plan",
                        "--workspace", WS, "--lane", LANE, "--issue", ISSUE,
                        "--revision", "1", "--journal", "96861", flag, "x",
                    ])

    def test_the_plan_and_the_adoption_cas_share_one_predicate(self) -> None:
        # The drift guard. If the planner grew its own notion of "is this the legacy shape",
        # a plan could read ready while the CAS refused — and only the plan is ever exercised
        # by an operator reading output.
        source = pathlib.Path(
            ROOT.parent
            / "src/mozyo_bridge/e_110_execution_platform/f_160_state_store_managed_events"
            / "application/lane_epoch_legacy_recovery_plan.py"
        ).read_text()
        self.assertIn("legacy_adoption_refusal", source)
        adoption = pathlib.Path(
            ROOT.parent / "src/mozyo_bridge/core/state/lane_epoch_adoption.py"
        ).read_text()
        # And the CAS still calls it INSIDE its write lock, rather than trusting a plan.
        self.assertIn("BEGIN IMMEDIATE", adoption)
        self.assertIn("legacy_adoption_refusal(", adoption)


class AMalformedStoredEpochIsNeverLaunderedIntoZero(unittest.TestCase):
    """Redmine #14756 j#96881 F2: "unreadable" and "never minted" are different facts.

    The old helper answered ``0`` for ``'corrupt'``, ``-7``, ``2.5``, ``True`` and ``NULL``
    alike. Read-side that is safe — both mean "cannot prove a generation". But the same helper
    backed the WRITER, so a hibernate CAS minted ``1`` from every one of them, and a
    non-hibernate transition wrote a normalised ``0`` back over the corrupt value, laundering
    it into a row the adoption rail would then mint. Both are counter rollbacks: they re-issue
    an epoch some released generation may still hold, which is the survivor admission this
    whole issue exists to close.
    """

    #: Every shape the column must refuse. The last seven are the ones j#96911 F2 added:
    #: under the original INTEGER affinity SQLite coerced them to integers before any Python
    #: check could run, so they read back as legitimate counters and re-minted an epoch.
    MALFORMED = (
        "corrupt", "-7", "2.5", "00", "+0", " 0 ", "0.0", "", "7_0",
        -7, 2.5, 0, 1, True, False, None, b"0",
    )

    def test_the_classifier_separates_malformed_from_zero(self) -> None:
        from mozyo_bridge.core.state.lane_epoch import (
            EPOCH_STORED_MALFORMED,
            EPOCH_STORED_MINTED,
            EPOCH_STORED_UNMINTED,
            classify_stored_epoch,
        )

        for value in self.MALFORMED:
            with self.subTest(value=value):
                self.assertEqual(classify_stored_epoch(value)[1], EPOCH_STORED_MALFORMED)
        self.assertEqual(classify_stored_epoch("0")[1], EPOCH_STORED_UNMINTED)
        self.assertEqual(classify_stored_epoch("3"), (3, EPOCH_STORED_MINTED))

    def test_a_hibernate_transition_never_mints_from_a_malformed_counter(self) -> None:
        from mozyo_bridge.core.state.lane_epoch import lane_epoch_on_transition

        for value in self.MALFORMED:
            with self.subTest(value=value):
                self.assertIsNone(
                    lane_epoch_on_transition(
                        value, target="hibernated", hibernated="hibernated"
                    )
                )
        # The valid cases still advance, or the guard would just be an outage.
        self.assertEqual(
            lane_epoch_on_transition("0", target="hibernated", hibernated="hibernated"), "1"
        )
        self.assertEqual(
            lane_epoch_on_transition("7", target="hibernated", hibernated="hibernated"), "8"
        )

    def test_a_non_hibernate_transition_does_not_normalise_a_malformed_counter(
        self,
    ) -> None:
        # The subtler half. Writing 0 back over 'corrupt' on the way to `active` would turn an
        # unreadable row into a legitimate-looking never-minted one — laundering by way of an
        # unrelated transition. The docstring already claimed byte preservation; it now holds.
        from mozyo_bridge.core.state.lane_epoch import lane_epoch_on_transition

        for value in self.MALFORMED:
            with self.subTest(value=value):
                self.assertIsNone(
                    lane_epoch_on_transition(
                        value, target="active", hibernated="hibernated"
                    )
                )
        self.assertEqual(
            lane_epoch_on_transition("5", target="active", hibernated="hibernated"), "5"
        )

    def test_the_read_side_still_fails_closed_rather_than_raising(self) -> None:
        # Readers keep their old behaviour: a corrupt row is authority-unavailable, exactly as
        # an unminted one is. Only writers gained the distinction.
        for value in self.MALFORMED:
            with self.subTest(value=value):
                record = SimpleNamespace(lane_epoch=value)
                self.assertEqual(
                    required_resume_epoch(record),
                    (LANE_EPOCH_UNMINTED, EPOCH_AUTHORITY_UNAVAILABLE),
                )

    def test_a_raw_corrupt_row_survives_a_hibernate_cas_untouched(self) -> None:
        """Measured against a real SQLite row, not only the pure probe.

        j#96881 asks for both, and they are different claims: the pure function can refuse
        while the CAS still writes, if the caller ignores the refusal.
        """
        for value in ("corrupt", "-7", "2.5", "00", "+0", " 0 ", "0.0", 2.0, 0, 1, True, False, b"0"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                store = LaneLifecycleStore(home=pathlib.Path(tmp))
                key = LaneLifecycleKey(WS, LANE)
                store.declare_active(key, decision=_decision(), issue_id=ISSUE)
                conn = sqlite3.connect(store.path)
                try:
                    conn.execute(
                        "UPDATE lane_lifecycle_records SET lane_epoch = ?", (value,)
                    )
                    conn.commit()
                finally:
                    conn.close()
                before = store.get(key)

                outcome = store.transition_disposition(
                    key,
                    expected_disposition=DISPOSITION_ACTIVE,
                    expected_revision=before.revision,
                    target=DISPOSITION_HIBERNATED,
                    decision=_decision(),
                )

                self.assertFalse(outcome.applied)
                after = store.get(key)
                # Zero write: the counter, the revision and the disposition are all unmoved.
                self.assertEqual(after.revision, before.revision)
                self.assertEqual(after.lane_disposition, before.lane_disposition)
                raw = sqlite3.connect(store.path)
                try:
                    stored = raw.execute(
                        "SELECT lane_epoch FROM lane_lifecycle_records"
                    ).fetchone()[0]
                finally:
                    raw.close()
                self.assertEqual(stored, value)  # byte-preserved, not normalised to 0

    def test_adoption_refuses_a_malformed_counter_distinctly_from_an_adopted_one(
        self,
    ) -> None:
        # Adoption's whole safety argument is "this lane has never minted". A corrupt value is
        # not evidence of that, and must not share the "already adopted" answer either — the
        # two need different operator actions.
        from mozyo_bridge.core.state.lane_lifecycle_model import (
            CAS_FORBIDDEN_TRANSITION,
            CAS_UNEXPECTED_STATE,
        )
        from mozyo_bridge.core.state.lane_epoch_adoption import LaneEpochAdoptionStore

        for value, expected in (
            ("corrupt", CAS_FORBIDDEN_TRANSITION),
            ("-7", CAS_FORBIDDEN_TRANSITION),
            ("2.5", CAS_FORBIDDEN_TRANSITION),
            ("00", CAS_FORBIDDEN_TRANSITION),
            ("0.0", CAS_FORBIDDEN_TRANSITION),
            (0, CAS_FORBIDDEN_TRANSITION),      # storage class INTEGER, not canonical TEXT
            (True, CAS_FORBIDDEN_TRANSITION),
            (b"0", CAS_FORBIDDEN_TRANSITION),
            ("1", CAS_UNEXPECTED_STATE),        # already minted
        ):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                store, key = (
                    TheLegacyNextRailIsActuallyExecutable._legacy_already_hibernated(tmp)
                )
                conn = sqlite3.connect(store.path)
                try:
                    conn.execute(
                        "UPDATE lane_lifecycle_records SET lane_epoch = ?", (value,)
                    )
                    conn.commit()
                finally:
                    conn.close()
                before = store.get(key)
                outcome = LaneEpochAdoptionStore(home=pathlib.Path(tmp)).adopt_legacy_epoch(
                    key, expected_revision=before.revision, issue_id=ISSUE,
                    decision=_decision(),
                )
                self.assertFalse(outcome.applied)
                self.assertEqual(outcome.reason, expected)
                after = store.get(key)
                self.assertEqual(after.revision, before.revision)
                self.assertEqual(after.lane_disposition, before.lane_disposition)

    def test_a_valid_zero_still_adopts_to_exactly_one(self) -> None:
        # The guard must not become an outage: the legitimate case still works.
        from mozyo_bridge.core.state.lane_epoch_adoption import LaneEpochAdoptionStore

        with tempfile.TemporaryDirectory() as tmp:
            store, key = (
                TheLegacyNextRailIsActuallyExecutable._legacy_already_hibernated(tmp)
            )
            rec = store.get(key)
            outcome = LaneEpochAdoptionStore(home=pathlib.Path(tmp)).adopt_legacy_epoch(
                key, expected_revision=rec.revision, issue_id=ISSUE, decision=_decision()
            )
            self.assertTrue(outcome.applied)
            self.assertEqual(store.get(key).lane_epoch, "1")


class ForgedEpochsAndUnboundedTokensFailClosed(unittest.TestCase):
    """Redmine #14756 review j#96949 F1/F2 — the four required regressions.

    Both findings were reproduced against the shipped code before the fix, and both are the
    same shape: a token nobody could honestly hold was treated as evidence. F1 admitted a
    LARGER epoch than the store had minted; F2 let an absurdly long one crash the parser
    instead of being refused.
    """

    @staticmethod
    def _attest(tmp, *, expected, observed):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            herdr_agent_attest as attest,
        )

        env = {"MOZYO_HERDR_BINARY": "/usr/bin/true"}
        if observed is not None:
            env[MOZYO_LANE_EPOCH_ENV] = observed
        return attest.perform_self_attestation(
            assigned_name="n", workspace_id=WS, role="claude", lane=LANE,
            env=env, lane_epoch=expected, home=Path(tmp),
            append_event=lambda stage, bounded_reason="": None,
            # Resolves the agent's own live row, so the bounded self-lookup succeeds and a
            # record is actually written (an empty locator writes nothing at all).
            runner=lambda *a, **k: type(
                "R", (), {
                    "returncode": 0,
                    "stdout": '{"agents":[{"name":"n","pane_id":"%1"}]}',
                }
            )(),
        )

    def test_expected_one_observed_999_is_refused_end_to_end(self) -> None:
        """The exact reproduction from j#96949 F1: required 1, observed 999 -> admitted."""
        with tempfile.TemporaryDirectory() as tmp:
            store, key = _hibernated_lane(tmp)  # mints epoch 1
            rec = store.get(key)
            self.assertEqual(required_resume_epoch(rec), (1, EPOCH_OK))

            record = self._attest(tmp, expected="1", observed="999")
            # The disagreement is on the RECORD, not only in the event stream: the resume
            # gate reads the record.
            self.assertEqual(record.lane_epoch, "")
            self.assertEqual(
                lane_epoch_verdict(rec, record.lane_epoch),
                (False, EPOCH_ATTESTATION_ABSENT),
            )
            # And even if such a token reached the gate by some other route, it is refused.
            self.assertFalse(lane_epoch_verdict(rec, "999")[0])

    def test_an_unexplained_epoch_with_no_expectation_is_refused(self) -> None:
        """j#96949 F1's second case: the launcher declared nothing, the process reported 1.

        The pre-fix condition only fired when an epoch was DECLARED and failed to land, so
        this direction skipped the check entirely and stored a clean-looking attestation that
        could satisfy admission on its own.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store, key = _hibernated_lane(tmp)
            record = self._attest(tmp, expected="", observed="1")
            self.assertEqual(record.lane_epoch, "")
            self.assertEqual(
                lane_epoch_verdict(store.get(key), record.lane_epoch),
                (False, EPOCH_ATTESTATION_ABSENT),
            )

    def test_an_agreeing_epoch_is_still_recorded_and_admitted(self) -> None:
        # The guard must not become an outage: agreement still round-trips and resumes.
        with tempfile.TemporaryDirectory() as tmp:
            store, key = _hibernated_lane(tmp)
            record = self._attest(tmp, expected="1", observed="1")
            self.assertEqual(record.lane_epoch, "1")
            self.assertEqual(
                lane_epoch_verdict(store.get(key), record.lane_epoch), (True, EPOCH_OK)
            )

    def test_a_huge_canonical_attested_token_is_typed_not_an_exception(self) -> None:
        # j#96949 F2: `int(raw)` on 5000 digits raises under CPython's conversion limit, so
        # a forged attestation crashed the resume verdict instead of being refused. This
        # function's contract is to be TOTAL over whatever arrives.
        huge = "9" * 5000
        self.assertEqual(parse_attested_epoch(huge), (LANE_EPOCH_UNMINTED, EPOCH_MALFORMED))
        with tempfile.TemporaryDirectory() as tmp:
            store, key = _hibernated_lane(tmp)
            self.assertEqual(
                lane_epoch_verdict(store.get(key), huge), (False, EPOCH_MALFORMED)
            )

    def test_a_huge_canonical_stored_epoch_is_typed_not_an_exception(self) -> None:
        # The storage side of the same bound: a corrupt row must not make the lifecycle
        # reader or the recovery planner raise instead of returning a total result.
        from mozyo_bridge.core.state.lane_epoch import (
            EPOCH_STORED_MALFORMED,
            classify_stored_epoch,
        )

        huge = "9" * 5000
        self.assertEqual(classify_stored_epoch(huge)[1], EPOCH_STORED_MALFORMED)
        with tempfile.TemporaryDirectory() as tmp:
            store, key = _hibernated_lane(tmp)
            conn = sqlite3.connect(store.path)
            try:
                conn.execute(
                    "UPDATE lane_lifecycle_records SET lane_epoch = ?", (huge,)
                )
                conn.commit()
            finally:
                conn.close()
            rec = store.get(key)  # total, not a raise
            self.assertEqual(
                required_resume_epoch(rec),
                (LANE_EPOCH_UNMINTED, EPOCH_AUTHORITY_UNAVAILABLE),
            )


class TheV10BackupIsARealRecoveryPoint(unittest.TestCase):
    """Redmine #14756 review j#96956 F5 — a backup that can lose the rows it preserves.

    ``shutil.copy2`` of ``state.sqlite`` is not a recovery point. Under WAL journalling
    committed pages live in ``-wal`` until a checkpoint folds them back, so a main-file copy
    silently drops every committed authority row since the last checkpoint —
    and ``wal_autocheckpoint=0`` makes that window unbounded. Every case below runs against a
    temp isolated store; the shared home is never migrated.
    """

    @staticmethod
    def _wal_store(tmp, *, rows=50, version=9):
        path = pathlib.Path(tmp) / "state.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")  # nothing folds back on its own
        conn.execute("CREATE TABLE lane_lifecycle_records (lane_id TEXT, lane_epoch TEXT)")
        for index in range(rows):
            conn.execute(
                "INSERT INTO lane_lifecycle_records VALUES (?, ?)", (str(index), "0")
            )
        conn.execute(f"PRAGMA user_version={version}")
        conn.commit()
        # The connection is deliberately LEFT OPEN and returned: closing it checkpoints the
        # WAL back into the main file and deletes `-wal`, which is precisely the state these
        # tests must not be in. The hazard only exists while committed pages are still in the
        # WAL, so a fixture that closes first would measure a store that had already healed.
        return path, conn

    def test_a_main_file_copy_would_have_lost_committed_rows(self) -> None:
        """The premise, measured — otherwise the fix guards a hazard nobody showed exists."""
        import shutil as _shutil

        with tempfile.TemporaryDirectory() as tmp:
            path, conn = self._wal_store(tmp)
            self.assertGreater(pathlib.Path(f"{path}-wal").stat().st_size, 0)
            naive = pathlib.Path(tmp) / "naive.sqlite"
            _shutil.copy2(path, naive)  # exactly what the old backup did
            conn = sqlite3.connect(f"file:{naive}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT count(*) FROM lane_lifecycle_records"
                ).fetchone()[0]
            except sqlite3.DatabaseError:
                rows = -1  # not even readable
            finally:
                conn.close()
            self.assertNotEqual(rows, 50, "the main-file copy unexpectedly kept every row")
            conn.close()

    def test_the_snapshot_preserves_uncheckpointed_rows_and_version(self) -> None:
        from mozyo_bridge.core.state.lane_lifecycle_backup import backup_state_container

        with tempfile.TemporaryDirectory() as tmp:
            path, conn = self._wal_store(tmp)
            backup_dir = backup_state_container(path)
            self.assertIsNotNone(backup_dir)
            snap = sqlite3.connect(f"file:{backup_dir / 'state.sqlite'}?mode=ro", uri=True)
            try:
                self.assertEqual(
                    snap.execute(
                        "SELECT count(*) FROM lane_lifecycle_records"
                    ).fetchone()[0],
                    50,
                )
                self.assertEqual(snap.execute("PRAGMA user_version").fetchone()[0], 9)
            finally:
                snap.close()
                conn.close()

    def test_a_failed_snapshot_publishes_nothing_and_leaves_the_store_untouched(
        self,
    ) -> None:
        # Injected failure at each stage: no snapshot is published under the backup name,
        # no staging residue is left, and the source store is byte-identical. A partial
        # backup published under the real name would be worse than none — a later operator
        # would restore from it believing it complete.
        from mozyo_bridge.core.state import lane_lifecycle_backup as schema

        for stage in ("_snapshot_state_container", "_readback_state_container"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                path, conn = self._wal_store(tmp)
                before = path.read_bytes()

                def _boom(*_a, **_k):
                    raise sqlite3.DatabaseError("injected")

                with mock.patch.object(schema, stage, _boom):
                    with self.assertRaises(schema.StateStoreError):
                        schema.backup_state_container(path)

                self.assertEqual(path.read_bytes(), before)  # source untouched
                backups = pathlib.Path(tmp) / "backups"
                published = list(backups.glob("state-*")) if backups.exists() else []
                staging = list(backups.glob(".staging-*")) if backups.exists() else []
                self.assertEqual(published, [], "a partial backup was published")
                self.assertEqual(staging, [], "staging residue was left behind")
                conn.close()

    def test_a_backup_failure_is_never_retried_as_a_raw_copy(self) -> None:
        # If SQLite cannot back a database up, the honest conclusion is that no verified
        # recovery point exists. Falling back to the copy that loses WAL pages would
        # manufacture one — the failure mode this whole finding is about.
        source = pathlib.Path(
            ROOT.parent / "src/mozyo_bridge/core/state/lane_lifecycle_backup.py"
        ).read_text()
        snapshot = source[source.index("def _snapshot_state_container") :]
        snapshot = snapshot[: snapshot.index("def _readback_state_container")]
        # Strip the docstring first: it NAMES `copy2` to explain why it is wrong, and a
        # substring check over the whole function would match that explanation and pass
        # whatever the body did.
        body = snapshot.split('"""')[-1]
        self.assertNotIn("copy2", body)
        self.assertIn("src.backup(dst)", snapshot)


class R11ReviewFindingsStayClosed(unittest.TestCase):
    """Redmine #14756 review j#96971 / j#96973 — R11-F6..F10, each reproduced first.

    Four of the five are the same shape as findings already closed on this issue: a value
    that could not have been produced honestly was normalised into one that looked like it
    could. F10 is different and worse — it was introduced BY the bound added for F2, so the
    fix for one forgery hole created a self-corruption hole.
    """

    # -- F6: the backup readback compared counts, not content --------------------

    def test_a_tampered_snapshot_is_not_published_as_a_backup(self) -> None:
        from mozyo_bridge.core.state import lane_lifecycle_backup as backup

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "state.sqlite"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE lane_lifecycle_records (lane_id TEXT)")
            conn.execute("INSERT INTO lane_lifecycle_records VALUES ('source-value')")
            conn.execute("PRAGMA user_version=9")
            conn.commit()
            before = path.read_bytes()

            real = backup._snapshot_state_container

            def _tamper(source, target):
                # Same schema, same version, same row COUNT — only the cell differs. This is
                # exactly what the count-only readback published (j#96971 R11-F6).
                real(source, target)
                edit = sqlite3.connect(target)
                edit.execute("UPDATE lane_lifecycle_records SET lane_id='different-value'")
                edit.commit()
                edit.close()

            with mock.patch.object(backup, "_snapshot_state_container", _tamper):
                with self.assertRaises(backup.StateStoreError):
                    backup.backup_state_container(path)

            backups = pathlib.Path(tmp) / "backups"
            self.assertEqual(list(backups.glob("state-*")), [])
            self.assertEqual(list(backups.glob(".staging-*")), [])
            self.assertEqual(path.read_bytes(), before)
            conn.close()

    def test_a_publish_stage_failure_leaves_no_backup_and_no_residue(self) -> None:
        # j#96971 named the PUBLISH stage specifically; the earlier tests only injected at
        # snapshot and readback.
        from mozyo_bridge.core.state import lane_lifecycle_backup as backup

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "state.sqlite"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE lane_lifecycle_records (lane_id TEXT)")
            conn.execute("PRAGMA user_version=9")
            conn.commit()
            before = path.read_bytes()

            original_rename = pathlib.Path.rename

            def _boom(self_path, target):
                if ".staging-" in str(self_path):
                    raise OSError("injected publish failure")
                return original_rename(self_path, target)

            with mock.patch.object(pathlib.Path, "rename", _boom):
                with self.assertRaises(backup.StateStoreError):
                    backup.backup_state_container(path)

            backups = pathlib.Path(tmp) / "backups"
            self.assertEqual(list(backups.glob("state-*")), [])
            self.assertEqual(list(backups.glob(".staging-*")), [])
            self.assertEqual(path.read_bytes(), before)
            conn.close()

    # -- F7: expected/observed compared after strip() ----------------------------

    def test_a_padded_expected_epoch_is_not_agreement(self) -> None:
        for expected in (" 1", "1 ", "01"):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                store, key = _hibernated_lane(tmp)
                record = ForgedEpochsAndUnboundedTokensFailClosed._attest(
                    tmp, expected=expected, observed="1"
                )
                # `_norm`-based comparison called these equal and promoted "1" to authority.
                self.assertEqual(record.lane_epoch, "")
                self.assertEqual(
                    lane_epoch_verdict(store.get(key), record.lane_epoch),
                    (False, EPOCH_ATTESTATION_ABSENT),
                )

    # -- F8: empty assertion dropped before the cardinality check ----------------

    def test_an_empty_or_padded_assertion_slot_is_refused(self) -> None:
        for asserted in (
            ("pair-gateway", "pair-worker", ""),
            ("pair-gateway", "pair-worker", "   "),
            ("pair-gateway", " pair-worker"),
        ):
            with self.subTest(asserted=asserted), tempfile.TemporaryDirectory() as tmp:
                home, store, key, pins, agents = (
                    TheLegacyRecoveryPlanRefusesBeforeItCloses()._ready_world(tmp)
                )
                plan = TheLegacyRecoveryPlanRefusesBeforeItCloses()._plan(
                    home, store, key,
                    TheLegacyRecoveryPlanRefusesBeforeItCloses._view(agents=agents),
                    asserted=asserted,
                )
                self.assertEqual(plan.state, "blocked_target_slot_assertion_failed")

    # -- F10: minting past the canonical bound ----------------------------------

    def test_the_counter_refuses_to_mint_past_its_own_bound(self) -> None:
        from mozyo_bridge.core.state.lane_epoch import (
            EPOCH_STORED_MALFORMED,
            EPOCH_STORED_MINTED,
            classify_stored_epoch,
            lane_epoch_on_transition,
        )

        largest = "9" * 18
        self.assertEqual(classify_stored_epoch(largest)[1], EPOCH_STORED_MINTED)
        # Pre-fix this returned a 19-digit successor that its own classifier then called
        # malformed — the row would have been advanced into a permanently unreadable epoch.
        self.assertIsNone(
            lane_epoch_on_transition(
                largest, target="hibernated", hibernated="hibernated"
            )
        )
        self.assertEqual(
            classify_stored_epoch("1" + "0" * 18)[1], EPOCH_STORED_MALFORMED
        )

    def test_a_lane_at_the_bound_takes_a_zero_write_refusal_on_the_next_mint(self) -> None:
        # Only the MINTING direction can overflow: a rehydrate carries the counter across
        # unchanged, so it stays canonical and must still be allowed. Asserting a refusal
        # there — as the first version of this test did — would have pinned an outage.
        with tempfile.TemporaryDirectory() as tmp:
            store, key = _hibernated_lane(tmp)
            rec = store.get(key)
            store.transition_disposition(
                key, expected_disposition=DISPOSITION_HIBERNATED,
                expected_revision=rec.revision, target=DISPOSITION_ACTIVE,
                decision=_decision(),
            )
            conn = sqlite3.connect(store.path)
            try:
                conn.execute(
                    "UPDATE lane_lifecycle_records SET lane_epoch = ?", ("9" * 18,)
                )
                conn.commit()
            finally:
                conn.close()
            before = store.get(key)
            outcome = store.transition_disposition(
                key,
                expected_disposition=DISPOSITION_ACTIVE,
                expected_revision=before.revision,
                target=DISPOSITION_HIBERNATED,  # the mint that would overflow
                decision=_decision(),
            )
            after = store.get(key)
            self.assertFalse(outcome.applied)
            self.assertEqual(after.revision, before.revision)
            self.assertEqual(after.lane_disposition, before.lane_disposition)
            self.assertEqual(after.lane_epoch, "9" * 18)  # byte-preserved, not advanced

    # -- F9: the docs stopped describing a contract the code does not have -------

    def test_no_doc_or_docstring_still_describes_the_superseded_contract(self) -> None:
        """Bans AND positive assertions (Redmine #14756 review j#96988 R12-F13).

        The first version of this test banned three literal strings and passed while several
        real stale variants sat untouched — `exact int 0`, the malformed-set spelled as
        "TEXT ... malformed", and a backup section still calling the lifecycle sibling a
        file copy. A ban list only closes the phrasings its author happened to think of, so
        each contract is ALSO asserted positively: if the docs stop saying the true thing,
        that fails too, whatever wording replaced it.
        """
        docs = {
            path.name: pathlib.Path(path).read_text()
            for path in (
                ROOT.parent / "src/mozyo_bridge/core/state/lane_epoch.py",
                ROOT.parent / "src/mozyo_bridge/core/state/lane_lifecycle_model.py",
                ROOT.parent / "vibes/docs/logics/managed-state-model.md",
                ROOT.parent / "vibes/docs/specs/herdr-native-identity.md",
            )
        }
        banned = (
            "at least the required",
            "現行 `lane_epoch` 以上",
            "backup は file copy",
            "exact int `0`",
            "lane_epoch = lane_epoch + 1` として",
            "`lane_lifecycle_schema.backup_state_container` and `state_store._backup` still use",
        )
        for name, text in docs.items():
            for phrase in banned:
                with self.subTest(doc=name, banned=phrase):
                    self.assertNotIn(phrase, text)

        # The contract must still be STATED, not merely un-contradicted.
        model = docs["managed-state-model.md"]
        self.assertIn("canonical decimal TEXT", model)
        self.assertIn("NONE affinity", model)
        identity = docs["herdr-native-identity.md"]
        self.assertIn("完全一致", identity)  # admission is equality, not a lower bound
        self.assertIn("canonical decimal TEXT", identity)

        # ...and it must be stated WHERE the malformed set is listed (review j#96997 F14).
        # A whole-file positive check is satisfied by the contract appearing anywhere, so it
        # passed while the very paragraph defining "malformed" listed bare `TEXT` — i.e. said
        # the exact opposite of the storage contract two lines above it. Scoping the check to
        # the section is what makes a self-contradiction detectable: an assertion whose
        # evidence can come from somewhere else cannot see a local disagreement.
        section = _lane_epoch_storage_section(model)
        self.assertIn("canonical decimal TEXT は valid", section)
        self.assertNotIn("malformed な格納値 (TEXT /", section)
        # The malformed set names noncanonical TEXT specifically, never TEXT wholesale.
        malformed_line = next(
            line for line in section.splitlines() if "malformed とは" in line
        )
        self.assertIn("canonical decimal TEXT 以外", malformed_line)


class R12ReviewFindingsStayClosed(unittest.TestCase):
    """Redmine #14756 review j#96988 — R12-F11..F13, each reproduced first.

    F11 is the third time on this issue that a fence I added to close one hole opened
    another: the raw byte-exact comparison from R11-F7 also started filing a failure event
    against every legitimate legacy boot.
    """

    def test_a_true_legacy_launch_records_no_failure_event(self) -> None:
        """R12-F11: both sides absent is AGREEMENT, not a failed injection.

        Conditional C (j#96844 rule 1) keeps the true legacy `lane_epoch=0` path supported.
        Such a launch declares no epoch and receives none, so the diagnostic projection must
        stay clean — this fence exists to make disagreement visible, not to invent it.
        """
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            herdr_agent_attest as attest,
        )

        with tempfile.TemporaryDirectory() as tmp:
            seen: list = []
            record = attest.perform_self_attestation(
                assigned_name="n", workspace_id=WS, role="claude", lane=LANE,
                env={"MOZYO_HERDR_BINARY": "/usr/bin/true"},  # no epoch injected
                lane_epoch="",                                 # and none declared
                home=Path(tmp),
                append_event=lambda stage, bounded_reason="": seen.append(
                    (stage, bounded_reason)
                ),
                runner=lambda *a, **k: type(
                    "R", (), {
                        "returncode": 0,
                        "stdout": '{"agents":[{"name":"n","pane_id":"%1"}]}',
                    }
                )(),
            )
            self.assertIsNotNone(record)
            self.assertEqual(record.lane_epoch, "")
            reasons = [reason for _stage, reason in seen]
            self.assertNotIn("lane_epoch_not_injected", reasons)

    def test_one_sided_or_noncanonical_agreement_still_fails(self) -> None:
        # The converse, so the fix above cannot have simply disabled the fence.
        for expected, observed in (("1", None), ("", "1"), ("01", "01")):
            with self.subTest(expected=expected, observed=observed):
                with tempfile.TemporaryDirectory() as tmp:
                    record = ForgedEpochsAndUnboundedTokensFailClosed._attest(
                        tmp, expected=expected, observed=observed
                    )
                    self.assertEqual(record.lane_epoch, "")

    def test_a_tampered_autoincrement_sequence_is_not_published(self) -> None:
        """R12-F12: `sqlite_sequence` is internal but not derivable.

        Measured before the fix: with user rows, schema and version identical, rewriting the
        snapshot's `seq` from 1 to 999 published — and the published snapshot's next insert
        took id 1000 where the source's would have taken 2. A backup that hands out IDs the
        source would never issue is not an exact recovery point.
        """
        from mozyo_bridge.core.state import lane_lifecycle_backup as backup

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "state.sqlite"
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE lane_lifecycle_records "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, lane_id TEXT)"
            )
            conn.execute("INSERT INTO lane_lifecycle_records (lane_id) VALUES ('a')")
            conn.execute("PRAGMA user_version=9")
            conn.commit()
            before = path.read_bytes()

            real = backup._snapshot_state_container

            def _tamper(source, target):
                real(source, target)
                edit = sqlite3.connect(target)
                edit.execute("UPDATE sqlite_sequence SET seq=999")
                edit.commit()
                edit.close()

            with mock.patch.object(backup, "_snapshot_state_container", _tamper):
                with self.assertRaises(backup.StateStoreError):
                    backup.backup_state_container(path)

            backups = pathlib.Path(tmp) / "backups"
            self.assertEqual(list(backups.glob("state-*")), [])
            self.assertEqual(list(backups.glob(".staging-*")), [])
            self.assertEqual(path.read_bytes(), before)
            conn.close()

    def test_an_untampered_autoincrement_store_still_backs_up(self) -> None:
        # Including `sqlite_sequence` must not make every AUTOINCREMENT store unbackupable.
        from mozyo_bridge.core.state import lane_lifecycle_backup as backup

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "state.sqlite"
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE lane_lifecycle_records "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, lane_id TEXT)"
            )
            conn.execute("INSERT INTO lane_lifecycle_records (lane_id) VALUES ('a')")
            conn.commit()
            self.assertIsNotNone(backup.backup_state_container(path))
            conn.close()

    def test_identifiers_are_quoted_rather_than_interpolated_bare(self) -> None:
        # The digest interpolates table and column names (identifiers cannot be bound), so a
        # reserved word or a quote in a name must not change the statement's shape.
        from mozyo_bridge.core.state.lane_lifecycle_backup import _quote_identifier

        self.assertEqual(_quote_identifier("order"), '"order"')
        self.assertEqual(_quote_identifier('we"ird'), '"we""ird"')


class ExistingFencesAreNotWeakened(unittest.TestCase):
    """Acceptance 4 / R1 scope 4: the epoch is a conjunct, never a replacement."""

    def test_the_epoch_conjunct_is_opt_in_and_defaults_to_the_pre_14756_verdict(
        self,
    ) -> None:
        # `epoch_record=None` (supersede's fresh recovery lane) must behave exactly as it
        # did before, so adding the conjunct cannot change an unrelated caller's outcome.
        import inspect

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_process_release import (  # noqa: E501
            evaluate_pair_attestation,
        )

        params = inspect.signature(evaluate_pair_attestation).parameters
        self.assertIsNone(params["epoch_record"].default)

    def test_resume_passes_the_lifecycle_row_as_the_epoch_authority(self) -> None:
        # A conjunct nobody supplies is not a fence. This pins that the resume use case
        # actually wires the row through (the #14477 R9 lesson: fixing the domain proves
        # nothing until the boundary uses it).
        source = Path(
            ROOT.parent
            / "src/mozyo_bridge/e_110_execution_platform"
            / "f_140_delegated_coordinator_nested_handoff/application/sublane_resume.py"
        ).read_text()
        self.assertIn("epoch_record=rec", source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
