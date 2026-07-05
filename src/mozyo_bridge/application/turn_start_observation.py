"""Standard-rail turn-start observation (Redmine #13166 / #13262).

The strict ``--mode standard`` rail historically returned ``sent`` / ``ok`` as
soon as it had (a) observed the landing marker in the receiver pane and (b)
issued a single ``Enter`` keypress. That judgment proves the sender *pressed*
Enter; it does NOT prove the receiver TUI actually *submitted* the prompt and
started a turn. Redmine #13166 recorded three consecutive codex sends that each
reported ``sent`` / ``ok`` while the codex TUI never started a turn — the Enter
was absorbed by the receiver's busy / redraw state and the marker+body stayed in
the composer, unsubmitted. The notification was silently lost.

This module adds a bounded, read-only *turn-start observation* to the
``--mode standard`` rail. #13166 first adopted it for the codex receiver only
(adopted scope: candidate 1); Redmine #13262 generalizes the SAME observation to
the claude receiver's standard rail, since claude's queue-oriented TUI can absorb
an Enter the same way (the observation itself has always been receiver-agnostic —
it keys on the receiver pane advancing, not on any codex-specific signal). After
the marker is observed and Enter is issued, the rail snapshots the receiver pane
and polls it for **new output activity** — the positive signal that the
submitted prompt cleared the composer and the receiver began rendering a turn.
When that activity is observed the send resolves to ``sent`` / ``ok`` exactly as
before; when it is not observed within the window the send fails closed to
``blocked`` / ``turn_start_unconfirmed`` (a new reason in the existing
``marker_timeout``-style vocabulary) and rides the existing blocked-path
fallback. No new transport or raw ``send-keys`` recovery path is added, and no
prompt is auto-resent — the marker+body is typed exactly once. The queue-enter
rail is deliberately NOT covered: its marker-unobserved path stays
``sent`` / ``queue_enter`` (Redmine #13262 auditor boundary; a queue-enter
contract change would need its own design record).

Signal choice (new output activity, not composer-clear-via-marker-absence): the
receiver-state doctrine (``vibes/docs/logics/ack-completion-receiver-state.md``)
and the C-u-rollback observation caveat both warn that *absence* of the marker
from a tmux capture does not prove the composer cleared — a submitted marker
persists in the receiver transcript as the sent user message, so marker-absence is
neither a reliable submit signal here (it is present on success) nor a safe
negative. This module therefore keys on a *presence* signal instead: the receiver
pane advancing past the pre-Enter snapshot. This is a delivery-ACK-layer
(``submitted`` vs not-submitted) hardening on the tmux compat rail, not a
completion detector; the durable-ledger design (Redmine #13166 candidate 2,
deferred) remains the complete fix.

This module performs NO direct I/O: the pane capture and the sleep are injected
callables, so the observation is exercisable with plain fakes and never touches a
real tmux. The pure classifier and the pure record-line renderer carry no
absolute paths, so the telemetry stays redaction-safe for the pasteable durable
record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Tuple

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_rail import (
    OUTCOME_ABSENT,
    OUTCOME_BLOCKED,
    OUTCOME_DELIVERED_NOT_STARTED,
    OUTCOME_INJECT_FAILED,
    OUTCOME_PRECONDITION_NOT_IDLE,
    OUTCOME_STARTED,
    TurnStartResult,
)

# The poll interval for the turn-start observation. The observation *window* is
# supplied by the caller and is the marker gate's own ``landing_timeout`` (default
# 8.0s), so the observation stays aligned with the existing rail timeout
# convention rather than introducing a second, unrelated deadline. Only the
# sub-window poll cadence is new; it is deliberately finer than the queue-enter
# Enter-only retry interval (2.0s) because a turn typically starts sub-second and
# the observation is read-only (a capture, never a keypress).
TURN_START_OBSERVE_INTERVAL_SECONDS = 0.5


@dataclass(frozen=True)
class TurnStartObservation:
    """The result of the codex standard-rail turn-start observation.

    ``confirmed`` is the only value that gates the transport outcome; ``polls`` /
    ``window_seconds`` / ``interval_seconds`` are record-layer telemetry
    (numbers only, redaction-safe) surfaced in the durable delivery record so an
    auditor can replay how long the rail waited before it confirmed or failed
    closed.
    """

    confirmed: bool
    polls: int
    window_seconds: float
    interval_seconds: float


def submit_activity_observed(baseline_capture: str, post_capture: str) -> bool:
    """True when the receiver pane advanced past the pre-Enter snapshot.

    The pre-Enter ``baseline_capture`` holds the marker+body sitting in the
    composer (the landing marker was observed, so it is present). A successful
    submit clears the composer and the receiver begins rendering its turn, which
    changes the captured text; an Enter absorbed by a busy / redrawing TUI leaves
    the composer — and therefore the capture — unchanged. Comparison is on the
    per-line right-stripped text so trailing-whitespace churn from a redraw is not
    mistaken for activity.
    """
    return _normalize(post_capture) != _normalize(baseline_capture)


def observe_standard_turn_start(
    target: str,
    *,
    baseline_capture: str,
    capture: Callable[[str, int], str],
    sleep: Callable[[float], None],
    window_seconds: float,
    lines: int,
    interval_seconds: float = TURN_START_OBSERVE_INTERVAL_SECONDS,
) -> TurnStartObservation:
    """Poll the receiver pane for turn-start activity after Enter (read-only).

    ``capture`` / ``sleep`` are injected so this never touches real tmux. The
    observation polls at ``interval_seconds`` until ``window_seconds`` elapses or
    activity is observed. A non-positive ``window_seconds`` disables the
    observation and returns ``confirmed=True`` with zero polls — the same posture
    as the marker gate when its wait is turned off — so an operator who has
    explicitly set ``--landing-timeout 0`` is not surprised by a hard block. A
    non-positive ``interval_seconds`` falls back to the module default.
    """
    if window_seconds <= 0:
        return TurnStartObservation(
            confirmed=True,
            polls=0,
            window_seconds=window_seconds,
            interval_seconds=interval_seconds,
        )
    if interval_seconds <= 0:
        interval_seconds = TURN_START_OBSERVE_INTERVAL_SECONDS
    polls = 0
    elapsed = 0.0
    confirmed = False
    while elapsed < window_seconds:
        sleep(interval_seconds)
        elapsed += interval_seconds
        polls += 1
        if submit_activity_observed(baseline_capture, capture(target, lines)):
            confirmed = True
            break
    return TurnStartObservation(
        confirmed=confirmed,
        polls=polls,
        window_seconds=window_seconds,
        interval_seconds=interval_seconds,
    )


def resolve_turn_start_window(
    raw_landing_timeout: object, coerced_window: float
) -> float:
    """The observation window from the raw ``--landing-timeout`` arg (j#71985).

    The orchestrator's legacy marker-gate coercion (``float(raw or 8.0)``) swallows
    an explicit ``0``, so the observation window is derived here from the raw arg
    instead: an unset arg (``None``) keeps the caller's coerced default, while an
    explicit non-positive value returns ``0.0`` — which
    :func:`observe_codex_turn_start` documents as "observation disabled". The
    marker gate's own coercion is deliberately left untouched (pre-#13166
    semantics for every rail).
    """
    if raw_landing_timeout is None:
        return coerced_window
    return 0.0 if float(raw_landing_timeout) <= 0 else coerced_window  # type: ignore[arg-type]


def turn_start_record_lines(
    observation: TurnStartObservation,
    *,
    rail_label: str = "codex standard-rail",
) -> List[str]:
    """Render the additive ``- Turn start:`` durable-record telemetry (pure).

    Follows the #12580 / #12581 retry-telemetry precedent: numbers + a verdict
    only, no free text and no absolute paths, so it is safe in the pasteable
    delivery record and the opt-in persisted note. It documents the turn-start
    observation the rail already performed and never overrides ``next_action``;
    the structured ``(status, reason)`` wire is owned by the outcome.

    ``rail_label`` names the rail in the record line. It defaults to
    ``"codex standard-rail"`` so the codex standard-rail record stays byte-identical
    to the #13166 wording; the caller passes ``"<receiver> standard-rail"`` so the
    Redmine #13262 claude generalization renders an accurate ``"claude standard-rail"``
    label instead of mislabelling a claude send as codex.
    """
    verdict = "confirmed" if observation.confirmed else "unconfirmed"
    detail = (
        "receiver pane advanced after Enter (new output activity observed)"
        if observation.confirmed
        else "no new receiver output activity observed after Enter within the window"
    )
    return [
        (
            f"- Turn start: {rail_label} submit observation "
            f"(window {observation.window_seconds:g}s / interval "
            f"{observation.interval_seconds:g}s, {observation.polls} poll(s)) — "
            f"turn start {verdict}; {detail}. The marker+body was typed once and "
            "never re-injected; no Enter re-issue and no auto-resend."
        )
    ]


def _normalize(text: str) -> str:
    return "\n".join(line.rstrip() for line in (text or "").splitlines())


#: The projection of the herdr event-driven turn-start rail's closed outcome
#: vocabulary (:data:`...domain.turn_start_rail.TURN_START_OUTCOMES`) onto the
#: handoff ``(Status, Reason)`` wire (Redmine #13255). Keeps ``Status`` at the
#: existing 3 tokens; only ``delivered_not_started`` reuses an existing reason
#: (``turn_start_unconfirmed``) — the structured ``turn_start_outcome`` telemetry
#: carried on the ``DeliveryOutcome`` (built by
#: ``TurnStartResult.to_telemetry_dict`` and also rendered human-readably by
#: ``turn_start_rail_record_lines``) disambiguates it from the capture-based
#: standard rail, both for a replaying auditor and for the delivery-record wording
#: (Redmine #13255 j#72695). The other four map to additive reason tokens.
HERDR_TURN_START_PROJECTION: dict = {
    OUTCOME_STARTED: ("sent", "ok"),
    OUTCOME_DELIVERED_NOT_STARTED: ("blocked", "turn_start_unconfirmed"),
    OUTCOME_BLOCKED: ("blocked", "receiver_blocked"),
    OUTCOME_ABSENT: ("blocked", "turn_start_absent"),
    OUTCOME_PRECONDITION_NOT_IDLE: ("blocked", "precondition_not_idle"),
    OUTCOME_INJECT_FAILED: ("blocked", "inject_failed"),
}


def project_herdr_turn_start(result: TurnStartResult) -> Tuple[str, str]:
    """Map a herdr :class:`TurnStartResult` onto the handoff ``(status, reason)`` wire.

    Pure. ``result.outcome`` is a closed vocabulary (validated by
    :class:`TurnStartResult`), so the lookup is total; a novel outcome would have
    already been rejected at rail-result construction. The additive
    ``outcome`` / ``snapshot_state`` / ``wait_kind`` / ``enter_resends``
    / ``reclassified_blocked`` telemetry is carried separately as the structured
    ``DeliveryOutcome.turn_start_outcome`` field (via
    ``TurnStartResult.to_telemetry_dict``) and rendered human-readably by
    ``turn_start_rail_record_lines`` so an auditor can replay the rail even when two
    rail outcomes share one wire reason.
    """
    return HERDR_TURN_START_PROJECTION[result.outcome]


__all__ = [
    "HERDR_TURN_START_PROJECTION",
    "TURN_START_OBSERVE_INTERVAL_SECONDS",
    "TurnStartObservation",
    "observe_standard_turn_start",
    "project_herdr_turn_start",
    "resolve_turn_start_window",
    "submit_activity_observed",
    "turn_start_record_lines",
]
