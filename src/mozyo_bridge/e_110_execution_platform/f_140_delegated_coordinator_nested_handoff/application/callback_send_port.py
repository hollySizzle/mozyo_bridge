"""Real callback send port over the existing semantic handoff (Redmine #13520 review F1).

The production ``send_fn`` for :class:`...handoff_callback_sender.HandoffCallbackSender`. Design
answer j#75098 Q2 + review F1 (j#75147): a callback fires an **existing semantic handoff
once** and reports its outcome — it must not reimplement the send rail / target resolution /
turn-start observation. This port therefore delegates to the sanctioned ``mozyo-bridge handoff
send`` surface (the same command an agent would use), which owns target resolution, the landing
rail, the transport, and turn-start verification, and parses its structured ``DeliveryOutcome``
(``status`` / ``reason``) back into a :class:`...handoff_callback_sender.HandoffDeliveryResult`.

Boundaries the port holds:

- **one send per call.** The processor has already claimed the row and gated on ``mark_sending``;
  this invokes the handoff exactly once and never retries internally.
- **fail-safe.** Any runner failure / unparseable outcome maps to a conservative result
  (``blocked`` -> uncertain), so a send whose fate is unknown is never auto-retried (a duplicate
  is the failure to avoid). The conservative default protects against a double delivery.
- **injectable runner.** The subprocess runner is a seam: production shells out to
  ``mozyo-bridge handoff send``; a test injects a fake runner returning a known outcome, and the
  #13490 live harness verifies the real positive / negative paths against a cockpit with QA-only
  anchors.
- **no raw Herdr / tmux on the LLM surface.** The port is background-runtime code; it never
  exposes a raw ``herdr agent wait/read/list/send`` to an LLM role.
"""

from __future__ import annotations

import json as _json
import subprocess  # noqa: S404 - the sanctioned mozyo-bridge handoff CLI boundary (injectable)
from dataclasses import dataclass
from typing import Callable, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.handoff_callback_sender import (
    HandoffDeliveryResult,
)
from mozyo_bridge.core.state.callback_outbox import CallbackOutboxRow

#: The runner seam: given an argv list, returns ``(returncode, stdout)``. Injectable for tests.
CallbackSendRunner = Callable[[list], "tuple[int, str]"]


def _default_runner(argv: list) -> "tuple[int, str]":
    """Run ``mozyo-bridge handoff send ...`` and capture ``(returncode, stdout)`` (production)."""
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell; the sanctioned handoff CLI
        argv, capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout


def _parse_outcome(stdout: str) -> Optional["tuple[str, str]"]:
    """Extract ``(status, reason)`` from the handoff ``DeliveryOutcome`` JSON, or ``None``.

    The handoff ``--record-format json`` path prints ``outcome.to_json()``. Scan the output for
    a JSON object carrying both ``status`` and ``reason`` (tolerant of surrounding lines). Any
    parse failure returns ``None`` (the caller fails safe).
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = _json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "status" in obj and "reason" in obj:
            return str(obj["status"]), str(obj["reason"])
    return None


@dataclass
class HandoffCallbackSendPort:
    """A real, fail-safe callback ``send_fn`` over ``mozyo-bridge handoff send``.

    ``runner`` is the injectable subprocess seam. ``mozyo_bridge_bin`` is the CLI entry (default
    ``mozyo-bridge``). Each call fires the semantic handoff to the row's ``callback_route`` with
    the row's durable anchor, once, and maps the parsed outcome; on any failure it returns a
    conservative ``blocked`` result (-> uncertain), never raising.
    """

    runner: CallbackSendRunner = _default_runner
    mozyo_bridge_bin: str = "mozyo-bridge"

    def __call__(self, row: CallbackOutboxRow) -> HandoffDeliveryResult:
        argv = [
            self.mozyo_bridge_bin, "handoff", "send",
            "--to", "codex",
            "--target", row.callback_route,
            "--source", "redmine",
            "--issue", row.issue,
            "--journal", row.journal,
            "--kind", "reply",
            "--mode", "standard",
            "--target-repo", "auto",
            "--record-format", "json",
        ]
        try:
            rc, stdout = self.runner(argv)
        except Exception:  # noqa: BLE001 - a runner blow-up is fail-safe uncertain, never a crash/retry
            return HandoffDeliveryResult("blocked", "inject_failed")
        parsed = _parse_outcome(stdout or "")
        if parsed is not None:
            return HandoffDeliveryResult(parsed[0], parsed[1])
        # No parseable structured outcome: fail safe. A clean rc still cannot confirm a
        # turn-start, so treat it as uncertain (no auto-retry); a nonzero rc is likewise
        # unconfirmed. Never optimistically report delivered without the structured outcome.
        return HandoffDeliveryResult("blocked", "turn_start_unconfirmed")


__all__ = (
    "CallbackSendRunner",
    "HandoffCallbackSendPort",
)
