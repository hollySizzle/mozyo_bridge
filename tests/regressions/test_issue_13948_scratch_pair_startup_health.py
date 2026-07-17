"""Redmine #13948 — session-start must not report a dead launch as a success.

The live defect (#13882 j#80951 / j#80968, reproduced twice on a clean repo with the same
installed runtime): `herdr session-start` reported Claude and Codex both ``launched`` and
exited 0, while Claude had already exec'd and left. The immediately-following dry-run saw
``stale_named_slot`` / shell residue for Claude and a live Codex — a partial pair that
``session-retire`` then refused to converge without an owner approval.

These regressions pin the contract from Design Answer j#80989 (+ j#80991 reconciliation):

1. success means *observed*, per role — live at the locator we launched, startup screen
   clear, and locator-matched self-attestation — never "the launcher accepted the start";
2. the cause is *named* on its own axis (trust interaction / provider exit / shell residue
   / attestation timeout / mismatch), never collapsed into one token;
3. a failed launch owes a compensation that ONLY the explicit public rollback rail may
   act on — session-start closes nothing itself.

The classifier tests below drive :func:`classify_startup_health` directly because it is
the whole decision. The precedence between the axes is not incidental: it is what makes
the report name the *actionable* cause rather than the first one noticed.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_rollback import (  # noqa: E501
    COMPOSER_EMPTY,
    COMPOSER_PENDING,
    COMPOSER_STARTUP_BLOCKER,
    COMPOSER_UNREADABLE,
    ROLLBACK_ABSENT,
    ROLLBACK_AGENT_BUSY,
    ROLLBACK_ALREADY_CLOSED,
    ROLLBACK_AMBIGUOUS,
    ROLLBACK_CLOSE_TARGETS,
    ROLLBACK_COMPOSER_UNREADABLE,
    ROLLBACK_SETTLED,
    ROLLBACK_DETAIL,
    ROLLBACK_ELIGIBLE,
    ROLLBACK_IDENTITY_DRIFT,
    ROLLBACK_INVENTORY_UNREADABLE,
    ROLLBACK_OBLIGATION_UNREADABLE,
    ROLLBACK_PENDING_INPUT,
    ROLLBACK_VERDICTS,
    ROLLBACK_WORK_OBLIGATION,
    ParticipantFacts,
    classify_rollback,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback import (  # noqa: E501
    REASON_ACTION_UNKNOWN,
    REASON_ALREADY_ROLLED_BACK,
    REASON_AUTHORITY_UNAVAILABLE,
    REASON_BLOCKED,
    REASON_INCOMPLETE,
    REASON_NOTHING_OWED,
    REASON_OK,
    REASON_PREFLIGHT,
    run_session_rollback,
)
from mozyo_bridge.core.state.startup_transaction_fence import (  # noqa: E501
    PHASE_COMPLETED_ROLLED_BACK,
    PHASE_COMPLETED_SUCCESS,
    PHASE_HEALTH_CHECK,
    PHASE_LAUNCHING,
    PHASE_PLANNED,
    PHASE_ROLLBACK_OWED,
    STARTUP_TRANSACTION_FENCE_SCHEMA_VERSION,
    STORE_ABSENT,
    STORE_DAMAGED,
    Participant,
    StartupTransactionBusy,
    StartupTransactionError,
    StartupTransactionFence,
    StartupUnit,
    startup_action_id,
)

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_health import (  # noqa: E501
    ATTESTATION_ABSENT,
    ATTESTATION_INVALID,
    ATTESTATION_NOT_PROBED,
    ATTESTATION_OK,
    COMPENSATION_NOT_NEEDED,
    COMPENSATION_ROLLBACK_OWED,
    DISPOSITION_ADOPTED,
    DISPOSITION_FRESH_LAUNCHED,
    DISPOSITION_SURFACED,
    HEALTH_ATTESTATION_MISMATCH,
    HEALTH_ATTESTATION_TIMEOUT,
    HEALTH_ATTESTATION_UNAVAILABLE,
    HEALTH_DETAIL,
    HEALTH_HEALTHY,
    HEALTH_INVENTORY_UNREADABLE,
    HEALTH_LOCATOR_DRIFT,
    HEALTH_OUTCOMES,
    HEALTH_PROVIDER_EXITED,
    HEALTH_RECEIVER_UNREADABLE,
    HEALTH_SHELL_RESIDUE,
    HEALTH_STARTUP_INTERACTION,
    HEALTH_UNPROFILED_PROVIDER,
    SCREEN_BLOCKED,
    SCREEN_CLEAR,
    SCREEN_NOT_PROBED,
    SCREEN_UNPROFILED,
    SCREEN_UNREADABLE,
    SlotHealth,
    StartupHealthError,
    classify_startup_health,
)


def _facts(**over):
    """The all-positive (healthy) fact set; each test negates exactly one thing."""
    base = dict(
        inventory_readable=True,
        row_present=True,
        row_stale=False,
        live_locator="w2G:p3",
        launched_locator="w2G:p3",
        screen=SCREEN_CLEAR,
        attestation=ATTESTATION_OK,
    )
    base.update(over)
    return base


class ClassifyStartupHealthTest(unittest.TestCase):
    def test_all_positive_is_the_only_healthy_path(self):
        self.assertEqual(classify_startup_health(**_facts()), HEALTH_HEALTHY)

    def test_unreadable_inventory_never_decays_to_healthy(self):
        # #13845 discipline: absence of a liveness proof is not proof of liveness. An
        # unreadable inventory outranks every other fact because it can prove none of them.
        self.assertEqual(
            classify_startup_health(**_facts(inventory_readable=False)),
            HEALTH_INVENTORY_UNREADABLE,
        )

    def test_launched_locator_gone_is_provider_exited(self):
        # The live #13882 Claude shape: `agent start` returned a locator, and by the time
        # anyone looked, nothing was there.
        self.assertEqual(
            classify_startup_health(**_facts(row_present=False)),
            HEALTH_PROVIDER_EXITED,
        )

    def test_positive_residue_is_shell_residue_not_provider_exited(self):
        # The other live #13882 shape: the name survives, the agent does not. These are
        # different operator situations (nothing there vs. a dead pane to reclaim) and
        # must not be reported as one.
        self.assertEqual(
            classify_startup_health(**_facts(row_stale=True)),
            HEALTH_SHELL_RESIDUE,
        )

    def test_name_resolving_elsewhere_is_locator_drift(self):
        self.assertEqual(
            classify_startup_health(**_facts(live_locator="w2G:p9")),
            HEALTH_LOCATOR_DRIFT,
        )

    def test_blank_live_locator_is_drift_not_healthy(self):
        # A row with no locator (or an ambiguous duplicate, which the probe collapses to
        # a blank locator) is unusable — never silently accepted as ours.
        self.assertEqual(
            classify_startup_health(**_facts(live_locator="")),
            HEALTH_LOCATOR_DRIFT,
        )

    def test_trust_screen_is_named_startup_interaction(self):
        self.assertEqual(
            classify_startup_health(**_facts(screen=SCREEN_BLOCKED)),
            HEALTH_STARTUP_INTERACTION,
        )

    def test_unreadable_pane_is_not_startup_clear(self):
        self.assertEqual(
            classify_startup_health(**_facts(screen=SCREEN_UNREADABLE)),
            HEALTH_RECEIVER_UNREADABLE,
        )

    def test_unprofiled_provider_is_never_guessed_clear(self):
        self.assertEqual(
            classify_startup_health(**_facts(screen=SCREEN_UNPROFILED)),
            HEALTH_UNPROFILED_PROVIDER,
        )

    def test_unclassified_visible_state_fails_closed(self):
        # Answer j#80989 Q1.6: an unclassified visible state is never admitted. A screen
        # fact the caller never established must not fall through to "clear".
        for screen in (SCREEN_NOT_PROBED, "something-new"):
            with self.subTest(screen=screen):
                self.assertEqual(
                    classify_startup_health(**_facts(screen=screen)),
                    HEALTH_RECEIVER_UNREADABLE,
                )

    def test_absent_record_is_timeout_and_invalid_record_is_mismatch(self):
        # Distinct causes with distinct fixes: "nothing arrived" vs "something arrived and
        # does not bind to this generation".
        self.assertEqual(
            classify_startup_health(**_facts(attestation=ATTESTATION_ABSENT)),
            HEALTH_ATTESTATION_TIMEOUT,
        )
        self.assertEqual(
            classify_startup_health(**_facts(attestation=ATTESTATION_INVALID)),
            HEALTH_ATTESTATION_MISMATCH,
        )

    def test_unwrapped_launch_is_unavailable_not_timeout(self):
        # An unwrapped launch (#13637 fallback: no `mozyo-bridge` on the launch PATH) can
        # never produce a record. Calling that a *timeout* would tell the operator to wait
        # for something that is not coming.
        self.assertEqual(
            classify_startup_health(**_facts(attestation=ATTESTATION_NOT_PROBED)),
            HEALTH_ATTESTATION_UNAVAILABLE,
        )

    def test_unrecognised_attestation_fact_is_never_healthy(self):
        self.assertEqual(
            classify_startup_health(**_facts(attestation="brand-new")),
            HEALTH_ATTESTATION_UNAVAILABLE,
        )

    def test_process_facts_outrank_screen_and_attestation(self):
        # A dead slot is dead regardless of what a screen/attestation read would have
        # said: reporting `attestation_timeout` for a pane with no process names the
        # wrong cause and sends the operator to the wrong fix.
        self.assertEqual(
            classify_startup_health(
                **_facts(
                    row_present=False,
                    screen=SCREEN_BLOCKED,
                    attestation=ATTESTATION_ABSENT,
                )
            ),
            HEALTH_PROVIDER_EXITED,
        )

    def test_screen_outranks_attestation(self):
        # The wrapper writes its record BEFORE exec (#13637), so a trust-screened agent
        # still has a valid record; and a trust-screened agent that somehow lacks one is
        # still, actionably, a trust screen.
        self.assertEqual(
            classify_startup_health(
                **_facts(screen=SCREEN_BLOCKED, attestation=ATTESTATION_ABSENT)
            ),
            HEALTH_STARTUP_INTERACTION,
        )

    def test_every_health_token_has_an_operator_detail(self):
        # A named cause with no sentence is a token the operator cannot act on.
        for token in HEALTH_OUTCOMES:
            with self.subTest(token=token):
                self.assertTrue(HEALTH_DETAIL.get(token, "").strip(), token)


class SlotHealthContractTest(unittest.TestCase):
    def _health(self, **over):
        base = dict(
            provider="claude",
            assigned_name="mzb1_ws_claude_lane",
            disposition=DISPOSITION_FRESH_LAUNCHED,
            health=HEALTH_HEALTHY,
        )
        base.update(over)
        return SlotHealth(**base)

    def test_only_healthy_reads_healthy(self):
        self.assertTrue(self._health().healthy)
        self.assertFalse(
            self._health(health=HEALTH_PROVIDER_EXITED).healthy
        )

    def test_unknown_tokens_fail_closed(self):
        with self.assertRaises(StartupHealthError):
            self._health(health="fine-probably")
        with self.assertRaises(StartupHealthError):
            self._health(disposition="sort-of-launched")
        with self.assertRaises(StartupHealthError):
            self._health(compensation="undone-ish")

    def test_startup_interaction_must_name_its_blocker(self):
        # The blocker id is the ONLY thing about a startup screen that may leave the pane
        # (#13760 j#77947 invariant 3) — and a blocked verdict without one is unactionable.
        with self.assertRaises(StartupHealthError):
            self._health(health=HEALTH_STARTUP_INTERACTION)
        ok = self._health(
            health=HEALTH_STARTUP_INTERACTION, blocker_id="workspace_trust_confirmation"
        )
        self.assertEqual(ok.blocker_id, "workspace_trust_confirmation")

    def test_a_blocker_id_may_not_ride_any_other_verdict(self):
        with self.assertRaises(StartupHealthError):
            self._health(health=HEALTH_HEALTHY, blocker_id="workspace_trust_confirmation")

    def test_only_a_fresh_launch_can_owe_a_compensation(self):
        # Answer j#80989 Q1.2 / j#80991: what this action did not start is never this
        # action's to undo. An adopted or surfaced slot carrying `rollback_owed` would be
        # a standing invitation for the rollback rail to close somebody else's agent.
        for disposition in (DISPOSITION_ADOPTED, DISPOSITION_SURFACED):
            with self.subTest(disposition=disposition):
                with self.assertRaises(StartupHealthError):
                    self._health(
                        disposition=disposition,
                        health=HEALTH_PROVIDER_EXITED,
                        compensation=COMPENSATION_ROLLBACK_OWED,
                    )
        owed = self._health(
            disposition=DISPOSITION_FRESH_LAUNCHED,
            health=HEALTH_PROVIDER_EXITED,
            compensation=COMPENSATION_ROLLBACK_OWED,
        )
        self.assertEqual(owed.compensation, COMPENSATION_ROLLBACK_OWED)

    def test_payload_carries_every_axis_and_no_pane_content(self):
        payload = self._health(
            health=HEALTH_STARTUP_INTERACTION, blocker_id="login_required"
        ).as_payload()
        self.assertEqual(payload["disposition"], DISPOSITION_FRESH_LAUNCHED)
        self.assertEqual(payload["health"], HEALTH_STARTUP_INTERACTION)
        self.assertEqual(payload["blocker_id"], "login_required")
        self.assertEqual(payload["compensation"], COMPENSATION_NOT_NEEDED)


class StartupActionIdentityTest(unittest.TestCase):
    """The identity that makes a rollback able to say "these panes are mine"."""

    def _unit(self, **over):
        base = dict(workspace_id="ws1", lane_id="lane-1", providers=("claude", "codex"))
        base.update(over)
        return StartupUnit(**base)

    def test_identity_is_stable_for_the_same_invocation(self):
        self.assertEqual(
            startup_action_id(self._unit(), "n1"), startup_action_id(self._unit(), "n1")
        )

    def test_provider_order_is_not_identity_but_membership_is(self):
        self.assertEqual(
            startup_action_id(self._unit(providers=("codex", "claude")), "n1"),
            startup_action_id(self._unit(providers=("claude", "codex")), "n1"),
        )
        self.assertNotEqual(
            startup_action_id(self._unit(providers=("codex",)), "n1"),
            startup_action_id(self._unit(providers=("claude", "codex")), "n1"),
        )

    def test_a_rerun_of_the_same_command_is_a_different_action(self):
        # The crux of "old completion applied to a new pair": without the nonce, the same
        # operator re-running the same command in the same lane would inherit the previous
        # run's record — and a rollback would then close a pane it never started.
        self.assertNotEqual(
            startup_action_id(self._unit(), "n1"), startup_action_id(self._unit(), "n2")
        )

    def test_every_component_is_required(self):
        for unit, nonce in (
            (self._unit(workspace_id=""), "n1"),
            (self._unit(lane_id=""), "n1"),
            (self._unit(providers=()), "n1"),
            (self._unit(), ""),
        ):
            with self.subTest(unit=unit, nonce=nonce):
                with self.assertRaises(ValueError):
                    startup_action_id(unit, nonce)


class StartupTransactionFenceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.fence = StartupTransactionFence(home=self.home)
        self.unit = StartupUnit(
            workspace_id="ws1", lane_id="lane-1", providers=("claude", "codex")
        )

    def _participant(self, role="codex", locator="w2G:p4"):
        return Participant(
            role=role,
            assigned_name=f"mzb1_ws1_{role}_lane-1",
            locator=locator,
            receipt="landed=w2G tab=w2G:t1",
        )

    def test_reserve_bootstraps_and_records_before_any_side_effect(self):
        self.assertEqual(self.fence.store_shape().state, STORE_ABSENT)
        action = self.fence.reserve(self.unit, "n1")
        self.assertEqual(action.phase, PHASE_PLANNED)
        self.assertEqual(action.participants, ())
        self.assertEqual(self.fence.read(action.action_id).phase, PHASE_PLANNED)

    def test_rollback_side_never_bootstraps_an_absent_store(self):
        # The deliberate asymmetry (Answer j#80989 Q3): a reserve mints a NEW identity, so
        # creating the store forgets nothing. A read against an absent store has no proof
        # of anything — it must return "no record", never conjure an authority.
        self.assertIsNone(self.fence.read(startup_action_id(self.unit, "n1")))
        self.assertEqual(self.fence.store_shape().state, STORE_ABSENT)

    def test_a_damaged_store_fails_closed_on_both_sides(self):
        action = self.fence.reserve(self.unit, "n1")
        self.fence.seal_path.unlink()  # a half-deleted artifact set: something WAS here
        self.assertEqual(self.fence.store_shape().state, STORE_DAMAGED)
        with self.assertRaises(StartupTransactionError):
            self.fence.read(action.action_id)
        with self.assertRaises(StartupTransactionError):
            self.fence.reserve(self.unit, "n2")

    def test_a_replaced_store_is_rejected_not_trusted(self):
        # Review j#81070 R1-F7: the schema check alone let a store swapped for another
        # valid-schema store answer for actions it never recorded. The seal/DB nonce join
        # (borrowed from scratch_retirement_fence._verify_identity, and previously dropped)
        # is what catches a replacement.
        action = self.fence.reserve(self.unit, "n1")
        self.assertIsNotNone(self.fence.read(action.action_id))  # trusted while intact
        self.fence.seal_path.write_text("a-different-store", encoding="utf-8")
        with self.assertRaises(StartupTransactionError):
            self.fence.read(action.action_id)
        # A rollback against the replaced authority closes nothing.
        ops = _RollbackOps([])
        verdict = run_session_rollback(
            action_id=action.action_id, ops=ops, fence=self.fence, execute=True
        )
        self.assertEqual(verdict.reason, REASON_AUTHORITY_UNAVAILABLE)
        self.assertFalse(ops.close_calls)

    def test_partial_schema_and_malformed_rows_are_structured_refusals(self):
        # Review j#81108 R3-F1: normalizing only the connect + version/seal read left the
        # row query and its decode raw, so a valid-SQLite store with a partial schema
        # (`no such table`) or a malformed cell (JSONDecodeError / non-int revision)
        # escaped the public rail as a raw error. The shape is part of the schema, and
        # every read/decode of the authority must fail closed, not just the first PRAGMA.
        import sqlite3

        def _seal(fence, nonce="n"):
            fence.seal_path.write_text(nonce, encoding="utf-8")

        def _missing_table(fence):
            conn = sqlite3.connect(fence.path, isolation_level=None)
            conn.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO store_meta VALUES ('store_nonce','n')")
            conn.execute(f"PRAGMA user_version = {STARTUP_TRANSACTION_FENCE_SCHEMA_VERSION}")
            conn.close(); _seal(fence)
            return startup_action_id(self.unit, "n1")

        def _missing_column(fence):
            conn = sqlite3.connect(fence.path, isolation_level=None)
            conn.execute("CREATE TABLE startup_actions (action_id TEXT PRIMARY KEY, phase TEXT)")
            conn.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO store_meta VALUES ('store_nonce','n')")
            conn.execute(f"PRAGMA user_version = {STARTUP_TRANSACTION_FENCE_SCHEMA_VERSION}")
            conn.close(); _seal(fence)
            return startup_action_id(self.unit, "n1")

        def _corrupt_cell(column, value):
            def _mut(fence):
                action = fence.reserve(self.unit, "n1")
                conn = sqlite3.connect(fence.path, isolation_level=None)
                conn.execute(
                    f"UPDATE startup_actions SET {column}=? WHERE action_id=?",
                    (value, action.action_id),
                )
                conn.close()
                return action.action_id
            return _mut

        cases = {
            "missing_table": _missing_table,
            "missing_column": _missing_column,
            "malformed_participants": _corrupt_cell("participants", "not-json"),
            "non_int_revision": _corrupt_cell("revision", "not-an-int"),
        }
        for label, setup in cases.items():
            with self.subTest(shape=label):
                home = Path(self._tmp.name) / label
                home.mkdir()
                fence = StartupTransactionFence(home=home)
                action_id = setup(fence)
                with self.assertRaises(StartupTransactionError):
                    fence.read(action_id)
                ops = _RollbackOps([])
                verdict = run_session_rollback(
                    action_id=action_id, ops=ops, fence=fence, execute=True
                )
                self.assertEqual(verdict.reason, REASON_AUTHORITY_UNAVAILABLE)
                self.assertFalse(ops.close_calls)

    def test_malformed_row_types_and_unknown_phase_are_structured_refusals(self):
        # Review j#81122 R4-F1: decoding a row is not validating it. A providers cell that
        # is not text (`.split()` -> AttributeError), a participant that is not an object
        # (`.get()` -> AttributeError), or an unknown phase (silently read as a no-op
        # action, `nothing_owed`) all slipped the R3 guard, which caught only
        # (DatabaseError, TypeError, ValueError). The row is now validated as a versioned
        # authority shape, and every violation is a structured refusal.
        import sqlite3

        def _store(participants, phase, providers, revision="1"):
            home = Path(self._tmp.name) / f"row_{len(list(Path(self._tmp.name).iterdir()))}"
            home.mkdir()
            fence = StartupTransactionFence(home=home)
            nonce = "nn"
            conn = sqlite3.connect(fence.path, isolation_level=None)
            conn.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO store_meta VALUES ('store_nonce', ?)", (nonce,))
            # A shape-complete table WITHOUT the NOT NULL constraints, so a NULL/typed cell
            # can be planted — the exact corrupt-authority shape R4-F1 describes.
            conn.execute(
                "CREATE TABLE startup_actions (action_id TEXT PRIMARY KEY, workspace_id "
                "TEXT, lane_id TEXT, providers TEXT, phase TEXT, revision, participants "
                "TEXT, reserved_at TEXT, updated_at TEXT)"
            )
            aid = startup_action_id(self.unit, "n1")
            conn.execute(
                "INSERT INTO startup_actions VALUES (?,?,?,?,?,?,?,?,?)",
                (aid, "ws1", "lane-1", providers, phase, revision, participants, "t", "t"),
            )
            conn.execute(f"PRAGMA user_version={STARTUP_TRANSACTION_FENCE_SCHEMA_VERSION}")
            conn.close()
            fence.seal_path.write_text(nonce, encoding="utf-8")
            return fence, aid

        cases = {
            "providers_null": dict(participants="[]", phase=PHASE_ROLLBACK_OWED, providers=None),
            "participant_not_object": dict(
                participants='["bad"]', phase=PHASE_ROLLBACK_OWED, providers="claude"
            ),
            "participants_not_list": dict(
                participants='{"x":1}', phase=PHASE_ROLLBACK_OWED, providers="claude"
            ),
            "unknown_phase": dict(participants="[]", phase="corrupt_phase", providers="claude"),
            "non_int_revision": dict(
                participants="[]", phase=PHASE_ROLLBACK_OWED, providers="claude", revision="x"
            ),
        }
        for label, kw in cases.items():
            with self.subTest(shape=label):
                fence, aid = _store(**kw)
                with self.assertRaises(StartupTransactionError):
                    fence.read(aid)
                ops = _RollbackOps([])
                verdict = run_session_rollback(
                    action_id=aid, ops=ops, fence=fence, execute=True
                )
                self.assertEqual(verdict.reason, REASON_AUTHORITY_UNAVAILABLE)
                self.assertFalse(ops.close_calls)

    def test_a_reserve_write_failure_is_structured_before_any_side_effect(self):
        # Review j#81122 R4-F2: reserve's SELECT/INSERT were outside the normalization that
        # _write already had, so a write that aborts leaked a raw IntegrityError — and
        # reserve is the reserve-before-effect anchor, so the caller could not tell
        # "reserved" from "refused".
        import sqlite3

        home = Path(self._tmp.name) / "reserve_write_fail"
        home.mkdir()
        fence = StartupTransactionFence(home=home)
        fence.reserve(self.unit, "seed")  # bootstrap the store
        conn = sqlite3.connect(fence.path, isolation_level=None)
        conn.execute(
            "CREATE TRIGGER block_insert BEFORE INSERT ON startup_actions "
            "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
        )
        conn.close()
        with self.assertRaises(StartupTransactionError):
            fence.reserve(self.unit, "n2")

    def test_a_read_never_creates_the_authority_it_checks(self):
        # Review j#81108 R3-F1: `sqlite3.connect(path)` defaults to `rwc`, so a read of an
        # absent-but-present-shaped path could fabricate an empty store. The read path is
        # existing-only (`mode=ro`); a read against a truly absent store returns None from
        # the shape gate and leaves no file behind.
        fence = StartupTransactionFence(home=Path(self._tmp.name) / "never_created")
        self.assertIsNone(fence.read(startup_action_id(self.unit, "n1")))
        self.assertFalse(fence.path.exists(), "a read fabricated the authority file")

    def test_a_non_utf8_seal_is_unreadable_not_a_match(self):
        # The seal reader catches UnicodeDecodeError (a ValueError) alongside OSError; a
        # seal of raw bytes must read as "no seal", never crash past the guard.
        action = self.fence.reserve(self.unit, "n1")
        self.fence.seal_path.write_bytes(b"\xff\xfe not text")
        with self.assertRaises(StartupTransactionError):
            self.fence.read(action.action_id)

    def test_participants_are_recorded_with_their_launch_evidence(self):
        action = self.fence.reserve(self.unit, "n1")
        self.fence.record_participant(action.action_id, self._participant("codex"))
        stored = self.fence.record_participant(
            action.action_id, self._participant("claude", "w2G:p3")
        )
        self.assertEqual(stored.phase, PHASE_LAUNCHING)
        self.assertEqual({p.role for p in stored.participants}, {"claude", "codex"})
        codex = stored.participant_for("codex")
        self.assertEqual(codex.locator, "w2G:p4")
        self.assertEqual(codex.receipt, "landed=w2G tab=w2G:t1")
        self.assertFalse(codex.closed)

    def test_a_role_is_never_started_twice_by_one_action(self):
        action = self.fence.reserve(self.unit, "n1")
        self.fence.record_participant(action.action_id, self._participant("codex"))
        with self.assertRaises(StartupTransactionError):
            self.fence.record_participant(
                action.action_id, self._participant("codex", "w2G:p9")
            )

    def test_a_reused_nonce_is_refused_rather_than_overwriting_a_record(self):
        self.fence.reserve(self.unit, "n1")
        with self.assertRaises(StartupTransactionError):
            self.fence.reserve(self.unit, "n1")

    def test_terminal_phases_are_write_once(self):
        action = self.fence.reserve(self.unit, "n1")
        self.fence.set_phase(action.action_id, PHASE_HEALTH_CHECK)
        self.fence.set_phase(action.action_id, PHASE_ROLLBACK_OWED)
        self.fence.set_phase(action.action_id, PHASE_COMPLETED_ROLLED_BACK)
        self.assertTrue(self.fence.read(action.action_id).terminal)
        # Replay must be answered from the record, never by acting again.
        with self.assertRaises(StartupTransactionError):
            self.fence.set_phase(action.action_id, PHASE_COMPLETED_SUCCESS)
        with self.assertRaises(StartupTransactionError):
            self.fence.record_participant(action.action_id, self._participant("claude"))

    def test_unknown_phase_is_refused(self):
        action = self.fence.reserve(self.unit, "n1")
        with self.assertRaises(StartupTransactionError):
            self.fence.set_phase(action.action_id, "probably_fine")

    def test_acting_without_a_record_is_refused(self):
        self.fence.reserve(self.unit, "n1")  # store exists, this action does not
        with self.assertRaises(StartupTransactionError):
            self.fence.set_phase(startup_action_id(self.unit, "other"), PHASE_HEALTH_CHECK)

    def test_contention_refuses_and_never_waits(self):
        # Two rollbacks racing the same panes is the thing that must not happen. The lock
        # is held across an external close, so a waiter would be a queue for a destructive
        # act; refusing is the only safe answer.
        action = self.fence.reserve(self.unit, "n1")
        with self.fence._hold():
            other = StartupTransactionFence(home=self.home)
            with self.assertRaises(StartupTransactionBusy):
                other.reserve(self.unit, "n2")
            with self.assertRaises(StartupTransactionBusy):
                other.set_phase(action.action_id, PHASE_HEALTH_CHECK)

    def test_mark_closed_pins_only_the_named_participant(self):
        action = self.fence.reserve(self.unit, "n1")
        self.fence.record_participant(action.action_id, self._participant("codex"))
        self.fence.record_participant(
            action.action_id, self._participant("claude", "w2G:p3")
        )
        stored = self.fence.mark_closed(action.action_id, "codex")
        self.assertTrue(stored.participant_for("codex").closed)
        self.assertFalse(stored.participant_for("claude").closed)


class ClassifyRollbackTest(unittest.TestCase):
    """The module that says NO (Answer j#80989 Q1, narrowed by j#80991).

    Each test starts from the one fact set that MAY be closed and breaks exactly one
    thing. A guard nobody drives is a guard nobody has: the eligible baseline is what
    stops these from passing against a classifier that simply refuses everything.
    """

    def _facts(self, **over):
        base = dict(
            recorded_closed=False,
            inventory_readable=True,
            name_matches=1,
            live_locator="w2G:p4",
            recorded_locator="w2G:p4",
            shell_residue=False,
            agent_idle=True,
            composer=COMPOSER_EMPTY,
            obligation_present=False,
            obligation_unreadable=False,
        )
        base.update(over)
        return ParticipantFacts(**base)

    def test_our_own_idle_empty_pane_is_the_only_plain_eligible_case(self):
        self.assertEqual(classify_rollback(self._facts()), ROLLBACK_ELIGIBLE)

    def test_a_replay_is_answered_from_the_record_not_by_closing_again(self):
        self.assertEqual(
            classify_rollback(self._facts(recorded_closed=True)), ROLLBACK_ALREADY_CLOSED
        )

    def test_unreadable_inventory_closes_nothing(self):
        self.assertEqual(
            classify_rollback(self._facts(inventory_readable=False)),
            ROLLBACK_INVENTORY_UNREADABLE,
        )

    def test_a_duplicate_name_is_never_resolved_by_guessing(self):
        self.assertEqual(
            classify_rollback(self._facts(name_matches=2)), ROLLBACK_AMBIGUOUS
        )

    def test_an_absent_participant_is_settled_but_is_never_a_close_target(self):
        # Two facts, and the first version of this test only pinned one of them (review
        # j#81070 R1-F2). Absence IS settled — blocking on a slot a previous attempt
        # already closed is how an interrupted rollback becomes permanently stuck
        # (#13847 R1-F1 / #13892). Absence is NOT a licence to close the address it used
        # to live at: `eligible` covered both, so the rail handed the recorded locator to
        # close and shut down a foreign agent that had since taken that pane id.
        verdict = classify_rollback(self._facts(name_matches=0))
        self.assertEqual(verdict, ROLLBACK_ABSENT)
        self.assertIn(verdict, ROLLBACK_SETTLED)
        self.assertNotIn(verdict, ROLLBACK_CLOSE_TARGETS)

    def test_only_a_live_ours_verdict_is_ever_a_close_target(self):
        # The whole close authority in one assertion: exactly one verdict names a pane.
        self.assertEqual(ROLLBACK_CLOSE_TARGETS, {ROLLBACK_ELIGIBLE})
        self.assertEqual(classify_rollback(self._facts()), ROLLBACK_ELIGIBLE)

    def test_a_drifted_locator_is_never_closed(self):
        # The name matches but the pane does not: this is someone else's process now, or a
        # newer generation of ours. Either way it is not what this action started.
        self.assertEqual(
            classify_rollback(self._facts(live_locator="w2G:p9")), ROLLBACK_IDENTITY_DRIFT
        )
        self.assertEqual(
            classify_rollback(self._facts(recorded_locator="")), ROLLBACK_IDENTITY_DRIFT
        )

    def test_a_durable_obligation_outranks_an_idle_empty_slot(self):
        # #13892 j#80506 F4: idle / settled composer are RECEIVER states. Neither proves
        # that no work is owed to the slot.
        self.assertEqual(
            classify_rollback(self._facts(obligation_present=True)),
            ROLLBACK_WORK_OBLIGATION,
        )

    def test_an_unreadable_ledger_is_never_an_empty_one(self):
        self.assertEqual(
            classify_rollback(self._facts(obligation_unreadable=True)),
            ROLLBACK_OBLIGATION_UNREADABLE,
        )

    def test_pending_input_is_preserved_and_no_approval_changes_that(self):
        # j#80991: action ownership is NOT a generic pending-composer discard permission.
        # A rollback throws away the startup state it created, never a body someone typed.
        self.assertEqual(
            classify_rollback(self._facts(composer=COMPOSER_PENDING)),
            ROLLBACK_PENDING_INPUT,
        )

    def test_an_unreadable_composer_is_never_an_empty_one(self):
        for composer in (COMPOSER_UNREADABLE, "something-new"):
            with self.subTest(composer=composer):
                self.assertEqual(
                    classify_rollback(self._facts(composer=composer)),
                    ROLLBACK_COMPOSER_UNREADABLE,
                )

    def test_a_busy_agent_is_never_interrupted(self):
        self.assertEqual(
            classify_rollback(self._facts(agent_idle=False)), ROLLBACK_AGENT_BUSY
        )

    def test_an_action_owned_startup_screen_is_closeable(self):
        # This action's launch put that screen there and nobody typed into it. Preserving
        # it would leave the operator a dead-end pane the tool itself created — while
        # still never ANSWERING the prompt (that stays an action in the provider's UI).
        self.assertEqual(
            classify_rollback(self._facts(composer=COMPOSER_STARTUP_BLOCKER)),
            ROLLBACK_ELIGIBLE,
        )

    def test_shell_residue_short_circuits_liveness_questions(self):
        # A pane with no agent has no turn and no composer. Demanding idle+empty proof
        # from it would preserve dead residue forever — the #13845 over-block defect.
        self.assertEqual(
            classify_rollback(
                self._facts(
                    shell_residue=True, agent_idle=False, composer=COMPOSER_UNREADABLE
                )
            ),
            ROLLBACK_ELIGIBLE,
        )

    def test_identity_outranks_every_liveness_fact(self):
        # Asking about a stranger's composer is already a trespass: identity is settled
        # first, and a drifted pane is refused whatever its runtime looks like.
        self.assertEqual(
            classify_rollback(
                self._facts(
                    live_locator="w2G:p9", agent_idle=True, composer=COMPOSER_EMPTY
                )
            ),
            ROLLBACK_IDENTITY_DRIFT,
        )

    def test_obligation_outranks_shell_residue(self):
        # Residue with work still owed to its name is not a free close: the work is owed
        # to the SLOT, and the ledger outlives the pane.
        self.assertEqual(
            classify_rollback(self._facts(shell_residue=True, obligation_present=True)),
            ROLLBACK_WORK_OBLIGATION,
        )

    def test_every_verdict_has_an_operator_detail(self):
        for verdict in ROLLBACK_VERDICTS:
            with self.subTest(verdict=verdict):
                self.assertTrue(ROLLBACK_DETAIL.get(verdict, "").strip(), verdict)


class _CloseResult:
    def __init__(self, closed=(), failed=()):
        self.closed = tuple(closed)
        self.failed = tuple(failed)


class _RollbackOps:
    """A stateful fake of the five reads + one close the rollback rail is allowed.

    Stateful on purpose: `close` actually removes the row, so the post-close re-measure
    reads a world the close produced rather than one the test hand-wrote. A fake whose
    close is a no-op would let "a close's return code is not evidence of absence" (#13892
    j#80506 F3) pass by construction — which is the guard being pinned.
    """

    def __init__(self, rows, *, obligations=(), obligations_unreadable=False):
        self.rows = list(rows)
        self._obligations = tuple(obligations)
        self._obligations_unreadable = obligations_unreadable
        self.inventory_readable = True
        self.runtime = {}
        self.composers = {}
        self.blockers = {}
        self.close_calls = []
        self.close_fails = set()
        self.close_is_a_lie = False

    def agent_rows(self):
        if not self.inventory_readable:
            raise RuntimeError("herdr agent list failed")
        return list(self.rows)

    def runtime_state(self, locator):
        return self.runtime.get(locator, "turn_ended")

    def observe_composer(self, locator):
        return self.composers.get(locator, (True, False))

    def startup_blocker(self, provider, locator):
        return self.blockers.get(locator, "")

    def open_obligations(self, workspace_id, assigned_names):
        if self._obligations_unreadable:
            return None
        return self._obligations

    def close(self, workspace_id, lane_id, targets):
        self.close_calls.append(list(targets))
        closed, failed = [], []
        for role, locator in targets:
            if role in self.close_fails:
                failed.append((role, locator, "pane close refused"))
                continue
            closed.append((role, locator))
            if not self.close_is_a_lie:
                self.rows = [r for r in self.rows if r.get("pane_id") != locator]
        return _CloseResult(closed=closed, failed=failed)


class _Obligation:
    def __init__(self, target):
        self.target = target
        self.blocks = True


class SessionRollbackRailTest(unittest.TestCase):
    """The explicit public rail: the only thing that may close what a run started."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.fence = StartupTransactionFence(home=self.home)
        self.unit = StartupUnit(
            workspace_id="ws1", lane_id="lane-1", providers=("claude", "codex")
        )

    def _owed_action(self, *, roles=("claude", "codex")):
        """A run that started `roles` and did not come up: the #13882 partial pair."""
        action = self.fence.reserve(self.unit, "n1")
        for index, role in enumerate(roles):
            self.fence.record_participant(
                action.action_id,
                Participant(
                    role=role,
                    assigned_name=f"mzb1_ws1_{role}_lane-1",
                    locator=f"w2G:p{3 + index}",
                    receipt="workspace=w2G",
                ),
            )
        self.fence.set_phase(action.action_id, PHASE_ROLLBACK_OWED)
        return self.fence.read(action.action_id)

    def _rows(self, *roles):
        return [
            {
                "name": f"mzb1_ws1_{role}_lane-1",
                "pane_id": f"w2G:p{3 + ('claude', 'codex').index(role)}",
                "agent": role,
                "agent_status": "idle",
            }
            for role in roles
        ]

    def _run(self, ops, action, **kw):
        return run_session_rollback(
            action_id=action.action_id, ops=ops, fence=self.fence, **kw
        )

    def test_preflight_is_read_only_and_closes_nothing(self):
        action = self._owed_action()
        ops = _RollbackOps(self._rows("claude", "codex"))
        verdict = self._run(ops, action)
        self.assertEqual(verdict.state, "actionable")
        self.assertEqual(verdict.reason, REASON_PREFLIGHT)
        self.assertFalse(verdict.executed)
        self.assertFalse(ops.close_calls)
        self.assertEqual(
            {p.verdict for p in verdict.participants}, {ROLLBACK_ELIGIBLE}
        )

    def test_execute_converges_the_pair_and_proves_it(self):
        # The live #13882 shape resolved: the healthy Codex sibling is closed because it is
        # a participant of the SAME action, not because its name matched.
        action = self._owed_action()
        ops = _RollbackOps(self._rows("claude", "codex"))
        verdict = self._run(ops, action, execute=True)
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.reason, REASON_OK)
        self.assertEqual({r for r, _ in ops.close_calls[0]}, {"claude", "codex"})
        self.assertEqual(
            self.fence.read(action.action_id).phase, PHASE_COMPLETED_ROLLED_BACK
        )

    def test_a_close_that_lies_is_caught_by_the_remeasure(self):
        # #13892 j#80506 F3: a close's return code is not evidence of absence. The rail
        # must re-measure and withhold success when the pane is still there.
        action = self._owed_action()
        ops = _RollbackOps(self._rows("claude", "codex"))
        ops.close_is_a_lie = True
        verdict = self._run(ops, action, execute=True)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, REASON_INCOMPLETE)
        # The debt survives, so the operator can re-run rather than start over.
        self.assertEqual(self.fence.read(action.action_id).phase, PHASE_ROLLBACK_OWED)

    def test_a_close_failure_is_reported_per_role_and_withholds_success(self):
        action = self._owed_action()
        ops = _RollbackOps(self._rows("claude", "codex"))
        ops.close_fails = {"codex"}
        verdict = self._run(ops, action, execute=True)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, REASON_INCOMPLETE)
        codex = [p for p in verdict.participants if p.role == "codex"][0]
        self.assertFalse(codex.closed)
        self.assertTrue(codex.close_detail)

    def test_pending_input_blocks_the_whole_rollback_and_closes_nothing(self):
        # j#80991: external input is preserved regardless of any approval, and a partial
        # close behind the operator's back is not an improvement over refusing.
        action = self._owed_action()
        ops = _RollbackOps(self._rows("claude", "codex"))
        ops.composers["w2G:p4"] = (True, True)
        verdict = self._run(ops, action, execute=True)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, REASON_BLOCKED)
        self.assertFalse(ops.close_calls)
        codex = [p for p in verdict.participants if p.role == "codex"][0]
        self.assertEqual(codex.verdict, ROLLBACK_PENDING_INPUT)

    def test_an_obligation_blocks_and_closes_nothing(self):
        action = self._owed_action()
        ops = _RollbackOps(
            self._rows("claude", "codex"),
            obligations=(_Obligation("mzb1_ws1_codex_lane-1"),),
        )
        verdict = self._run(ops, action, execute=True)
        self.assertEqual(verdict.reason, REASON_BLOCKED)
        self.assertFalse(ops.close_calls)

    def test_an_unreadable_ledger_blocks_and_closes_nothing(self):
        action = self._owed_action()
        ops = _RollbackOps(self._rows("claude", "codex"), obligations_unreadable=True)
        verdict = self._run(ops, action, execute=True)
        self.assertEqual(verdict.reason, REASON_BLOCKED)
        self.assertFalse(ops.close_calls)

    def test_a_drifted_locator_is_never_closed(self):
        action = self._owed_action()
        rows = self._rows("claude", "codex")
        rows[1]["pane_id"] = "w2G:p99"  # the name is ours; this process is not
        ops = _RollbackOps(rows)
        verdict = self._run(ops, action, execute=True)
        self.assertEqual(verdict.reason, REASON_BLOCKED)
        self.assertFalse(ops.close_calls)

    def test_an_action_owned_startup_screen_is_closeable_and_never_answered(self):
        action = self._owed_action()
        ops = _RollbackOps(self._rows("claude", "codex"))
        ops.blockers["w2G:p3"] = "workspace_trust_confirmation"
        verdict = self._run(ops, action, execute=True)
        self.assertTrue(verdict.ok)
        # The rail has no send/type port at all: answering the prompt is not expressible.
        self.assertFalse(hasattr(ops, "sent"))

    def test_an_unreadable_inventory_closes_nothing(self):
        action = self._owed_action()
        ops = _RollbackOps(self._rows("claude", "codex"))
        ops.inventory_readable = False
        verdict = self._run(ops, action, execute=True)
        self.assertEqual(verdict.reason, REASON_BLOCKED)
        self.assertFalse(ops.close_calls)

    def test_an_unknown_action_is_refused(self):
        self.fence.reserve(self.unit, "n1")
        ops = _RollbackOps([])
        verdict = run_session_rollback(
            action_id=startup_action_id(self.unit, "someone-elses"),
            ops=ops,
            fence=self.fence,
            execute=True,
        )
        self.assertEqual(verdict.reason, REASON_ACTION_UNKNOWN)
        self.assertFalse(ops.close_calls)

    def test_a_successful_action_owes_nothing_and_is_refused(self):
        action = self.fence.reserve(self.unit, "n1")
        self.fence.record_participant(
            action.action_id,
            Participant(role="codex", assigned_name="mzb1_ws1_codex_lane-1", locator="w2G:p4"),
        )
        self.fence.set_phase(action.action_id, PHASE_COMPLETED_SUCCESS)
        ops = _RollbackOps(self._rows("codex"))
        verdict = run_session_rollback(
            action_id=action.action_id, ops=ops, fence=self.fence, execute=True
        )
        self.assertEqual(verdict.reason, REASON_NOTHING_OWED)
        self.assertFalse(ops.close_calls)

    def test_terminal_replay_is_answered_from_the_record(self):
        action = self._owed_action()
        ops = _RollbackOps(self._rows("claude", "codex"))
        self.assertTrue(self._run(ops, action, execute=True).ok)
        replay = self._run(_RollbackOps([]), action, execute=True)
        self.assertTrue(replay.ok)
        self.assertEqual(replay.reason, REASON_ALREADY_ROLLED_BACK)

    def test_a_completion_write_failure_withholds_success_rather_than_faking_it(self):
        # The panes ARE gone; we simply cannot prove it durably. #13892 j#80526: withhold
        # the success — there is no capacity leak either way, and a fabricated completion
        # would let a later replay believe this action is settled when its record is not.
        action = self._owed_action()
        ops = _RollbackOps(self._rows("claude", "codex"))
        original = self.fence.set_phase

        def _fail_completion(action_id, phase):
            if phase == PHASE_COMPLETED_ROLLED_BACK:
                raise StartupTransactionError("completion write refused")
            return original(action_id, phase)

        self.fence.set_phase = _fail_completion
        verdict = self._run(ops, action, execute=True)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, REASON_INCOMPLETE)
        self.assertTrue(verdict.executed)
        # The record still says the debt stands, so a re-run resumes from proven facts.
        self.fence.set_phase = original
        self.assertEqual(self.fence.read(action.action_id).phase, PHASE_ROLLBACK_OWED)

    def test_an_unreadable_post_close_inventory_withholds_success(self):
        # A close's return code is not evidence of absence, and neither is a remeasure
        # that could not be read (#13892 j#80506 F3).
        action = self._owed_action()

        class _BlindAfterClose(_RollbackOps):
            def close(self, workspace_id, lane_id, targets):
                result = super().close(workspace_id, lane_id, targets)
                self.inventory_readable = False  # the world goes dark right after
                return result

        ops = _BlindAfterClose(self._rows("claude", "codex"))
        verdict = self._run(ops, action, execute=True)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, REASON_INCOMPLETE)
        self.assertEqual(self.fence.read(action.action_id).phase, PHASE_ROLLBACK_OWED)

    def test_a_damaged_authority_refuses_and_closes_nothing(self):
        action = self._owed_action()
        self.fence.seal_path.unlink()  # a partial artifact set: something WAS here
        ops = _RollbackOps(self._rows("claude", "codex"))
        verdict = self._run(ops, action, execute=True)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, REASON_AUTHORITY_UNAVAILABLE)
        self.assertFalse(ops.close_calls)

    def test_a_corrupt_authority_is_a_structured_refusal_not_a_raw_error(self):
        # Review j#81092 R2-F2: a store whose bytes are not a database raised a raw
        # sqlite3.DatabaseError straight out of the public rail — the "never raises"
        # contract was false, and public recovery could not answer
        # `rollback_authority_unavailable`. The borrowed precedent normalizes exactly this
        # in _connect_ro/_connect_rw; porting only the identity check (R1-F7) left it out.
        for label, db_bytes in (
            ("not a database", b"not-a-sqlite-database"),
            ("empty file", b""),
            ("truncated header", b"SQLite format 3\x00truncated"),
        ):
            with self.subTest(label=label):
                home = Path(self._tmp.name) / label.replace(" ", "_")
                home.mkdir()
                fence = StartupTransactionFence(home=home)
                fence.path.write_bytes(db_bytes)
                fence.seal_path.write_text("some-nonce", encoding="utf-8")
                # The store is present-shaped (row-bearing artifact + seal), so the rail
                # reaches _connect rather than short-circuiting on absence.
                with self.assertRaises(StartupTransactionError):
                    fence.read(startup_action_id(self.unit, "n1"))
                ops = _RollbackOps([])
                verdict = run_session_rollback(
                    action_id=startup_action_id(self.unit, "n1"),
                    ops=ops,
                    fence=fence,
                    execute=True,
                )
                self.assertEqual(verdict.reason, REASON_AUTHORITY_UNAVAILABLE)
                self.assertFalse(ops.close_calls)

    def test_an_absent_authority_never_bootstraps_to_close_something(self):
        # The reserve/rollback asymmetry, driven end to end: a rollback against a store
        # that does not exist has no proof of anything and must not conjure one.
        unknown = StartupTransactionFence(home=Path(self._tmp.name) / "elsewhere")
        ops = _RollbackOps([])
        verdict = run_session_rollback(
            action_id=startup_action_id(self.unit, "n1"),
            ops=ops,
            fence=unknown,
            execute=True,
        )
        self.assertEqual(verdict.reason, REASON_ACTION_UNKNOWN)
        self.assertTrue(unknown.store_shape().absent)
        self.assertFalse(ops.close_calls)

    def test_a_launching_action_that_died_mid_pair_is_still_recoverable(self):
        # A run that died between two starts never reached its health check, so its phase
        # is `launching` — and its first agent is exactly the orphan this rail exists for.
        action = self.fence.reserve(self.unit, "n1")
        self.fence.record_participant(
            action.action_id,
            Participant(
                role="codex", assigned_name="mzb1_ws1_codex_lane-1", locator="w2G:p4"
            ),
        )
        ops = _RollbackOps(self._rows("codex"))
        verdict = run_session_rollback(
            action_id=action.action_id, ops=ops, fence=self.fence, execute=True
        )
        self.assertTrue(verdict.ok)
        self.assertEqual(
            self.fence.read(action.action_id).phase, PHASE_COMPLETED_ROLLED_BACK
        )

    def test_a_foreign_agent_on_the_recorded_locator_is_never_closed(self):
        # Review j#81070 R1-F2 (the worst one): the participant's name is gone from the
        # inventory, and a DIFFERENT agent now holds the pane id this action once launched
        # at. Handing the recorded locator to close shut down that foreign agent and
        # reported success. Absence is settled, never a target.
        action = self._owed_action()
        foreign = [
            {"name": "somebody_elses_agent", "pane_id": "w2G:p3", "agent": "codex",
             "agent_status": "idle"},
            {"name": "another_stranger", "pane_id": "w2G:p4", "agent": "claude",
             "agent_status": "idle"},
        ]
        ops = _RollbackOps(foreign)
        verdict = self._run(ops, action, execute=True)
        self.assertFalse(
            ops.close_calls, f"closed a foreign agent: {ops.close_calls}"
        )
        # Both participants are positively absent -> the action is settled, not a failure.
        self.assertTrue(verdict.ok)
        self.assertEqual(
            {p.verdict for p in verdict.participants}, {ROLLBACK_ABSENT}
        )

    def test_a_mix_of_absent_and_live_closes_only_the_live_one(self):
        action = self._owed_action()
        # codex present at its recorded pane; claude's name absent, its pane taken over.
        rows = [
            {"name": "mzb1_ws1_codex_lane-1", "pane_id": "w2G:p4", "agent": "codex",
             "agent_status": "idle"},
            {"name": "a_stranger", "pane_id": "w2G:p3", "agent": "claude",
             "agent_status": "idle"},
        ]
        ops = _RollbackOps(rows)
        verdict = self._run(ops, action, execute=True)
        self.assertEqual([c for c in ops.close_calls[0]], [("codex", "w2G:p4")])
        self.assertTrue(verdict.ok)

    def test_a_startup_screen_over_an_unreadable_composer_is_preserved(self):
        # Review j#81070 R1-F3: a recognised trust screen used to license a close even
        # when the composer could not be read — "we saw no typing" is not "there is no
        # typing". Only an exact positive read (readable, not pending) is action-owned.
        action = self._owed_action()
        ops = _RollbackOps(self._rows("claude", "codex"))
        ops.blockers["w2G:p3"] = "workspace_trust_confirmation"
        ops.composers["w2G:p3"] = (False, None)  # unreadable
        verdict = self._run(ops, action, execute=True)
        self.assertFalse(ops.close_calls)
        self.assertEqual(verdict.reason, REASON_BLOCKED)
        claude = [p for p in verdict.participants if p.role == "claude"][0]
        self.assertEqual(claude.verdict, ROLLBACK_COMPOSER_UNREADABLE)

    def test_a_lying_close_never_records_closed_so_the_replay_still_acts(self):
        # Review j#81070 R1-F4: the durable `closed` flag used to be written from the
        # close's own report, so a close that returned success but left the pane made the
        # NEXT run skip it as already-settled — the participant could never be closed
        # again. The flag must come from the remeasure, which is the only thing that can
        # see absence.
        action = self._owed_action()
        ops = _RollbackOps(self._rows("claude", "codex"))
        ops.close_is_a_lie = True
        self.assertFalse(self._run(ops, action, execute=True).ok)
        # Nothing was recorded closed, because nothing was proven gone.
        self.assertFalse(
            any(p.closed for p in self.fence.read(action.action_id).participants)
        )
        # A second run with an honest close finishes the job (it still has targets).
        ops.close_is_a_lie = False
        ops.close_calls.clear()
        verdict = self._run(ops, self.fence.read(action.action_id), execute=True)
        self.assertTrue(verdict.ok)
        self.assertTrue(ops.close_calls, "the replay had no targets — it was stuck")

    def test_a_crash_at_health_check_is_still_recoverable(self):
        # Review j#81070 R1-F5: settle() writes `health_check` before it writes the
        # rollback_owed verdict, so a crash in that window left an action holding live
        # participants that public recovery refused with `nothing_owed`.
        from mozyo_bridge.core.state.startup_transaction_fence import PHASE_HEALTH_CHECK

        action = self.fence.reserve(self.unit, "n1")
        self.fence.record_participant(
            action.action_id,
            Participant(role="codex", assigned_name="mzb1_ws1_codex_lane-1",
                        locator="w2G:p4", receipt="w"),
        )
        self.fence.set_phase(action.action_id, PHASE_HEALTH_CHECK)  # crashed here
        ops = _RollbackOps(self._rows("codex"))
        verdict = run_session_rollback(
            action_id=action.action_id, ops=ops, fence=self.fence, execute=True
        )
        self.assertTrue(verdict.ok)
        self.assertEqual({r for r, _ in ops.close_calls[0]}, {"codex"})

    def test_a_partial_rollback_resumes_rather_than_sticking(self):
        # The first attempt closes claude and fails on codex; the second finds claude
        # positively absent and finishes the job. Blocking on the already-closed slot is
        # how an interrupted rollback becomes permanently stuck (#13847 R1-F1).
        action = self._owed_action()
        ops = _RollbackOps(self._rows("claude", "codex"))
        ops.close_fails = {"codex"}
        self.assertFalse(self._run(ops, action, execute=True).ok)
        ops.close_fails = set()
        verdict = self._run(ops, self.fence.read(action.action_id), execute=True)
        self.assertTrue(verdict.ok)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
