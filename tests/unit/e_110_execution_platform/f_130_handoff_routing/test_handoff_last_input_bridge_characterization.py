"""Family 4 / DeliveryOutcome -> last_input bridge characterization wave 1.

Pins the currently-landed bridge semantics from
``mozyo_bridge.domain.handoff.project_last_input`` (and the equivalent
:meth:`DeliveryOutcome.to_last_input_projection` method) into the
``last_input`` block defined by

    mozyo_bridge_pty/vibes/docs/specs/receiver-state-inspector-contract.md

The "Receiver Inspector and Existing DeliveryOutcome" section of that
spec freezes the mapping table:

- ``sent`` / ``ok``         -> projection (submitted_at, ack_status=submitted)
- ``sent`` / ``queue_enter`` -> same projection as ``sent`` / ``ok`` (per
                              the inspector contract's derive rule; the
                              wording differentiator lives in the
                              durable record narrative, not in the
                              projection)
- ``pending_input`` / ``ok`` -> projection (submitted_at=None,
                              ack_status=unobserved)
- ``blocked`` / *           -> no projection (return None) — ACK terminal
                              states are not receiver-runtime facts and
                              are explicitly forbidden from translating
                              into runtime state.

Existing tests in ``test_mozyo_bridge.py`` cover the basic mapping rows;
this file consolidates the **characterization safety net** the Family 4
plan calls out:

- The full no-fabrication matrix across every blocked reason × every
  caller-supplied field combination.
- Timestamp / source-of-truth consistency: ``submitted_at`` is a verbatim
  pass-through of the caller-supplied value; the helper has no clock,
  no wall-clock fallback, and never authors ``acknowledged_at`` itself.
- Mode independence within ``sent``: the projection only derives from
  ``status`` and ``reason``; ``mode`` (standard / queue-enter / pending)
  must not change which row of the mapping table is selected.
- Field pass-through: ``input_kind`` / ``prompt_turn_id`` / ``input_id``
  flow through verbatim and are not inferred from outcome state when
  the caller omits them.

Boundaries (per the task description and characterization plan):

- This wave introduces **no new bridge semantics**. Only test code is
  added; production code in ``mozyo_bridge/src/`` is not touched.
- ``acknowledged`` must never appear on the tmux path; this file pins
  the enum-level invariant that the helper outputs only ``submitted``
  and ``unobserved``.
- No runtime completion logic. No assistant-turn completion. No
  reinterpretation of queue-enter / strict ``sent``.
- Cleanup of pre-existing redundant tests is out of scope.
"""

from __future__ import annotations

import json
import sys
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields
from itertools import product
from pathlib import Path
from typing import Any, Literal, Optional, Union, get_args, get_origin, get_type_hints

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.handoff import (
    AckStatus,
    DeliveryOutcome,
    LastInputProjection,
    MODE_PENDING,
    MODE_QUEUE_ENTER,
    MODE_STANDARD,
    make_outcome,
    normalize_anchor,
    project_last_input,
)


def _asana_anchor():
    return normalize_anchor("asana", task_id="T_fam4", comment_id="C_fam4")


def _outcome(
    *,
    status,
    reason,
    receiver: str = "claude",
    mode: Optional[str] = MODE_STANDARD,
    target: Optional[str] = "%2",
    notification_marker: Optional[str] = "[marker]",
    anchor=None,
    source: Optional[str] = None,
) -> DeliveryOutcome:
    """Build a DeliveryOutcome shaped like a real sender-side handoff outcome.

    ``anchor`` defaults to a valid Asana anchor; pass ``None`` to mirror
    early-rejection paths (``invalid_anchor`` / ``invalid_args``) that
    fail before resolving the receiver pane.
    """
    return make_outcome(
        status=status,
        reason=reason,
        receiver=receiver,
        target=target,
        anchor=anchor if anchor is not None else _asana_anchor(),
        mode=mode,
        kind="implementation_request",
        notification_marker=notification_marker,
        source=source,
    )


# ---------------------------------------------------------------------------
# Mapping table characterization (status × reason matrix).
#
# Pin the complete spec-defined mapping in one place. Each row asserts
# the projection's presence/absence, ack_status, submitted_at semantics,
# and acknowledged_at remaining ``None`` on every row.
# ---------------------------------------------------------------------------


class BridgeMappingMatrixCharacterizationTest(unittest.TestCase):
    """Each (status, reason) pair the helper observes lands on exactly one
    row of the spec's mapping table; pin them line-by-line."""

    SUBMITTED_AT = "2026-05-14T03:30:00Z"

    def test_sent_ok_yields_submitted_projection_with_caller_timestamp(self) -> None:
        outcome = _outcome(status="sent", reason="ok")

        projection = project_last_input(
            outcome,
            submitted_at=self.SUBMITTED_AT,
            input_kind="prompt",
            prompt_turn_id="turn-fam4-sent-ok",
            input_id="in-fam4-sent-ok",
        )

        self.assertEqual(
            projection,
            LastInputProjection(
                submitted_at=self.SUBMITTED_AT,
                acknowledged_at=None,
                ack_status="submitted",
                input_kind="prompt",
                prompt_turn_id="turn-fam4-sent-ok",
                input_id="in-fam4-sent-ok",
            ),
        )

    def test_sent_queue_enter_yields_same_submitted_projection_as_sent_ok(
        self,
    ) -> None:
        # The inspector contract's derive rule: ack_status is derived from
        # submitted_at / acknowledged_at. queue-enter is a wording-layer
        # differentiator on the sender side; the projection cannot
        # distinguish it from strict sent/ok because both produce a
        # tmux-compat-shaped "submitted, no ack" row.
        sent_ok = project_last_input(
            _outcome(status="sent", reason="ok"),
            submitted_at=self.SUBMITTED_AT,
            input_kind="prompt",
            prompt_turn_id="turn-fam4",
        )
        sent_queue = project_last_input(
            _outcome(
                status="sent", reason="queue_enter", mode=MODE_QUEUE_ENTER
            ),
            submitted_at=self.SUBMITTED_AT,
            input_kind="prompt",
            prompt_turn_id="turn-fam4",
        )

        self.assertEqual(sent_ok, sent_queue)
        assert sent_queue is not None
        self.assertEqual("submitted", sent_queue.ack_status)
        self.assertEqual(self.SUBMITTED_AT, sent_queue.submitted_at)
        self.assertIsNone(sent_queue.acknowledged_at)

    def test_pending_input_ok_yields_unobserved_projection_without_submitted_at(
        self,
    ) -> None:
        # pending_input/ok means the input was typed but Enter was not
        # pressed. The receiver runtime has not received the turn, so
        # submitted_at MUST stay None even when the caller passes one.
        outcome = _outcome(status="pending_input", reason="ok", mode=MODE_PENDING)

        projection = project_last_input(
            outcome,
            submitted_at=self.SUBMITTED_AT,
            input_kind="prompt",
            prompt_turn_id="turn-fam4-pending",
            input_id="in-fam4-pending",
        )

        self.assertEqual(
            projection,
            LastInputProjection(
                submitted_at=None,
                acknowledged_at=None,
                ack_status="unobserved",
                input_kind="prompt",
                prompt_turn_id="turn-fam4-pending",
                input_id="in-fam4-pending",
            ),
        )

    def test_blocked_reasons_all_yield_no_projection(self) -> None:
        # Pin the complete no-fabrication matrix for the five blocked
        # reasons the upstream ACK contract defines. Each row independently
        # must refuse to fabricate a runtime-side last_input record.
        blocked_reasons = (
            "marker_timeout",
            "target_unavailable",
            "target_not_agent",
            "invalid_anchor",
            "invalid_args",
        )
        for reason in blocked_reasons:
            with self.subTest(reason=reason):
                outcome = _outcome(
                    status="blocked",
                    reason=reason,
                    # invalid_anchor / invalid_args fail before resolving
                    # the receiver pane; anchor is None on those rows.
                    anchor=None if reason in ("invalid_anchor", "invalid_args") else _asana_anchor(),
                    target=None if reason in ("invalid_anchor", "invalid_args") else "%2",
                    notification_marker=None if reason in ("invalid_anchor", "invalid_args") else "[m]",
                    source="asana" if reason in ("invalid_anchor", "invalid_args") else None,
                )
                self.assertIsNone(project_last_input(outcome))


# ---------------------------------------------------------------------------
# No-fabrication safety invariant: no caller-supplied parameter can
# rescue a ``blocked`` outcome into a projection. The mapping is a
# strict status / reason gate; the input pipeline does not influence it.
# ---------------------------------------------------------------------------


class BlockedNoFabricationSafetyInvariantTest(unittest.TestCase):
    """Every combination of caller-supplied submitted_at / input_kind /
    prompt_turn_id / input_id must still return ``None`` for a blocked
    outcome.

    This pins the "ACK terminal state is not a runtime fact" boundary
    against the easy refactor mistake of "if caller passed a timestamp,
    project it anyway."
    """

    BLOCKED_REASONS = (
        ("marker_timeout", True),
        ("target_unavailable", True),
        ("target_not_agent", True),
        ("invalid_anchor", False),
        ("invalid_args", False),
    )

    SUBMITTED_AT_VALUES = (None, "2026-05-14T03:31:00Z")
    INPUT_KIND_VALUES = (None, "prompt", "raw")
    TURN_ID_VALUES = (None, "turn-x")
    INPUT_ID_VALUES = (None, "in-x")

    def test_caller_supplied_fields_cannot_rescue_blocked_projection(self) -> None:
        for reason, has_anchor in self.BLOCKED_REASONS:
            outcome = _outcome(
                status="blocked",
                reason=reason,
                anchor=_asana_anchor() if has_anchor else None,
                target="%2" if has_anchor else None,
                notification_marker="[m]" if has_anchor else None,
                source=None if has_anchor else "asana",
            )
            combinations = product(
                self.SUBMITTED_AT_VALUES,
                self.INPUT_KIND_VALUES,
                self.TURN_ID_VALUES,
                self.INPUT_ID_VALUES,
            )
            for submitted_at, input_kind, prompt_turn_id, input_id in combinations:
                with self.subTest(
                    reason=reason,
                    submitted_at=submitted_at,
                    input_kind=input_kind,
                    prompt_turn_id=prompt_turn_id,
                    input_id=input_id,
                ):
                    self.assertIsNone(
                        project_last_input(
                            outcome,
                            submitted_at=submitted_at,
                            input_kind=input_kind,
                            prompt_turn_id=prompt_turn_id,
                            input_id=input_id,
                        )
                    )

    def test_to_last_input_projection_method_matches_helper_for_blocked(
        self,
    ) -> None:
        # The bound-method facade must match the free-function helper
        # for every blocked row; otherwise a caller switching between
        # the two could observe different projection semantics for the
        # same outcome.
        for reason, has_anchor in self.BLOCKED_REASONS:
            with self.subTest(reason=reason):
                outcome = _outcome(
                    status="blocked",
                    reason=reason,
                    anchor=_asana_anchor() if has_anchor else None,
                    target="%2" if has_anchor else None,
                    notification_marker="[m]" if has_anchor else None,
                    source=None if has_anchor else "asana",
                )
                self.assertEqual(
                    project_last_input(outcome, submitted_at="2026-05-14T03:31:00Z"),
                    outcome.to_last_input_projection(submitted_at="2026-05-14T03:31:00Z"),
                )


# ---------------------------------------------------------------------------
# Timestamp / source-of-truth consistency.
#
# Pin the contract that ``submitted_at`` is a verbatim pass-through of
# the caller-supplied value (no normalization, no clock fallback), and
# that ``acknowledged_at`` is *never* authored by this helper on any
# path — the runtime ACK signal is the sole source of truth for that
# field, and the tmux compat path cannot observe it.
# ---------------------------------------------------------------------------


class TimestampSourceOfTruthCharacterizationTest(unittest.TestCase):
    def test_submitted_at_is_verbatim_passthrough_on_sent_ok(self) -> None:
        # Several iso8601 strings (different offsets / fractional seconds)
        # must all survive the helper unchanged; the bridge does not
        # normalize timezones or precision.
        timestamps = (
            "2026-05-14T03:30:00Z",
            "2026-05-14T03:30:00.123456Z",
            "2026-05-14T03:30:00+09:00",
            "1970-01-01T00:00:00Z",
        )
        for ts in timestamps:
            with self.subTest(timestamp=ts):
                projection = project_last_input(
                    _outcome(status="sent", reason="ok"), submitted_at=ts
                )
                assert projection is not None
                self.assertEqual(ts, projection.submitted_at)
                self.assertIsNone(projection.acknowledged_at)
                self.assertEqual("submitted", projection.ack_status)

    def test_helper_has_no_wall_clock_dependency_on_sent_ok(self) -> None:
        # Calling twice with identical inputs must yield equal outputs;
        # if the helper read a clock, the two projections would differ on
        # any timing-sensitive field.
        outcome = _outcome(status="sent", reason="ok")
        a = project_last_input(outcome, submitted_at="2026-05-14T03:31:01Z")
        b = project_last_input(outcome, submitted_at="2026-05-14T03:31:01Z")
        self.assertEqual(a, b)

    def test_helper_does_not_invent_submitted_at_when_caller_omits_it(
        self,
    ) -> None:
        # sent/ok without a caller-supplied submitted_at returns a
        # projection that carries ``None`` for the timestamp. The helper
        # does not fabricate one from any source.
        projection = project_last_input(_outcome(status="sent", reason="ok"))
        assert projection is not None
        self.assertIsNone(projection.submitted_at)
        # ack_status remains "submitted" — the spec's derive rule
        # documents that the bridge defers to the caller for the
        # timestamp, not that it downgrades the ack_status when the
        # timestamp is absent.
        self.assertEqual("submitted", projection.ack_status)

    def test_pending_input_drops_caller_submitted_at(self) -> None:
        # Even when the caller passes a submitted_at, the pending_input
        # row of the mapping table forces submitted_at to None and
        # ack_status to "unobserved". The receiver runtime has not seen
        # the turn yet, so claiming a submitted timestamp would be a
        # source-of-truth lie.
        projection = project_last_input(
            _outcome(status="pending_input", reason="ok", mode=MODE_PENDING),
            submitted_at="2026-05-14T03:32:00Z",
        )
        assert projection is not None
        self.assertIsNone(projection.submitted_at)
        self.assertEqual("unobserved", projection.ack_status)

    def test_acknowledged_at_is_never_authored_by_this_helper(self) -> None:
        # Across every (status, reason, mode) row + every caller field
        # combination that produces a non-None projection, the
        # acknowledged_at field must remain None. The PTY-side
        # ``runtime.input.ack`` channel is the sole source of truth for
        # that field; the bridge helper cannot synthesize it.
        passing_rows = (
            ("sent", "ok", MODE_STANDARD),
            ("sent", "ok", MODE_QUEUE_ENTER),
            ("sent", "queue_enter", MODE_QUEUE_ENTER),
            ("pending_input", "ok", MODE_PENDING),
        )
        for status, reason, mode in passing_rows:
            for ts in (None, "2026-05-14T03:33:00Z"):
                with self.subTest(status=status, reason=reason, mode=mode, ts=ts):
                    projection = project_last_input(
                        _outcome(status=status, reason=reason, mode=mode),
                        submitted_at=ts,
                        input_kind="prompt",
                        prompt_turn_id="t",
                        input_id="i",
                    )
                    self.assertIsNotNone(projection)
                    assert projection is not None
                    self.assertIsNone(
                        projection.acknowledged_at,
                        f"helper authored acknowledged_at for {status}/{reason}/{mode}",
                    )

    def test_ack_status_enum_output_excludes_acknowledged(self) -> None:
        # Iterate every defined status × reason × mode (filtered to
        # combinations make_outcome accepts) and pin the set of
        # ack_status values the helper ever returns. The runtime
        # signal-bound "acknowledged" value MUST NOT appear here; it
        # only ever lands on the PTY-side ``apply_sidecar_input_ack``
        # path of the inspector projection store.
        observed_ack_status: set[str] = set()
        passing_rows = (
            ("sent", "ok"),
            ("sent", "queue_enter"),
            ("pending_input", "ok"),
        )
        for status, reason in passing_rows:
            mode = MODE_PENDING if status == "pending_input" else MODE_STANDARD
            projection = project_last_input(
                _outcome(status=status, reason=reason, mode=mode),
                submitted_at="2026-05-14T03:34:00Z",
            )
            assert projection is not None
            observed_ack_status.add(projection.ack_status)

        self.assertEqual(observed_ack_status, {"submitted", "unobserved"})
        self.assertNotIn("acknowledged", observed_ack_status)


# ---------------------------------------------------------------------------
# Field pass-through invariant.
#
# Pin that input_kind / prompt_turn_id / input_id flow through verbatim
# on the rows that produce a projection, and that the helper never
# infers them from the outcome when the caller passes ``None``.
# ---------------------------------------------------------------------------


class FieldPassthroughCharacterizationTest(unittest.TestCase):
    def test_input_kind_prompt_turn_id_input_id_pass_through_on_sent_ok(
        self,
    ) -> None:
        projection = project_last_input(
            _outcome(status="sent", reason="ok"),
            submitted_at="2026-05-14T03:35:00Z",
            input_kind="raw",
            prompt_turn_id="turn-fam4-raw",
            input_id="in-fam4-raw",
        )
        assert projection is not None
        self.assertEqual("raw", projection.input_kind)
        self.assertEqual("turn-fam4-raw", projection.prompt_turn_id)
        self.assertEqual("in-fam4-raw", projection.input_id)

    def test_input_kind_prompt_turn_id_input_id_pass_through_on_pending_input(
        self,
    ) -> None:
        projection = project_last_input(
            _outcome(status="pending_input", reason="ok", mode=MODE_PENDING),
            input_kind="prompt",
            prompt_turn_id="turn-fam4-pending",
            input_id="in-fam4-pending",
        )
        assert projection is not None
        self.assertEqual("prompt", projection.input_kind)
        self.assertEqual("turn-fam4-pending", projection.prompt_turn_id)
        self.assertEqual("in-fam4-pending", projection.input_id)

    def test_omitted_passthrough_fields_stay_none(self) -> None:
        # When the caller omits the passthrough fields, the projection
        # MUST carry None for each — the helper does not infer them
        # from outcome state (e.g., it must not read outcome.kind or
        # outcome.notification_marker as a fallback input_id).
        projection = project_last_input(
            _outcome(status="sent", reason="ok"),
            submitted_at="2026-05-14T03:36:00Z",
        )
        assert projection is not None
        self.assertIsNone(projection.input_kind)
        self.assertIsNone(projection.prompt_turn_id)
        self.assertIsNone(projection.input_id)

    def test_passthrough_fields_do_not_leak_between_calls(self) -> None:
        # Two consecutive calls with different passthrough values must
        # not contaminate each other — the helper holds no mutable
        # state about the previous projection.
        outcome = _outcome(status="sent", reason="ok")
        first = project_last_input(
            outcome,
            submitted_at="2026-05-14T03:37:00Z",
            input_kind="prompt",
            prompt_turn_id="first",
            input_id="in-first",
        )
        second = project_last_input(
            outcome,
            submitted_at="2026-05-14T03:37:01Z",
            input_kind="raw",
            prompt_turn_id="second",
            input_id="in-second",
        )
        assert first is not None and second is not None
        self.assertEqual("first", first.prompt_turn_id)
        self.assertEqual("second", second.prompt_turn_id)
        self.assertEqual("in-first", first.input_id)
        self.assertEqual("in-second", second.input_id)


# ---------------------------------------------------------------------------
# Mode independence within ``sent``.
#
# The projection table is keyed only on (status, reason). ``mode`` is a
# sender-side wording axis; it must not flip the projection's row.
# ---------------------------------------------------------------------------


class ModeIndependenceCharacterizationTest(unittest.TestCase):
    def test_sent_ok_projection_invariant_across_modes(self) -> None:
        # standard, queue-enter, and pending modes all map sent/ok to
        # the same projection shape. (`pending` mode plus status=sent is
        # not produced by the live primitive today, but the projection
        # helper is mode-blind by contract; pinning that here prevents a
        # silent regression where a future refactor reads outcome.mode.)
        results = []
        for mode in (MODE_STANDARD, MODE_QUEUE_ENTER, MODE_PENDING):
            results.append(
                project_last_input(
                    _outcome(status="sent", reason="ok", mode=mode),
                    submitted_at="2026-05-14T03:38:00Z",
                    input_kind="prompt",
                    prompt_turn_id="turn-mode",
                    input_id="in-mode",
                )
            )
        baseline = results[0]
        for other in results[1:]:
            self.assertEqual(baseline, other)

    def test_blocked_marker_timeout_returns_none_regardless_of_mode(self) -> None:
        for mode in (MODE_STANDARD, MODE_QUEUE_ENTER, MODE_PENDING):
            with self.subTest(mode=mode):
                self.assertIsNone(
                    project_last_input(
                        _outcome(status="blocked", reason="marker_timeout", mode=mode),
                        submitted_at="2026-05-14T03:38:01Z",
                    )
                )


# ---------------------------------------------------------------------------
# Conservation invariants.
#
# Pin that the helper does not mutate the outcome and that the
# projection's serialized shape is stable / matches the spec exactly.
# ---------------------------------------------------------------------------


class ProjectionConservationCharacterizationTest(unittest.TestCase):
    EXPECTED_FIELD_NAMES = frozenset(
        {
            "submitted_at",
            "acknowledged_at",
            "ack_status",
            "input_kind",
            "prompt_turn_id",
            "input_id",
        }
    )

    def test_last_input_projection_field_set_matches_spec_exactly(self) -> None:
        # The inspector contract's ``last_input`` block names these six
        # fields. The projection dataclass must mirror that set so the
        # consumer's snapshot serialization stays stable.
        actual = {f.name for f in fields(LastInputProjection)}
        self.assertEqual(actual, self.EXPECTED_FIELD_NAMES)

    def test_to_dict_field_set_matches_spec_exactly(self) -> None:
        projection = project_last_input(
            _outcome(status="sent", reason="ok"),
            submitted_at="2026-05-14T03:39:00Z",
        )
        assert projection is not None
        self.assertEqual(set(projection.to_dict().keys()), self.EXPECTED_FIELD_NAMES)

    def test_projection_to_dict_round_trips_through_json(self) -> None:
        projection = project_last_input(
            _outcome(status="sent", reason="ok"),
            submitted_at="2026-05-14T03:39:01Z",
            input_kind="prompt",
            prompt_turn_id="t",
            input_id="i",
        )
        assert projection is not None
        encoded = json.dumps(projection.to_dict(), sort_keys=True)
        self.assertEqual(json.loads(encoded), projection.to_dict())

    def test_projection_is_frozen_dataclass(self) -> None:
        # Inspector consumers may store projections in a snapshot
        # cache; pinning frozenness prevents an accidental in-place
        # mutation of a cached row.
        projection = project_last_input(
            _outcome(status="sent", reason="ok"),
            submitted_at="2026-05-14T03:39:02Z",
        )
        assert projection is not None
        with self.assertRaises(FrozenInstanceError):
            projection.submitted_at = "tampered"  # type: ignore[misc]

    def test_helper_does_not_mutate_input_outcome(self) -> None:
        # DeliveryOutcome is a frozen dataclass; even so, pin that the
        # helper returns an outcome semantically untouched (same dict
        # representation before and after) to detect a future refactor
        # that switches to a mutable container.
        outcome = _outcome(status="sent", reason="ok")
        before = deepcopy(outcome.to_dict())
        project_last_input(outcome, submitted_at="2026-05-14T03:39:03Z")
        after = outcome.to_dict()
        self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# Inspector-contract-derived enum invariants.
#
# Pin the AckStatus Literal's enum membership matches the spec exactly
# so a silent enum extension (e.g., adding a new ack_status variant
# without updating the projection helper) is caught at the type-system
# level rather than via a hand-written value check elsewhere.
# ---------------------------------------------------------------------------


def _literal_values(annotation: Any) -> Optional[tuple[Any, ...]]:
    """Return the values of a ``Literal[...]`` annotation (or an
    ``Optional[Literal]``), peeling one Union layer.
    """
    origin = get_origin(annotation)
    if origin is Union:
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return _literal_values(non_none[0])
        return None
    if origin is Literal:
        args = get_args(annotation)
        if args and all(isinstance(v, str) for v in args):
            return tuple(args)
    return None


class AckStatusEnumCharacterizationTest(unittest.TestCase):
    def test_ack_status_literal_vocabulary_matches_inspector_contract(
        self,
    ) -> None:
        # The contract's last_input block defines exactly three values
        # for ack_status. Pin them as a frozen set so adding or
        # removing one elsewhere will trip this test.
        self.assertEqual(
            set(get_args(AckStatus)),
            {"submitted", "acknowledged", "unobserved"},
        )

    def test_projection_ack_status_field_uses_the_ack_status_literal(
        self,
    ) -> None:
        hints = get_type_hints(LastInputProjection)
        values = _literal_values(hints["ack_status"])
        self.assertIsNotNone(values)
        assert values is not None
        self.assertEqual(set(values), {"submitted", "acknowledged", "unobserved"})


if __name__ == "__main__":
    unittest.main()
