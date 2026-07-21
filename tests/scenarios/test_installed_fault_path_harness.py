"""Deterministic fault-path truth tables — source-public-dispatch layer (Redmine #14097).

This is the **hermetic source-public-dispatch layer** of the #14097 harness (coordinator decision
j#83766): it carries the detailed per-shape fault *truth tables* by routing ``argv`` through the
public command dispatch (``build_parser() -> args.func``) — the same parser/handlers the installed
binary runs — driven in-process over the worktree source, confined to an isolated
``MOZYO_BRIDGE_HOME`` + a scratch herdr workspace/process (a fake herdr over the subprocess
boundary), so no managed lane / callback / lease is ever touched. Each release-critical fault
shape already has a deterministic regression, but every one drives its use case / store / domain
fold through **internal module imports**, never the public command dispatch; this layer closes
that gap.

Installed *provenance* (a wheel built from the review head, installed into an isolated temp venv
and driven as a real subprocess) is the SEPARATE ``installed`` smoke layer — a CI/network gate,
not this offline suite — and is never claimed here.

The shapes and what the public command surface must show (issue Acceptance + the stale-locator
addendum j#83362 + the callback-lease addendum j#83426 + the #13897 addendum j#83575):

1. **Post-close stale-worker resume** (#13806) — ``sublane recover-stale``. The public preflight
   POSITIVELY observes a locator-present shell-residue worker (``is_stale`` + ``identity_resolved``
   + ``is_standard_sublane_worker`` + ``not_productive``) and closes nothing (a preflight is
   read-only); an unresolvable / gateway / foreign identity is a zero-close refusal. The full
   close -> launch-owed -> post-close resume *actuation* (additional close 0 / single redispatch)
   stays covered by the internal #13806 tranche-D live regression; this harness adds the public
   preflight + zero-close negative rail (the release's actual installed-negative-safety posture,
   never a fabricated installed positive).
2. **Nested unhealthy launch -> rollback pointer** (#13948) — ``herdr session-rollback``. A fresh
   idle launch that owes a rollback is surfaced as an ``eligible`` pointer by the read-only
   preflight; ``--execute`` closes exactly that fresh pane; a replay is idempotent
   (``already_rolled_back``, nothing re-closed). A busy / foreign slot is never closed.
3. **Stale-locator ``sublane list`` projection** (#14063 / j#83362) — ``sublane list --json``. A
   locator-present shell-residue slot never populates a live pane: a live+stale pair reads
   one-sided (``gateway_only`` / ``worker_only``) with the role-specific stale hint, a both-stale
   unit reads ``detached`` with both hints, and a genuinely-live lane still reads ``active``.
4. **Callback-sweep lease recovery** (#13951 / j#83426) — ``workflow callback-lease``. A clean loss
   diagnoses ``missing_db`` and recovers under a fingerprint-bound apply; a live owner / an
   unreadable store / a concurrent mutation is zero-write; a rollback whose backup cleanup fails
   is a typed ``rollback_incomplete`` residue (honestly ``zero_write=False``), never hidden.
5. **Hibernated-legacy migration foreign-inventory gate** (#13897 / j#83575) — ``sublane retire
   --migrate-hibernated-legacy``. A lane unit occupied by a foreign / duplicate / unreadable
   occupant is zero-write / zero-close / fixed-reason (``foreign_inventory_present`` /
   ``duplicate_inventory`` / ``expected_identity_unresolved``); exact managed-slot absence stays a
   necessary conjunct; a quiescent unit migrates and an already-retired replay re-verifies
   quiescence. Added per j#83575 without re-implementing the #13897 runtime source.

Every fault is prepared through the safe isolated fixture rails the harness owns (the home-scoped
public stores + the fake's one-shot stimuli), so an operator/agent driving it never issues a raw
SQLite / tmux / Herdr mutation. Cleanup is structural: the isolated home is removed, so a scratch
lane / lease / callback row can never amplify managed state — the harness additionally asserts the
scratch inventory drains to zero.
"""

from __future__ import annotations

import unittest

from tests.support.installed_fault_harness import InstalledFaultHarness


# ---------------------------------------------------------------------------
# Shape 3 — stale-locator ``sublane list`` projection (#14063 / j#83362)
# ---------------------------------------------------------------------------
class StaleLocatorProjectionThroughPublicList(unittest.TestCase):
    """``sublane list --json`` must never leak a shell-residue locator into a live pane."""

    def _by_lane(self, payload):
        return {lane["lane_id"]: lane for lane in payload["sublanes"]}

    def test_live_stale_and_detached_are_projected_distinctly(self):
        h = InstalledFaultHarness(self)
        # Distinct issues so the read is about slot liveness, not duplicate-issue grouping.
        h.seed_lane("issue_14097_live", issue="14097", gateway="live", worker="live")
        h.seed_lane("issue_14201_gwonly", issue="14201", gateway="live", worker="stale")
        h.seed_lane("issue_14202_workeronly", issue="14202", gateway="stale", worker="live")
        h.seed_lane("issue_14203_detached", issue="14203", gateway="stale", worker="stale")

        result = h.run_cli(["sublane", "list", "--json", "--repo", str(h.repo_root)])
        self.assertEqual(result.rc, 0)
        lanes = self._by_lane(result.json())

        # A both-live pair stays active with both panes populated.
        live = lanes["issue_14097_live"]
        self.assertEqual(live["state"], "active")
        self.assertIsNotNone(live["gateway_pane"])
        self.assertIsNotNone(live["worker_pane"])
        self.assertNotIn("worker_slot_stale", live["stale_hints"])
        self.assertNotIn("gateway_slot_stale", live["stale_hints"])

        # A live gateway + stale worker reads one-sided; the stale locator NEVER populates
        # worker_pane, and the role-specific stale hint (not the missing hint) is present.
        gw_only = lanes["issue_14201_gwonly"]
        self.assertEqual(gw_only["state"], "gateway_only")
        self.assertIsNotNone(gw_only["gateway_pane"])
        self.assertIsNone(gw_only["worker_pane"])
        self.assertIn("worker_slot_stale", gw_only["stale_hints"])
        self.assertNotIn("worker_slot_missing", gw_only["stale_hints"])

        # The mirror: a live worker + stale gateway.
        worker_only = lanes["issue_14202_workeronly"]
        self.assertEqual(worker_only["state"], "worker_only")
        self.assertIsNone(worker_only["gateway_pane"])
        self.assertIsNotNone(worker_only["worker_pane"])
        self.assertIn("gateway_slot_stale", worker_only["stale_hints"])

        # A both-stale unit is detached with both stale hints and no live pane.
        detached = lanes["issue_14203_detached"]
        self.assertEqual(detached["state"], "detached")
        self.assertIsNone(detached["gateway_pane"])
        self.assertIsNone(detached["worker_pane"])
        self.assertIn("gateway_slot_stale", detached["stale_hints"])
        self.assertIn("worker_slot_stale", detached["stale_hints"])

    def test_a_productive_lane_is_never_downgraded_by_a_sibling_stale_lane(self):
        # The Acceptance: a current productive lane is not changed by observing a stale sibling.
        h = InstalledFaultHarness(self)
        h.seed_lane("issue_14097_productive", issue="14097", gateway="live", worker="live")
        h.seed_lane("issue_14097_detached", issue="14098", gateway="stale", worker="stale")
        lanes = self._by_lane(
            h.run_cli(["sublane", "list", "--json", "--repo", str(h.repo_root)]).json()
        )
        self.assertEqual(lanes["issue_14097_productive"]["state"], "active")


# ---------------------------------------------------------------------------
# Shape 4 — callback-sweep lease recovery (#13951 / j#83426)
# ---------------------------------------------------------------------------
class CallbackLeaseRecoveryThroughPublicCli(unittest.TestCase):
    """``workflow callback-lease`` status / dry-run / fingerprint-bound apply, all zero-write-safe."""

    def test_clean_loss_status_dryrun_then_fingerprint_bound_apply(self):
        h = InstalledFaultHarness(self)
        self.assertEqual(h.callback_lease_cli("--bootstrap").rc, 0)
        self.assertEqual(h.callback_lease_cli().rc, 0)  # healthy

        # A clean loss: the DB is gone, the sidecar survives (a recoverable store loss).
        h.lease_store().path.unlink()
        status = h.callback_lease_cli()
        self.assertEqual(status.rc, 1)
        self.assertIn("missing_db", status.stdout)
        self.assertIn("recoverable=True", status.stdout)
        fingerprint = h.lease_fingerprint_from(status)

        # A dry-run writes nothing and reports the plan (a recoverable loss -> exit 0).
        dry = h.callback_lease_cli("--recover")
        self.assertEqual(dry.rc, 0)
        self.assertIn("planned (zero_write=True)", dry.stdout)
        self.assertTrue(h.lease_store().path.exists() is False)  # still nothing minted

        # The fingerprint-bound apply mints a fresh store and reports the backup it took.
        applied = h.callback_lease_cli("--recover", "--apply", "--expect-fingerprint", fingerprint)
        self.assertEqual(applied.rc, 0)
        self.assertIn("applied (zero_write=False)", applied.stdout)
        self.assertIn("backups:", applied.stdout)
        self.assertEqual(h.callback_lease_cli().rc, 0)  # healthy again

    def test_apply_without_a_fingerprint_is_refused(self):
        h = InstalledFaultHarness(self)
        h.callback_lease_cli("--bootstrap")
        h.lease_store().path.unlink()
        out = h.callback_lease_cli("--recover", "--apply")
        self.assertEqual(out.rc, 2)
        self.assertIn("requires --expect-fingerprint", out.stdout)

    def test_live_owner_is_zero_write(self):
        # A live lease owner is the case recovery must never mint past. Drop the SIDECAR (the DB
        # keeps the live lease visible) so the diagnosis is missing_sidecar + has_live_owner.
        h = InstalledFaultHarness(self)
        h.callback_lease_cli("--bootstrap")
        from mozyo_bridge.core.state.callback_sweep_lease import LeaseKey

        lease = h.lease_store()
        lease.acquire(LeaseKey("ws-live", "lane-live", "14097", "anchor-live"), ttl_seconds=9999)
        lease.sidecar_path.unlink()
        status = h.callback_lease_cli()
        self.assertIn("has_live_owner=True", status.stdout)
        fingerprint = h.lease_fingerprint_from(status)
        out = h.callback_lease_cli("--recover", "--apply", "--expect-fingerprint", fingerprint)
        self.assertEqual(out.rc, 1)
        self.assertIn("zero_write=True", out.stdout)

    def test_dead_owner_recovers(self):
        # A DEAD (expired) owner does not block recovery — the contrast with a live owner.
        h = InstalledFaultHarness(self)
        h.callback_lease_cli("--bootstrap")
        from mozyo_bridge.core.state.callback_sweep_lease import LeaseKey

        lease = h.lease_store()
        lease.acquire(LeaseKey("ws-dead", "lane-dead", "14097", "anchor-dead"), ttl_seconds=0.01)
        import time

        time.sleep(0.05)  # the owner's lease lapses -> a dead owner
        lease.sidecar_path.unlink()
        status = h.callback_lease_cli()
        self.assertIn("has_live_owner=False", status.stdout)
        fingerprint = h.lease_fingerprint_from(status)
        out = h.callback_lease_cli("--recover", "--apply", "--expect-fingerprint", fingerprint)
        self.assertEqual(out.rc, 0)
        self.assertIn("applied", out.stdout)

    def test_concurrent_mutation_between_status_and_apply_is_zero_write(self):
        h = InstalledFaultHarness(self)
        h.callback_lease_cli("--bootstrap")
        lease = h.lease_store()
        lease.path.unlink()
        fingerprint = h.lease_fingerprint_from(h.callback_lease_cli())
        # A concurrent process swaps the sidecar after the operator read the fingerprint.
        lease.sidecar_path.write_text("a-different-store-nonce", encoding="utf-8")
        out = h.callback_lease_cli("--recover", "--apply", "--expect-fingerprint", fingerprint)
        self.assertEqual(out.rc, 1)
        self.assertIn("zero_write=True", out.stdout)

    def test_callback_ingest_is_exactly_once_and_sweep_never_amplifies(self):
        # The callback half of the addendum j#83426: the same dispatch anchor is recovered/enqueued
        # EXACTLY ONCE (duplicate notification 0), and a fresh-turn sweep never amplifies the
        # pending / dead-letter backlog.
        h = InstalledFaultHarness(self)
        snapshot = h.write_redmine_snapshot("14097", "84000", "implementation_done")
        candidate = "14097:84000:coordinator:implementation_done"
        common = [
            "--candidate", candidate, "--redmine-json", str(snapshot),
            "--workspace-id", h.workspace_id, "--cursor", "84001", "--json",
        ]

        first = h.callbacks_cli("--ingest", *common).json()
        self.assertEqual(first["enqueued"], 1)
        self.assertEqual(first["duplicates"], 0)
        self.assertEqual(first["dead_lettered"], 0)

        # Re-ingesting the SAME dispatch anchor is idempotent: the outbox UNIQUE fence dedupes it,
        # so it is never enqueued (or notified) twice.
        again = h.callbacks_cli("--ingest", *common).json()
        self.assertEqual(again["enqueued"], 0)
        self.assertEqual(again["duplicates"], 1)
        self.assertFalse(again["outcomes"][0]["inserted"])

        # The SEND edge, not just enqueue-uniqueness: with an isolated counting transport, deliver
        # the anchor and re-deliver it. The anchor is SENT exactly once (a delivered row is
        # terminal; the re-deliver sends nothing) — duplicate notification 0.
        with h.counting_callback_transport() as sends:
            delivered = h.callbacks_cli("--deliver", "--workspace-id", h.workspace_id, "--json").json()
            self.assertEqual(len(delivered["delivered"]), 1)
            self.assertEqual(delivered["delivered"][0]["send_outcome"], "delivered")
            self.assertEqual(len(sends), 1)  # exactly one send

            redelivered = h.callbacks_cli("--deliver", "--workspace-id", h.workspace_id, "--json").json()
            self.assertEqual(redelivered["delivered"], [])  # nothing re-sent
            self.assertEqual(len(sends), 1)  # still exactly one send (duplicate notification 0)

        # After delivery, a fresh-turn sweep does NOT amplify the pending / dead-letter backlog.
        swept = h.callbacks_cli("--sweep", "--workspace-id", h.workspace_id, "--json").json()
        self.assertEqual(swept["dead_letter"], [])
        self.assertEqual(len(swept["pending"]), 0)  # the anchor is delivered, not re-pending

    def test_rollback_cleanup_failure_is_a_typed_residue_never_hidden(self):
        # Item 3 of the IR: a rollback whose backup cleanup fails is an HONEST rollback_incomplete
        # residue (zero_write=False, the residue named), never a hidden write reported as clean.
        h = InstalledFaultHarness(self)
        h.callback_lease_cli("--bootstrap")
        h.lease_store().path.unlink()
        fingerprint = h.lease_fingerprint_from(h.callback_lease_cli())
        out = h.run_lease_apply_with_failing_backup_cleanup(fingerprint)
        self.assertEqual(out.rc, 1)
        self.assertIn("rollback_incomplete", out.stdout)
        self.assertIn("zero_write=False", out.stdout)
        self.assertIn("RESIDUE", out.stdout)


# ---------------------------------------------------------------------------
# Shape 2 — nested unhealthy launch -> public rollback pointer (#13948)
# ---------------------------------------------------------------------------
class NestedRollbackPointerThroughPublicCli(unittest.TestCase):
    """``herdr session-rollback``: preflight pointer -> execute closes -> idempotent replay."""

    def test_preflight_points_execute_closes_replay_is_idempotent(self):
        h = InstalledFaultHarness(self)
        action_id, locators = h.seed_owed_rollback("issue_14097_nested", providers=("claude",))
        self.assertEqual(h.live_locator_count(), 1)

        preflight = h.session_rollback_cli(action_id)
        self.assertEqual(preflight.rc, 0)
        payload = preflight.json()
        self.assertEqual(payload["reason"], "preflight_only")
        self.assertEqual(payload["state"], "actionable")
        self.assertFalse(payload["executed"])  # a preflight closes nothing
        self.assertEqual(payload["participants"][0]["verdict"], "eligible")
        self.assertFalse(payload["participants"][0]["closed"])
        self.assertEqual(h.live_locator_count(), 1)  # preflight is zero-close

        execute = h.session_rollback_cli(action_id, execute=True)
        self.assertEqual(execute.rc, 0)
        done = execute.json()
        self.assertTrue(done["executed"])
        self.assertEqual(done["state"], "completed")
        self.assertTrue(done["participants"][0]["closed"])
        self.assertEqual(h.live_locator_count(), 0)  # the fresh unhealthy launch was closed

        replay = h.session_rollback_cli(action_id)
        self.assertEqual(replay.rc, 0)
        self.assertEqual(replay.json()["reason"], "already_rolled_back")
        self.assertEqual(replay.json()["participants"], [])  # nothing left to close

    def test_a_busy_participant_is_never_closed(self):
        # A rollback never interrupts work in flight: a busy slot refuses the close, zero-close.
        h = InstalledFaultHarness(self)
        action_id, _ = h.seed_owed_rollback(
            "issue_14097_busy", providers=("claude",), busy=True
        )
        preflight = h.session_rollback_cli(action_id)
        self.assertEqual(preflight.json()["participants"][0]["verdict"], "agent_busy")
        execute = h.session_rollback_cli(action_id, execute=True)
        self.assertEqual(execute.rc, 1)
        self.assertEqual(execute.json()["state"], "blocked")
        self.assertEqual(h.live_locator_count(), 1)  # never closed

    def test_an_unknown_action_id_closes_nothing(self):
        h = InstalledFaultHarness(self)
        h.seed_owed_rollback("issue_14097_other", providers=("claude",))
        out = h.session_rollback_cli("startup-does-not-exist")
        self.assertEqual(out.json()["participants"], [])
        self.assertEqual(h.live_locator_count(), 1)  # the other action's slot is untouched

    def test_same_binding_new_action_replay_after_rollback_discharge(self):
        # The nested-rollback acceptance tail (issue Required work 2): after the PUBLIC rollback
        # rail discharges the debt, the SAME unit replays toward a FRESH launch under a NEW action
        # id — the discharged action is never resurrected.
        h = InstalledFaultHarness(self)
        action_a, _ = h.seed_owed_rollback(
            "issue_14097_replay", providers=("claude",), nonce="n1"
        )
        self.assertEqual(h.session_rollback_cli(action_a, execute=True).json()["state"], "completed")
        self.assertEqual(h.live_locator_count(), 0)  # action A's fresh launch was closed

        # The same binding (same startup unit) replays: a fresh reservation mints a NEW action id.
        action_b, _ = h.seed_owed_rollback(
            "issue_14097_replay", providers=("claude",), nonce="n2"
        )
        self.assertNotEqual(action_b, action_a)  # a distinct new action id
        replay = h.session_rollback_cli(action_b).json()
        self.assertEqual(replay["participants"][0]["verdict"], "eligible")  # B is a live fresh launch

        # Action A stays terminally discharged — the replay never resurrects the rolled-back one.
        self.assertEqual(h.session_rollback_cli(action_a).json()["reason"], "already_rolled_back")


# ---------------------------------------------------------------------------
# Shape 1 — post-close stale-worker recovery preflight + zero-close (#13806)
# ---------------------------------------------------------------------------
class StaleWorkerRecoveryThroughPublicCli(unittest.TestCase):
    """``sublane recover-stale``: the public preflight observes the fault and closes nothing."""

    def test_preflight_positively_observes_a_stale_worker_and_closes_nothing(self):
        h = InstalledFaultHarness(self)
        outcome = h.recover_stale_preflight("issue_14097_worker")
        self.assertEqual(outcome.rc, 0)
        payload = outcome.json()
        self.assertEqual(payload["status"], "preflight")
        self.assertFalse(payload["executed"])
        self.assertFalse(payload["closed_old_worker"])  # a preflight closes nothing
        obs = payload["observation"]
        self.assertTrue(obs["is_stale"])  # the locator-present shell residue is seen
        self.assertTrue(obs["identity_resolved"])
        self.assertTrue(obs["is_standard_sublane_worker"])
        self.assertTrue(obs["not_productive"])
        self.assertTrue(obs["issue_lane_matches"])
        self.assertTrue(obs["no_authority_conflict"])

    def test_an_unresolvable_identity_is_a_zero_close_refusal(self):
        # An --execute against an identity with no live match refuses zero-close (never a blind
        # close of an unknown slot).
        h = InstalledFaultHarness(self)
        result = h.recover_stale_execute(
            issue="14097",
            lane="issue_14097_ghost",
            role="claude",
            provider="claude",
            assigned_name="mzb1_ghost_claude_lane",
            locator="w9:p9",
        )
        self.assertEqual(result.rc, 1)
        payload = result.json()
        self.assertEqual(payload["status"], "refused")
        self.assertFalse(payload["closed_old_worker"])

    def test_a_gateway_provider_pin_is_protected_never_closed_as_a_worker(self):
        # The approval's own provider field is validated: a pin at the GATEWAY provider is
        # protected, never classified as a standard worker, and closes nothing.
        h = InstalledFaultHarness(self)
        lane = "issue_14097_gwpin"
        name = h.seed_stale_worker(lane, role="claude")
        result = h.recover_stale_execute(
            issue="14097", lane=lane, role="codex", provider="codex",
            assigned_name=name, locator=h.locator_of(name),
        )
        self.assertEqual(result.rc, 1)
        self.assertFalse(result.json()["observation"]["is_standard_sublane_worker"])
        self.assertFalse(result.json()["closed_old_worker"])

    def test_preflight_over_a_git_lane_is_fully_actionable(self):
        # The full positive observation: over a real git-backed lane the preflight measures
        # every recover-stale conjunct as satisfied (the fault is a genuine stale worker).
        h = InstalledFaultHarness(self)
        ctx = h.recover_stale_git_lane("issue_14097_resume", issue="14097")
        obs = h.recover_stale_cli(ctx).json()["observation"]
        for axis in (
            "identity_resolved", "is_standard_sublane_worker", "issue_lane_matches",
            "generation_matches", "not_productive", "is_stale", "worktree_readable",
            "no_authority_conflict",
        ):
            self.assertTrue(obs[axis], axis)

    def test_close_succeeds_then_post_close_resume_with_additional_close_zero(self):
        # THE post-close-resume acceptance measured through the public orchestration path:
        # --execute closes the exact stale worker (once), owes the launch; a re-run recognises
        # the durable transaction as a post-close resume and NEVER re-closes.
        h = InstalledFaultHarness(self)
        ctx = h.recover_stale_git_lane("issue_14097_resume", issue="14097")
        self.assertEqual(h.live_locator_count(), 1)

        first = h.recover_stale_cli(ctx, execute=True).json()
        self.assertTrue(first["executed"])
        self.assertTrue(first["closed_old_worker"])  # the exact old worker was closed
        self.assertEqual(first["status"], "stopped")
        self.assertIn("re-run resumes", first["detail"])  # the launch is owed, replay resumes
        self.assertEqual(h.live_locator_count(), 0)  # the stale worker is gone (closed once)

        # The re-run observes the old worker gone and recognises the durable launch-owed
        # transaction: it is a post-close resume, and it closes NOTHING more.
        second = h.recover_stale_cli(ctx, execute=True).json()
        self.assertTrue(second["post_close_resume"])
        self.assertEqual(h.live_locator_count(), 0)  # additional close 0

    def test_an_identity_unknown_with_no_transaction_refuses_zero_close(self):
        # The fail-closed fence: a genuinely unknown identity with NO durable transaction is a
        # fresh recovery whose block is real — never launched blind as a "resume", zero-close.
        h = InstalledFaultHarness(self)
        ctx = h.recover_stale_git_lane("issue_14097_ghost", issue="14097")
        # Drop the worker row so the identity is unknown, with no prior --execute (no txn).
        h.fake._agents.pop(ctx.worker_locator, None)
        outcome = h.recover_stale_cli(ctx, execute=True).json()
        self.assertEqual(outcome["status"], "refused")
        self.assertFalse(outcome["post_close_resume"])
        self.assertFalse(outcome["closed_old_worker"])


# ---------------------------------------------------------------------------
# Shape 5 — hibernated-legacy migration foreign-inventory gate (#13897 / j#83575)
# ---------------------------------------------------------------------------
class LegacyMigrationForeignInventoryThroughPublicCli(unittest.TestCase):
    """``sublane retire --migrate-hibernated-legacy``: a foreign / duplicate / unreadable
    occupant of the lane unit must be zero-write / zero-close / fixed-reason. Exact managed-slot
    absence stays a necessary conjunct; an already-retired replay re-verifies quiescence.

    This drives the #13897 gate through the PUBLIC CLI dispatch over a real (isolated) git-backed
    lane + a fake herdr inventory — the installed-surface counterpart of the internal #13897
    regression, added per j#83575 without re-implementing the #13897 runtime source.
    """

    def _migration(self, result):
        return result.json()["hibernated_legacy_retire_migration"]

    def test_foreign_only_occupant_blocks_zero_write_zero_close(self):
        h = InstalledFaultHarness(self)
        ctx = h.legacy_migration_lane("issue_14097_legacyA", issue="14097")
        h.seed_foreign_occupant(ctx, provider="gemini")
        before = h.live_locator_count()
        result = h.retire_migrate_cli(ctx)
        self.assertEqual(result.rc, 1)
        migration = self._migration(result)
        self.assertEqual(migration["reason"], "foreign_inventory_present")
        self.assertEqual(migration["expected_live"], [])  # no expected managed slot is live
        self.assertTrue(migration["foreign_names"])  # yet the unit is not quiescent
        self.assertFalse(result.json()["retire_ok"])
        # zero-write: the durable row stays hibernated; zero-close: the foreign agent survives.
        self.assertEqual(h.legacy_disposition(ctx), "hibernated")
        self.assertEqual(h.live_locator_count(), before)

    def test_duplicate_managed_rows_block_zero_write(self):
        h = InstalledFaultHarness(self)
        ctx = h.legacy_migration_lane("issue_14097_legacyB", issue="14097")
        h.seed_duplicate_managed(ctx, role="codex")
        result = h.retire_migrate_cli(ctx)
        self.assertEqual(result.rc, 1)
        self.assertEqual(self._migration(result)["reason"], "duplicate_inventory")
        self.assertEqual(h.legacy_disposition(ctx), "hibernated")

    def test_locatorless_expected_row_blocks_zero_write(self):
        # An unreadable / locator-less expected row is "cannot resolve", not "absent".
        h = InstalledFaultHarness(self)
        ctx = h.legacy_migration_lane("issue_14097_legacyC", issue="14097")
        h.seed_locatorless_expected(ctx, role="codex")
        result = h.retire_migrate_cli(ctx)
        self.assertEqual(result.rc, 1)
        self.assertEqual(self._migration(result)["reason"], "expected_identity_unresolved")
        self.assertEqual(h.legacy_disposition(ctx), "hibernated")

    def test_a_quiescent_unit_migrates_and_replay_is_idempotent(self):
        # Exact managed-slot absence + no foreign / duplicate / unreadable = quiescent -> migrate;
        # a duplicate replay re-verifies quiescence and is an idempotent no-op.
        h = InstalledFaultHarness(self)
        ctx = h.legacy_migration_lane("issue_14097_legacyD", issue="14097")
        first = h.retire_migrate_cli(ctx)
        self.assertEqual(first.rc, 0)
        self.assertEqual(self._migration(first)["state"], "retired")
        self.assertEqual(h.legacy_disposition(ctx), "retired")
        replay = h.retire_migrate_cli(ctx)
        self.assertEqual(replay.rc, 0)
        self.assertEqual(self._migration(replay)["state"], "already_retired")
        self.assertEqual(h.legacy_disposition(ctx), "retired")

    def test_already_retired_replay_re_blocks_on_a_foreign_occupant(self):
        # A persisted `retired` does not prove present quiescence: an occupant appearing after
        # the migration must re-block the replay (success withheld), zero-write.
        h = InstalledFaultHarness(self)
        ctx = h.legacy_migration_lane("issue_14097_legacyE", issue="14097")
        self.assertEqual(h.retire_migrate_cli(ctx).rc, 0)
        self.assertEqual(h.legacy_disposition(ctx), "retired")
        h.seed_foreign_occupant(ctx, provider="gemini")
        replay = h.retire_migrate_cli(ctx)
        self.assertEqual(replay.rc, 1)
        self.assertEqual(self._migration(replay)["reason"], "foreign_inventory_present")
        self.assertEqual(h.legacy_disposition(ctx), "retired")  # stays retired, success withheld

    def test_a_foreign_occupant_in_another_lane_does_not_block(self):
        # The fence is scoped to the TARGETED unit: a foreign occupant of a different lane is
        # none of this migration's business (exact managed-slot absence stays the conjunct).
        h = InstalledFaultHarness(self)
        ctx = h.legacy_migration_lane("issue_14097_legacyF", issue="14097")
        # A foreign occupant in a DIFFERENT lane of the SAME workspace.
        h.seed_foreign_occupant(ctx, provider="gemini", lane_id="issue_99999_other_lane")
        result = h.retire_migrate_cli(ctx)
        self.assertEqual(result.rc, 0)
        self.assertEqual(self._migration(result)["state"], "retired")
        self.assertEqual(self._migration(result)["foreign_names"], [])


# ---------------------------------------------------------------------------
# Cleanup — the scratch inventory / stores never amplify managed state
# ---------------------------------------------------------------------------
class ScratchCleanupNeverAmplifies(unittest.TestCase):
    """After the faults are driven + retired, the scratch inventory drains to zero (no residue)."""

    def test_rollback_and_retire_leave_no_live_residue(self):
        h = InstalledFaultHarness(self)
        # A nested rollback discharge is the harness's own retire rail for a scratch launch.
        action_id, _ = h.seed_owed_rollback("issue_14097_cleanup", providers=("claude", "codex"))
        self.assertEqual(h.live_locator_count(), 2)
        h.session_rollback_cli(action_id, execute=True)
        self.assertEqual(h.live_locator_count(), 0)  # both scratch slots retired, zero residue

    def test_a_callback_lease_scenario_leaves_a_healthy_bounded_store(self):
        # A recovered lease store is healthy and bounded (one DB + one sidecar) — the recovery
        # never amplifies pending / lease / dead-letter rows.
        h = InstalledFaultHarness(self)
        h.callback_lease_cli("--bootstrap")
        h.lease_store().path.unlink()
        fingerprint = h.lease_fingerprint_from(h.callback_lease_cli())
        h.callback_lease_cli("--recover", "--apply", "--expect-fingerprint", fingerprint)
        self.assertEqual(h.callback_lease_cli().rc, 0)  # healthy, single bounded store


if __name__ == "__main__":
    unittest.main()
