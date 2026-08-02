"""Typed containment for a raised transport primitive on the handoff rails (Redmine #14232).

The #14232 defect, from the issue's live evidence: under ``terminal_transport.backend: herdr``
the tmux-shaped shim (``transport_binding._HerdrTmuxShim``) raises ``TransportBindingError``
when a mapped primitive fails — ``herdr send_keys(enter) failed (reason=transport_error): herdr
command timed out``. The common tmux transport rail (the ``queue-enter`` default and
``pending``) drove ``inject_body`` / ``wait_for_marker`` / ``capture`` / ``press_enter`` /
``rollback`` with **no guard**, so that exception propagated out of ``orchestrate_handoff`` and
the CLI exited 1 with a stack trace and no structured ``status`` / ``reason`` /
``next_action``. The sender was then unable to classify the delivery at all — the state the
issue recorded as "senderはbody/Enterの部分到達を否定できず、zero-send/deliveredのどちらにも分類
不能". (Only the herdr *event-driven* ``--mode standard`` rail already closed to a typed outcome,
which is why the improvement recorded in j#84870 did not cover the daily-default rail.)

This module is the containment. It lives beside the rail rather than inside it because
``handoff_tmux_transport_rail.py`` is under the module-health line-count gate: the gate's prose
and outcome assembly belong somewhere, and a sibling is the established answer (the
``gateway_route_wording`` / ``startup_admission_gate`` precedent) rather than a self-approved
baseline bump.

Two properties the containment must hold, both deliberate:

- **Classification is post-injection.** The rail only reaches a transport primitive at or after
  its single body injection, so every failure contained here is ``uncertain_partial`` under the
  shared authority (``domain/injection_stage.py``): partial reach of the body and/or Enter
  cannot be excluded. Pre-injection transport failures are already contained elsewhere and
  already classify as ``not_sent`` — the pre-send startup-admission read fails closed to
  ``target_unavailable`` (#13760), and the target/admission gates never touch the transport.
- **No rollback and no re-send.** Consistent with the rail's existing no-blind-retry boundary
  and the issue's Non-goals, an unknown partial delivery is never "repaired": the marker+body
  was typed at most once and stays that way.

Secret-safe by construction: the caller names the failed primitive with a fixed rail-owned
token, and the adapter's own exception message is **never** read or folded into the outcome or
the ``die`` text — it can name a binary path or carry a raw backend status, and a delivery
record is pasteable into a durable journal (#13760 j#77947 invariant 3).
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (
    injection_stage_record_lines,
)

#: The fixed, rail-owned tokens naming which transport primitive was in flight. These are the
#: ONLY strings the failure record may use to identify the failure point — never the adapter's
#: message. Named constants (not inline literals) so the rail and its tests agree on the set.
STEP_SEND_TEXT_BODY = "send_text (body injection)"
STEP_READ_PANE_LANDING_WAIT = "read_pane (landing-marker wait)"
STEP_SEND_KEYS_ROLLBACK = "send_keys(C-u) (composer rollback)"
STEP_READ_PANE_TURN_START_BASELINE = "read_pane (pre-Enter turn-start baseline)"
STEP_SEND_KEYS_ENTER = "send_keys(enter) (submit)"
STEP_READ_PANE_RETRY_PROBE = "read_pane (Enter-only retry marker probe)"
STEP_SEND_KEYS_ENTER_RETRY = "send_keys(enter) (Enter-only retry)"
STEP_READ_PANE_TURN_START_OBSERVE = "read_pane (post-Enter turn-start observation)"

TRANSPORT_STEPS: tuple[str, ...] = (
    STEP_SEND_TEXT_BODY,
    STEP_READ_PANE_LANDING_WAIT,
    STEP_SEND_KEYS_ROLLBACK,
    STEP_READ_PANE_TURN_START_BASELINE,
    STEP_SEND_KEYS_ENTER,
    STEP_READ_PANE_RETRY_PROBE,
    STEP_SEND_KEYS_ENTER_RETRY,
    STEP_READ_PANE_TURN_START_OBSERVE,
)


def transport_failure_die_message(*, primitive: str, target: str, marker: str) -> str:
    """The ``die`` text for a contained transport failure (pure; fixed prose only)."""
    return (
        "handoff transport failed while the send was in flight: the "
        f"{primitive} primitive raised. The delivery is NOT confirmed and partial reach of "
        "the body and/or Enter cannot be excluded, so no C-u rollback and no re-send were "
        "issued (the marker+body was typed at most once). Read the receiver or the durable "
        "anchor to establish whether the turn started before re-issuing; do not "
        f"blind-resend. target={target} marker={marker}"
    )


def close_transport_failure(
    *,
    outcome: Any,
    primitive: str,
    target: str,
    marker: str,
    emit: Callable[..., None],
    die: Callable[[str], None],
    record_format: str,
    record_command: Optional[str],
    duplicate_lane_panes: Optional[List[str]],
    role_profile_contract: Optional[str],
    submit_lines: Optional[List[str]],
) -> None:
    """Emit the typed ``blocked`` / ``transport_error`` terminal, then ``die`` (never returns).

    ``outcome`` is the caller-assembled :class:`...domain.handoff.DeliveryOutcome` (the rail owns
    its own context threading, so this gate does not re-derive it). The additive injection-stage
    record block is appended through the rail's existing ``turn_start_lines`` channel, which is
    the established additive-telemetry channel and never overrides ``next_action``.

    ``die`` raises, so this function does not return; the ``AssertionError`` guards a ``die``
    implementation that failed to raise rather than letting the rail silently continue past a
    transport failure.
    """
    emit(
        outcome,
        record_format=record_format,
        command=record_command,
        duplicate_lane_panes=duplicate_lane_panes or None,
        role_profile_contract=role_profile_contract,
        submit_lines=submit_lines,
        turn_start_lines=injection_stage_record_lines(
            getattr(outcome, "injection_stage", None)
        ),
    )
    die(transport_failure_die_message(primitive=primitive, target=target, marker=marker))
    raise AssertionError("die() must not return on a contained transport failure")


__all__ = (
    "STEP_READ_PANE_LANDING_WAIT",
    "STEP_READ_PANE_RETRY_PROBE",
    "STEP_READ_PANE_TURN_START_BASELINE",
    "STEP_READ_PANE_TURN_START_OBSERVE",
    "STEP_SEND_KEYS_ENTER",
    "STEP_SEND_KEYS_ENTER_RETRY",
    "STEP_SEND_KEYS_ROLLBACK",
    "STEP_SEND_TEXT_BODY",
    "TRANSPORT_STEPS",
    "close_transport_failure",
    "transport_failure_die_message",
)
