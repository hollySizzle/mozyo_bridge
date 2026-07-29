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

from mozyo_bridge.core.state.replacement_preservation import (
    PreservationObservation,
    assess_worker_recovery_preservation,
)
from mozyo_bridge.core.state.replacement_transaction import ContinuationPointer
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_launch_failure import (  # noqa: E501
    port_launch_failure_reason,
)
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.worker_refresh_approval import (  # noqa: E501
    WorkerRefreshApprovalError,
    render_worker_refresh_approval_marker,
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
    def __init__(self, journal_id, notes, issue=ANCHOR_ISSUE, author="5"):
        self.journal_id = journal_id
        self.notes = notes
        self.issue_id = issue
        self.author_id = author


def _record(marker: str, **overrides):
    base = dict(
        notification_marker=marker, source="redmine", issue_id=ANCHOR_ISSUE,
        journal_id=ANCHOR_JOURNAL, receiver=WORKER_PROVIDER, target="w4B:p10",
        status="sent", reason="ok", backend="herdr", provider=WORKER_PROVIDER,
        rail="queue_enter_rail", recorded_at="2026-07-29T00:00:00+00:00",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _pin():
    """A minimal ParticipantPin for the close-boundary port under test."""
    from mozyo_bridge.core.state.replacement_transaction import ParticipantPin

    return ParticipantPin(
        lane_id=LANE, role=WORKER_PROVIDER, provider=WORKER_PROVIDER,
        assigned_name="wk", old_locator="w4B:p10", is_self=False,
        lane_revision="5", lane_generation="2",
    )


class _FakeInnerPort:
    """The shared #13806 port, faked: it always permits the close on identity grounds."""

    def __init__(self):
        self.observation = PreservationObservation(
            identity_matches=True, attestation_fresh=True
        )
        self.calls: list[str] = []
        self.launch_failure_reason = ""

    def observe_old_slot(self, pin):
        self.calls.append("observe_old_slot")
        return "present"

    def observe_preservation(self, pin):
        return self.observation

    def close_exact_generation(self, pin):
        self.calls.append("close_exact_generation")
        return "closed"

    def launch_action_bound(self, action_id, pin):
        self.calls.append("launch_action_bound")
        return "launched"

    def verify_attestation(self, action_id, pin):
        self.calls.append("verify_attestation")
        return "bound"


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
    """The structured owner-approval authority (reviews j#92487 F1 / j#92533 F1+F2)."""

    APPROVAL_JOURNAL = "92500"
    ACTION_ID = "refresh-worker:" + LANE + ":claude:claude:wk:w4B:p10:r4"
    OWNER = "5"
    OTHER_AUTHOR = "9"

    def _operation(self, **overrides):
        base = dict(
            issue="14661", lane=LANE, action_id=self.ACTION_ID, action_generation=3,
            lane_revision="5", lane_generation="2", anchor_issue=ANCHOR_ISSUE,
            resume_anchor_journal=ANCHOR_JOURNAL, resume_gate=ANCHOR_GATE,
        )
        base.update(overrides)
        return base

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.request = _request(
            journal=self.APPROVAL_JOURNAL, action_id=self.ACTION_ID, action_generation=3,
            worker_revision="4", lane_revision="5", lane_generation="2",
        )
        self.ops = LiveWorkerRefreshOps(
            repo_root=self.repo, request=self.request,
            issuer_resolver=lambda entry: None,
        )
        self.marker = render_worker_refresh_approval_marker(**self._operation())

    def _entry(self, notes, journal=None, author=None, issue=ANCHOR_ISSUE):
        return _Entry(journal or self.APPROVAL_JOURNAL, notes, issue=issue,
                      author=self.OWNER if author is None else author)

    def _verify(self, entries, request=None):
        self.ops.journal_reader = lambda issue: entries
        self.ops.journal_reader_fresh = True
        return self.ops.approval_verified(request or self.request)

    # -- the positive case ---------------------------------------------------

    def test_every_approval_refuses_while_no_owner_authority_is_resolvable(self):
        # Review j#92601 F1: the surface must not close on an authority it cannot establish.
        # The preflight is unaffected; only --execute is gated.
        self.assertFalse(self._verify([self._entry(f"## owner approval\n\n{self.marker}\n")]))

    # -- j#92533 F1: issuer authority ---------------------------------------

    def test_an_unresolvable_issuer_never_verifies(self):
        self.ops.issuer_resolver = lambda entry: None
        self.assertFalse(self._verify([self._entry(self.marker)]))
        self.ops.issuer_resolver = None
        self.assertFalse(self._verify([self._entry(self.marker)]))

    def test_an_issuer_resolver_that_raises_never_verifies(self):
        def _boom(entry):
            raise RuntimeError("redmine down")

        self.ops.issuer_resolver = _boom
        self.assertFalse(self._verify([self._entry(self.marker)]))

    def test_the_issuer_is_resolved_from_the_approval_JOURNAL_not_the_issue(self):
        # The authority question is "who wrote THIS journal", so the resolver is handed the
        # entry, never the issue id.
        seen: list = []
        self.ops.issuer_resolver = lambda entry: seen.append(entry) or None
        self._verify([self._entry(self.marker)])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].journal_id, self.APPROVAL_JOURNAL)

    # -- j#92533 F2 A: the approval must bind the WHOLE operation ------------

    def test_one_approval_never_authorizes_a_different_operation(self):
        # Measured before the fix: all four verified under one unchanged marker.
        for label, kw in (
            ("resume anchor", dict(resume_anchor_journal="99999")),
            ("resume gate", dict(resume_gate="implementation_request")),
            ("lane revision", dict(lane_revision="999")),
            ("lane generation", dict(lane_generation="999")),
            ("action generation", dict(action_generation=30)),
        ):
            with self.subTest(differs=label):
                fields = dict(
                    journal=self.APPROVAL_JOURNAL, action_id=self.ACTION_ID,
                    action_generation=3, worker_revision="4", lane_revision="5",
                    lane_generation="2",
                )
                fields.update(kw)
                self.assertFalse(self._verify([self._entry(self.marker)], _request(**fields)))

    def test_the_digest_requires_every_operation_component(self):
        for missing in ("lane_revision", "lane_generation", "anchor_issue",
                        "resume_anchor_journal", "resume_gate"):
            with self.subTest(missing=missing):
                with self.assertRaises(WorkerRefreshApprovalError):
                    render_worker_refresh_approval_marker(**self._operation(**{missing: ""}))

    # -- j#92533 F2 B: conflicting / malformed / extra fields ---------------

    def test_a_conflicting_duplicate_field_never_verifies(self):
        # ``decision=declined:decision=approved`` used to collapse last-write-wins.
        for original, injected in (
            ("decision=approved", "decision=declined:decision=approved"),
            ("decision=approved", "decision=approved:decision=declined"),
            ("approval_source=direct_owner",
             "approval_source=standing_delegation:approval_source=direct_owner"),
        ):
            with self.subTest(injected=injected):
                self.assertFalse(
                    self._verify([self._entry(self.marker.replace(original, injected))])
                )

    def test_a_malformed_fragment_never_verifies(self):
        self.assertFalse(self._verify([self._entry(self.marker[:-1] + ":nonsense]")]))
        self.assertFalse(self._verify([self._entry(self.marker[:-1] + ":=novalue]")]))
        self.assertFalse(self._verify([self._entry(self.marker[:-1] + ":empty=]")]))

    def test_an_unknown_extra_field_never_verifies(self):
        self.assertFalse(self._verify([self._entry(self.marker[:-1] + ":bogus=1]")]))

    def test_a_missing_canonical_field_never_verifies(self):
        stripped = self.marker.replace(":effect=worker_close_relaunch_resume", "")
        self.assertFalse(self._verify([self._entry(stripped)]))

    # -- the j#92487 F1 holes stay closed ------------------------------------

    def test_prose_quoted_and_log_mentions_never_verify(self):
        for notes in (
            f"この action は **承認しない**。{self.ACTION_ID} は保留する。",
            f"retry:\n```\nmozyo-bridge ... --action-id {self.ACTION_ID}\n```\n(未承認)",
            f"[debug] refused action {self.ACTION_ID} (no approval on file)",
        ):
            with self.subTest(notes=notes[:40]):
                self.assertFalse(self._verify([self._entry(notes)]))

    def test_a_declined_or_delegated_marker_never_verifies(self):
        self.assertFalse(
            self._verify([self._entry(
                self.marker.replace("decision=approved", "decision=declined"))])
        )
        self.assertFalse(
            self._verify([self._entry(self.marker.replace(
                "approval_source=direct_owner", "approval_source=standing_delegation"))])
        )

    def test_two_approval_markers_on_one_journal_never_verify(self):
        self.assertFalse(self._verify([self._entry(self.marker + "\n" + self.marker)]))

    # -- pointer / source fences ---------------------------------------------

    def test_the_marker_must_live_on_the_PINNED_journal(self):
        self.assertFalse(self._verify([self._entry(self.marker, journal="99999")]))

    def test_a_duplicated_journal_id_never_verifies(self):
        self.assertFalse(self._verify([self._entry(self.marker), self._entry(self.marker)]))

    def test_an_absent_journal_never_verifies(self):
        self.assertFalse(self._verify([]))

    def test_a_marker_on_another_issue_never_verifies(self):
        self.assertFalse(self._verify([self._entry(self.marker, issue="99999")]))

    def test_no_reader_or_a_snapshot_reader_never_verifies(self):
        self.ops.journal_reader = None
        self.ops.journal_reader_fresh = True
        self.assertFalse(self.ops.approval_verified(self.request))
        self.ops.journal_reader = lambda issue: [self._entry(self.marker)]
        self.ops.journal_reader_fresh = False
        self.assertFalse(self.ops.approval_verified(self.request))

    def test_an_unreadable_source_never_verifies(self):
        def _boom(issue):
            raise RuntimeError("redmine down")

        self.ops.journal_reader = _boom
        self.ops.journal_reader_fresh = True
        self.assertFalse(self.ops.approval_verified(self.request))


class JournalAuthorSeamTests(unittest.TestCase):
    """The shared journal seam extension j#92494 authorized (backward compatible)."""

    def test_the_entry_carries_an_opaque_author_id_and_never_the_name(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            MappingRedmineJournalSource,
        )

        source = MappingRedmineJournalSource({
            "issue": {"id": "14661", "author": {"id": "5", "name": "PERSONAL NAME"}},
            "journals": [
                {"id": "1", "notes": "a", "user": {"id": "5", "name": "PERSONAL NAME"}},
                {"id": "2", "notes": "b"},                       # no user => unattributable
                {"id": "3", "notes": "c", "user": "not-a-mapping"},
            ],
        })
        entries = source.read_entries()
        self.assertEqual([(e.journal_id, e.author_id) for e in entries],
                         [("1", "5"), ("2", ""), ("3", "")])
        self.assertEqual(source.issue_author_id(), "5")
        # The display name is personal data and must not travel anywhere.
        self.assertNotIn("PERSONAL NAME", repr(entries))
        self.assertNotIn("PERSONAL NAME", source.issue_author_id())

    def test_the_extension_is_backward_compatible(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            RedmineJournalEntry,
        )

        # Existing consumers construct entries positionally / without the new field.
        entry = RedmineJournalEntry(issue_id="1", journal_id="2", notes="n")
        self.assertEqual(entry.author_id, "")
        self.assertEqual(entry.created_on, "")

    def test_the_cursor_projection_preserves_the_issue_author(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
            _apply_since,
        )

        payload = {"issue": {"id": "14661", "author": {"id": "5"},
                             "journals": [{"id": "9", "notes": "n",
                                           "created_on": "2026-07-29T00:00:00Z"}]}}
        projected = _apply_since(payload, "2026-01-01T00:00:00Z")
        self.assertEqual(projected["issue"].get("author"), {"id": "5"})


class CloseBoundarySettledFenceTests(unittest.TestCase):
    """The action-time close-boundary fence (review j#92487 F2).

    The shared #13806 boundary reduces the runtime to ``running_process = (state == busy)``,
    so every non-``working`` herdr status — including a ``blocked`` permission prompt and an
    unreadable ``unknown`` — reached ``may_close=True``. Each case below was admitted before.
    """

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.request = _request(worker_revision="4")
        self.ops = LiveWorkerRefreshOps(repo_root=self.repo, request=self.request)
        self.inner = _FakeInnerPort()
        self.port = live_mod.SettledCloseBoundaryPort(
            inner=self.inner, ops=self.ops, request=self.request
        )

    def _may_close(self, raw_status, composer_clear=True):
        rows = [{"name": "wk", "pane_id": "w4B:p10", "revision": "4"}]
        if raw_status is not None:
            rows[0]["status"] = raw_status
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=rows):
            with patch.object(self.ops, "_composer_clear", return_value=composer_clear):
                observation = self.port.observe_preservation(_pin())
        return assess_worker_recovery_preservation(observation).may_close, observation

    def test_a_settled_worker_may_close(self):
        for raw in ("done", "idle"):
            with self.subTest(status=raw):
                may_close, _obs = self._may_close(raw)
                self.assertTrue(may_close)

    def test_a_working_worker_never_closes(self):
        may_close, _obs = self._may_close("working")
        self.assertFalse(may_close)

    def test_a_blocked_worker_never_closes(self):
        # A live agent sitting at a permission prompt.
        may_close, obs = self._may_close("blocked")
        self.assertFalse(may_close)
        self.assertIn("worker_not_settled:blocked", obs.detail)

    def test_an_unreadable_or_novel_state_never_closes(self):
        for raw in (None, "some_novel_status", "unknown"):
            with self.subTest(status=raw):
                may_close, obs = self._may_close(raw)
                self.assertFalse(may_close)
                self.assertIn("worker_not_settled:unknown", obs.detail)

    def test_an_absent_row_never_closes(self):
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=[]):
            with patch.object(self.ops, "_composer_clear", return_value=True):
                observation = self.port.observe_preservation(_pin())
        self.assertFalse(assess_worker_recovery_preservation(observation).may_close)

    def test_composer_input_gained_after_preflight_never_closes(self):
        may_close, obs = self._may_close("done", composer_clear=False)
        self.assertFalse(may_close)
        self.assertIn("pending_composer_input", obs.detail)

    def test_the_shared_identity_refusal_is_preserved_verbatim(self):
        # The wrapper only ever ADDS a refusal; a shared identity refusal must reach the
        # policy with its own detail intact, not be masked by a settled-state message.
        self.inner.observation = PreservationObservation(
            identity_matches=False, detail="row_revision_drift:pinned=4:live=9"
        )
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=[]):
            observation = self.port.observe_preservation(_pin())
        self.assertFalse(observation.identity_matches)
        self.assertEqual(observation.detail, "row_revision_drift:pinned=4:live=9")

    def test_the_wrapper_forwards_every_actuation_call_and_the_typed_diagnostic(self):
        self.port.observe_old_slot(_pin())
        self.port.close_exact_generation(_pin())
        self.port.launch_action_bound("a", _pin())
        self.port.verify_attestation("a", _pin())
        self.assertEqual(
            self.inner.calls,
            ["observe_old_slot", "close_exact_generation", "launch_action_bound",
             "verify_attestation"],
        )
        # ``port_launch_failure_reason`` must read the INNER port's diagnostic through the
        # wrapper, not see an attribute-less object.
        self.inner.launch_failure_reason = "launch_target_absent"
        self.assertEqual(
            port_launch_failure_reason(self.port), "launch_target_absent"
        )


class RealIssuerResolutionTests(unittest.TestCase):
    """The REAL issuer resolution path, with no resolver override (review j#92641 wiring).

    Every other approval test injects an issuer, so the adapter's own resolution — the gate->role
    policy plus the committed-config anchor — was never executed and a mutation blanking the
    policy pointer survived. The authority a destructive close rests on has to be driven for
    real at least once.
    """

    APPROVAL_JOURNAL = "92500"
    ACTION_ID = "refresh-worker:" + LANE + ":claude:claude:wk:w4B:p10:r4"

    def setUp(self):
        self.repo = Path(__file__).resolve().parents[4]   # the real repo: it has the config blob
        self.request = _request(
            journal=self.APPROVAL_JOURNAL, action_id=self.ACTION_ID, action_generation=3,
            worker_revision="4", lane_revision="5", lane_generation="2",
        )
        self.ops = LiveWorkerRefreshOps(repo_root=self.repo, request=self.request)
        self.marker = render_worker_refresh_approval_marker(
            issue="14661", lane=LANE, action_id=self.ACTION_ID, action_generation=3,
            lane_revision="5", lane_generation="2", anchor_issue=ANCHOR_ISSUE,
            resume_anchor_journal=ANCHOR_JOURNAL, resume_gate=ANCHOR_GATE,
        )

    def _verify(self, notes):
        self.ops.journal_reader = lambda issue: [_Entry(self.APPROVAL_JOURNAL, notes)]
        self.ops.journal_reader_fresh = True
        return self.ops.approval_verified(self.request)

    def test_the_committed_config_anchor_resolves_from_the_tracked_object(self):
        pointer = self.ops._issuer_policy_pointer()
        self.assertTrue(pointer.startswith("git:.mozyo-bridge/config.yaml@"), pointer)
        self.assertGreater(len(pointer.split("@")[-1]), 0, "an anchor needs a real blob")

    def test_the_anchor_cites_the_ruling_that_actually_decided_this_gate(self):
        # Review j#92715: the anchor used to carry a repo-wide pointer at #14219 j#85530 Q3 — a
        # ruling that says nothing about this gate — so ``is_anchored`` passed while pointing at
        # a record that could not have decided the binding. Checking only "non-empty" cannot see
        # that; the anchor has to name the ruling that actually decided it.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_issuer_policy import (  # noqa: E501
            config_policy_pointer,
            resolve_journal_issuer,
        )

        issuer = resolve_journal_issuer(
            notes=self.marker, journal_id=self.APPROVAL_JOURNAL,
            policy_pointer=config_policy_pointer("deadbeef"),
        )
        self.assertEqual(issuer.role, "coordinator")
        self.assertTrue(issuer.is_anchored)
        self.assertIn("redmine:#14661:j#92641", issuer.authority_anchor)
        self.assertNotIn("#14219", issuer.authority_anchor)

    def test_the_pre_existing_gates_keep_their_own_ruling_anchor(self):
        # The fix must not retroactively re-attribute the hibernate-evidence gates.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_issuer_policy import (  # noqa: E501
            config_policy_pointer,
            resolve_journal_issuer,
        )

        issuer = resolve_journal_issuer(
            notes="[mozyo:workflow-event:gate=park_declared:workspace=ws:lane=l:lane_generation=2]",
            journal_id="1", policy_pointer=config_policy_pointer("deadbeef"),
        )
        self.assertEqual(issuer.role, "lane_worker")
        self.assertIn("redmine:#14219:j#85530:Q3", issuer.authority_anchor)
        self.assertNotIn("j#92641", issuer.authority_anchor)

    def test_every_gate_with_a_role_also_names_the_ruling_that_gave_it(self):
        # The runtime "unruled gate" guard is unreachable on its own (a gate with no role is
        # filtered before it), so the property it protects is pinned here instead: the two maps
        # must describe the SAME gates. Adding a gate to one alone is the drift that produced
        # this finding — a role with no ruling of its own, silently anchored to someone else's.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (  # noqa: E501
            hibernate_evidence_authority as auth,
        )

        self.assertEqual(
            set(auth._KIND_ISSUER), set(auth._KIND_RULING),
            "every gate with a canonical writer must name the ruling that decided it",
        )

    def test_a_gate_no_ruling_claims_is_unanchored(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
            contract_ruling_pointer,
        )

        self.assertEqual(contract_ruling_pointer("some_unruled_gate"), "")

    def test_a_coordinator_written_approval_verifies_through_the_real_resolver(self):
        # No issuer_resolver override: the gate->role policy and the committed anchor do the work.
        self.assertTrue(self._verify(f"## Gate: owner approval\n\n{self.marker}\n"))

    def test_without_a_committed_anchor_nothing_verifies(self):
        # The mutation that survived before: a blank policy pointer must make every approval
        # unanchored and therefore refused.
        with patch.object(self.ops, "_issuer_policy_pointer", return_value=""):
            self.assertFalse(self._verify(f"## Gate: owner approval\n\n{self.marker}\n"))

    def test_a_delegated_source_is_refused_through_the_real_resolver(self):
        delegated = self.marker.replace(
            "approval_source=direct_owner", "approval_source=standing_delegation"
        )
        self.assertFalse(self._verify(delegated))

    def test_a_second_authority_gate_in_the_same_journal_refuses(self):
        # Two authority-bearing gates in one note claim two contracts and prove neither.
        other = (
            "[mozyo:workflow-event:gate=park_declared:workspace=ws:lane="
            + LANE + ":lane_generation=2]"
        )
        self.assertFalse(self._verify(f"{self.marker}\n{other}\n"))


class CloseEdgeProgressRaceTests(unittest.TestCase):
    """The progress guard sits at the INNER CLOSE call site (review j#92656 F2).

    The actuator re-authenticates its lease between the preservation decision and the close, so
    a guard in ``observe_preservation`` is not the last observation. These drive the wrapper
    directly and land progress in exactly that window.
    """

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.request = _request(worker_revision="4")
        self.ops = LiveWorkerRefreshOps(repo_root=self.repo, request=self.request)
        self.inner = _FakeInnerPort()

    def _port(self, guard):
        return live_mod.SettledCloseBoundaryPort(
            inner=self.inner, ops=self.ops, request=self.request,
            progress_still_failed=guard,
        )

    def test_progress_landing_after_preservation_still_blocks_the_close(self):
        # The exact measured window: preservation passes, then progress lands, then close.
        state = {"failed": True}
        port = self._port(lambda: state["failed"])
        rows = [{"name": "wk", "pane_id": "w4B:p10", "revision": "4", "status": "done"}]
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=rows), \
                patch.object(self.ops, "_composer_clear", return_value=True):
            observation = port.observe_preservation(_pin())
        self.assertTrue(assess_worker_recovery_preservation(observation).may_close)
        state["failed"] = False          # a gate lands during the actuator's lease re-auth
        self.assertEqual(
            port.close_exact_generation(_pin()), live_mod.CLOSE_REFUSED_PROGRESS_MOVED
        )
        self.assertEqual(self.inner.calls, [], "the inner close must never run")

    def test_an_unreadable_progress_authority_blocks_the_close(self):
        def _boom():
            raise RuntimeError("durable source down")

        self.assertEqual(
            self._port(_boom).close_exact_generation(_pin()),
            live_mod.CLOSE_REFUSED_PROGRESS_MOVED,
        )
        self.assertEqual(self.inner.calls, [])

    def test_a_still_failed_turn_closes(self):
        # Discriminating in both directions.
        self.assertEqual(self._port(lambda: True).close_exact_generation(_pin()), "closed")
        self.assertEqual(self.inner.calls, ["close_exact_generation"])

    def test_a_post_close_replay_passes_no_guard_and_closes(self):
        # Replay supplies None: the close already committed, and re-litigating it would refuse
        # the very transactions that exist to be finished.
        self.assertEqual(self._port(None).close_exact_generation(_pin()), "closed")
        self.assertEqual(self.inner.calls, ["close_exact_generation"])

    def test_the_guard_runs_on_the_close_leg_not_the_preservation_leg(self):
        calls: list = []
        port = self._port(lambda: calls.append("guard") or True)
        rows = [{"name": "wk", "pane_id": "w4B:p10", "revision": "4", "status": "done"}]
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=rows), \
                patch.object(self.ops, "_composer_clear", return_value=True):
            port.observe_preservation(_pin())
        self.assertEqual(calls, [], "preservation must not be where progress is judged")
        port.close_exact_generation(_pin())
        self.assertEqual(calls, ["guard"])


class ResumeRailTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.ops = LiveWorkerRefreshOps(
            repo_root=self.repo,
            request=_request(
                action_id="refresh-worker:" + LANE + ":claude:claude:wk:w4B:p10:r4",
                action_generation=3, worker_revision="4",
            ),
        )
        self.continuation = ContinuationPointer(
            source="redmine", issue_id=ANCHOR_ISSUE, journal_id=ANCHOR_JOURNAL,
            expected_gate=ANCHOR_GATE, next_semantic_action="callback_recovery_once",
        )

    def _confirmable(self):
        """Every confirmation axis but the action binding satisfied.

        Built so the action binding is the SOLE discriminator: an earlier version of this
        test asserted ``False`` while the ledger lookup was failing anyway, so it passed
        whether or not the binding was checked at all.
        """
        reply_marker = build_marker(
            RedmineAnchor(issue=ANCHOR_ISSUE, journal=ANCHOR_JOURNAL), "reply",
            WORKER_PROVIDER,
        )
        record = _record(
            reply_marker, target="w4B:p22", recorded_at="2026-06-01T00:00:00+00:00"
        )
        self.ops.ledger = _FakeLedger([record])
        return [{"name": "wk", "pane_id": "w4B:p22", "status": "idle"}]

    def test_resume_confirmed_holds_when_the_slot_is_action_bound(self):
        rows = self._confirmable()
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=rows), \
                patch.object(
                    self.ops, "_providers",
                    return_value=(WORKER_PROVIDER, GATEWAY_PROVIDER)), \
                patch.object(
                    self.ops, "_fresh_attestation_observed_at",
                    return_value="2026-01-01T00:00:00+00:00"), \
                patch.object(self.ops, "_fresh_slot_action_bound", return_value=True):
            self.assertTrue(self.ops.resume_confirmed(self.continuation))

    def test_resume_once_never_sends_without_a_distinct_fresh_worker(self):
        rows = [{"name": "wk", "pane_id": "w4B:p10", "status": "done"}]
        oneshot: list = []
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=rows), \
                patch.object(self.ops, "_providers",
                             return_value=(WORKER_PROVIDER, GATEWAY_PROVIDER)), \
                patch.object(self.ops, "_recovery_delivery_service_ready", return_value=True), \
                patch.object(self.ops, "_oneshot_resume",
                             side_effect=lambda *a: oneshot.append(a) or DRAIN_SEND_OK):
            # The only row still carries the OLD locator -> never a blind send.
            self.assertEqual(self.ops.resume_once(self.continuation), DRAIN_SEND_ERROR)
        self.assertEqual(oneshot, [])

    def test_resume_once_never_sends_to_a_slot_not_bound_to_this_action(self):
        rows = [{"name": "wk", "pane_id": "w4B:p22", "status": "idle"}]
        oneshot: list = []
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=rows), \
                patch.object(self.ops, "_providers",
                             return_value=(WORKER_PROVIDER, GATEWAY_PROVIDER)), \
                patch.object(self.ops, "_recovery_delivery_service_ready", return_value=True), \
                patch.object(self.ops, "_fresh_slot_action_bound", return_value=False), \
                patch.object(self.ops, "_oneshot_resume",
                             side_effect=lambda *a: oneshot.append(a) or DRAIN_SEND_OK):
            self.assertEqual(self.ops.resume_once(self.continuation), DRAIN_SEND_ERROR)
        self.assertEqual(oneshot, [], "an unbound slot must receive nothing")

    def test_only_the_action_bound_rail_can_resume(self):
        # Review j#92601 F4: the governed CLI cannot carry the replacement action into the
        # transport, so it is no longer a rail this destructive surface may resume on.
        rows = [{"name": "wk", "pane_id": "w4B:p22", "status": "idle", "revision": "9"}]
        oneshot: list = []
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=rows), \
                patch.object(self.ops, "_providers",
                             return_value=(WORKER_PROVIDER, GATEWAY_PROVIDER)), \
                patch.object(self.ops, "_fresh_slot_action_bound", return_value=True), \
                patch.object(self.ops, "_recovery_delivery_service_ready", return_value=False), \
                patch.object(self.ops, "_oneshot_resume",
                             side_effect=lambda *a: oneshot.append(a) or DRAIN_SEND_OK):
            self.assertEqual(self.ops.resume_once(self.continuation), DRAIN_SEND_ERROR)
        self.assertEqual(oneshot, [], "no rail is available; nothing may be sent")

    def test_rail_readiness_requires_the_action_bound_service(self):
        with patch.object(self.ops, "_recovery_delivery_service_ready", return_value=False):
            self.assertFalse(self.ops.resume_rail_ready(_request()))
        with patch.object(self.ops, "_recovery_delivery_service_ready", return_value=True):
            self.assertTrue(self.ops.resume_rail_ready(_request()))

    def test_a_proven_zero_send_is_reported_as_such_not_as_an_error(self):
        # Review j#92601 F5: collapsing zero_send into the generic error left a post-close
        # transaction permanently unresumable.
        rows = [{"name": "wk", "pane_id": "w4B:p22", "status": "idle", "revision": "9"}]

        class _Svc:
            def __init__(self, outcome):
                self.outcome = outcome

            def ready(self):
                return True

            def deliver(self, request):
                return self.outcome

        cases = (
            (SimpleNamespace(started=True, zero_send=False), DRAIN_SEND_OK),
            (SimpleNamespace(started=False, zero_send=True), live_mod.DRAIN_SEND_ZERO),
            (SimpleNamespace(started=False, zero_send=False), DRAIN_SEND_ERROR),
        )
        for outcome, expected in cases:
            with self.subTest(expected=expected):
                with patch.object(live_mod, "list_herdr_agent_rows", return_value=rows), \
                        patch.object(live_mod, "repo_scope_workspace_id", return_value=LOCAL_WS), \
                        patch.object(self.ops, "_providers",
                                     return_value=(WORKER_PROVIDER, GATEWAY_PROVIDER)), \
                        patch.object(self.ops, "_fresh_slot_action_bound", return_value=True), \
                        patch.object(self.ops, "_recovery_delivery_service",
                                     return_value=_Svc(outcome)):
                    self.assertEqual(self.ops.resume_once(self.continuation), expected)

    def test_resume_confirmed_requires_the_action_binding(self):
        # Identical setup to the passing case above — ONLY the binding flips. Without this
        # symmetry the assertion would hold even if the binding were never consulted.
        rows = self._confirmable()
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=rows), \
                patch.object(
                    self.ops, "_providers",
                    return_value=(WORKER_PROVIDER, GATEWAY_PROVIDER)), \
                patch.object(
                    self.ops, "_fresh_attestation_observed_at",
                    return_value="2026-01-01T00:00:00+00:00"), \
                patch.object(self.ops, "_fresh_slot_action_bound", return_value=False):
            self.assertFalse(self.ops.resume_confirmed(self.continuation))

    def test_the_action_binding_helper_delegates_to_the_delivery_authority(self):
        # C3: with the helper itself never exercised, an implementation that always returned
        # True survived every test. This drives its real body.
        class _Svc:
            def __init__(self, may, boom=False):
                self.may, self.boom, self.seen = may, boom, []

            def preflight(self, request):
                if self.boom:
                    raise RuntimeError("attestation store unreadable")
                self.seen.append(request)
                return SimpleNamespace(may_deliver=self.may)

        rows = [{"name": "wk", "pane_id": "w4B:p22", "status": "idle", "revision": "9"}]
        def bound(svc):
            with patch.object(live_mod, "list_herdr_agent_rows", return_value=rows), \
                    patch.object(
                        live_mod, "repo_scope_workspace_id", return_value=LOCAL_WS), \
                    patch.object(self.ops, "_recovery_delivery_service", return_value=svc):
                return self.ops._fresh_slot_action_bound(
                    self.continuation, "w4B:p22", WORKER_PROVIDER
                )

        ok = _Svc(True)
        self.assertTrue(bound(ok))
        # The request handed to the authority carries THIS action and the fresh slot.
        self.assertEqual(ok.seen[0].target_action_id, self.ops.request.action_id)
        self.assertEqual(ok.seen[0].target_locator, "w4B:p22")
        self.assertFalse(bound(_Svc(False)))
        self.assertFalse(bound(_Svc(True, boom=True)))

    def test_the_action_binding_helper_fails_closed_on_an_unresolvable_slot(self):
        class _Svc:
            def preflight(self, request):
                raise AssertionError("must not be consulted for an unresolvable slot")

        # Ambiguous rows at the assigned name => no delivery request can be built.
        rows = [
            {"name": "wk", "pane_id": "w4B:p22", "status": "idle", "revision": "9"},
            {"name": "wk", "pane_id": "w4B:p22", "status": "idle", "revision": "9"},
        ]
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=rows), \
                patch.object(live_mod, "repo_scope_workspace_id", return_value=LOCAL_WS), \
                patch.object(self.ops, "_recovery_delivery_service", return_value=_Svc()):
            self.assertFalse(
                self.ops._fresh_slot_action_bound(
                    self.continuation, "w4B:p22", WORKER_PROVIDER
                )
            )

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


class RowStatusKeyCoverageTests(unittest.TestCase):
    """Every status key the row reader accepts must be exercised, not just the ones tests feed.

    Found by sweeping this Task's own closed vocabularies for partial coverage after review
    j#92846 (the sampling finding). ``_STATUS_KEYS`` has three members and the tests only ever
    fed ``status`` / ``state``, so deleting ``agent_status`` — the key the LIVE herdr row
    actually carries — passed all 206 #14661 tests. That is the R2 defect class exactly: a
    production-only seam no test executes, green all the way to a live no-op.

    The population is derived from the tuple, so a key added to it arrives already covered.

    The probe values are HERDR WIRE tokens, not mozyo runtime states — the two vocabularies read
    alike (``turn_ended`` vs ``done``) and an unrecognised token maps to ``unknown``, so feeding
    a runtime state would make every assertion compare unknown to unknown and pass for nothing.
    That is why the discriminating guard below is asserted first.
    """

    #: The herdr wire token for a settled turn, and one that must NOT win over it.
    _WIRE_SETTLED = "done"
    _WIRE_BUSY = "working"

    def test_each_accepted_status_key_is_honoured_on_its_own(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
            map_agent_status,
        )

        expected = map_agent_status(self._WIRE_SETTLED)
        self.assertNotEqual(expected, map_agent_status(None), "the probe value must discriminate")
        for key in live_mod._STATUS_KEYS:
            with self.subTest(key=key):
                self.assertEqual(
                    live_mod._row_runtime_state({key: self._WIRE_SETTLED}), expected,
                    f"a row carrying only {key!r} is not read; if that key is the live one, "
                    "every runtime observation silently degrades to unknown",
                )

    def test_the_keys_are_tried_in_the_declared_order(self):
        # The tuple's order is a contract (the live key first), not decoration: a row that
        # carries several keys must resolve by the declared precedence, or two readers of the
        # same row can disagree about the worker's state.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
            map_agent_status,
        )

        keys = list(live_mod._STATUS_KEYS)
        for i, winner in enumerate(keys):
            with self.subTest(winner=winner):
                row = {winner: self._WIRE_SETTLED}
                row.update({loser: self._WIRE_BUSY for loser in keys[i + 1:]})
                self.assertEqual(
                    live_mod._row_runtime_state(row), map_agent_status(self._WIRE_SETTLED),
                    f"{winner!r} must win over the keys declared after it",
                )

    def test_the_accepted_keys_match_the_shared_herdr_reader(self):
        # This module keeps its own copy of the shared reader's key tuple. Reported to the
        # reviewer rather than collapsed here (no finding covers that src surface), but the
        # duplication must not drift silently: if the shared authority grows a key, this fails
        # instead of the worker reading rows the rest of the system already understands.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
            _ROW_STATUS_KEYS,
        )

        self.assertEqual(
            tuple(live_mod._STATUS_KEYS), tuple(_ROW_STATUS_KEYS),
            "the worker refresh adapter's status keys have drifted from the shared herdr reader",
        )


class RunbookAuthorityAnchorTests(unittest.TestCase):
    """The cataloged runbook must describe the anchor the code actually builds (review j#92767).

    R7 gave each gate its own ruling pointer but left the operator/auditor source of truth
    describing a ONE-part anchor ("the committed config blob"). Both the broken R6 anchor and the
    fixed R7 one satisfied that sentence, so the very misattribution R7 fixed was undetectable
    from the canonical doc — the third docs drift in three rounds.

    So this pins the agreement by DERIVING both sides from the code: the expected pointers come
    from ``contract_ruling_pointer``, never from a literal spelled here. Change the code pointer
    and the doc goes red; swap the pointers in the doc and it goes red too. A spelling check
    could do neither (#14539: pin the operation, not the spelling).

    The POPULATION is derived too (review j#92846). The first version derived the pointer values
    but hand-picked one representative gate, ``park_declared``, to stand for "the pre-existing
    gates keep their ruling" — so re-attributing any of the other four in code passed every test.
    A derived oracle is only derived when BOTH halves are: an oracle whose whole purpose is to
    catch misattribution across a gate set cannot sample that set.
    """

    def setUp(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (  # noqa: E501
            hibernate_evidence_authority as auth,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.worker_refresh_approval import (  # noqa: E501
            WORKER_REFRESH_APPROVAL_GATE,
        )

        self.gate = WORKER_REFRESH_APPROVAL_GATE
        # The population comes from the map itself, so a gate added to the code is a gate this
        # oracle immediately demands the runbook account for.
        self.rulings = {
            gate: auth.contract_ruling_pointer(gate) for gate in auth._KIND_RULING
        }
        self.distinct_rulings = set(self.rulings.values())
        self.repo = Path(__file__).resolve().parents[4]
        body = (self.repo / "vibes" / "docs" / "tasks" / "herdr-lane-operations.md").read_text(
            encoding="utf-8"
        )
        lines = body.splitlines()
        start = next(
            i for i, line in enumerate(lines) if line.startswith("## ") and "#14661" in line
        )
        end = next(
            (i for i, line in enumerate(lines[start + 1:], start + 1) if line.startswith("#")),
            len(lines),
        )
        self.section = lines[start:end]

    def _lines_mentioning(self, token):
        return [line for line in self.section if token in line]

    def test_every_gate_has_a_rival_ruling_it_could_be_misattributed_to(self):
        # The per-gate check below asserts a gate's line carries NO OTHER known ruling. If the
        # map ever collapsed onto a single pointer that assertion would be vacuously true — and
        # a single repo-wide pointer is exactly the R6 defect. So the rival set must be non-empty
        # for every gate, or the negative half of the oracle proves nothing.
        for gate, ruling in sorted(self.rulings.items()):
            with self.subTest(gate=gate):
                self.assertTrue(
                    self.distinct_rulings - {ruling},
                    "every gate needs some other ruling it could be wrongly attributed to; "
                    "with one pointer for the whole map the misattribution check is vacuous",
                )

    def test_the_runbook_attributes_every_gate_to_its_own_ruling(self):
        # Derived on BOTH axes: which gates to check comes from the map, and what each one must
        # say comes from contract_ruling_pointer. Re-attributing ANY single gate in the code
        # reddens this (review j#92846 — the version that sampled one gate let four through).
        for gate, ruling in sorted(self.rulings.items()):
            with self.subTest(gate=gate):
                mentions = self._lines_mentioning(gate)
                self.assertTrue(
                    mentions,
                    f"the runbook never names the gate {gate!r}; a gate the authority map "
                    "carries but the cataloged doc does not account for",
                )
                self.assertTrue(
                    any(ruling in line for line in mentions),
                    f"the runbook names {gate!r} without the ruling that decided it "
                    f"({ruling!r}); code and cataloged doc have drifted",
                )
                for rival in sorted(self.distinct_rulings - {ruling}):
                    self.assertFalse(
                        any(rival in line for line in mentions),
                        f"the runbook attributes {gate!r} to {rival!r}, a ruling that says "
                        "nothing about it — the R6 misattribution, restated in the doc",
                    )

    def test_the_runbook_explains_every_part_of_the_real_anchor(self):
        # Derive the parts from a real resolution rather than describing them from memory.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_issuer_policy import (  # noqa: E501
            config_policy_pointer,
            resolve_journal_issuer,
        )

        issuer = resolve_journal_issuer(
            journal_id="92500",
            notes=f"[mozyo:workflow-event:gate={self.gate}:head=abc]",
            policy_pointer=config_policy_pointer("deadbeef"),
        )
        parts = issuer.authority_anchor.split(" ")
        self.assertEqual(len(parts), 3, issuer.authority_anchor)
        ruling, config, evidence = parts
        text = "\n".join(self.section)
        for token in (
            ruling,                                    # (a) the gate's own ruling
            config.split("@")[0] + "@",                # (b) the committed config blob
            evidence.split("j#")[0] + "j#",            # (c) the exact evidence journal
            ":gate=",                                  # ...bound to the exact gate
        ):
            self.assertIn(
                token, text,
                f"the runbook does not explain the anchor component {token!r}",
            )

    def test_the_committed_config_carries_no_gate_role_mapping(self):
        # The runbook says the config blob alone cannot prove a writer contract, because it has
        # no gate->role mapping. That is a fact about the committed file, so pin it: if a gate
        # name ever lands in the config, the runbook's reasoning silently stops holding.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (  # noqa: E501
            hibernate_evidence_authority as auth,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_issuer_policy import (  # noqa: E501
            CONFIG_RELPATH,
        )

        config = (self.repo / CONFIG_RELPATH).read_text(encoding="utf-8")
        for gate in auth._KIND_ISSUER:
            self.assertNotIn(
                gate, config,
                f"{CONFIG_RELPATH} now mentions the gate {gate!r}; the runbook's claim that the "
                "config blob carries no gate->role mapping needs re-deriving",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
