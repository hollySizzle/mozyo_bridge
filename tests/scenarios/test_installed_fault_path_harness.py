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
   idle launch that owes a rollback is surfaced by the read-only preflight. When the selected
   Herdr runtime lacks server-side conditional close for the observed pane generation, the
   participant verdict is ``conditional_close_unavailable`` and ``--execute`` reports the same
   reason; no pane is closed and the rollback debt remains retryable. A busy / foreign / ambiguous
   slot is likewise never closed.
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
scratch inventory never grows as a side effect. When the runtime cannot close conditionally, the
measured live residue is preserved until the isolated fixture is structurally removed.
"""

from __future__ import annotations

import contextlib
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

from tests.support.installed_fault_harness import InstalledFaultHarness

# The F2 acceptance predicate is single-sourced in the installed-smoke orchestrator (review
# j#85253): load it so this hermetic scenario accepts/rejects the completed terminal by the SAME
# rule the installed smoke's positive drive and negative control use — one predicate, no copies.
_SMOKE = Path(__file__).resolve().parents[2] / "smoke" / "installed_fault_smoke.py"
_smoke_spec = importlib.util.spec_from_file_location("installed_fault_smoke", _SMOKE)
_smoke = importlib.util.module_from_spec(_smoke_spec)
assert _smoke_spec.loader is not None
_smoke_spec.loader.exec_module(_smoke)
recover_stale_accepts = _smoke.recover_stale_accepts


class _FreshStaleRecoveryApprovalSource:
    """A fresh-reader fixture carrying one operation-bound canonical approval journal."""

    fresh_read = True

    def __init__(self, *, issue: str, journal: str, marker: str) -> None:
        self.issue = issue
        self.journal = journal
        self.marker = marker

    def read_entries(self, issue: str):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            RedmineJournalEntry,
        )

        if str(issue) != self.issue:
            return []
        # Return a new projection on every call: this fixture models an action-time fresh read,
        # not a cached object whose reuse could accidentally satisfy the destructive boundary.
        return [RedmineJournalEntry(self.issue, self.journal, self.marker)]


@contextlib.contextmanager
def _fresh_stale_recovery_approval(ctx):
    """Wire the exact canonical owner approval for ``ctx`` into the public CLI seam."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
        LiveRedmineJournalSource,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery import (  # noqa: E501
        RecoveryRequest,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_owner_approval import (  # noqa: E501
        STALE_WORKER_RECOVERY_APPROVAL_EFFECT,
        STALE_WORKER_RECOVERY_APPROVAL_GATE,
        render_recovery_owner_approval_marker,
        stale_worker_recovery_approval_operation,
    )

    journal = "79485"
    request = RecoveryRequest(
        issue=ctx.issue,
        lane=ctx.lane_id,
        role="claude",
        provider="claude",
        assigned_name=ctx.worker_name,
        locator=ctx.worker_locator,
        journal=journal,
        action_id=ctx.action_id,
        action_generation=7,
        worker_revision=ctx.worker_revision,
        lane_revision=ctx.lane_revision,
        lane_generation=ctx.lane_generation,
        expected_gate="implementation_request",
        next_semantic_action="dispatch_once",
    )
    marker = render_recovery_owner_approval_marker(
        gate=STALE_WORKER_RECOVERY_APPROVAL_GATE,
        effect=STALE_WORKER_RECOVERY_APPROVAL_EFFECT,
        issue=ctx.issue,
        lane=ctx.lane_id,
        operation=stale_worker_recovery_approval_operation(request),
    )

    def _source(**_kwargs):
        return _FreshStaleRecoveryApprovalSource(
            issue=ctx.issue, journal=journal, marker=marker
        )

    with mock.patch.object(LiveRedmineJournalSource, "from_environment", side_effect=_source):
        yield


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
    """``herdr session-rollback`` preserves panes when atomic close is unavailable."""

    def test_preflight_execute_and_replay_preserve_debt_without_conditional_close(self):
        h = InstalledFaultHarness(self)
        action_id, _ = h.seed_owed_rollback("issue_14097_nested", providers=("claude",))
        self.assertEqual(h.live_locator_count(), 1)

        preflight = h.session_rollback_cli(action_id)
        self.assertEqual(preflight.rc, 0)
        payload = preflight.json()
        self.assertEqual(payload["reason"], "preflight_only")
        self.assertEqual(payload["state"], "blocked")
        self.assertFalse(payload["executed"])  # a preflight closes nothing
        self.assertEqual(
            payload["participants"][0]["verdict"], "conditional_close_unavailable"
        )
        self.assertFalse(payload["participants"][0]["closed"])
        self.assertEqual(h.live_locator_count(), 1)  # preflight is zero-close

        execute = h.session_rollback_cli(action_id, execute=True)
        self.assertEqual(execute.rc, 1)
        blocked = execute.json()
        self.assertFalse(blocked["executed"])
        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(blocked["reason"], "conditional_close_unavailable")
        self.assertEqual(
            blocked["participants"][0]["verdict"], "conditional_close_unavailable"
        )
        self.assertFalse(blocked["participants"][0]["closed"])
        self.assertEqual(h.live_locator_count(), 1)  # no non-atomic read-then-close fallback

        replay = h.session_rollback_cli(action_id)
        self.assertEqual(replay.rc, 0)
        replayed = replay.json()
        self.assertEqual(replayed["reason"], "preflight_only")
        self.assertEqual(replayed["state"], "blocked")
        self.assertEqual(
            replayed["participants"][0]["verdict"], "conditional_close_unavailable"
        )
        self.assertEqual(h.live_locator_count(), 1)  # debt and participant remain retryable

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

    def test_same_binding_new_action_keeps_old_debt_and_refuses_noncanonical_inventory(self):
        # Without conditional close, action A cannot be discharged. A later fixture reservation
        # for the SAME durable name still mints a distinct action id. Duplicate logical names
        # violate the globally unique terminal-identity snapshot, so neither action interprets
        # the inventory or closes either row.
        h = InstalledFaultHarness(self)
        action_a, _ = h.seed_owed_rollback(
            "issue_14097_replay", providers=("claude",), nonce="n1"
        )
        first = h.session_rollback_cli(action_a, execute=True)
        self.assertEqual(first.rc, 1)
        self.assertEqual(first.json()["reason"], "conditional_close_unavailable")
        self.assertEqual(h.live_locator_count(), 1)  # action A's participant is preserved

        # The same binding (same startup unit) gets a fresh reservation under a NEW action id.
        action_b, _ = h.seed_owed_rollback(
            "issue_14097_replay", providers=("claude",), nonce="n2"
        )
        self.assertNotEqual(action_b, action_a)  # a distinct new action id
        replay = h.session_rollback_cli(action_b).json()
        self.assertEqual(replay["state"], "blocked")
        self.assertEqual(replay["participants"][0]["verdict"], "inventory_unreadable")
        self.assertEqual(h.live_locator_count(), 2)

        # Action A's debt is not silently discharged or rewritten by action B.
        original = h.session_rollback_cli(action_a).json()
        self.assertEqual(original["state"], "blocked")
        self.assertEqual(original["participants"][0]["verdict"], "inventory_unreadable")
        self.assertEqual(h.live_locator_count(), 2)


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

    def test_recovery_drives_to_completed_terminal_single_redispatch_additional_close_zero(self):
        # THE full #13806 recovery acceptance measured through the public orchestration path:
        # pass 1 closes the exact stale worker (once) and OWNS the launch; pass 2 attests the
        # fresh receiver, recognises the durable launch-owed transaction as a post-close resume,
        # and drives it to the COMPLETED terminal — a single confirmed queue-enter redispatch of
        # the original gate, with NO additional close (Redmine #14097 review j#85090 F2). Asserting
        # ``post_close_resume`` alone was a false green: that flag is true for an authority refusal,
        # a stopped launch/attestation, or ``redispatch_status=uncertain`` too.
        h = InstalledFaultHarness(self)
        ctx = h.recover_stale_git_lane("issue_14097_resume", issue="14097")
        self.assertEqual(h.live_locator_count(), 2)  # the stale worker + its surviving gateway

        with _fresh_stale_recovery_approval(ctx):
            outcome = h.drive_recover_stale_to_completion(ctx)

        # THE single shared acceptance predicate — the same one the installed smoke's positive
        # drive scores and its negative control negates (review j#85253).
        self.assertTrue(recover_stale_accepts(outcome.acceptance_outcome()))

        # Pass 1: the exact old worker was closed once and the launch was owed (in_progress).
        self.assertTrue(outcome.first["executed"])
        self.assertTrue(outcome.first["closed_old_worker"])
        self.assertEqual(outcome.first["status"], "stopped")
        self.assertEqual(outcome.first["recovery_status"], "in_progress")

        # A distinct fresh worker was launched (not the vanished old locator).
        self.assertTrue(outcome.fresh_locator)
        self.assertNotEqual(outcome.fresh_locator, outcome.old_locator)

        # Pass 2: the COMPLETED terminal — replaced worker + single confirmed redispatch.
        second = outcome.second
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["recovery_status"], "recovered")
        self.assertEqual(second["redispatch_status"], "confirmed")
        self.assertTrue(second["fresh_slot_attested"])
        self.assertTrue(second["post_close_resume"])
        # ``closed_old_worker`` is the DURABLE close-committed reflection (the participant is past
        # ``close_owed``), so a completed post-close resume necessarily still reports it true — it
        # is NOT a per-pass "closed something now" flag (`_closed_old_worker`, the phase predicate).
        self.assertTrue(second["closed_old_worker"])

        # "additional close 0" as an OBSERVABLE, not a boolean: pass 2 removed no inventory row
        # (a close deletes the pane), and it fired exactly ONE exact-marker redispatch attempt to
        # the fresh worker, which confirmed (single redispatch, never a duplicate dispatch).
        self.assertEqual(outcome.agents_before, outcome.agents_after)
        self.assertEqual(outcome.redispatch_attempt_count, 1)
        self.assertEqual(outcome.redispatch_ok_count, 1)

    def test_an_installed_launcher_lacking_generation_protocol_refuses_the_heal_launch(self):
        # #14203 review j#87479 F1 (installed-launcher fault harness): an installed launcher
        # whose attestation schema/store contract landed but that predates the generation
        # protocol event is refused at the preservation boundary BEFORE a heal closes the old
        # worker or launches a fresh one. No pair whose generation could never be finalized is
        # actuated.
        #
        # Baseline (non-vacuous control): a fully-capable installed launcher heals — the first
        # `--execute` pass owns and lands a fresh worker.
        capable = InstalledFaultHarness(self)
        cap_ctx = capable.recover_stale_git_lane("issue_14203_gen_capable", issue="14203")
        with _fresh_stale_recovery_approval(cap_ctx):
            capable.recover_stale_cli(cap_ctx, execute=True)
        self.assertTrue(capable._fresh_worker_locator(cap_ctx))

        # Fault: the SAME heal through a generation-incapable launcher stops at effect_failed
        # with no fresh worker and no fresh attestation (the launch preflight refused it).
        faulty = InstalledFaultHarness(self)
        ctx = faulty.recover_stale_git_lane("issue_14203_gen_incapable", issue="14203")
        faulty.make_launcher_generation_incapable()
        before = faulty.live_locator_count()

        with _fresh_stale_recovery_approval(ctx):
            outcome = faulty.recover_stale_cli(ctx, execute=True).json()
        self.assertEqual(outcome["recovery_status"], "preservation_blocked")
        self.assertIn("generation_protocol_contract_absent", outcome["detail"])
        self.assertFalse(outcome["closed_old_worker"])
        self.assertFalse(outcome["fresh_slot_attested"])
        self.assertFalse(faulty._fresh_worker_locator(ctx))
        self.assertEqual(faulty.live_locator_count(), before)

    def test_injected_uncertain_redispatch_is_rejected_by_the_shared_predicate(self):
        # The negative CONTROL: an attestation landing OUTSIDE the redispatch's durable window makes
        # the confirm fence reject the send, so the drive stops at ``redispatch_status=uncertain`` —
        # and the SAME shared acceptance predicate the positive test asserts must return False on it
        # (Redmine #14097 review j#85253). Sharing one predicate means the post_close_resume-only
        # regression j#85090 flagged would flip THIS control red, not pass silently.
        h = InstalledFaultHarness(self)
        ctx = h.recover_stale_git_lane("issue_14097_uncertain", issue="14097")

        with _fresh_stale_recovery_approval(ctx):
            outcome = h.drive_recover_stale_to_completion(ctx, inject_uncertain=True)

        # The injection actually landed the uncertain fault (guard against a vacuous negation)...
        self.assertNotEqual(outcome.second["status"], "completed")
        self.assertEqual(outcome.second["redispatch_status"], "uncertain")
        # ...and the ONE shared predicate rejects it.
        self.assertFalse(recover_stale_accepts(outcome.acceptance_outcome()))

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
    """Fault drives never add hidden effects; isolated teardown removes their scratch state."""

    def test_rollback_without_conditional_close_preserves_exact_live_residue(self):
        h = InstalledFaultHarness(self)
        # Herdr 0.8 cannot atomically close an observed pane generation. The public rail must
        # report the residue and preserve both rows; the harness's structural temp teardown owns
        # final cleanup, so production never falls back to an unsafe read-then-close sequence.
        action_id, _ = h.seed_owed_rollback("issue_14097_cleanup", providers=("claude", "codex"))
        self.assertEqual(h.live_locator_count(), 2)
        result = h.session_rollback_cli(action_id, execute=True)
        self.assertEqual(result.rc, 1)
        payload = result.json()
        self.assertEqual(payload["state"], "blocked")
        self.assertEqual(payload["reason"], "conditional_close_unavailable")
        self.assertEqual(
            {participant["verdict"] for participant in payload["participants"]},
            {"conditional_close_unavailable"},
        )
        self.assertTrue(
            all(not participant["closed"] for participant in payload["participants"])
        )
        self.assertEqual(h.live_locator_count(), 2)  # zero close and zero amplification

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
