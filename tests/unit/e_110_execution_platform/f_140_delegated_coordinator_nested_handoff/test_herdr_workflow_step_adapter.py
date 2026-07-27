"""herdr-native `workflow step` application adapter tests (Redmine #13489).

Hermetic: the terminal-runtime seams (repo root, sender identity, lane-metadata anchor, live
inventory) are patched so no test depends on a repo-local config, the workspace registry, or a
live herdr binary. Pins the mid-review corrections (j#74748 / j#74749 / j#74750): the adapter
verifies the Redmine issue anchor from the lane metadata store (F3), reads the worker liveness
only when the gateway lane reaches the worker gate, folds the inventory into a 0 / 1 / 2+
cardinality (F2/D), and no longer consults registry project_name (F1).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    herdr_workflow_step as adapter,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_herdr import (
    ANCHOR_MISSING,
    ANCHOR_VERIFIED,
    REASON_HERDR_ANCHOR_UNRESOLVED,
    REASON_HERDR_DEFAULT_COORDINATOR_UNRESOLVED,
    REASON_HERDR_SENDER_IDENTITY_UNRESOLVED,
    REASON_HERDR_WORKER_AMBIGUOUS,
    REASON_HERDR_WORKER_DISPATCH_READY,
    REASON_HERDR_WORKER_STEP_READY,
    WORKER_ABSENT,
    WORKER_AMBIGUOUS,
    WORKER_LIVE,
    WORKER_LOCATOR_MISSING,
    WORKER_UNAVAILABLE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain import (
    herdr_target_resolution as htr,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_LOCATOR,
    AGENT_KEY_NAME,
    encode_assigned_name,
)

WS = "e1487dcb1f2d4412b28e825fdeccf9e8"
PTR = "redmine:issue=13489"


def _sender_ok(role, lane):
    return htr.SenderIdentityResolution.success(
        htr.SenderIdentity(workspace_id=WS, role=role, lane_id=lane)
    )


class ResolveHerdrStepOutcomeTest(unittest.TestCase):
    def setUp(self):
        from mozyo_bridge.application import commands_common

        self._patches = [
            patch.object(commands_common, "repo_root_from_args", return_value=Path("/repo")),
            patch.object(adapter, "_anchor_workspace_id", return_value=WS),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _run(self):
        return adapter.resolve_herdr_step_outcome(argparse.Namespace(repo=None))

    def test_missing_env_fails_closed(self):
        with patch.object(
            htr,
            "resolve_sender_identity",
            return_value=htr.SenderIdentityResolution.failure(
                htr.REASON_MISSING_SENDER_ENV, "unset"
            ),
        ):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_SENDER_IDENTITY_UNRESOLVED)
        self.assertEqual(out.execution, "blocked")

    def test_default_lane_blocks_without_anchor_or_inventory_read(self):
        with patch.object(
            htr, "resolve_sender_identity", return_value=_sender_ok("codex", "default")
        ), patch.object(
            adapter, "_resolve_lane_anchor", side_effect=AssertionError("anchor read for default")
        ), patch.object(
            adapter, "_same_lane_worker_liveness", side_effect=AssertionError("inventory read")
        ):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_DEFAULT_COORDINATOR_UNRESOLVED)

    def test_worker_verified_anchor_resolves_without_inventory_read(self):
        with patch.object(
            htr, "resolve_sender_identity", return_value=_sender_ok("claude", "issue_1")
        ), patch.object(
            adapter, "_resolve_lane_anchor", return_value=(ANCHOR_VERIFIED, PTR)
        ), patch.object(
            adapter, "_same_lane_worker_liveness", side_effect=AssertionError("inventory for worker")
        ):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_WORKER_STEP_READY)
        self.assertEqual(out.durable_anchor, PTR)

    def test_worker_missing_anchor_fails_closed(self):
        with patch.object(
            htr, "resolve_sender_identity", return_value=_sender_ok("claude", "issue_1")
        ), patch.object(adapter, "_resolve_lane_anchor", return_value=(ANCHOR_MISSING, "")):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_ANCHOR_UNRESOLVED)

    def test_gateway_verified_anchor_reads_worker_liveness(self):
        seen = {}

        def _liveness(ws, lane, *, env):
            seen["args"] = (ws, lane)
            return WORKER_LIVE

        with patch.object(
            htr, "resolve_sender_identity", return_value=_sender_ok("codex", "issue_1")
        ), patch.object(
            adapter, "_resolve_lane_anchor", return_value=(ANCHOR_VERIFIED, PTR)
        ), patch.object(adapter, "_same_lane_worker_liveness", side_effect=_liveness):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_WORKER_DISPATCH_READY)
        self.assertEqual(seen["args"], (WS, "issue_1"))

    def test_gateway_duplicate_worker_is_ambiguous(self):
        with patch.object(
            htr, "resolve_sender_identity", return_value=_sender_ok("codex", "issue_1")
        ), patch.object(
            adapter, "_resolve_lane_anchor", return_value=(ANCHOR_VERIFIED, PTR)
        ), patch.object(adapter, "_same_lane_worker_liveness", return_value=WORKER_AMBIGUOUS):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_WORKER_AMBIGUOUS)

    def test_gateway_missing_anchor_skips_inventory(self):
        with patch.object(
            htr, "resolve_sender_identity", return_value=_sender_ok("codex", "issue_1")
        ), patch.object(
            adapter, "_resolve_lane_anchor", return_value=(ANCHOR_MISSING, "")
        ), patch.object(
            adapter, "_same_lane_worker_liveness", side_effect=AssertionError("inventory read")
        ):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_ANCHOR_UNRESOLVED)


class SameLaneWorkerLivenessTest(unittest.TestCase):
    """The inventory fold cardinality (real assigned-name decode)."""

    def _rows(self, *specs):
        rows = []
        for role, lane, loc in specs:
            row = {AGENT_KEY_NAME: encode_assigned_name(WS, role, lane)}
            if loc:
                row[AGENT_KEY_LOCATOR] = loc
            rows.append(row)
        return rows

    def _patch_rows(self, rows=None, error=None):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            sublane_herdr_projection,
        )

        if error is not None:
            return patch.object(
                sublane_herdr_projection, "list_herdr_agent_rows", side_effect=error
            )
        return patch.object(
            sublane_herdr_projection, "list_herdr_agent_rows", return_value=rows
        )

    def test_single_worker_with_locator_is_live(self):
        with self._patch_rows(self._rows(("claude", "issue_1", "p1"), ("codex", "issue_1", "p2"))):
            self.assertEqual(adapter._same_lane_worker_liveness(WS, "issue_1", env={}), WORKER_LIVE)

    def test_no_worker_is_absent(self):
        with self._patch_rows(self._rows(("claude", "other", "p1"), ("codex", "issue_1", "p2"))):
            self.assertEqual(
                adapter._same_lane_worker_liveness(WS, "issue_1", env={}), WORKER_ABSENT
            )

    def test_duplicate_workers_is_ambiguous(self):
        with self._patch_rows(self._rows(("claude", "issue_1", "p1"), ("claude", "issue_1", "p9"))):
            self.assertEqual(
                adapter._same_lane_worker_liveness(WS, "issue_1", env={}), WORKER_AMBIGUOUS
            )

    def test_single_worker_without_locator_is_locator_missing(self):
        with self._patch_rows(self._rows(("claude", "issue_1", ""))):
            self.assertEqual(
                adapter._same_lane_worker_liveness(WS, "issue_1", env={}), WORKER_LOCATOR_MISSING
            )

    def test_inventory_error_is_unavailable(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
            HerdrSessionStartError,
        )

        with self._patch_rows(error=HerdrSessionStartError("down")):
            self.assertEqual(
                adapter._same_lane_worker_liveness(WS, "issue_1", env={}), WORKER_UNAVAILABLE
            )


from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    MappingRedmineJournalSource,
    RedmineJournalEntry,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (
    LiveRedmineJournalError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_work_anchor import (
    WORK_ANCHOR_AMBIGUOUS,
    WORK_ANCHOR_FOREIGN,
    WORK_ANCHOR_MISSING,
    WORK_ANCHOR_RESOLVED,
    WORK_ANCHOR_STALE_GENERATION,
    WORK_ANCHOR_UNBOUND,
    LaneWorkAnchor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_herdr import (
    ANCHOR_AMBIGUOUS,
    ANCHOR_RETIRED,
    ANCHOR_STORE_MISMATCH,
    ANCHOR_UNVERIFIED,
    ANCHOR_WORK_AMBIGUOUS,
    ANCHOR_WORK_FOREIGN,
    ANCHOR_WORK_MISSING,
    ANCHOR_WORK_STALE,
    ANCHOR_WORK_UNBOUND,
)

VERIFIED_PTR = "redmine:issue=13489:journal=74766"

# A real structured gate marker (handoff channel, gate-bearing kind) in a journal note. The
# journal record's own id (74766) is the authoritative journal anchor, NOT the token's journal
# field (redmine_journal_source contract).
_GATE_NOTE = "[mozyo:handoff:source=redmine:issue=13489:journal=74766:kind=review_result:to=claude] review result"

#: The canonical dispatch marker that binds work to a lane + generation — the ONLY thing the work
#: anchor is resolved from (Redmine #14586). ``lane_generation`` is what makes it a binding rather
#: than "the newest thing on the issue".
LANE = "issue_1"


def _dispatch_note(lane=LANE, generation=1, body="implementation request"):
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
        render_dispatch_note,
    )

    return render_dispatch_note(body, lane=lane, lane_generation=generation)


def _work_anchor(journal="74766", status=WORK_ANCHOR_RESOLVED, **kw):
    return LaneWorkAnchor(status=status, journal=journal, lane=LANE, lane_generation=1, **kw)


def _lane_record(**kw):
    base = dict(repo_workspace_id=WS, lane_id="issue_1", issue_id="13489", retired=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _snapshot_source(journals):
    return MappingRedmineJournalSource(payload={"issue": {"id": "13489"}, "journals": journals})


class CandidateIssueTest(unittest.TestCase):
    """Lane-metadata candidate issue with preserved record cardinality (F3b)."""

    def _run(self, records):
        from mozyo_bridge.core.state import lane_metadata
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            sublane_herdr_projection,
        )

        with patch.object(sublane_herdr_projection, "repo_scope_workspace_id", return_value=WS), \
             patch.object(lane_metadata, "load_lane_records", return_value=records):
            return adapter._candidate_issue(Path("/repo"), "issue_1")

    def test_single_active_record_is_candidate(self):
        issue, status = self._run({"t1": _lane_record()})
        self.assertEqual((issue, status), ("13489", ""))

    def test_duplicate_active_same_issue_fails_closed(self):
        # F3b: two active records for the lane must NOT collapse to one candidate.
        issue, status = self._run({"t1": _lane_record(), "t2": _lane_record()})
        self.assertEqual((issue, status), ("", ANCHOR_AMBIGUOUS))

    def test_active_plus_retired_stale_fails_closed(self):
        issue, status = self._run(
            {"t1": _lane_record(), "t2": _lane_record(retired=True)}
        )
        self.assertEqual((issue, status), ("", ANCHOR_AMBIGUOUS))

    def test_single_retired_record_fails_closed(self):
        issue, status = self._run({"t1": _lane_record(retired=True)})
        self.assertEqual((issue, status), ("", ANCHOR_RETIRED))

    def test_no_record_is_missing(self):
        issue, status = self._run({"t1": _lane_record(lane_id="other")})
        self.assertEqual((issue, status), ("", ANCHOR_MISSING))

    def test_record_without_issue_is_missing(self):
        issue, status = self._run({"t1": _lane_record(issue_id="")})
        self.assertEqual((issue, status), ("", ANCHOR_MISSING))


class LaneWorkBindingTest(unittest.TestCase):
    """The lifecycle read that supplies the generation half of the join (Redmine #14586)."""

    def _run(self, record):
        from mozyo_bridge.core.state import lane_lifecycle
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            sublane_herdr_projection,
        )

        store = types.SimpleNamespace(get=lambda _key: record)
        with patch.object(sublane_herdr_projection, "repo_scope_workspace_id", return_value=WS), \
             patch.object(lane_lifecycle, "LaneLifecycleStore", lambda *a, **k: store):
            return adapter._lane_work_binding(Path("/repo"), LANE)

    def _record(self, **kw):
        base = dict(lane_disposition="active", lane_generation=3, decision_journal="90999")
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_active_row_supplies_its_generation(self):
        self.assertEqual(self._run(self._record()), 3)

    def test_decision_journal_is_never_read_as_work(self):
        # The conflation this issue removes. `decision_journal` is the record that put the lane in
        # its current STATE (a resume, a hibernate); it is not what delegated the lane's work. A
        # row carrying one but no usable generation must yield no binding, not the decision.
        self.assertEqual(self._run(self._record(lane_generation=0)), 0)

    def test_non_active_disposition_is_not_a_binding(self):
        self.assertEqual(self._run(self._record(lane_disposition="retired")), 0)

    def test_absent_row_is_not_a_binding(self):
        self.assertEqual(self._run(None), 0)

    def test_non_numeric_generation_is_not_a_binding(self):
        self.assertEqual(self._run(self._record(lane_generation="two")), 0)

    def test_blank_lane_short_circuits(self):
        self.assertEqual(adapter._lane_work_binding(Path("/repo"), ""), 0)

    def test_unreadable_store_fails_closed(self):
        from mozyo_bridge.core.state import lane_lifecycle
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            sublane_herdr_projection,
        )

        def _boom(*_a, **_k):
            raise RuntimeError("lifecycle store unreadable")

        with patch.object(sublane_herdr_projection, "repo_scope_workspace_id", return_value=WS), \
             patch.object(lane_lifecycle, "LaneLifecycleStore", _boom):
            self.assertEqual(adapter._lane_work_binding(Path("/repo"), LANE), 0)

    def test_unresolvable_workspace_scope_fails_closed(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            sublane_herdr_projection,
        )

        with patch.object(sublane_herdr_projection, "repo_scope_workspace_id", return_value=""):
            self.assertEqual(adapter._lane_work_binding(Path("/repo"), LANE), 0)


class ResolveWorkAnchorLiveTest(unittest.TestCase):
    """The source-of-truth Redmine work-anchor resolution (F3a as re-based by Redmine #14586)."""

    def _run(self, source, *, generation=1):
        with patch.object(adapter, "_redmine_journal_source_for", return_value=source):
            return adapter._resolve_work_anchor_live(
                argparse.Namespace(), "13489", lane=LANE, lane_generation=generation
            )

    def test_dispatch_marker_journal_is_the_work_anchor(self):
        anchor = self._run(_snapshot_source([{"id": 74766, "notes": _dispatch_note()}]))
        self.assertTrue(anchor.resolved)
        # The OWNING entry's id, never the marker's self-report.
        self.assertEqual(anchor.journal, "74766")

    def test_gate_journal_is_not_a_work_anchor(self):
        # THE #14586 DEFECT. A gate-bearing callback marker is a previous round's answer, not this
        # lane's work. Before the fix this returned 74766 as the anchor for a lane that had never
        # been dispatched against it.
        anchor = self._run(_snapshot_source([{"id": 74766, "notes": _GATE_NOTE}]))
        self.assertEqual(anchor.status, WORK_ANCHOR_MISSING)
        self.assertEqual(anchor.journal, "")

    def test_newest_gate_journal_never_outranks_the_lane_dispatch(self):
        # The exact live shape of #14577 j#90416 F2: an issue with review history whose NEWEST
        # journal is a callback, plus this lane's own (older) dispatch. The dispatch wins because
        # it is the one that names this lane; "latest on the issue" is not a binding.
        anchor = self._run(
            _snapshot_source(
                [
                    {"id": 90409, "notes": _dispatch_note()},
                    {"id": 90416, "notes": _GATE_NOTE},
                ]
            )
        )
        self.assertEqual(anchor.journal, "90409")

    def test_note_without_dispatch_marker_is_missing(self):
        anchor = self._run(_snapshot_source([{"id": 74766, "notes": "plain note, no marker"}]))
        self.assertEqual(anchor.status, WORK_ANCHOR_MISSING)

    def test_unconfigured_credentials_fail_closed(self):
        with patch.object(
            adapter, "_redmine_journal_source_for", side_effect=LiveRedmineJournalError("unconfigured")
        ):
            anchor = adapter._resolve_work_anchor_live(
                argparse.Namespace(), "13489", lane=LANE, lane_generation=1
            )
        self.assertEqual((anchor.status, anchor.journal), (WORK_ANCHOR_MISSING, ""))

    def test_transport_error_fails_closed(self):
        class _BoomSource:
            def read_entries(self, issue):
                raise LiveRedmineJournalError("transport down")

        anchor = self._run(_BoomSource())
        self.assertEqual((anchor.status, anchor.journal), (WORK_ANCHOR_MISSING, ""))

    def test_blank_issue_fails_closed_without_reading(self):
        class _NeverRead:
            def read_entries(self, issue):  # pragma: no cover - must not be reached
                raise AssertionError("live read for a blank issue")

        with patch.object(adapter, "_redmine_journal_source_for", return_value=_NeverRead()):
            anchor = adapter._resolve_work_anchor_live(
                argparse.Namespace(), "", lane=LANE, lane_generation=1
            )
        self.assertEqual(anchor.status, WORK_ANCHOR_MISSING)

    def test_quoted_dispatch_marker_is_not_a_work_anchor(self):
        # Redmine #14585 reaching the anchor gate: a callback record that ECHOES the landing
        # marker it observed is discussing a dispatch, not issuing one.
        marker = _dispatch_note(body="").strip()
        self.assertIn(LANE, marker)  # the quotation names THIS lane: only the quoting refuses it
        anchor = self._run(
            _snapshot_source([{"id": 74766, "notes": "- observed landing marker: `%s`" % marker}])
        )
        self.assertEqual(anchor.status, WORK_ANCHOR_MISSING)
        # Control: the same marker at top level DOES resolve, so the refusal is the quoting.
        self.assertTrue(self._run(_snapshot_source([{"id": 74766, "notes": marker}])).resolved)

    def test_stale_generation_fails_closed(self):
        anchor = self._run(
            _snapshot_source([{"id": 90409, "notes": _dispatch_note(generation=2)}]),
            generation=1,
        )
        self.assertEqual(anchor.status, WORK_ANCHOR_STALE_GENERATION)

    def test_cross_lane_dispatch_is_named_as_foreign(self):
        anchor = self._run(
            _snapshot_source([{"id": 90409, "notes": _dispatch_note(lane="other_lane")}])
        )
        self.assertEqual(anchor.status, WORK_ANCHOR_FOREIGN)
        self.assertEqual(anchor.foreign_lanes, ("other_lane",))

    def test_duplicate_dispatch_entries_are_ambiguous(self):
        anchor = self._run(
            _snapshot_source(
                [{"id": 90409, "notes": _dispatch_note()}, {"id": 90410, "notes": _dispatch_note()}]
            )
        )
        self.assertEqual(anchor.status, WORK_ANCHOR_AMBIGUOUS)


class CanonicalEventJournalTest(unittest.TestCase):
    """The canonical `redmine:<issue>:<journal>` event-id validation (F3a)."""

    def test_canonical_redmine_prefixed(self):
        self.assertEqual(adapter._canonical_event_journal("redmine:13489:74766", "13489"), "74766")

    def test_bare_prefixless_id_rejected(self):
        # F3c-1: canonical requires the `redmine:` prefix; a bare `<issue>:<journal>` (the
        # internal store key any caller can write) is NOT a canonical Redmine anchor.
        self.assertEqual(adapter._canonical_event_journal("13489:74766", "13489"), "")

    def test_issue_mismatch_rejected(self):
        self.assertEqual(adapter._canonical_event_journal("redmine:99999:74766", "13489"), "")

    def test_non_canonical_rejected(self):
        self.assertEqual(adapter._canonical_event_journal("opaque:74766", "13489"), "")
        self.assertEqual(adapter._canonical_event_journal("redmine:13489:74766:extra", "13489"), "")
        self.assertEqual(adapter._canonical_event_journal("redmine:13489", "13489"), "")


class ResolveLaneAnchorTest(unittest.TestCase):
    """Compose candidate + lane binding + live work anchor + advisory store cross-check (F3 / F3c)."""

    def _run(self, candidate, anchor, store_anchor=None, generation=1):
        with patch.object(adapter, "_candidate_issue", return_value=candidate), patch.object(
            adapter, "_lane_work_binding", return_value=generation
        ), patch.object(
            adapter, "_resolve_work_anchor_live", return_value=anchor
        ), patch.object(adapter, "_store_lane_anchor", return_value=store_anchor):
            return adapter._resolve_lane_anchor(
                argparse.Namespace(), WS, Path("/repo"), "issue_1"
            )

    def test_candidate_plus_resolved_work_anchor_is_verified(self):
        status, ptr = self._run(("13489", ""), _work_anchor())
        self.assertEqual(status, ANCHOR_VERIFIED)
        self.assertEqual(ptr, VERIFIED_PTR)  # issue + the DISPATCH journal, from live Redmine

    def test_candidate_failure_short_circuits_without_live_read(self):
        called = {}

        def _verify(*_a, **_kw):
            called["hit"] = True
            return _work_anchor()

        with patch.object(adapter, "_candidate_issue", return_value=("", ANCHOR_AMBIGUOUS)), \
             patch.object(adapter, "_resolve_work_anchor_live", side_effect=_verify):
            status, _ = adapter._resolve_lane_anchor(
                argparse.Namespace(), WS, Path("/repo"), "issue_1"
            )
        self.assertEqual(status, ANCHOR_AMBIGUOUS)
        self.assertNotIn("hit", called)  # no live read when the candidate already fails closed

    def test_lane_generation_is_passed_to_the_join(self):
        # The binding is what makes the anchor exact; passing the wrong generation (or none)
        # would silently widen the join back to "any round of this lane".
        seen = {}

        def _verify(_args, issue, *, lane, lane_generation):
            seen.update(issue=issue, lane=lane, generation=lane_generation)
            return _work_anchor()

        with patch.object(adapter, "_candidate_issue", return_value=("13489", "")), patch.object(
            adapter, "_lane_work_binding", return_value=7
        ), patch.object(
            adapter, "_resolve_work_anchor_live", side_effect=_verify
        ), patch.object(adapter, "_store_lane_anchor", return_value=None):
            adapter._resolve_lane_anchor(argparse.Namespace(), WS, Path("/repo"), "issue_1")
        self.assertEqual(seen, {"issue": "13489", "lane": "issue_1", "generation": 7})

    def test_each_work_anchor_failure_keeps_its_own_status(self):
        # Collapsing these into one "unresolved" is what makes an operator chase a missing record
        # that is not missing. Every domain status must reach the resolver as its own.
        for domain_status, expected in (
            (WORK_ANCHOR_UNBOUND, ANCHOR_WORK_UNBOUND),
            (WORK_ANCHOR_MISSING, ANCHOR_WORK_MISSING),
            (WORK_ANCHOR_FOREIGN, ANCHOR_WORK_FOREIGN),
            (WORK_ANCHOR_AMBIGUOUS, ANCHOR_WORK_AMBIGUOUS),
            (WORK_ANCHOR_STALE_GENERATION, ANCHOR_WORK_STALE),
        ):
            with self.subTest(domain_status):
                status, ptr = self._run(
                    ("13489", ""), _work_anchor(journal="", status=domain_status)
                )
                self.assertEqual(status, expected)
                self.assertEqual(ptr, "")

    def test_resolved_status_without_a_journal_still_fails_closed(self):
        # `resolved` is a conjunction of status AND journal: a status token alone is a claim.
        status, ptr = self._run(("13489", ""), _work_anchor(journal=""))
        self.assertEqual(status, ANCHOR_UNVERIFIED)
        self.assertEqual(ptr, "")

    def test_store_agreeing_is_verified(self):
        status, _ = self._run(
            ("13489", ""), _work_anchor(), store_anchor=("13489", "74766", "review")
        )
        self.assertEqual(status, ANCHOR_VERIFIED)

    def test_store_absent_is_verified(self):
        status, _ = self._run(("13489", ""), _work_anchor(), store_anchor=None)
        self.assertEqual(status, ANCHOR_VERIFIED)

    def test_store_issue_mismatch_fails_closed(self):
        # The dimension the store actually asserts about this lane: which ticket it routes to.
        status, _ = self._run(("13489", ""), _work_anchor(), store_anchor=("99999", "", ""))
        self.assertEqual(status, ANCHOR_STORE_MISMATCH)

    def test_store_ambiguous_route_fails_closed(self):
        status, _ = self._run(
            ("13489", ""), _work_anchor(), store_anchor=("<ambiguous>", "", "")
        )
        self.assertEqual(status, ANCHOR_STORE_MISMATCH)

    def test_store_gate_event_does_not_have_to_equal_the_work_anchor(self):
        # Redmine #14586: a store GATE event and the work anchor answer different questions (what
        # state the lane is in vs what work it was given). Requiring them to be the same journal
        # is the very conflation this issue removes — and it would fail every lane whose issue has
        # any gate history. The issue still has to agree.
        status, ptr = self._run(
            ("13489", ""), _work_anchor(), store_anchor=("13489", "99999", "implementation_done")
        )
        self.assertEqual(status, ANCHOR_VERIFIED)
        self.assertEqual(ptr, VERIFIED_PTR)


class ResolveLaneAnchorEndToEndStoreTest(unittest.TestCase):
    """End-to-end resolver over the real store cross-check (`_store_lane_anchor` unpatched, j#74827)."""

    def _route(self, issue="13489"):
        from mozyo_bridge.core.state.workflow_runtime_store import WorkflowRouteRow

        return WorkflowRouteRow(
            route_id="r", issue=issue, workspace_id=WS, lane_id="issue_1", role="codex",
            pane_name="p", last_seen_pane_id="", observed_at="t",
        )

    def _event(self, event_id, issue="13489", gate="review"):
        from mozyo_bridge.core.state.workflow_runtime_store import WorkflowEventRow

        return WorkflowEventRow(
            event_id=event_id, issue=issue, gate=gate, review_conclusion="",
            callback_state="", commit_bearing=False, integration_recorded=False,
            issue_open=True, blocker_recorded=False,
        )

    def _store(self, routes=(), events=(), exists=True):
        return types.SimpleNamespace(
            path=types.SimpleNamespace(exists=lambda: exists),
            read_route_identities=lambda: tuple(routes),
            read_events=lambda: tuple(events),
        )

    def _run(self, store):
        # Live resolves issue 13489 / dispatch journal 74766; only the store varies.
        with patch.object(adapter, "_candidate_issue", return_value=("13489", "")), patch.object(
            adapter, "_lane_work_binding", return_value=1
        ), patch.object(
            adapter, "_resolve_work_anchor_live", return_value=_work_anchor()
        ), patch.object(adapter, "_load_workflow_store", return_value=store):
            return adapter._resolve_lane_anchor(argparse.Namespace(), WS, Path("/repo"), "issue_1")

    def test_truly_absent_store_is_verified(self):
        status, _ = self._run(self._store(exists=False))
        self.assertEqual(status, ANCHOR_VERIFIED)

    def test_matching_canonical_store_is_verified(self):
        status, _ = self._run(
            self._store(routes=[self._route()], events=[self._event("redmine:13489:74766")])
        )
        self.assertEqual(status, ANCHOR_VERIFIED)

    def test_route_to_a_foreign_issue_fails_closed(self):
        # The class of drift this cross-check exists for (j#74827): a caller-supplied store
        # steering this lane at somebody else's ticket.
        status, _ = self._run(
            self._store(routes=[self._route(issue="99999")], events=[])
        )
        self.assertEqual(status, ANCHOR_STORE_MISMATCH)

    def test_two_routes_to_distinct_issues_fail_closed(self):
        status, _ = self._run(
            self._store(routes=[self._route(), self._route(issue="99999")], events=[])
        )
        self.assertEqual(status, ANCHOR_STORE_MISMATCH)

    def test_store_gate_events_do_not_gate_the_work_anchor(self):
        # Redmine #14586: a store gate event is a lifecycle observation, and the anchor is the
        # dispatch record. A non-canonical / absent / newer gate event says nothing about which
        # record delegated this lane's work, so it no longer blocks — the issue is what must agree.
        for label, events in (
            ("forged synthetic event", [self._event("opaque:74766")]),
            ("no event at all", []),
            (
                "valid then newer event",
                [self._event("redmine:13489:74766"), self._event("redmine:13489:99999")],
            ),
        ):
            with self.subTest(label):
                status, _ = self._run(self._store(routes=[self._route()], events=events))
                self.assertEqual(status, ANCHOR_VERIFIED)

    def test_event_on_a_foreign_issue_still_fails_closed(self):
        # A route for this lane whose only event names another issue: the store's own two
        # assertions disagree about the ticket, which is exactly the drift signal.
        status, _ = self._run(
            self._store(
                routes=[self._route(issue="99999")],
                events=[self._event("redmine:99999:74766", issue="99999")],
            )
        )
        self.assertEqual(status, ANCHOR_STORE_MISMATCH)


class StoreLaneAnchorTest(unittest.TestCase):
    """The advisory store's per-lane (issue, journal, gate) extraction (F3c)."""

    def _run(self, *, store):
        with patch.object(adapter, "_load_workflow_store", return_value=store):
            return adapter._store_lane_anchor(argparse.Namespace(store_path=None), WS, "issue_1")

    def _route(self, issue="13489"):
        from mozyo_bridge.core.state.workflow_runtime_store import WorkflowRouteRow

        return WorkflowRouteRow(
            route_id="r", issue=issue, workspace_id=WS, lane_id="issue_1", role="codex",
            pane_name="p", last_seen_pane_id="", observed_at="t",
        )

    def _event(self, event_id, issue="13489", gate="review"):
        from mozyo_bridge.core.state.workflow_runtime_store import WorkflowEventRow

        return WorkflowEventRow(
            event_id=event_id, issue=issue, gate=gate, review_conclusion="",
            callback_state="", commit_bearing=False, integration_recorded=False,
            issue_open=True, blocker_recorded=False,
        )

    def _store(self, routes=(), events=(), exists=True):
        return types.SimpleNamespace(
            path=types.SimpleNamespace(exists=lambda: exists),
            read_route_identities=lambda: tuple(routes),
            read_events=lambda: tuple(events),
        )

    def test_absent_store_contributes_nothing(self):
        self.assertIsNone(self._run(store=self._store(exists=False)))
        self.assertIsNone(self._run(store=None))

    def test_no_route_for_lane_contributes_nothing(self):
        self.assertIsNone(self._run(store=self._store(routes=[self._route(issue="")])))

    def test_single_route_plus_canonical_event(self):
        anchor = self._run(
            store=self._store(routes=[self._route()], events=[self._event("redmine:13489:74766")])
        )
        self.assertEqual(anchor, ("13489", "74766", "review"))

    def test_two_distinct_route_issues_is_ambiguous_sentinel(self):
        anchor = self._run(
            store=self._store(routes=[self._route("13489"), self._route("13490")])
        )
        self.assertEqual(anchor[0], "<ambiguous>")

    def test_route_without_canonical_event_has_empty_journal(self):
        anchor = self._run(
            store=self._store(routes=[self._route()], events=[self._event("opaque:74766")])
        )
        self.assertEqual(anchor, ("13489", "", ""))

    def test_latest_forged_event_does_not_fall_back_to_earlier_valid(self):
        # j#74838: a valid row followed by a latest forged row must NOT return the earlier valid.
        anchor = self._run(
            store=self._store(
                routes=[self._route()],
                events=[self._event("redmine:13489:74766"), self._event("opaque:99999")],
            )
        )
        self.assertEqual(anchor, ("13489", "", ""))

    def test_latest_valid_after_forged_is_used(self):
        anchor = self._run(
            store=self._store(
                routes=[self._route()],
                events=[self._event("opaque:1"), self._event("redmine:13489:74766")],
            )
        )
        self.assertEqual(anchor, ("13489", "74766", "review"))

    def test_multiple_valid_events_latest_wins(self):
        anchor = self._run(
            store=self._store(
                routes=[self._route()],
                events=[self._event("redmine:13489:100"), self._event("redmine:13489:200")],
            )
        )
        self.assertEqual(anchor, ("13489", "200", "review"))


class RoleAuthorityAdapterTest(unittest.TestCase):
    """End-to-end: a repo-local binding file resolves the default lane's role (Redmine #13583)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        from mozyo_bridge.application import commands_common

        patches = [
            patch.object(commands_common, "repo_root_from_args", return_value=self.repo),
            patch.object(adapter, "_anchor_workspace_id", return_value=WS),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _write_bindings(self, *bindings):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (
            SCHEMA_NAME,
            SCHEMA_VERSION,
        )

        path = self.repo / ".mozyo-bridge" / "workflow-role-bindings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"schema": SCHEMA_NAME, "version": SCHEMA_VERSION, "bindings": list(bindings)}),
            encoding="utf-8",
        )

    def _run(self):
        return adapter.resolve_herdr_step_outcome(argparse.Namespace(repo=None))

    def test_default_lane_with_grandparent_binding_resolves_without_anchor_read(self):
        # Increment 3 (Redmine #13583): a resolved grandparent default lane short-circuits to the
        # executable consultation forward outcome WITHOUT reading the worker/gateway anchor or the
        # inventory (that resolution stays coordinator-only); the send itself is the cli leg.
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
            ROLE_GRANDPARENT_COORDINATOR,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_forward_route import (
            PRIMITIVE_HERDR_FORWARD_CONSULT,
            REASON_HERDR_FORWARD_CONSULT_READY,
        )

        self._write_bindings({"role": "grandparent_coordinator", "source_pointer": "redmine:#13583"})
        with patch.object(
            htr, "resolve_sender_identity", return_value=_sender_ok("codex", "default")
        ), patch.object(
            adapter, "_resolve_lane_anchor", side_effect=AssertionError("anchor read for grandparent")
        ), patch.object(
            adapter, "_same_lane_worker_liveness", side_effect=AssertionError("inventory read")
        ):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_FORWARD_CONSULT_READY)
        self.assertEqual(out.caller_role, ROLE_GRANDPARENT_COORDINATOR)
        self.assertEqual(out.primitive, PRIMITIVE_HERDR_FORWARD_CONSULT)
        self.assertEqual(out.execution, "ready")

    def test_default_lane_provider_mismatch_fails_closed(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (
            REASON_ROLE_PROVIDER_MISMATCH,
        )

        # The default lane is bound to the grandparent (expected provider codex) but the sender
        # runs claude -> provider mismatch, fail closed rather than resolve on the wrong surface.
        self._write_bindings({"role": "grandparent_coordinator"})
        with patch.object(
            htr, "resolve_sender_identity", return_value=_sender_ok("claude", "default")
        ):
            out = self._run()
        self.assertEqual(out.reason, REASON_ROLE_PROVIDER_MISMATCH)
        self.assertEqual(out.execution, "blocked")

    def test_no_bindings_file_keeps_default_lane_ambiguous(self):
        # Byte-invariant: with no declaration the default lane still fails closed as before.
        with patch.object(
            htr, "resolve_sender_identity", return_value=_sender_ok("codex", "default")
        ), patch.object(
            adapter, "_resolve_lane_anchor", side_effect=AssertionError("anchor read")
        ):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_DEFAULT_COORDINATOR_UNRESOLVED)

    def test_worker_lane_not_in_bindings_falls_through_to_anchor_flow(self):
        # A binding file present, but a normal worker lane not named in it keeps its anchor flow.
        self._write_bindings({"role": "grandparent_coordinator"})
        with patch.object(
            htr, "resolve_sender_identity", return_value=_sender_ok("claude", "issue_1")
        ), patch.object(adapter, "_resolve_lane_anchor", return_value=(ANCHOR_VERIFIED, PTR)):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_WORKER_STEP_READY)

    def test_broken_provider_config_fails_closed_not_resolved(self):
        # R1: a broken provider config must NOT degrade to the compat default and resolve the
        # role on an unverified surface; the bound lane fails closed (provider mismatch).
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            workflow_binding_source,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (
            REASON_ROLE_PROVIDER_MISMATCH,
        )

        self._write_bindings({"role": "grandparent_coordinator"})
        with patch.object(
            htr, "resolve_sender_identity", return_value=_sender_ok("codex", "default")
        ), patch.object(
            workflow_binding_source, "load_workflow_binding", side_effect=RuntimeError("malformed config")
        ):
            out = self._run()
        self.assertEqual(out.reason, REASON_ROLE_PROVIDER_MISMATCH)
        self.assertEqual(out.execution, "blocked")

    def test_broken_config_still_missing_when_lane_unbound(self):
        # A broken config must not block a lane that has no binding at all -> still fall-through.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            workflow_binding_source,
        )

        self._write_bindings({"role": "grandparent_coordinator"})
        with patch.object(
            htr, "resolve_sender_identity", return_value=_sender_ok("claude", "issue_1")
        ), patch.object(
            workflow_binding_source, "load_workflow_binding", side_effect=RuntimeError("malformed config")
        ), patch.object(adapter, "_resolve_lane_anchor", return_value=(ANCHOR_VERIFIED, PTR)):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_WORKER_STEP_READY)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
