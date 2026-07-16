"""Tests for the herdr startup self-attestation store (Redmine #13637).

Covers the pure classifier (present / missing / conflict), the generation-bound
read-side join (attested / absent / stale / missing / conflict), the home-scoped
store round-trip + snapshot-replace, the fail-open reads, and the privacy invariant
that no env VALUE / secret is ever persisted (only tokens, identity segments, a
locator, and a variable NAME in ``detail``).
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.herdr_identity_attestation import (
    ATTEST_ABSENT,
    ATTEST_CONFLICT,
    ATTEST_MISSING,
    ATTEST_OK,
    ATTEST_STALE,
    HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
    VERDICT_CONFLICT,
    VERDICT_MISSING,
    VERDICT_PRESENT,
    classify_identity_env,
    evaluate_attestation,
    herdr_identity_attestation_path,
    record_identity_attestation,
)


def _rec(**over) -> IdentityAttestationRecord:
    base = dict(
        assigned_name="mzb1_ws1_claude_default",
        workspace_id="ws1",
        role="claude",
        lane_id="default",
        locator="wY:p2",
        verdict=VERDICT_PRESENT,
    )
    base.update(over)
    return IdentityAttestationRecord(**base)


class ClassifyIdentityEnvTest(unittest.TestCase):
    def test_all_present_matching_is_present(self) -> None:
        verdict, detail = classify_identity_env(
            expected_workspace_id="ws1",
            expected_role="claude",
            expected_lane="",  # normalises to default
            env={
                "MOZYO_WORKSPACE_ID": "ws1",
                "MOZYO_AGENT_ROLE": "claude",
                "MOZYO_LANE_ID": "default",
            },
        )
        self.assertEqual(verdict, VERDICT_PRESENT)
        self.assertEqual(detail, "")

    def test_absent_vars_are_missing_and_detail_names_variables_not_values(self) -> None:
        verdict, detail = classify_identity_env(
            expected_workspace_id="ws1",
            expected_role="claude",
            expected_lane="lane-1",
            env={"MOZYO_HERDR_BINARY": "/x/herdr"},  # triplet absent
        )
        self.assertEqual(verdict, VERDICT_MISSING)
        # detail names the missing VARIABLES (workspace + role — an absent lane
        # defaults to `default` per spec §2, so it is not itself a missing var),
        # never a value.
        self.assertIn("MOZYO_WORKSPACE_ID", detail)
        self.assertIn("MOZYO_AGENT_ROLE", detail)
        self.assertNotIn("lane-1", detail)
        self.assertNotIn("ws1", detail)

    def test_mismatching_value_is_conflict(self) -> None:
        verdict, detail = classify_identity_env(
            expected_workspace_id="ws1",
            expected_role="claude",
            expected_lane="default",
            env={
                "MOZYO_WORKSPACE_ID": "wsOTHER",
                "MOZYO_AGENT_ROLE": "claude",
                "MOZYO_LANE_ID": "default",
            },
        )
        self.assertEqual(verdict, VERDICT_CONFLICT)
        self.assertEqual(detail, "MOZYO_WORKSPACE_ID")
        self.assertNotIn("wsOTHER", detail)  # never a value

    def test_missing_takes_precedence_over_conflict(self) -> None:
        # role missing AND workspace mismatching -> reported as missing (env-less boot
        # is never masked by also mismatching).
        verdict, _ = classify_identity_env(
            expected_workspace_id="ws1",
            expected_role="claude",
            expected_lane="default",
            env={"MOZYO_WORKSPACE_ID": "wsOTHER", "MOZYO_LANE_ID": "default"},
        )
        self.assertEqual(verdict, VERDICT_MISSING)


class EvaluateAttestationTest(unittest.TestCase):
    def test_present_generation_matched_is_attested(self) -> None:
        join = evaluate_attestation(
            _rec(),
            live_locator="wY:p2",
            expected_workspace_id="ws1",
            expected_role="claude",
            expected_lane="default",
        )
        self.assertTrue(join.ok)
        self.assertEqual(join.state, ATTEST_OK)

    def test_no_record_is_absent(self) -> None:
        join = evaluate_attestation(
            None,
            live_locator="wY:p2",
            expected_workspace_id="ws1",
            expected_role="claude",
            expected_lane="default",
        )
        self.assertFalse(join.ok)
        self.assertEqual(join.state, ATTEST_ABSENT)

    def test_locator_moved_is_stale(self) -> None:
        # A present record from an earlier generation whose live locator moved is
        # never re-used as this process's attestation.
        join = evaluate_attestation(
            _rec(locator="wY:p2"),
            live_locator="wY:p9",
            expected_workspace_id="ws1",
            expected_role="claude",
            expected_lane="default",
        )
        self.assertFalse(join.ok)
        self.assertEqual(join.state, ATTEST_STALE)

    def test_empty_recorded_locator_is_stale(self) -> None:
        join = evaluate_attestation(
            _rec(locator=""),
            live_locator="wY:p2",
            expected_workspace_id="ws1",
            expected_role="claude",
            expected_lane="default",
        )
        self.assertEqual(join.state, ATTEST_STALE)

    def test_missing_verdict_record_is_missing(self) -> None:
        join = evaluate_attestation(
            _rec(verdict=VERDICT_MISSING),
            live_locator="wY:p2",
            expected_workspace_id="ws1",
            expected_role="claude",
            expected_lane="default",
        )
        self.assertEqual(join.state, ATTEST_MISSING)

    def test_conflict_verdict_record_is_conflict(self) -> None:
        join = evaluate_attestation(
            _rec(verdict=VERDICT_CONFLICT),
            live_locator="wY:p2",
            expected_workspace_id="ws1",
            expected_role="claude",
            expected_lane="default",
        )
        self.assertEqual(join.state, ATTEST_CONFLICT)

    def test_identity_drift_record_is_conflict(self) -> None:
        # A record whose recorded identity does not match the queried slot is a foreign
        # record and is never trusted (checked before locator, so it is conflict).
        join = evaluate_attestation(
            _rec(role="codex"),
            live_locator="wY:p2",
            expected_workspace_id="ws1",
            expected_role="claude",
            expected_lane="default",
        )
        self.assertFalse(join.ok)
        self.assertEqual(join.state, ATTEST_CONFLICT)


class StoreRoundTripTest(unittest.TestCase):
    def test_upsert_read_round_trip_stamps_observed_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            store = HerdrIdentityAttestationStore(home=home)
            persisted = store.upsert(_rec())
            self.assertTrue(persisted.observed_at)
            got = store.read("mzb1_ws1_claude_default")
            self.assertEqual(got.locator, "wY:p2")
            self.assertEqual(got.verdict, VERDICT_PRESENT)

    def test_upsert_replaces_prior_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = HerdrIdentityAttestationStore(home=Path(tmp))
            store.upsert(_rec(locator="wY:p2", verdict=VERDICT_PRESENT))
            store.upsert(_rec(locator="wZ:p5", verdict=VERDICT_MISSING))
            got = store.read("mzb1_ws1_claude_default")
            # one row per assigned name — the latest generation replaced the prior
            self.assertEqual(got.locator, "wZ:p5")
            self.assertEqual(got.verdict, VERDICT_MISSING)

    def test_read_absent_file_and_unknown_name_are_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = HerdrIdentityAttestationStore(home=Path(tmp))
            self.assertIsNone(store.read("mzb1_ws1_claude_default"))  # no file yet
            store.upsert(_rec())
            self.assertIsNone(store.read("mzb1_ws1_codex_default"))  # unknown name

    def test_no_env_value_column_exists(self) -> None:
        # Privacy invariant (refinement 3): the schema stores tokens / identity /
        # locator / a variable-name detail only — never an env value column.
        # ``replacement_action_id`` (Redmine #13806 tranche D R2-F2) is a token id, not an
        # env value, so it belongs to the token-only set.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            record_identity_attestation(_rec(), home=home)
            path = herdr_identity_attestation_path(home)
            conn = sqlite3.connect(str(path))
            try:
                cols = {
                    r[1]
                    for r in conn.execute(
                        "PRAGMA table_info(herdr_identity_attestations)"
                    )
                }
            finally:
                conn.close()
            self.assertEqual(
                cols,
                {
                    "assigned_name",
                    "workspace_id",
                    "role",
                    "lane_id",
                    "locator",
                    "verdict",
                    "detail",
                    "observed_at",
                    "replacement_action_id",
                },
            )

    def test_schema_version_stamped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            record_identity_attestation(_rec(), home=home)
            conn = sqlite3.connect(str(herdr_identity_attestation_path(home)))
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(version, HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION)

    def test_replacement_action_id_round_trips(self) -> None:
        # Redmine #13806 tranche D R2-F2: the additive replacement action id persists.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            store = HerdrIdentityAttestationStore(home=home)
            store.upsert(_rec(replacement_action_id="recover:l:worker:claude:wk:w2"))
            back = store.read("mzb1_ws1_claude_default")
            self.assertEqual(back.replacement_action_id, "recover:l:worker:claude:wk:w2")

    def test_normal_launch_leaves_action_id_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = HerdrIdentityAttestationStore(home=Path(tmp))
            store.upsert(_rec())  # no replacement_action_id
            self.assertEqual(store.read("mzb1_ws1_claude_default").replacement_action_id, "")

    def test_old_schema_version_is_fail_closed(self) -> None:
        # An older (v1) attestation file is rejected fail-closed: the read returns None
        # (adopt / doctor then fail closed) rather than decoding a record missing the field.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = herdr_identity_attestation_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path))
            try:
                conn.execute("PRAGMA user_version = 1")  # a stale pre-#13806 schema
                conn.execute(
                    "CREATE TABLE herdr_identity_attestations ("
                    "assigned_name TEXT PRIMARY KEY, workspace_id TEXT, role TEXT, "
                    "lane_id TEXT, locator TEXT, verdict TEXT, detail TEXT, observed_at TEXT)"
                )
                conn.execute(
                    "INSERT INTO herdr_identity_attestations VALUES "
                    "('mzb1_ws1_claude_default','ws1','claude','default','wY:p2','present','','t')"
                )
                conn.commit()
            finally:
                conn.close()
            self.assertIsNone(
                HerdrIdentityAttestationStore(home=home).read("mzb1_ws1_claude_default")
            )

    def test_record_best_effort_never_raises_on_unwritable_home(self) -> None:
        # A store failure must never block an agent boot: it degrades to None.
        with tempfile.TemporaryDirectory() as tmp:
            # Point home at a path whose parent is a FILE, so mkdir/connect fails.
            blocker = Path(tmp) / "afile"
            blocker.write_text("x", encoding="utf-8")
            result = record_identity_attestation(_rec(), home=blocker / "sub")
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
