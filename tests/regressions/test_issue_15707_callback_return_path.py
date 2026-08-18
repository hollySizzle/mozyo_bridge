"""Redmine #15707 — the gateway->coordinator callback return path's fixed defects.

One file per R3-c (tests-placement-discovery-policy): every defect fixed under #15707 is
pinned here. The measured incident (2026-08-18): every lane's completion callback to the
coordinator systematically zero-sent or terminalized, so finished lanes looked stalled.

Pinned defects:

- **(a) receiver token** (#15704 j#108012): the callback send port hardcoded ``--to codex``
  while the ``coordinator`` route resolved to the claude pane the rebound coordinator role
  binds (#13229) — refused as ``invalid_args`` by the receiver-binding fence. Fixed: the port
  derives the token (stamped ``target_receiver`` -> role authority), fail-closed.
- **(a') invalid non-blank stamp** (review j#108062 finding_receiverstamp): the first fix let
  a non-provider stamp silently fall back to the CURRENT role authority, papering over a
  broken durable expectation. Fixed: typed pre-send refusal, resolver only for a BLANK stamp.
- **(b) default-lane target repo** (#15701 j#107992 / #15704 j#108011): the coordinator
  (default) lane is main-checkout resident and structurally row-less, so ``--target-repo
  auto`` always refused it with ``lane_binding_absent``. Fixed: verified read-only registry
  fallback.
- **(c) busy dead-letter** (#15700 j#107939 / #15702 j#107933): ``precondition_not_idle``
  bounded retries terminalized a deliverable row and no path could ever deliver it again.
  Fixed: explicit fingerprint-gated redrive that re-admits into the fenced pipeline.
- **(c') dry-run migration** (review j#108062 finding_dryrunmigration): the redrive dry-run
  migrated a recognized older store just by reading it. Fixed: strictly read-only.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from mozyo_bridge.core.state.callback_outbox import (  # noqa: E402
    CallbackOutbox,
    CallbackOutboxRow,
)
from mozyo_bridge.core.state.callback_outbox_redrive import (  # noqa: E402
    REDRIVE_FINGERPRINT_MISMATCH,
    REDRIVE_REQUEUED,
    CallbackRedriveStore,
)
from mozyo_bridge.core.state.workflow_runtime_store import (  # noqa: E402
    CALLBACK_DEAD_LETTER,
    CALLBACK_DELIVERED,
    CALLBACK_PENDING,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.herdr_auto_target_root import (  # noqa: E402,E501
    BASIS_WORKSPACE_CANONICAL,
    REFUSE_LANE_BINDING_ABSENT,
    resolve_herdr_auto_target_repo,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_outbox_processor import (  # noqa: E402,E501
    CallbackCandidate,
    CallbackOutboxProcessor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_send_port import (  # noqa: E402,E501
    RECEIVER_PROVIDER_UNRESOLVED,
    RECEIVER_STAMP_INVALID,
    HandoffCallbackSendPort,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.handoff_callback_sender import (  # noqa: E402,E501
    HandoffCallbackSender,
    HandoffDeliveryResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (  # noqa: E402,E501
    SEND_NOT_SENT,
    ZERO_SEND_REASON_ALLOWLIST,
    normalize_zero_send_reason,
    send_outcome_for_delivery,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E402,E501
    RedmineJournalEntry,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (  # noqa: E402,E501
    PROVIDER_CLAUDE,
    ROLE_COORDINATOR,
    RoleProviderBinding,
)

#: The incident binding (#15631 / #13229): coordinator rebound onto the implementation
#: provider — exactly what ``agents.roles.coordinator: implementation`` resolves to.
COORDINATOR_REBOUND = RoleProviderBinding.default().with_overrides(
    {ROLE_COORDINATOR: PROVIDER_CLAUDE}
)


# ---------------------------------------------------------------------------------------------
# (a) / (a') receiver token derivation (#15704 j#108012; review j#108062 finding_receiverstamp)
# ---------------------------------------------------------------------------------------------


def _port_row(target_receiver=""):
    return CallbackOutboxRow(
        source="redmine", issue="15704", journal="108010",
        normalized_gate="review", callback_route="coordinator", state="inflight",
        attempts=0, max_attempts=3, send_attempted=True, notification_kind="review_result",
        notification_summary="", gate_mismatch=False, detail="", payload="",
        workspace_id="ws-15707", target_receiver=target_receiver,
    )


def _capture_port(**kwargs):
    calls = []
    port = HandoffCallbackSendPort(
        runner=lambda argv: calls.append(argv) or (0, '{"status": "sent", "reason": "ok"}'),
        **kwargs,
    )
    return port, calls


def _to_value(argv):
    return argv[argv.index("--to") + 1]


class StampedReceiverBindsTheToken(unittest.TestCase):
    def test_the_stamped_binding_resolved_provider_is_the_token(self) -> None:
        port, calls = _capture_port()
        result = port(_port_row(target_receiver=PROVIDER_CLAUDE))
        self.assertEqual(result.status, "sent")
        self.assertEqual(_to_value(calls[0]), PROVIDER_CLAUDE)


class BlankStampDerivesFromTheRoleAuthority(unittest.TestCase):
    def test_the_incident_binding_derives_claude_not_the_old_literal(self) -> None:
        # The exact j#108012 arrangement: a coordinator-route row with no stamped receiver,
        # under the coordinator->claude rebind. The port's DEFAULT resolver chain
        # (resolve_coordinator_provider) must answer claude — the pre-#15707 literal `codex`
        # is the refused contradiction.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            workflow_provider_resolution,
        )

        port, calls = _capture_port()
        with mock.patch.object(
            workflow_provider_resolution,
            "load_workflow_binding",
            return_value=(COORDINATOR_REBOUND, ()),
        ):
            result = port(_port_row(target_receiver=""))
        self.assertEqual(result.status, "sent")
        self.assertEqual(_to_value(calls[0]), PROVIDER_CLAUDE)


class InvalidNonBlankStampRefusesTyped(unittest.TestCase):
    def test_a_non_provider_stamp_never_reaches_the_resolver(self) -> None:
        # Review j#108062 finding_receiverstamp: the broken durable expectation refuses; the
        # current role authority is never consulted to paper over it.
        port, calls = _capture_port(
            coordinator_provider_resolver=lambda: (_ for _ in ()).throw(AssertionError(
                "the resolver must not be consulted for an invalid non-blank stamp"
            ))
        )
        result = port(_port_row(target_receiver="coordinator"))
        self.assertEqual(
            (result.status, result.reason), ("blocked", RECEIVER_STAMP_INVALID)
        )
        self.assertEqual(result.injection_stage, "not_sent")
        self.assertEqual(calls, [])

    def test_the_stamp_refusal_reason_survives_durable_normalization(self) -> None:
        self.assertIn(RECEIVER_STAMP_INVALID, ZERO_SEND_REASON_ALLOWLIST)
        self.assertEqual(
            normalize_zero_send_reason(RECEIVER_STAMP_INVALID), RECEIVER_STAMP_INVALID
        )


class UnresolvedDerivationFailsClosedAsNotSent(unittest.TestCase):
    def test_refusal_is_typed_and_pre_send(self) -> None:
        def unresolved():
            raise RuntimeError("role authority unavailable")

        port, calls = _capture_port(coordinator_provider_resolver=unresolved)
        result = port(_port_row(target_receiver=""))
        self.assertEqual(
            (result.status, result.reason), ("blocked", RECEIVER_PROVIDER_UNRESOLVED)
        )
        self.assertEqual(result.injection_stage, "not_sent")
        self.assertEqual(calls, [])  # zero bytes typed: the handoff was never invoked

    def test_refusal_classifies_as_bounded_retry_not_uncertain(self) -> None:
        # The dead-letter path stays bounded-retry (the row can be redriven after a config
        # repair) instead of poisoning to the never-retried `uncertain` terminal.
        outcome = send_outcome_for_delivery(
            "blocked", RECEIVER_PROVIDER_UNRESOLVED, injection_stage="not_sent"
        )
        self.assertEqual(outcome, SEND_NOT_SENT)

    def test_refusal_reason_survives_durable_normalization(self) -> None:
        self.assertIn(RECEIVER_PROVIDER_UNRESOLVED, ZERO_SEND_REASON_ALLOWLIST)
        self.assertEqual(
            normalize_zero_send_reason(RECEIVER_PROVIDER_UNRESOLVED),
            RECEIVER_PROVIDER_UNRESOLVED,
        )


# ---------------------------------------------------------------------------------------------
# (b) default-lane --target-repo auto resolution (#15701 j#107992 / #15704 j#108011)
# ---------------------------------------------------------------------------------------------

WORKSPACE_ID = "fixture-15707-workspace"
SENDER_LANE = "issue_15707_callback_robustness"


class DefaultLaneAutoTargetRepoTest(unittest.TestCase):
    def setUp(self) -> None:
        from mozyo_bridge.core.state.workspace_registry import write_anchor
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            repo_scope_workspace_id,
        )
        from support.herdr_workspace_fixtures import _anchor_record

        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name).resolve()
        self.home = base / "home"
        self.home.mkdir()
        # Hermetic home: the registry AND the lifecycle authority resolve through
        # `mozyo_bridge_home()`, so the fixture stays off the operator's real state.
        self._prev_home = os.environ.get("MOZYO_BRIDGE_HOME")
        os.environ["MOZYO_BRIDGE_HOME"] = str(self.home)
        self.addCleanup(self._restore_home)

        self.repo = base / "repo"
        self._env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@x",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@x",
            "PATH": "/usr/bin:/bin",
        }
        self._git("init", "-q", "-b", "main", str(self.repo))
        # #14685: a synthetic repo must not let git auto maintenance daemonize into the
        # temp tree TemporaryDirectory is about to remove.
        self._git("-C", str(self.repo), "config", "--local", "maintenance.auto", "false")
        self._git("-C", str(self.repo), "config", "--local", "gc.auto", "0")
        (self.repo / "seed.txt").write_text("seed\n")
        self._git("-C", str(self.repo), "add", "-A")
        self._git("-C", str(self.repo), "commit", "-qm", "c1")

        # The sender's lane worktree — the gateway a callback fires from.
        self.sender_worktree = base / "wt-lane"
        self._git(
            "-C", str(self.repo), "worktree", "add", "-q",
            str(self.sender_worktree), "-b", SENDER_LANE,
        )

        write_anchor(self.repo, _anchor_record(WORKSPACE_ID, self.repo))
        scope = repo_scope_workspace_id(self.sender_worktree)
        if scope != WORKSPACE_ID:
            self.skipTest(
                f"the fixture repo resolved workspace {scope!r}, not the anchored "
                f"{WORKSPACE_ID!r} (is TMPDIR inside a git worktree?)"
            )

    def _restore_home(self) -> None:
        if self._prev_home is None:
            os.environ.pop("MOZYO_BRIDGE_HOME", None)
        else:
            os.environ["MOZYO_BRIDGE_HOME"] = self._prev_home
        self._tmp.cleanup()

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args], check=True, capture_output=True, env=self._env
        )

    def _register(self) -> None:
        from mozyo_bridge.core.state.workspace_registry import register_workspace

        register_workspace(self.repo)

    def _target_info(self, *, lane: str) -> dict:
        # The synthesized herdr target record exactly as `resolve_herdr_send_target` leaves
        # it for `auto`: no pane cwd, route identity = the coordinator's unit.
        return {
            "id": "mzb1_ws_claude_lane",
            "cwd": "",
            "workspace_id": WORKSPACE_ID,
            "lane_id": lane,
            "herdr_sender_workspace_id": WORKSPACE_ID,
            "herdr_sender_lane_id": SENDER_LANE,
        }

    def _resolve(self, *, lane: str):
        return resolve_herdr_auto_target_repo(
            self.sender_worktree, self._target_info(lane=lane)
        )

    def test_default_lane_resolves_the_registered_main_checkout(self) -> None:
        # The j#107992 / j#108011 arrangement, fixed: gateway lane worktree -> coordinator
        # (default) lane, no lifecycle row, registry knows the canonical main checkout.
        self._register()
        resolved = self._resolve(lane="default")
        self.assertTrue(resolved.ok, (resolved.reason, resolved.detail))
        self.assertEqual(Path(resolved.root).resolve(), self.repo.resolve())
        self.assertEqual(resolved.basis, BASIS_WORKSPACE_CANONICAL)

    def test_an_empty_lane_normalizes_to_default_and_resolves(self) -> None:
        # `--target coordinator` derives the DEFAULT lane; a blank lane id is the same unit.
        self._register()
        resolved = self._resolve(lane="")
        self.assertTrue(resolved.ok, (resolved.reason, resolved.detail))
        self.assertEqual(Path(resolved.root).resolve(), self.repo.resolve())

    def test_without_a_registry_row_the_refusal_is_unchanged(self) -> None:
        resolved = self._resolve(lane="default")
        self.assertFalse(resolved.ok)
        self.assertEqual(resolved.reason, REFUSE_LANE_BINDING_ABSENT)
        self.assertEqual(resolved.root, "")

    def test_a_hijacked_canonical_refuses(self) -> None:
        # The #13152 shape: a registry row whose canonical_path is a LINKED worktree (not
        # the main checkout) must not answer the coordinator frame.
        from mozyo_bridge.core.state.workspace_registry import (
            load_workspace_by_id,
            registry_path,
        )

        self._register()
        record = load_workspace_by_id(WORKSPACE_ID)
        self.assertIsNotNone(record)
        conn = sqlite3.connect(registry_path())
        try:
            conn.execute(
                "UPDATE workspaces SET canonical_path=? WHERE workspace_id=?",
                (str(self.sender_worktree.resolve()), WORKSPACE_ID),
            )
            conn.commit()
        finally:
            conn.close()
        resolved = self._resolve(lane="default")
        self.assertFalse(resolved.ok)
        self.assertEqual(resolved.reason, REFUSE_LANE_BINDING_ABSENT)

    def test_a_non_default_rowless_lane_still_refuses(self) -> None:
        self._register()
        resolved = self._resolve(lane="issue_99999_other_lane")
        self.assertFalse(resolved.ok)
        self.assertEqual(resolved.reason, REFUSE_LANE_BINDING_ABSENT)

    def test_no_refusal_detail_carries_a_filesystem_path(self) -> None:
        # j#95911 finding 2: the detail travels onto the wire outcome / pasteable record.
        for arrange in (lambda: None, self._register):
            arrange()
            for lane in ("default", "issue_99999_other_lane"):
                resolved = self._resolve(lane=lane)
                if resolved.ok:
                    continue
                self.assertNotIn(str(self.repo), resolved.detail)
                self.assertNotIn(str(self.home), resolved.detail)


# ---------------------------------------------------------------------------------------------
# (c) busy dead-letter -> explicit redrive (#15700 j#107939 / #15702 j#107933)
# ---------------------------------------------------------------------------------------------

ISSUE = "15700"
JOURNAL = "107938"


class _FakeSource:
    def __init__(self, entries):
        self._entries = entries

    def read_entries(self, issue_id):
        return self._entries.get(str(issue_id), [])


def _busy_sender():
    """The measured coordinator-mid-turn shape: a deterministic pre-injection zero-send."""
    return HandoffCallbackSender(
        lambda row: HandoffDeliveryResult(
            "blocked", "precondition_not_idle", injection_stage="not_sent"
        )
    )


def _idle_sender(sent):
    return HandoffCallbackSender(
        lambda row: sent.append(row) or HandoffDeliveryResult("sent", "ok", injection_stage="submitted_confirmed")
    )


class BusyDeadLetterRedriveTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.outbox = CallbackOutbox(path=Path(self._tmp.name) / "workflow-runtime.sqlite")
        self.redrive = CallbackRedriveStore(self.outbox)
        source = _FakeSource(
            {
                ISSUE: [
                    RedmineJournalEntry(
                        issue_id=ISSUE,
                        journal_id=JOURNAL,
                        notes="[mozyo:workflow-event:gate=review_result]",
                    )
                ]
            }
        )
        self.processor = CallbackOutboxProcessor(self.outbox, source)
        self.processor.ingest(
            [CallbackCandidate(ISSUE, JOURNAL, "coordinator", "review_result")]
        )

    def _exhaust_against_busy_coordinator(self) -> None:
        busy = _busy_sender()
        for _ in range(3):  # the default bounded budget
            self.processor.deliver(busy)

    def test_busy_retries_dead_letter_with_the_normalized_reason(self) -> None:
        self._exhaust_against_busy_coordinator()
        row = self.outbox.read()[0]
        self.assertEqual(row.state, CALLBACK_DEAD_LETTER)
        self.assertIn("precondition_not_idle", row.detail)

    def test_dead_letter_stays_invisible_to_automatic_delivery(self) -> None:
        self._exhaust_against_busy_coordinator()
        sent: list = []
        report = self.processor.deliver(_idle_sender(sent))
        self.assertEqual(sent, [])
        self.assertEqual(report.delivered, [])

    def test_explicit_redrive_then_idle_delivery_completes_the_callback(self) -> None:
        self._exhaust_against_busy_coordinator()
        row, fingerprint = self.redrive.dead_letter_fingerprints()[0]
        # A stale observation zero-writes; the exact one requeues.
        self.assertEqual(
            self.redrive.requeue_dead_letter(row.key, expect_fingerprint="stale"),
            REDRIVE_FINGERPRINT_MISMATCH,
        )
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DEAD_LETTER)
        self.assertEqual(
            self.redrive.requeue_dead_letter(row.key, expect_fingerprint=fingerprint),
            REDRIVE_REQUEUED,
        )
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_PENDING)
        # The redriven row is delivered by the NORMAL fenced pass once the coordinator is idle
        # — the redrive re-admitted it, nothing was sent out-of-band.
        sent: list = []
        self.processor.deliver(_idle_sender(sent))
        self.assertEqual([r.journal for r in sent], [JOURNAL])
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DELIVERED)

    def test_a_redriven_row_that_stays_busy_dead_letters_again_bounded(self) -> None:
        self._exhaust_against_busy_coordinator()
        row, fingerprint = self.redrive.dead_letter_fingerprints()[0]
        self.redrive.requeue_dead_letter(row.key, expect_fingerprint=fingerprint)
        # The grant is ONE fresh bounded budget, not an unbounded retry loop.
        for _ in range(8):
            self.processor.deliver(_busy_sender())
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DEAD_LETTER)


# ---------------------------------------------------------------------------------------------
# (c') the redrive dry-run must never migrate a store (review j#108062 finding_dryrunmigration)
# ---------------------------------------------------------------------------------------------


class DryRunNeverMigratesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "workflow-runtime.sqlite"

    def _write_v1_db(self) -> None:
        # The recognized-older-store shape the reviewer's probe used: a v1 workflow-runtime DB
        # with the legacy tables and NO callback_outbox table.
        conn = sqlite3.connect(self.path)
        conn.execute(
            "CREATE TABLE workflow_events (event_id TEXT PRIMARY KEY, issue TEXT NOT NULL, "
            "gate TEXT NOT NULL, review_conclusion TEXT NOT NULL, callback_state TEXT NOT NULL, "
            "commit_bearing INTEGER NOT NULL DEFAULT 0, integration_recorded INTEGER NOT NULL "
            "DEFAULT 0, issue_open INTEGER NOT NULL DEFAULT 1, blocker_recorded INTEGER NOT NULL "
            "DEFAULT 0, seq INTEGER NOT NULL, recorded_at TEXT NOT NULL)"
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

    def _schema_facts(self) -> "tuple[int, bool]":
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='callback_outbox'"
            ).fetchone()
            return version, table is not None
        finally:
            conn.close()

    def test_dry_run_on_a_v1_store_is_a_schema_level_zero_write(self) -> None:
        self._write_v1_db()
        redrive = CallbackRedriveStore(CallbackOutbox(path=self.path))
        # A recognized older store without the callback table reads as provably empty...
        self.assertEqual(redrive.dead_letter_fingerprints(), ())
        # ...and asking wrote NOTHING: user_version stays 1 and no table was created (the
        # pre-fix behavior migrated user_version 1 -> current and created callback_outbox).
        self.assertEqual(self._schema_facts(), (1, False))

    def test_dry_run_on_a_missing_store_creates_nothing(self) -> None:
        redrive = CallbackRedriveStore(CallbackOutbox(path=self.path))
        self.assertEqual(redrive.dead_letter_fingerprints(), ())
        self.assertFalse(self.path.exists())

    def test_an_unreadable_store_raises_instead_of_reading_empty(self) -> None:
        from mozyo_bridge.core.state.workflow_runtime_store import WorkflowRuntimeStoreError

        self.path.write_bytes(b"this is not a sqlite database at all........")
        redrive = CallbackRedriveStore(CallbackOutbox(path=self.path))
        with self.assertRaises(WorkflowRuntimeStoreError):
            redrive.dead_letter_fingerprints()


if __name__ == "__main__":
    unittest.main()
