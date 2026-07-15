"""Structured high-level send binding for the resume leg (Redmine #13813 F2, j#79332).

The Design Answer (j#79332 §4) forbids optimistically converting ``orchestrate_handoff``'s
exit code into a delivery. This port mirrors the sanctioned :class:`HandoffCallbackSendPort`
pattern: it runs the high-level ``mozyo-bridge handoff send --mode standard --record-format
json`` **exactly once** to re-issue the original request anchor, parses the structured
``DeliveryOutcome`` + ``turn_start_outcome`` from stdout, and maps ONLY a positively
confirmed landing onto :data:`SendOutcome` ``started``. Every ambiguous / negative / parse /
runner-failure case is ``not_started`` / ``unknown`` so the fence falls to ``uncertain``
(operator reconcile) — never a false ``delivered``.

The ``runner`` is the injectable transport seam (default: a real subprocess). No raw Herdr /
tmux, no low-level read/message/type/keys, no self-send choreography — the single send is the
existing governed high-level rail.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution import (
    TURN_START_NOT_STARTED,
    TURN_START_STARTED,
    TURN_START_UNKNOWN,
    SendOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (
    OperatorStartupGate,
)

#: A ``handoff send`` runner: argv -> (returncode, stdout). Injectable for hermetic tests.
Runner = Callable[[Sequence[str]], "tuple[int, str]"]

#: The positive delivery wire contract under ``--mode standard`` (a verified landing):
#: ``status == "sent"`` AND ``reason == "ok"`` (mirrors ``delivery_outcome_gate``).
_POSITIVE_STATUS = "sent"
_POSITIVE_REASON = "ok"
#: The positive turn-start token (herdr event rail).
_TURN_START_STARTED_TOKEN = "started"


def _default_runner(argv: Sequence[str]) -> "tuple[int, str]":
    proc = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout


def resolve_execution_workdir(repo_root: str, execution_root: str) -> Optional[str]:
    """The absolute ``--workdir`` = gate ``execution_root`` safely resolved under ``repo_root``.

    ``execution_root`` is the gate's repo-relative POSIX pointer (``.`` at the repo root, else
    ``projects/x``). The joined path is normalized (``..`` collapsed) and must stay at or under
    the normalized ``repo_root``; any escape returns ``None`` so the caller zero-sends BEFORE
    reserving (Design Answer j#79405 §C). Pure — no filesystem access — so it is hermetic and
    the leg's pre-reserve check and the send argv derive the identical value. The domain already
    rejects absolute / ``..`` execution roots; this re-checks at action time against the freshly
    resolved repo root (defense in depth).
    """
    root_norm = os.path.normpath(repo_root)
    rel = (execution_root or "").strip()
    if rel in ("", "."):
        return root_norm
    if os.path.isabs(rel):
        return None
    joined = os.path.normpath(os.path.join(root_norm, rel))
    if joined == root_norm or joined.startswith(root_norm + os.sep):
        return joined
    return None


def _last_json_object(stdout: str, *required_keys: str) -> Optional[dict]:
    """The last ``{...}`` stdout line carrying every required key, or None (mirrors the port)."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and all(key in obj for key in required_keys):
            return obj
    return None


def map_handoff_stdout_to_send_outcome(rc: int, stdout: str) -> SendOutcome:
    """Map a ``handoff send --record-format json`` result onto a :class:`SendOutcome`.

    ``started`` ONLY when the structured ``turn_start_outcome.outcome == "started"`` (the
    event-driven herdr rail) or, absent that, the verified standard-mode delivery
    (``status == "sent"`` AND ``reason == "ok"``). Every other case — a blocked / pending /
    ack-only / delivered_not_started / precondition_not_idle / inject_failed outcome, or an
    unparseable stdout — is ``not_started`` / ``unknown`` so the fence falls to uncertain
    (Design Answer j#79332 §4). Never a false ``delivered``.
    """
    turn_start = _last_json_object(stdout, "outcome")
    # Prefer the event-driven turn-start telemetry when the DeliveryOutcome carries it.
    delivery = _last_json_object(stdout, "status", "reason")
    embedded_turn = None
    if isinstance(delivery, dict):
        embedded = delivery.get("turn_start_outcome")
        if isinstance(embedded, dict):
            embedded_turn = embedded
    turn = embedded_turn if embedded_turn is not None else (
        turn_start if (turn_start is not None and "status" not in turn_start) else None
    )

    if turn is not None:
        if str(turn.get("outcome")) == _TURN_START_STARTED_TOKEN:
            return SendOutcome(turn_start=TURN_START_STARTED, detail="turn_start_outcome=started")
        return SendOutcome(
            turn_start=TURN_START_NOT_STARTED,
            detail=f"turn_start_outcome={turn.get('outcome')}; not started",
        )

    if isinstance(delivery, dict):
        status = str(delivery.get("status"))
        reason = str(delivery.get("reason"))
        if status == _POSITIVE_STATUS and reason == _POSITIVE_REASON:
            return SendOutcome(
                turn_start=TURN_START_STARTED, detail="verified standard-mode delivery sent/ok"
            )
        return SendOutcome(
            turn_start=TURN_START_NOT_STARTED,
            detail=f"delivery status={status} reason={reason}; not delivered",
        )

    return SendOutcome(
        turn_start=TURN_START_UNKNOWN,
        detail=f"handoff send outcome unparseable (rc={rc}); uncertain",
    )


@dataclass(frozen=True)
class ResumeHandoffSendPort:
    """Builds the single high-level send that re-issues the original request anchor.

    ``locator`` is the exact resolved live target (from the action-time target resolver);
    ``mozyo_bridge_bin`` / ``runner`` are the transport seam. :meth:`build` returns the
    ``send`` callable the resume orchestrator invokes at most once, only after a winning
    reserve.
    """

    locator: str
    mozyo_bridge_bin: str = "mozyo-bridge"
    runner: Runner = field(default=_default_runner)

    def build(
        self, gate: OperatorStartupGate, repo_root: str, env: Mapping[str, str]
    ) -> Callable[[], SendOutcome]:
        orig = gate.original_request

        def _send() -> SendOutcome:
            # Exact target/lane/repo/workdir bind (review j#79366 F1, j#79405 §C): the explicit
            # resolved repo root, lane label, and the gate's execution_root safely resolved to an
            # absolute `--workdir` under repo_root — NOT `--target-repo auto` inference and NOT
            # the pane cwd — so the re-issue's envelope carries the exact `execution_root` the
            # gate was pinned against (the planner renders the portable `.` pointer from it).
            workdir = resolve_execution_workdir(repo_root, gate.target.execution_root) or repo_root
            argv = [
                self.mozyo_bridge_bin,
                "handoff",
                "send",
                "--to",
                gate.target.provider_id,
                "--target",
                self.locator,
                "--source",
                "redmine",
                "--issue",
                orig.issue,
                "--journal",
                orig.journal,
                "--kind",
                "implementation_request",
                "--mode",
                "standard",
                "--target-repo",
                repo_root,
                "--target-lane",
                gate.target.lane_id,
                "--workdir",
                workdir,
                "--record-format",
                "json",
            ]
            try:
                rc, stdout = self.runner(argv)
            except Exception:  # noqa: BLE001 - a runner failure is uncertain, never delivered
                return SendOutcome(
                    turn_start=TURN_START_UNKNOWN,
                    detail="handoff send runner raised; outcome unknown -> uncertain",
                )
            return map_handoff_stdout_to_send_outcome(rc, stdout)

        return _send


__all__ = (
    "Runner",
    "ResumeHandoffSendPort",
    "map_handoff_stdout_to_send_outcome",
    "resolve_execution_workdir",
)
