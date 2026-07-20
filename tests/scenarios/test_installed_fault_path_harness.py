"""Deterministic installed fault-path harness (Redmine #14097).

Each of this repo's four release-critical fault shapes already has a deterministic regression,
but every one of those drives its use case / store / domain fold through **internal module
imports**. None routes ``argv`` through the *public* CLI dispatch (``build_parser() -> args.func``)
— the exact surface the installed ``mozyo-bridge`` binary runs. This scenario closes that gap: it
reproduces and MEASURES each fault shape through the public command dispatch, confined to an
isolated ``MOZYO_BRIDGE_HOME`` + a scratch herdr workspace / process (a fake herdr over the
subprocess boundary), so no managed lane / callback / lease is ever touched.

The four shapes and what the installed public surface must show (issue Acceptance + the
stale-locator addendum j#83362 + the callback-lease addendum j#83426):

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
