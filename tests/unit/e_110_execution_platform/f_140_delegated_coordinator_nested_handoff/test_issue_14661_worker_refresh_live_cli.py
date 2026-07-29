"""Unit: ``sublane refresh-worker`` live wiring (Redmine #14661).

The CLI constructs the LIVE composition root and runs the real use case (never a staged seam —
the #14203 j#87356 F1 rule). These tests exercise the wiring hermetically: the herdr binary env
points at a nonexistent path and no Redmine credentials are set, so every live boundary fails
CLOSED — the preflight honestly reports ``turn_unobservable`` + ``identity_unknown`` with zero
process effect, and an ``--execute`` refuses. The live adapter's observation / binding / resume
logic is pinned at the module seams with fakes (no live herdr, no live Redmine, no real
process).
"""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mozyo_bridge.core.state.replacement_transaction import ContinuationPointer
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    RedmineAnchor,
    build_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    sublane_worker_refresh_live as live_mod,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.fresh_coordinator_drain import (  # noqa: E501
    DRAIN_SEND_ERROR,
    DRAIN_SEND_OK,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_refresh import (  # noqa: E501
    WorkerRefreshRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_refresh_cli import (  # noqa: E501
    SEAM_UNAVAILABLE_VERDICT,
    cmd_sublane_refresh_worker,
    format_refresh_worker_text,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_refresh_live import (  # noqa: E501
    LiveWorkerRefreshOps,
    port_pin_request,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_launch_authority import (  # noqa: E501
    LAUNCH_AUTHORITY_BRANCH_DRIFTED,
    LAUNCH_AUTHORITY_GENERATION_MOVED,
    LAUNCH_AUTHORITY_LIFECYCLE_ABSENT,
    LAUNCH_AUTHORITY_LIFECYCLE_UNREADABLE,
    LAUNCH_AUTHORITY_OK,
    LAUNCH_AUTHORITY_PINS_UNPINNED,
    LAUNCH_AUTHORITY_REASONS,
    LAUNCH_AUTHORITY_UNKNOWN,
    LAUNCH_AUTHORITY_WORKTREE_MISMATCH,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.worker_turn_recovery import (  # noqa: E501
    WORKER_REFRESH_BLOCK_UNKNOWN,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E501
    TURN_CLASS_UNOBSERVABLE,
)

LANE = "issue_14661_lane"
ANCHOR_ISSUE = "14658"
ANCHOR_JOURNAL = "92366"
ANCHOR_GATE = "review_result"
WORKER_PROVIDER = "claude"
GATEWAY_PROVIDER = "codex"
#: Two distinct workspace identities on ONE host — the shape that made a foreign lane of the
#: same name satisfy the preserved-gateway axis before review j#92443 F3.
LOCAL_WS = "local_workspace_id_aaaabbbbccccdddd"
FOREIGN_WS = "foreign_workspace_id_eeeeffff0000"


def _gateway_row(workspace: str, pane: str, status: str = "idle") -> dict:
    """A live same-lane GATEWAY row, encoded through the canonical identity codec."""
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        encode_assigned_name,
    )

    return {
        "name": encode_assigned_name(workspace, GATEWAY_PROVIDER, LANE),
        "pane_id": pane,
        "status": status,
    }


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        issue="14661", lane=LANE, role=WORKER_PROVIDER, provider=WORKER_PROVIDER,
        assigned_name="wk", locator="w4B:p10", journal="", action_id="",
        action_generation=0, worker_revision="", lane_revision="", lane_generation="",
        anchor_issue="", resume_anchor_journal=ANCHOR_JOURNAL, resume_gate=ANCHOR_GATE,
        reason_token="", execute=False, json=True, repo=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _request(**overrides) -> WorkerRefreshRequest:
    base = dict(
        issue="14661", lane=LANE, role=WORKER_PROVIDER, provider=WORKER_PROVIDER,
        assigned_name="wk", locator="w4B:p10", anchor_issue=ANCHOR_ISSUE,
        resume_anchor_journal=ANCHOR_JOURNAL, resume_gate=ANCHOR_GATE,
        lane_generation="2",
    )
    base.update(overrides)
    return WorkerRefreshRequest(**base)


class _Entry:
    def __init__(self, journal_id, notes):
        self.journal_id = journal_id
        self.notes = notes


def _record(marker: str, **overrides):
    base = dict(
        notification_marker=marker, source="redmine", issue_id=ANCHOR_ISSUE,
        journal_id=ANCHOR_JOURNAL, receiver=WORKER_PROVIDER, target="w4B:p10",
        status="sent", reason="ok", backend="herdr", provider=WORKER_PROVIDER,
        rail="queue_enter_rail", recorded_at="2026-07-29T00:00:00+00:00",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeLedger:
    def __init__(self, records=()):
        self._records = list(records)

    def records_for_marker(self, marker):
        return [r for r in self._records if r.notification_marker == marker]


class LiveWiringFailClosedTests(unittest.TestCase):
    """Hermetic CLI runs: every live boundary unavailable => fail-closed, zero effect."""

    def _run(self, **overrides):
        out = io.StringIO()
        env = {
            "MOZYO_HERDR_BINARY": "/nonexistent/herdr-binary-for-tests",
            "PATH": "/nonexistent",
        }
        with patch.dict("os.environ", env, clear=False), redirect_stdout(out):
            code = cmd_sublane_refresh_worker(_args(**overrides))
        return code, json.loads(out.getvalue())

    def test_a_hermetic_preflight_fails_closed_with_zero_effect(self):
        with tempfile.TemporaryDirectory() as repo:
            code, payload = self._run(repo=repo, execute=False)
        self.assertEqual(payload["turn_class"], TURN_CLASS_UNOBSERVABLE)
        self.assertIn(
            payload["verdict"], (WORKER_REFRESH_BLOCK_UNKNOWN, SEAM_UNAVAILABLE_VERDICT)
        )
        self.assertFalse(payload["closed_old_worker"])
        self.assertFalse(payload["fresh_slot_attested"])
        if payload["verdict"] == SEAM_UNAVAILABLE_VERDICT:
            self.assertEqual(code, 1)
        else:
            self.assertEqual(code, 0)  # a preflight reporting a blocker is exit 0

    def test_a_hermetic_execute_refuses_with_zero_effect(self):
        with tempfile.TemporaryDirectory() as repo:
            code, payload = self._run(repo=repo, execute=True)
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "refused")
        self.assertTrue(payload["executed"])
        self.assertFalse(payload["closed_old_worker"])
        self.assertFalse(payload["fresh_slot_attested"])

    def test_the_typed_axes_are_present_on_every_json_payload(self):
        with tempfile.TemporaryDirectory() as repo:
            _code, payload = self._run(repo=repo, execute=False)
        for key in (
            "turn_class", "turn_reason", "verdict", "status", "launch_authority_reason",
            "launch_failure_reason", "turn_observation", "observation", "post_close_resume",
        ):
            self.assertIn(key, payload)


class TextRenderingTests(unittest.TestCase):
    def _make_outcome(self, **overrides):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_refresh import (  # noqa: E501
            WorkerRefreshOutcome,
        )

        base = dict(
            issue="14661", lane=LANE, role=WORKER_PROVIDER,
            turn_class=TURN_CLASS_UNOBSERVABLE, turn_reason="unknown",
            verdict=WORKER_REFRESH_BLOCK_UNKNOWN, status="preflight",
        )
        base.update(overrides)
        return WorkerRefreshOutcome(**base)

    def test_the_authority_axis_is_always_rendered(self):
        text = format_refresh_worker_text(self._make_outcome())
        self.assertIn("launch_authority:", text)

    def test_the_launch_failure_line_appears_only_when_a_fence_fired(self):
        clean = format_refresh_worker_text(self._make_outcome())
        self.assertNotIn("launch_failure:", clean)
        fenced = format_refresh_worker_text(
            self._make_outcome(launch_failure_reason="launch_target_absent")
        )
        self.assertIn("launch_failure: launch_target_absent", fenced)


class ObservationSeamTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.ops = LiveWorkerRefreshOps(repo_root=self.repo, request=_request())

    def test_an_unreadable_inventory_is_identity_unknown(self):
        with patch.object(
            live_mod, "list_herdr_agent_rows", side_effect=RuntimeError("no herdr")
        ):
            obs = self.ops.observe_target(_request())
        self.assertFalse(obs.identity_resolved)
        self.assertFalse(obs.worktree_readable)

    def test_port_pin_request_maps_the_worker_revision(self):
        pin = port_pin_request(
            _request(worker_revision="7", lane_revision="5", lane_generation="2")
        )
        self.assertEqual(pin.worker_revision, "7")
        self.assertEqual(pin.lane_revision, "5")
        self.assertEqual(pin.lane_generation, "2")
        self.assertEqual(pin.assigned_name, "wk")

    def test_an_unresolvable_gateway_binding_never_preserves_the_gateway(self):
        with patch.object(self.ops, "_providers", return_value=("", "")):
            self.assertFalse(self.ops._gateway_distinct_preserved(_request()))

    def _preserved(self, rows, workspace=LOCAL_WS):
        with patch.object(
            self.ops, "_providers", return_value=(WORKER_PROVIDER, GATEWAY_PROVIDER)
        ):
            with patch.object(live_mod, "list_herdr_agent_rows", return_value=rows):
                with patch.object(
                    live_mod, "repo_scope_workspace_id", return_value=workspace
                ):
                    return self.ops._gateway_distinct_preserved(_request())

    def test_exactly_one_local_live_gateway_is_preserved(self):
        self.assertTrue(self._preserved([_gateway_row(LOCAL_WS, "w4B:p1Z")]))

    def test_a_foreign_workspace_lane_of_the_same_name_never_satisfies_the_axis(self):
        # Review j#92443 F3: the herdr inventory is host-global, so lane labels are unique
        # only WITHIN a workspace. Without the workspace join a foreign workspace running a
        # same-named lane satisfied "the gateway is preserved" while THIS workspace had no
        # gateway at all.
        self.assertFalse(self._preserved([_gateway_row(FOREIGN_WS, "wZZ:p9")]))

    def test_two_ambiguous_live_gateway_rows_never_satisfy_the_axis(self):
        # Swept with the same fix: an axis that cannot name WHICH slot it preserves has not
        # established the fact it claims.
        self.assertFalse(
            self._preserved(
                [_gateway_row(LOCAL_WS, "w4B:p1Z"), _gateway_row(LOCAL_WS, "w4B:p1Y")]
            )
        )

    def test_a_coexisting_foreign_row_does_not_block_a_real_local_gateway(self):
        # Discriminating in both directions: the fence must not refuse a legitimate lane just
        # because some other workspace runs a lane of the same name.
        self.assertTrue(
            self._preserved(
                [_gateway_row(LOCAL_WS, "w4B:p1Z"), _gateway_row(FOREIGN_WS, "wZZ:p9")]
            )
        )

    def test_an_unresolvable_workspace_fails_closed(self):
        self.assertFalse(self._preserved([_gateway_row(LOCAL_WS, "w4B:p1Z")], workspace=""))

    def test_the_same_locator_is_never_the_distinct_gateway(self):
        # A row at the CLOSE TARGET's own locator can never be the preserved gateway.
        self.assertFalse(self._preserved([_gateway_row(LOCAL_WS, "w4B:p10")]))


class ParticipantRevisionBindingTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.ops = LiveWorkerRefreshOps(repo_root=self.repo, request=_request())

    def _rows(self, revision="4", pane="w4B:p10"):
        return [{"name": "wk", "pane_id": pane, "status": "done", "revision": revision}]

    def test_an_empty_pinned_revision_never_matches(self):
        # Deliberately stricter than recover-stale: a destructive refresh of a LIVE worker may
        # not ride an unpinned generation (the #14203 j#87364 F5 rule).
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=self._rows()):
            self.assertFalse(
                self.ops._participant_revision_matches(_request(worker_revision=""))
            )

    def test_an_exact_pinned_revision_matches(self):
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=self._rows()):
            self.assertTrue(
                self.ops._participant_revision_matches(_request(worker_revision="4"))
            )

    def test_a_recycled_row_revision_does_not_match(self):
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=self._rows("9")):
            self.assertFalse(
                self.ops._participant_revision_matches(_request(worker_revision="4"))
            )

    def test_an_ambiguous_or_absent_row_never_matches(self):
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=[]):
            self.assertFalse(
                self.ops._participant_revision_matches(_request(worker_revision="4"))
            )
        two = self._rows() + [
            {"name": "wk", "pane_id": "w4B:p11", "status": "done", "revision": "4"}
        ]
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=two):
            self.assertFalse(
                self.ops._participant_revision_matches(_request(worker_revision="4"))
            )


class LaneGenerationBindingTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.ops = LiveWorkerRefreshOps(repo_root=self.repo, request=_request())

    def _bound_for(self, reason: str) -> bool:
        with patch.object(self.ops, "lane_authority_reason", return_value=reason):
            return self.ops._lane_generation_bound(_request())

    def test_the_generation_is_unbound_exactly_for_the_lifecycle_axis_tokens(self):
        unbound = {
            LAUNCH_AUTHORITY_PINS_UNPINNED,
            LAUNCH_AUTHORITY_LIFECYCLE_UNREADABLE,
            LAUNCH_AUTHORITY_LIFECYCLE_ABSENT,
            LAUNCH_AUTHORITY_GENERATION_MOVED,
            LAUNCH_AUTHORITY_UNKNOWN,
        }
        # Driven from the CLOSED vocabulary itself, so a token added upstream must be
        # classified here rather than silently defaulting to "bound".
        for reason in LAUNCH_AUTHORITY_REASONS:
            with self.subTest(reason=reason):
                self.assertEqual(self._bound_for(reason), reason not in unbound)

    def test_a_worktree_or_branch_failure_does_not_unbind_the_generation(self):
        # Those are a different axis: they block through ``launch_authority_unavailable``,
        # not by making the classification unobservable.
        self.assertTrue(self._bound_for(LAUNCH_AUTHORITY_WORKTREE_MISMATCH))
        self.assertTrue(self._bound_for(LAUNCH_AUTHORITY_BRANCH_DRIFTED))
        self.assertTrue(self._bound_for(LAUNCH_AUTHORITY_OK))


class AnchorBindingTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.ops = LiveWorkerRefreshOps(repo_root=self.repo, request=_request())

    def test_a_complete_anchor_pointer_binds(self):
        self.assertTrue(self.ops._anchor_bound(_request()))

    def test_a_non_numeric_or_empty_anchor_journal_never_binds(self):
        for bad in ("", "  ", "j92366", "92366a"):
            with self.subTest(journal=bad):
                self.assertFalse(
                    self.ops._anchor_bound(_request(resume_anchor_journal=bad))
                )

    def test_a_non_resumable_gate_never_binds(self):
        self.assertFalse(self.ops._anchor_bound(_request(resume_gate="not_a_gate")))
        self.assertFalse(self.ops._anchor_bound(_request(resume_gate="")))

    def test_an_unbound_anchor_short_circuits_the_durable_read(self):
        # An unbound anchor must not even consult the durable source: an ordered comparison
        # against an unresolvable anchor is meaningless, and "absent" must never be inferred.
        reads: list = []
        self.ops.journal_reader = lambda issue: reads.append(issue) or []
        self.ops.journal_reader_fresh = True
        with patch.object(
            self.ops, "_providers", return_value=(WORKER_PROVIDER, GATEWAY_PROVIDER)
        ):
            with patch.object(live_mod, "list_herdr_agent_rows", return_value=[]):
                obs = self.ops.observe_turn(_request(resume_gate="not_a_gate"))
        self.assertFalse(obs.anchor_bound)
        self.assertFalse(obs.expected_gate_absent)
        self.assertFalse(obs.durable_source_fresh)
        self.assertEqual(reads, [])


class WorkerProgressReadTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.ops = LiveWorkerRefreshOps(repo_root=self.repo, request=_request())
        self.ops.journal_reader_fresh = True

    def _facts(self, entries):
        self.ops.journal_reader = lambda issue: entries
        return self.ops._progress_facts(_request())

    def test_progress_facts_require_a_fresh_reader(self):
        self.ops.journal_reader_fresh = True
        self.ops.journal_reader = None
        self.assertEqual(self.ops._progress_facts(_request()), (False, False, False))
        self.ops.journal_reader = lambda issue: []
        self.ops.journal_reader_fresh = False
        self.assertEqual(self.ops._progress_facts(_request()), (False, False, False))

    def test_progress_is_anchored_and_ordered(self):
        marker = "[mozyo:workflow-event:gate=implementation_done]"
        self.assertEqual(
            self._facts([_Entry("92300", marker), _Entry(ANCHOR_JOURNAL, marker)]),
            (False, True, True),
        )
        self.assertEqual(self._facts([_Entry("92400", marker)]), (True, False, True))

    def test_prose_without_a_structured_marker_is_not_progress(self):
        self.assertEqual(
            self._facts([_Entry("92400", "implementation_done, honest")]),
            (False, True, True),
        )

    def test_a_review_result_after_the_anchor_is_not_worker_progress(self):
        # The reviewer's output is what gets delivered TO the worker; counting it would
        # suppress the recovery of the worker that never answered it.
        marker = "[mozyo:workflow-event:gate=review_result:conclusion=approved:req=92366]"
        self.assertEqual(self._facts([_Entry("92400", marker)]), (False, True, True))

    def test_an_unenveloped_progress_marker_counts_as_progress(self):
        # The SAFE direction: unknown lane provenance classifies productive, which refuses
        # the refresh. The reverse would close a worker that had in fact delivered its gate.
        self.assertEqual(
            self._facts([_Entry("92400", "[mozyo:workflow-event:gate=review_request]")]),
            (True, False, True),
        )

    def test_an_enveloped_marker_must_match_the_lane_and_generation(self):
        def marker(lane, generation):
            return (
                "[mozyo:workflow-event:gate=implementation_done:"
                f"workspace=ws:lane={lane}:lane_generation={generation}]"
            )

        self.assertEqual(self._facts([_Entry("92400", marker(LANE, "2"))]), (True, False, True))
        # A different lane's gate is not this worker's progress.
        self.assertEqual(
            self._facts([_Entry("92400", marker("issue_99999_other", "2"))]),
            (False, True, True),
        )
        # A superseded generation's gate is not this generation's progress.
        self.assertEqual(self._facts([_Entry("92400", marker(LANE, "1"))]), (False, True, True))

    def test_a_partial_envelope_counts_as_progress_not_as_a_skipped_marker(self):
        # The canonical producer cannot emit a half envelope (it is all-or-none), so a marker
        # carrying only one half has unreadable lane provenance. Skipping it would admit a
        # close of a worker that had in fact delivered its gate — the fail-OPEN direction.
        lane_only = (
            "[mozyo:workflow-event:gate=implementation_done:workspace=ws:lane=" + LANE + "]"
        )
        gen_only = "[mozyo:workflow-event:gate=implementation_done:lane_generation=2]"
        for notes in (lane_only, gen_only):
            with self.subTest(notes=notes):
                self.assertEqual(self._facts([_Entry("92400", notes)]), (True, False, True))

    def test_a_partial_envelope_naming_a_foreign_lane_still_counts_as_progress(self):
        # Even when the readable half points elsewhere: a marker that cannot be trusted to
        # describe its own lane cannot be trusted to exclude this one either.
        notes = "[mozyo:workflow-event:gate=review_request:lane=issue_99999_other]"
        self.assertEqual(self._facts([_Entry("92400", notes)]), (True, False, True))

    def test_an_unreadable_source_is_unobservable_never_absent(self):
        def _boom(issue):
            raise RuntimeError("source down")

        self.ops.journal_reader = _boom
        self.assertEqual(self.ops._progress_facts(_request()), (False, False, False))

    def test_a_non_numeric_anchor_is_unobservable(self):
        self.ops.journal_reader = lambda issue: []
        self.assertEqual(
            self.ops._progress_facts(_request(resume_anchor_journal="nope")),
            (False, False, False),
        )

    def test_the_read_targets_the_anchor_issue_not_the_lane_issue(self):
        seen: list = []
        self.ops.journal_reader = lambda issue: seen.append(issue) or []
        self.ops._progress_facts(_request())
        self.assertEqual(seen, [ANCHOR_ISSUE])


class AnchorDeliveryRecordTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.ops = LiveWorkerRefreshOps(repo_root=self.repo, request=_request())

    def _marker(self, kind: str) -> str:
        return build_marker(
            RedmineAnchor(issue=ANCHOR_ISSUE, journal=ANCHOR_JOURNAL), kind, WORKER_PROVIDER
        )

    def test_the_original_anchor_kind_delivery_confirms(self):
        self.ops.ledger = _FakeLedger([_record(self._marker(ANCHOR_GATE))])
        self.assertIsNotNone(self.ops._anchor_delivery_record(WORKER_PROVIDER))

    def test_a_previous_refresh_resume_pointer_also_confirms(self):
        # Otherwise a lane that failed the same way twice would be permanently
        # ``turn_unconfirmed`` — the exactly-once gap #14661 was opened on, one round later.
        self.ops.ledger = _FakeLedger([_record(self._marker("reply"))])
        self.assertIsNotNone(self.ops._anchor_delivery_record(WORKER_PROVIDER))

    def test_a_neighbouring_handoff_never_confirms(self):
        other = build_marker(
            RedmineAnchor(issue=ANCHOR_ISSUE, journal="99999"), ANCHOR_GATE, WORKER_PROVIDER
        )
        self.ops.ledger = _FakeLedger([_record(other, journal_id="99999")])
        self.assertIsNone(self.ops._anchor_delivery_record(WORKER_PROVIDER))

    def test_every_record_axis_is_fail_closed(self):
        marker = self._marker(ANCHOR_GATE)
        for field, bad in (
            ("source", "asana"),
            ("issue_id", "99999"),
            ("journal_id", "99999"),
            ("receiver", GATEWAY_PROVIDER),
            ("target", "w4B:p99"),
            ("status", "blocked"),
            ("reason", "queue_enter"),
        ):
            with self.subTest(field=field):
                self.ops.ledger = _FakeLedger([_record(marker, **{field: bad})])
                self.assertIsNone(self.ops._anchor_delivery_record(WORKER_PROVIDER))

    def test_an_unreadable_ledger_is_unconfirmed(self):
        class _Boom:
            def records_for_marker(self, marker):
                raise RuntimeError("ledger down")

        self.ops.ledger = _Boom()
        self.assertIsNone(self.ops._anchor_delivery_record(WORKER_PROVIDER))


class RealTurnObservationTests(unittest.TestCase):
    """The live ``observe_turn`` seam end-to-end (review j#92443 F1).

    R1's tests replaced ``observe_turn`` with a fake everywhere, so the generation-authority
    wiring behind it was never executed — and it was broken: the shared seam read the pin as
    ``request.gateway_revision`` through a defaulted attribute lookup, which a worker request
    does not have, so ``turn_started`` was permanently ``False`` and the surface could never
    reach ``turn_failed_no_durable_gate`` in production. These tests drive the REAL seam.
    """

    REVISION = "4"
    TOKEN = "startup-abc123"

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())

    def _ops(self, request, *, binding_revision=None, landed=False):
        marker = build_marker(
            RedmineAnchor(issue=ANCHOR_ISSUE, journal=ANCHOR_JOURNAL),
            ANCHOR_GATE, WORKER_PROVIDER,
        )
        binding = {
            "assigned_name": "wk", "locator": "w4B:p10",
            "row_revision": self.REVISION if binding_revision is None else binding_revision,
            "provider": WORKER_PROVIDER, "startup_action_id": self.TOKEN,
        }
        rec = _record(marker)
        rec.queue_enter_observation = {
            "event_wait_kind": "changed", "gateway_binding": binding,
        }
        notes = (
            "[mozyo:workflow-event:gate=implementation_done]" if landed else "unrelated prose"
        )
        ops = LiveWorkerRefreshOps(
            repo_root=self.repo, request=request, ledger=_FakeLedger([rec]),
            journal_reader=lambda issue: [_Entry("92400", notes)],
            journal_reader_fresh=True,
        )
        return ops

    def _rows(self, revision=None, status="done"):
        return [{
            "name": "wk", "pane_id": "w4B:p10", "status": status,
            "revision": self.REVISION if revision is None else revision,
        }]

    def _observe(self, ops, request, rows=None):
        with patch.object(
            ops, "_providers", return_value=(WORKER_PROVIDER, GATEWAY_PROVIDER)
        ):
            with patch.object(
                live_mod, "list_herdr_agent_rows", return_value=rows or self._rows()
            ):
                with patch.object(
                    live_mod._gen_authority, "current_request_generation_token",
                    return_value=self.TOKEN,
                ):
                    with patch.object(
                        ops, "_lane_generation_bound", return_value=True
                    ):
                        return ops.observe_turn(request)

    def test_a_real_worker_turn_reaches_turn_failed_no_durable_gate(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E501
            TURN_CLASS_FAILED,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.worker_turn_recovery import (  # noqa: E501
            classify_worker_turn,
        )

        request = _request(worker_revision=self.REVISION)
        obs = self._observe(self._ops(request), request)
        self.assertTrue(obs.delivery_confirmed)
        self.assertTrue(obs.turn_started, "the generation binding must succeed for a worker")
        self.assertTrue(obs.settled_turn_ended)
        self.assertTrue(obs.expected_gate_absent)
        self.assertTrue(obs.durable_source_fresh)
        self.assertTrue(obs.identity_bound)
        self.assertEqual(classify_worker_turn(obs), TURN_CLASS_FAILED)

    def test_the_pin_revision_is_this_surfaces_worker_revision(self):
        # A binding recorded at a DIFFERENT row revision must not bind — proving the pin that
        # reaches the shared authority is really the worker's, not a constant or an ignored
        # argument.
        request = _request(worker_revision=self.REVISION)
        obs = self._observe(self._ops(request, binding_revision="99"), request)
        self.assertTrue(obs.delivery_confirmed)
        self.assertFalse(obs.turn_started)

    def test_an_unpinned_worker_revision_never_binds(self):
        request = _request(worker_revision="")
        obs = self._observe(self._ops(request), request)
        self.assertFalse(obs.turn_started)
        self.assertFalse(obs.participant_revision_bound)

    def test_a_landed_worker_gate_makes_the_real_turn_productive(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E501
            TURN_CLASS_PRODUCTIVE,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.worker_turn_recovery import (  # noqa: E501
            classify_worker_turn,
        )

        request = _request(worker_revision=self.REVISION)
        obs = self._observe(self._ops(request, landed=True), request)
        self.assertTrue(obs.expected_gate_landed)
        self.assertEqual(classify_worker_turn(obs), TURN_CLASS_PRODUCTIVE)

    def test_the_shared_authority_refuses_a_caller_that_omits_the_pin(self):
        # The defect class itself: a silent attribute default cannot fail loudly. The seam is
        # now a required keyword, so a caller that forgets it raises instead of degrading to
        # a permanently unbound authority.
        with self.assertRaises(TypeError):
            live_mod._gen_authority.record_observed_turn_start(
                object(), request=_request(), repo_root=self.repo, attestation_home=None,
            )


class ApprovalVerificationTests(unittest.TestCase):
    """The positive owner-approval authority (review j#92443 F2)."""

    APPROVAL_JOURNAL = "92500"

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.request = _request(
            journal=self.APPROVAL_JOURNAL,
            action_id="refresh-worker:" + LANE + ":claude:claude:wk:w4B:p10:r4",
            action_generation=3,
            worker_revision="4",
        )
        self.token = self.request.holder
        self.ops = LiveWorkerRefreshOps(repo_root=self.repo, request=self.request)

    def _with_entries(self, entries):
        self.ops.journal_reader = lambda issue: entries
        self.ops.journal_reader_fresh = True
        return self.ops.approval_verified(self.request)

    def test_an_approval_naming_the_exact_action_and_generation_verifies(self):
        notes = f"## Gate: owner_close_approval\n- approves: {self.token}\n"
        self.assertTrue(self._with_entries([_Entry(self.APPROVAL_JOURNAL, notes)]))

    def test_a_journal_that_does_not_name_the_action_is_not_an_approval(self):
        # The R1 hole verbatim: any well-shaped, existing journal used to authorize a close.
        self.assertTrue(bool(self.token))
        self.assertFalse(
            self._with_entries([_Entry(self.APPROVAL_JOURNAL, "looks good, go ahead")])
        )

    def test_an_approval_of_a_different_generation_does_not_verify(self):
        other = self.token.replace(":g3", ":g2")
        self.assertNotEqual(other, self.token)
        self.assertFalse(self._with_entries([_Entry(self.APPROVAL_JOURNAL, other)]))

    def test_an_approval_of_a_different_worker_does_not_verify(self):
        other = self.token.replace(":wk:", ":other_worker:")
        self.assertNotEqual(other, self.token)
        self.assertFalse(self._with_entries([_Entry(self.APPROVAL_JOURNAL, other)]))

    def test_the_token_must_live_on_the_PINNED_journal(self):
        # A correct approval sitting on a different journal never authorizes this pointer.
        self.assertFalse(self._with_entries([_Entry("99999", self.token)]))

    def test_an_absent_journal_never_verifies(self):
        self.assertFalse(self._with_entries([]))

    def test_no_reader_or_a_snapshot_reader_never_verifies(self):
        self.ops.journal_reader = None
        self.ops.journal_reader_fresh = True
        self.assertFalse(self.ops.approval_verified(self.request))
        self.ops.journal_reader = lambda issue: [
            _Entry(self.APPROVAL_JOURNAL, self.token)
        ]
        self.ops.journal_reader_fresh = False
        self.assertFalse(self.ops.approval_verified(self.request))

    def test_an_unreadable_source_never_verifies(self):
        def _boom(issue):
            raise RuntimeError("redmine down")

        self.ops.journal_reader = _boom
        self.ops.journal_reader_fresh = True
        self.assertFalse(self.ops.approval_verified(self.request))

    def test_the_read_targets_the_anchor_issue(self):
        seen: list = []
        self.ops.journal_reader = lambda issue: seen.append(issue) or []
        self.ops.journal_reader_fresh = True
        self.ops.approval_verified(self.request)
        self.assertEqual(seen, [ANCHOR_ISSUE])

    def test_an_empty_journal_or_action_id_never_verifies(self):
        self.ops.journal_reader_fresh = True
        self.ops.journal_reader = lambda issue: [_Entry("", "anything")]
        self.assertFalse(self.ops.approval_verified(_request(journal="", action_id="x")))
        self.assertFalse(
            self.ops.approval_verified(_request(journal="1", action_id=""))
        )


class ResumeRailTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.ops = LiveWorkerRefreshOps(repo_root=self.repo, request=_request())
        self.continuation = ContinuationPointer(
            source="redmine", issue_id=ANCHOR_ISSUE, journal_id=ANCHOR_JOURNAL,
            expected_gate=ANCHOR_GATE, next_semantic_action="callback_recovery_once",
        )

    def test_resume_once_never_sends_without_a_distinct_fresh_worker(self):
        rows = [{"name": "wk", "pane_id": "w4B:p10", "status": "done"}]
        driven: list = []
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=rows):
            with patch.object(
                self.ops, "_drive_cli", side_effect=lambda argv: driven.append(argv) or 0
            ):
                with patch.object(
                    self.ops, "_providers", return_value=(WORKER_PROVIDER, GATEWAY_PROVIDER)
                ):
                    result = self.ops.resume_once(self.continuation)
        self.assertEqual(result, DRAIN_SEND_ERROR)
        self.assertEqual(driven, [])

    def test_resume_once_drives_the_governed_rail_with_the_existing_anchor(self):
        rows = [{"name": "wk", "pane_id": "w4B:p22", "status": "idle"}]
        driven: list = []
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=rows):
            with patch.object(
                self.ops, "_drive_cli", side_effect=lambda argv: driven.append(argv) or 0
            ):
                with patch.object(
                    self.ops, "_providers", return_value=(WORKER_PROVIDER, GATEWAY_PROVIDER)
                ):
                    with patch.object(
                        self.ops, "_governed_sender_resolves", return_value=True
                    ):
                        result = self.ops.resume_once(self.continuation)
        self.assertEqual(result, DRAIN_SEND_OK)
        self.assertEqual(len(driven), 1)
        argv = driven[0]
        self.assertEqual(argv[:2], ["handoff", "send"])
        self.assertEqual(argv[argv.index("--to") + 1], WORKER_PROVIDER)
        self.assertEqual(argv[argv.index("--issue") + 1], ANCHOR_ISSUE)
        self.assertEqual(argv[argv.index("--journal") + 1], ANCHOR_JOURNAL)
        self.assertEqual(argv[argv.index("--kind") + 1], "reply")
        self.assertEqual(argv[argv.index("--target") + 1], "w4B:p22")
        self.assertEqual(argv[argv.index("--target-lane") + 1], LANE)

    def test_resume_confirmed_requires_a_distinct_fresh_worker_and_an_attestation(self):
        rows = [{"name": "wk", "pane_id": "w4B:p10", "status": "idle"}]
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=rows):
            with patch.object(
                self.ops, "_providers", return_value=(WORKER_PROVIDER, GATEWAY_PROVIDER)
            ):
                with patch.object(
                    self.ops, "_fresh_attestation_observed_at", return_value=""
                ):
                    self.assertFalse(self.ops.resume_confirmed(self.continuation))
                with patch.object(
                    self.ops, "_fresh_attestation_observed_at", return_value="2026-01-01"
                ):
                    # Still the OLD locator -> never confirmed.
                    self.assertFalse(self.ops.resume_confirmed(self.continuation))

    def test_an_unresolvable_worker_binding_never_sends(self):
        with patch.object(self.ops, "_providers", return_value=("", "")):
            self.assertEqual(self.ops.resume_once(self.continuation), DRAIN_SEND_ERROR)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
