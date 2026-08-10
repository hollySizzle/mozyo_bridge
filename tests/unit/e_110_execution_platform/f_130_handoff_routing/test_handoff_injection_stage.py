"""Contract + drift guard for the shared injection-stage authority (Redmine #14232).

``domain/injection_stage.py`` answers exactly one question for the whole handoff surface:
**may a blind retry duplicate this payload?** #14232 j#84877 recorded that three readers
answered it separately and inconsistently, so the value of the module is only as good as two
properties, both pinned here:

- the ``(status, reason)`` -> stage mapping itself (the truth table), and
- **exhaustiveness**: the blocked-reason partition covers the whole ``Reason`` wire vocabulary,
  so a newly added reason cannot be *forgotten* into a bucket by default. That guard is the
  point — the two reasons the old private table had silently omitted
  (``reader_upgrade_required`` / ``execution_root_outside_target_repo``) were both added by
  later issues that had no reason to know a second table existed.

These are module-contract assertions, not recurrence pins, so they live in ``tests/unit``; the
#14232 recurrence pins are in
``tests/regressions/test_issue_14232_handoff_partial_delivery_outcome.py``.
"""
from __future__ import annotations

import typing
import unittest

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    Reason,
    make_outcome,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (
    INJECTION_STAGES,
    NON_BLOCKED_REASONS,
    POST_INJECTION_BLOCKED_REASONS,
    PRE_INJECTION_BLOCKED_REASONS,
    REASON_TRANSPORT_ERROR,
    STAGE_NOT_SENT,
    STAGE_SUBMITTED_CONFIRMED,
    STAGE_UNCERTAIN_PARTIAL,
    blind_retry_prohibited,
    injection_stage_for,
    injection_stage_record_lines,
    injection_stage_telemetry,
    canonical_v2_generation_binding,
    injection_stage_for_outcome,
    stage_from_telemetry,
    stage_guidance,
    turn_start_positively_observed,
)


class ReasonVocabularyPartitionTest(unittest.TestCase):
    """The partition is an exhaustive, disjoint split of the ``Reason`` wire vocabulary."""

    def setUp(self) -> None:
        self.reasons = frozenset(typing.get_args(Reason))

    def test_partition_covers_every_reason(self):
        classified = (
            NON_BLOCKED_REASONS
            | PRE_INJECTION_BLOCKED_REASONS
            | POST_INJECTION_BLOCKED_REASONS
        )
        self.assertEqual(
            self.reasons - classified,
            frozenset(),
            "a handoff Reason is unclassified: add it to exactly one of "
            "NON_BLOCKED_REASONS / PRE_INJECTION_BLOCKED_REASONS / "
            "POST_INJECTION_BLOCKED_REASONS after deciding whether a blind retry could "
            "duplicate it",
        )

    def test_partition_names_no_reason_outside_the_wire_vocabulary(self):
        classified = (
            NON_BLOCKED_REASONS
            | PRE_INJECTION_BLOCKED_REASONS
            | POST_INJECTION_BLOCKED_REASONS
        )
        self.assertEqual(
            classified - self.reasons,
            frozenset(),
            "the partition classifies a token the handoff wire cannot emit (renamed / removed?)",
        )

    def test_the_three_buckets_are_pairwise_disjoint(self):
        for left, right in (
            (NON_BLOCKED_REASONS, PRE_INJECTION_BLOCKED_REASONS),
            (NON_BLOCKED_REASONS, POST_INJECTION_BLOCKED_REASONS),
            (PRE_INJECTION_BLOCKED_REASONS, POST_INJECTION_BLOCKED_REASONS),
        ):
            with self.subTest(left=sorted(left)[:1], right=sorted(right)[:1]):
                self.assertEqual(left & right, frozenset())

    def test_transport_error_is_a_wire_reason_and_post_injection(self):
        self.assertIn(REASON_TRANSPORT_ERROR, self.reasons)
        self.assertIn(REASON_TRANSPORT_ERROR, POST_INJECTION_BLOCKED_REASONS)


class InjectionStageTruthTableTest(unittest.TestCase):
    """The ``(status, reason)`` -> stage mapping."""

    def test_only_sent_ok_is_a_confirmed_submission(self):
        self.assertEqual(
            injection_stage_for("sent", "ok"), STAGE_SUBMITTED_CONFIRMED
        )
        # Every other cell must NOT be confirmed, including the relaxed rail's `sent`.
        for status, reason in (
            ("sent", "queue_enter"),
            ("pending_input", "ok"),
            ("blocked", "marker_timeout"),
            ("blocked", "invalid_args"),
        ):
            with self.subTest(status=status, reason=reason):
                self.assertNotEqual(
                    injection_stage_for(status, reason), STAGE_SUBMITTED_CONFIRMED
                )

    def test_pre_injection_blocked_reasons_are_not_sent(self):
        for reason in sorted(PRE_INJECTION_BLOCKED_REASONS):
            with self.subTest(reason=reason):
                self.assertEqual(
                    injection_stage_for("blocked", reason), STAGE_NOT_SENT
                )

    def test_post_injection_blocked_reasons_are_uncertain_partial(self):
        for reason in sorted(POST_INJECTION_BLOCKED_REASONS):
            with self.subTest(reason=reason):
                self.assertEqual(
                    injection_stage_for("blocked", reason), STAGE_UNCERTAIN_PARTIAL
                )

    def test_pending_input_and_marker_unobserved_queue_enter_are_uncertain_partial(self):
        self.assertEqual(
            injection_stage_for("pending_input", "ok"), STAGE_UNCERTAIN_PARTIAL
        )
        self.assertEqual(
            injection_stage_for("sent", "queue_enter"), STAGE_UNCERTAIN_PARTIAL
        )

    def test_a_pre_injection_reason_on_a_non_blocked_status_is_not_not_sent(self):
        """The mapping keys on ``(status, reason)``, not on the reason alone.

        ``ok`` is carried by both ``sent`` (confirmed) and ``pending_input`` (parked), so a
        reason-only classifier would collapse them. A pre-injection reason arriving on a
        non-``blocked`` status is incoherent input and must fail closed, not claim zero-send.
        """
        self.assertEqual(
            injection_stage_for("sent", "invalid_args"), STAGE_UNCERTAIN_PARTIAL
        )

    def test_unrecognised_input_fails_closed_to_uncertain_partial(self):
        for status, reason in (
            ("weird", "weird"),
            ("blocked", "a_reason_that_does_not_exist"),
            (None, None),
            ("", ""),
            (object(), object()),
        ):
            with self.subTest(status=status, reason=reason):
                self.assertEqual(
                    injection_stage_for(status, reason), STAGE_UNCERTAIN_PARTIAL
                )

    def test_whitespace_is_stripped_before_classification(self):
        self.assertEqual(injection_stage_for(" sent ", " ok "), STAGE_SUBMITTED_CONFIRMED)


#: A generation-coherent gateway binding, in the shape `observe_queue_enter_gateway_binding`
#: returns. The rail writes it together with `event_wait_kind` ONLY when the pre-arm and
#: post-collect generations match, so its presence is what makes the wait attributable.
_BINDING_NAME = "mzb1_ws_codex_lane"
_BINDING_TERMINAL = "terminal-test"
_BINDING_LOCATOR = "w4B:p4T"
_BINDING_REVISION = "1"
_BINDING = {
    "provider": "codex",
    "assigned_name": _BINDING_NAME,
    "locator": _BINDING_LOCATOR,
    "terminal_id": _BINDING_TERMINAL,
    "row_revision": _BINDING_REVISION,
    "process_generation": (
        f"{len(_BINDING_NAME)}:{_BINDING_NAME}:"
        f"{len(_BINDING_TERMINAL)}:{_BINDING_TERMINAL}:"
        f"{len(_BINDING_LOCATOR)}:{_BINDING_LOCATOR}:r{_BINDING_REVISION}"
    ),
    "attestation_observed_at": "2026-07-29T20:10:01+00:00",
    "startup_action_id": "startup-abc",
}


def _snapshot(
    runtime_state: str, *, read_ok: bool = True, event_wait_kind=None, binding=None
) -> dict:
    """A queue-enter observation in the shape the rail persists.

    Without ``event_wait_kind`` / ``binding`` this is the v1 post-choreography snapshot (a
    non-causal poll). With both it is the v2 record the rail emits under a coherent
    generation — the only queue-enter shape that carries a causally attributable start.
    """
    observation = {
        "observation_kind": "post_choreography_snapshot",
        "source": "herdr_agent_get",
        "runtime_state": runtime_state,
        "read_ok": read_ok,
        "read_reason": None if read_ok else "transport_error",
        "poll_attempts": 3,
    }
    extra = {}
    if event_wait_kind is not None:
        extra["event_wait_kind"] = event_wait_kind
    if binding is not None:
        extra["gateway_binding"] = binding
    if extra:
        extra["observation_version"] = 2
        observation.update(extra)
    return observation


def _causal(runtime_state: str = "busy") -> dict:
    """The v2 observation whose armed wait fired under a coherent generation."""
    observation = _snapshot(
        runtime_state, event_wait_kind="changed", binding=_BINDING
    )
    observation["baseline_runtime_state"] = "turn_ended"
    return observation


def _run_front_door(status, reason, *, mode="queue-enter", rail_rc=0):
    """Drive the q-enter front door with a stubbed rail and return ``(exit_code, outcome)``."""
    import argparse

    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application import (
        cli_handoff_q_enter as mod,
    )

    emitted = []

    def _fake_orchestrate(args, **kwargs):
        args.delivery_outcome = make_outcome(
            status=status, reason=reason, receiver="claude", target="%7", anchor=None,
            mode=mode, kind="implementation_request", notification_marker="[m]",
            source="redmine",
        )
        return rail_rc

    original_emit, original_orchestrate = mod._emit_submit_outcome, mod.orchestrate_handoff
    mod._emit_submit_outcome = lambda o, *, record_format: emitted.append(o)
    mod.orchestrate_handoff = _fake_orchestrate
    try:
        rc = mod.cmd_handoff_q_enter(argparse.Namespace(
            intent="worker_dispatch", source="redmine", issue="14232", journal="94508",
            task_id=None, comment_id=None, anchor_url=None,
            kind="implementation_request", to="claude", classification=None,
            record_format="both",
        ))
    finally:
        mod._emit_submit_outcome = original_emit
        mod.orchestrate_handoff = original_orchestrate
    return rc, emitted[0]


class TurnStartEvidenceTest(unittest.TestCase):
    """What counts as POSITIVE evidence that the receiver began a turn (j#95333 F1)."""

    def test_only_an_armed_wait_counts(self):
        # The queue-enter rail's own armed working-transition wait, under a coherent
        # generation ...
        self.assertTrue(turn_start_positively_observed(_causal()))
        # ... and the herdr event rail's armed wait (used by `--mode standard`).
        self.assertTrue(
            turn_start_positively_observed(None, {"outcome": "started"})
        )

    def test_a_post_hoc_busy_snapshot_is_not_evidence(self):
        """Review j#95601: the field's own source contract forbids reading it as a start.

        `DeliveryOutcome.queue_enter_turn_start_observation` says a post-hoc snapshot "does
        not prove causality the way an armed `wait agent-status` transition does, so it must
        not be read as an event-observed turn start". The queue-enter rail runs no idle
        precondition gate, so a receiver that was ALREADY busy before the send — or a recycled
        process running someone else's turn — reads `busy` just the same.
        """
        self.assertFalse(turn_start_positively_observed(_snapshot("busy")))
        self.assertFalse(
            turn_start_positively_observed(_snapshot("busy", event_wait_kind="timeout"))
        )

    def test_a_causal_start_is_not_retracted_by_the_later_snapshot_state(self):
        """A fast turn reads `turn_ended`; an idle-again receiver reads `awaiting_input`.

        Neither retracts an armed wait that already fired — the wait was set up BEFORE this
        send's Enter, so what it saw belongs to this send.
        """
        for runtime_state in ("turn_ended", "awaiting_input", "blocked", "unknown"):
            with self.subTest(runtime_state=runtime_state):
                self.assertTrue(turn_start_positively_observed(_causal(runtime_state)))

    def test_awaiting_input_is_evidence_AGAINST_a_start_absent_a_causal_signal(self):
        # The observation module documents it as "delivered, but a turn start was not
        # observed" — so it is not merely an absent signal.
        self.assertFalse(turn_start_positively_observed(_snapshot("awaiting_input")))

    def test_an_incoherent_generation_is_not_evidence(self):
        """The rail drops BOTH fields when the generation is incoherent.

        A record carrying one without the other therefore did not come from that gate, and
        must not be trusted as if it had.
        """
        self.assertFalse(
            turn_start_positively_observed(_snapshot("busy", event_wait_kind="changed"))
        )
        self.assertFalse(
            turn_start_positively_observed(_snapshot("busy", binding=_BINDING))
        )

    def test_missing_or_non_idle_baseline_is_not_causal_authority(self):
        missing = _snapshot(
            "busy", event_wait_kind="changed", binding=_BINDING
        )
        busy = dict(missing, baseline_runtime_state="busy")
        malformed = dict(missing, baseline_runtime_state=" turn_ended ")

        self.assertFalse(turn_start_positively_observed(missing))
        self.assertFalse(turn_start_positively_observed(busy))
        self.assertFalse(turn_start_positively_observed(malformed))

    def test_every_other_signal_fails_closed(self):
        for observation, event in (
            (_snapshot("blocked"), None),
            (_snapshot("unknown"), None),
            (_snapshot("busy", read_ok=False), None),   # a failed read observed nothing
            (_snapshot("busy", event_wait_kind="absent", binding=_BINDING), None),
            (_snapshot("busy", event_wait_kind="timeout", binding=_BINDING), None),
            (None, None),                                # no observation at all
            (None, {"outcome": "delivered_not_started"}),
            (None, {"outcome": "inject_failed"}),
            ("not-a-mapping", "not-a-mapping"),
        ):
            with self.subTest(observation=observation, event=event):
                self.assertFalse(turn_start_positively_observed(observation, event))


class QueueEnterConfirmationCarveOutTest(unittest.TestCase):
    """`sent`/`ok` means different things per rail, so the mode is part of the input."""

    def test_queue_enter_ok_needs_a_causally_attributable_start(self):
        self.assertEqual(
            injection_stage_for("sent", "ok", mode="queue-enter",
                                queue_enter_turn_start_observation=_causal()),
            STAGE_SUBMITTED_CONFIRMED,
        )
        # No post-hoc snapshot state confirms on its own — including `busy`.
        for state in ("busy", "awaiting_input", "turn_ended", "blocked", "unknown"):
            with self.subTest(runtime_state=state):
                self.assertEqual(
                    injection_stage_for("sent", "ok", mode="queue-enter",
                                        queue_enter_turn_start_observation=_snapshot(state)),
                    STAGE_UNCERTAIN_PARTIAL,
                )

    def test_queue_enter_ok_with_no_observation_at_all_is_unconfirmed(self):
        # The tmux backend runs no queue-enter snapshot, so this is its normal shape.
        self.assertEqual(
            injection_stage_for("sent", "ok", mode="queue-enter"),
            STAGE_UNCERTAIN_PARTIAL,
        )

    def test_the_standard_rail_is_untouched_by_the_carve_out(self):
        # `--mode standard` DID verify a turn start before resolving to `ok`.
        self.assertEqual(
            injection_stage_for("sent", "ok", mode="standard"), STAGE_SUBMITTED_CONFIRMED
        )
        self.assertEqual(
            injection_stage_for("sent", "ok", mode="standard",
                                turn_start_outcome={"outcome": "started"}),
            STAGE_SUBMITTED_CONFIRMED,
        )

    def test_an_unset_mode_keeps_the_pre_carve_out_reading(self):
        """A two-token reader cannot apply the carve-out and must not demote everything.

        Demoting on unknown mode would downgrade every genuinely-confirmed standard send a
        legacy reader sees. The authoritative call site always passes the mode (see
        `MakeOutcomeCarriesTheStageTest`), and consumers read the carried result.
        """
        self.assertEqual(
            injection_stage_for("sent", "ok"), STAGE_SUBMITTED_CONFIRMED
        )


class CanonicalV2BindingShapeTest(unittest.TestCase):
    """What the shape gate ACCEPTS (review j#95827). Green on both heads -> contract, not pin.

    The rejections it must make are recurrence pins and live in the #14232 regressions file;
    these are the cells that keep the gate from becoming a blanket refusal.
    """

    def test_a_canonical_v2_binding_is_accepted(self):
        self.assertTrue(canonical_v2_generation_binding(_causal()))

    def test_an_empty_row_revision_is_rejected(self):
        observation = _snapshot(
            "busy", event_wait_kind="changed",
            binding={**_BINDING, "row_revision": ""},
        )
        self.assertFalse(canonical_v2_generation_binding(observation))

    def test_unknown_extra_keys_are_accepted(self):
        """Additive schema growth must not silently demote every delivery.

        Rejecting unknown keys would fail closed in the more damaging direction: a future
        #14203 field would look like a transport regression rather than a schema change.
        """
        observation = _snapshot(
            "busy", event_wait_kind="changed",
            binding={**_BINDING, "future_field": "x"},
        )
        self.assertTrue(canonical_v2_generation_binding(observation))

    def test_the_canonical_int_version_is_the_only_accepted_version(self):
        """Review j#95881: exact type, so numeric equality cannot widen the schema gate."""
        self.assertTrue(canonical_v2_generation_binding(_causal()))

    def test_empty_or_whitespace_revision_is_rejected(self):
        observation = _snapshot(
            "busy", event_wait_kind="changed", binding={**_BINDING, "row_revision": ""},
        )
        self.assertFalse(canonical_v2_generation_binding(observation))
        blank = _snapshot(
            "busy", event_wait_kind="changed", binding={**_BINDING, "row_revision": " "},
        )
        self.assertFalse(canonical_v2_generation_binding(blank))

    def test_a_non_mapping_observation_is_rejected(self):
        for value in (None, "obs", ["obs"], 1):
            with self.subTest(value=value):
                self.assertFalse(canonical_v2_generation_binding(value))


class CarveOutIsNotAnOverCorrectionTest(unittest.TestCase):
    """The j#95333 F1 / F2 fixes must not blanket-demote or blanket-fail.

    Relocated here from the #14232 regressions file: measured GREEN on the reviewed head
    ``0426e915``, so these assert a contract rather than detect a recurrence (the
    tests-placement policy's R3-b is a file-unit rule). They exist because a guard that
    demoted *every* send, or an exit mapping that failed *every* invocation, would satisfy
    the recurrence pins while being just as wrong.
    """

    def test_a_receiver_that_did_start_a_turn_is_still_confirmed(self):
        self.assertEqual(
            injection_stage_for(
                "sent", "ok", mode="queue-enter",
                queue_enter_turn_start_observation=_causal(),
            ),
            STAGE_SUBMITTED_CONFIRMED,
        )

    def test_a_confirmed_front_door_delivery_still_exits_zero(self):
        rc, front = _run_front_door("sent", "ok", mode="standard")
        self.assertFalse(front.blocked)
        self.assertTrue(front.dispatched)
        self.assertEqual(rc, 0)

    def test_a_rail_that_already_failed_keeps_its_own_exit_code(self):
        rc, _front = _run_front_door("blocked", "transport_error", rail_rc=3)
        self.assertEqual(rc, 3)


class InjectionStageForOutcomeTest(unittest.TestCase):
    def test_absent_outcome_fails_closed(self):
        self.assertEqual(injection_stage_for_outcome(None), STAGE_UNCERTAIN_PARTIAL)

    def test_a_carried_stage_wins_over_re_derivation(self):
        class _Carrying:
            injection_stage = {"stage": STAGE_NOT_SENT}
            status, reason, mode = "sent", "ok", "standard"

        self.assertEqual(injection_stage_for_outcome(_Carrying()), STAGE_NOT_SENT)

    def test_an_outcome_without_a_carried_stage_is_re_derived_with_full_context(self):
        class _Legacy:
            injection_stage = None
            status, reason, mode = "sent", "ok", "queue-enter"
            queue_enter_turn_start_observation = None
            turn_start_outcome = None

        self.assertEqual(injection_stage_for_outcome(_Legacy()), STAGE_UNCERTAIN_PARTIAL)


class BlindRetryPredicateTest(unittest.TestCase):
    def test_only_not_sent_permits_a_blind_retry(self):
        self.assertFalse(blind_retry_prohibited(STAGE_NOT_SENT))
        self.assertTrue(blind_retry_prohibited(STAGE_UNCERTAIN_PARTIAL))
        self.assertTrue(
            blind_retry_prohibited(STAGE_SUBMITTED_CONFIRMED),
            "re-issuing a confirmed submission duplicates it; the predicate answers "
            "'may I resend without re-reading?', not 'is there work left'",
        )

    def test_an_unknown_stage_token_prohibits_a_blind_retry(self):
        for token in ("", None, "not_a_stage", object()):
            with self.subTest(token=token):
                self.assertTrue(blind_retry_prohibited(token))


class StageTelemetryTest(unittest.TestCase):
    def test_every_stage_has_guidance(self):
        for stage in INJECTION_STAGES:
            with self.subTest(stage=stage):
                self.assertTrue(stage_guidance(stage))

    def test_unknown_stage_has_no_guidance(self):
        self.assertEqual(stage_guidance("not_a_stage"), "")

    def test_telemetry_carries_stage_flag_and_guidance(self):
        telemetry = injection_stage_telemetry("blocked", REASON_TRANSPORT_ERROR)
        self.assertEqual(telemetry["stage"], STAGE_UNCERTAIN_PARTIAL)
        self.assertTrue(telemetry["blind_retry_prohibited"])
        self.assertTrue(telemetry["next_action"])
        self.assertEqual(
            set(telemetry), {"stage", "blind_retry_prohibited", "next_action"}
        )

    def test_stage_round_trips_through_the_telemetry_mapping(self):
        for status, reason in (("sent", "ok"), ("blocked", "invalid_args"), ("x", "y")):
            with self.subTest(status=status, reason=reason):
                telemetry = injection_stage_telemetry(status, reason)
                self.assertEqual(
                    stage_from_telemetry(telemetry), injection_stage_for(status, reason)
                )

    def test_stage_from_telemetry_is_none_for_absent_or_bogus_input(self):
        for value in (None, {}, {"stage": "not_a_stage"}, {"stage": 3}, "sent"):
            with self.subTest(value=value):
                self.assertIsNone(stage_from_telemetry(value))

    def test_record_lines_render_for_a_known_stage_and_are_empty_otherwise(self):
        lines = injection_stage_record_lines(
            injection_stage_telemetry("blocked", "turn_start_unconfirmed")
        )
        self.assertEqual(len(lines), 1)
        self.assertIn(STAGE_UNCERTAIN_PARTIAL, lines[0])
        self.assertIn("blind retry PROHIBITED", lines[0])
        self.assertEqual(injection_stage_record_lines(None), [])
        self.assertEqual(injection_stage_record_lines({"stage": "bogus"}), [])

    def test_not_sent_record_line_says_retry_is_safe(self):
        lines = injection_stage_record_lines(
            injection_stage_telemetry("blocked", "invalid_args")
        )
        self.assertIn("retry safe", lines[0])


class MakeOutcomeCarriesTheStageTest(unittest.TestCase):
    """``make_outcome`` derives the projection for EVERY terminal path.

    Deriving it in the one factory (rather than at each terminal ``emit``) is what makes it
    impossible for a newly added terminal path to ship a delivery record whose retry safety a
    reader has to re-derive — the same posture the #13583 delivery-outcome gate adopted after
    hand-picked publish sites were missed.
    """

    def _built(self, status: str, reason: str, *, mode: str = "standard", **extra):
        return make_outcome(
            status=status,
            reason=reason,
            receiver="codex",
            target="%7",
            anchor=None,
            mode=mode,
            kind="reply",
            notification_marker=None,
            source="redmine",
            **extra,
        )

    def test_stage_is_present_on_sent_pending_and_blocked_outcomes(self):
        for status, reason, expected in (
            ("sent", "ok", STAGE_SUBMITTED_CONFIRMED),
            ("sent", "queue_enter", STAGE_UNCERTAIN_PARTIAL),
            ("pending_input", "ok", STAGE_UNCERTAIN_PARTIAL),
            ("blocked", "invalid_args", STAGE_NOT_SENT),
            ("blocked", REASON_TRANSPORT_ERROR, STAGE_UNCERTAIN_PARTIAL),
        ):
            with self.subTest(status=status, reason=reason):
                outcome = self._built(status, reason)
                self.assertEqual(outcome.injection_stage["stage"], expected)
                self.assertEqual(
                    outcome.injection_stage["blind_retry_prohibited"],
                    blind_retry_prohibited(expected),
                )

    def test_make_outcome_passes_the_mode_and_telemetry_to_the_authority(self):
        """The one full-context call: `make_outcome` must not drop the carve-out inputs.

        If it ever passed only the two tokens again, a marker-observed queue-enter send whose
        receiver never started a turn would be recorded as a confirmed submission — the exact
        j#95333 F1 defect.
        """
        # Same terminal `(status, reason)`, same rail — only the causal telemetry differs.
        confirmed = self._built(
            "sent", "ok", mode="queue-enter",
            queue_enter_turn_start_observation=_causal(),
        )
        unconfirmed = self._built(
            "sent", "ok", mode="queue-enter",
            queue_enter_turn_start_observation=_snapshot("awaiting_input"),
        )
        self.assertEqual(
            confirmed.injection_stage["stage"], STAGE_SUBMITTED_CONFIRMED
        )
        self.assertEqual(
            unconfirmed.injection_stage["stage"], STAGE_UNCERTAIN_PARTIAL
        )
        # status / reason / next_action_owner stay identical — the #13292 telemetry-only
        # boundary forbids the observation influencing the wire, and it still does.
        self.assertEqual(confirmed.status, unconfirmed.status)
        self.assertEqual(confirmed.reason, unconfirmed.reason)
        self.assertEqual(confirmed.next_action_owner, unconfirmed.next_action_owner)

    def test_stage_survives_the_json_projection(self):
        payload = self._built("blocked", REASON_TRANSPORT_ERROR).to_dict()
        self.assertEqual(
            payload["injection_stage"]["stage"], STAGE_UNCERTAIN_PARTIAL
        )


class TransportErrorWordingTest(unittest.TestCase):
    """The additive ``transport_error`` reason renders complete, secret-safe wording."""

    def _built(self):
        return make_outcome(
            status="blocked",
            reason=REASON_TRANSPORT_ERROR,
            receiver="codex",
            target="%7",
            anchor=None,
            mode="queue-enter",
            kind="reply",
            notification_marker="[m]",
            source="redmine",
        )

    def test_next_action_is_owned_by_the_sender_and_forbids_a_blind_resend(self):
        outcome = self._built()
        self.assertEqual(outcome.next_action_owner, "sender")
        self.assertIn("blind", outcome.next_action.lower())

    def test_wording_is_not_the_generic_fallback(self):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            _outcome_narrative,
            next_action_for,
        )

        self.assertNotEqual(
            next_action_for("blocked", REASON_TRANSPORT_ERROR, "codex")[1],
            "inspect handoff failure and decide the next step",
        )
        self.assertNotEqual(
            _outcome_narrative("blocked", REASON_TRANSPORT_ERROR, "queue-enter", "codex"),
            "Handoff did not deliver; see structured outcome for details.",
        )

    def test_receiver_contract_names_the_receiver(self):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            _receiver_contract_line,
        )

        line = _receiver_contract_line("blocked", REASON_TRANSPORT_ERROR, "codex")
        self.assertIsNotNone(line)
        self.assertIn("codex", line)


if __name__ == "__main__":  # pragma: no cover - manual runner parity
    unittest.main()
