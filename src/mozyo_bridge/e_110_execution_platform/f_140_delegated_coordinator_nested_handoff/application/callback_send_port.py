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

Durability contract (#13520 review F6, design D1)
-------------------------------------------------
The **authoritative durable callback record is the home-scoped callback outbox row**
(``workflow-runtime.sqlite``, design answer j#75098 Q3), not the Redmine delivery receipt.
``--persist-delivery`` writes a *best-effort, credential-gated Redmine delivery receipt* — a
human-visible notification pointer — through the same staged transport all handoff deliveries
use; it can legitimately report ``write_optin_unset`` (the ``MOZYO_REDMINE_DELIVERY_WRITE`` opt-in
is unset) or a transport failure WITHOUT affecting durability or the delivered outcome. The port
therefore parses the receipt only as **observable evidence** (surfaced on
:class:`...handoff_callback_sender.HandoffDeliveryResult`); a confirmed turn-start is ``delivered``
regardless of whether the receipt persisted. On local outbox loss, recovery is by re-reading the
**exact Redmine gate journal** (the fresh-turn sweep / host-restart reconciler), never the delivery
receipt. (This supersedes the earlier j#75108 phrasing that the receipt "records the outcome
durably"; the outbox is the durability mechanism, the receipt is a best-effort human pointer.)
"""

from __future__ import annotations

import json as _json
import subprocess  # noqa: S404 - the sanctioned mozyo-bridge handoff CLI boundary (injectable)
from dataclasses import dataclass
from typing import Callable, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.handoff_callback_sender import (
    HandoffDeliveryResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reconcile_delivery_route import (
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    COORDINATOR_ROUTE,
)
from mozyo_bridge.core.state.callback_outbox import CallbackOutboxRow

#: The runner seam: given an argv list, returns ``(returncode, stdout)``. Injectable for tests.
CallbackSendRunner = Callable[[list], "tuple[int, str]"]

#: The two valid ``--to`` receiver tokens a callback may be delivered with (the handoff CLI's
#: public receiver vocabulary). A coordinator-route receiver derivation that lands outside this
#: set refuses to send rather than shipping an unresolvable token.
_PROVIDER_TOKENS = frozenset({PROVIDER_CLAUDE, PROVIDER_CODEX})

#: The port's own pre-send refusal: the coordinator route's receiver provider could not be
#: derived (no stamped ``target_receiver``, and the role-authority resolution failed). A
#: deterministic zero-send — nothing was typed — so it bounded-retries rather than poisoning
#: the row to ``uncertain``. Allowlisted in
#: :data:`...domain.callback_delivery.ZERO_SEND_REASON_ALLOWLIST` (Redmine #15707).
RECEIVER_PROVIDER_UNRESOLVED = "receiver_provider_unresolved"

#: The port's second pre-send refusal (review j#108062 finding_receiverstamp): the row DOES
#: carry a non-blank ``target_receiver``, but it is not a provider token. That stamp is the
#: row's durable intended-target expectation (#13683 R4-F2) — a broken one signals a row whose
#: expectation cannot be honored, and silently substituting the CURRENT role authority could
#: mis-route against what was recorded (the background sender refuses the analogous shape as
#: ``target_tuple_mismatch``). Deterministic zero-send; also allowlisted.
RECEIVER_STAMP_INVALID = "receiver_stamp_invalid"

#: The producer's own injection-stage token for the port's pre-send refusals (#14232 j#95333
#: F1 semantics): the port refused before invoking the handoff CLI, so zero bytes were typed.
_STAGE_NOT_SENT = "not_sent"


def _default_coordinator_provider() -> str:
    """Resolve the coordinator role's bound provider from the repo-local role authority.

    The same resolution the ``coordinator`` pseudo-target route uses
    (:func:`...main_lane_guard_gate.resolve_coordinator_provider`, #15655 j#107777 finding_1),
    so the ``--to`` token and the pane the route resolves can no longer contradict each other
    after a coordinator rebind (Redmine #15707 — j#108012 measured exactly that contradiction
    as an ``invalid_args`` fail-closed refusal). Imported lazily: the port stays importable
    without the role-authority composition.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.main_lane_guard_gate import (  # noqa: E501
        resolve_coordinator_provider,
    )

    return resolve_coordinator_provider()


def _default_runner(argv: list) -> "tuple[int, str]":
    """Run ``mozyo-bridge handoff send ...`` and capture ``(returncode, stdout)`` (production)."""
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell; the sanctioned handoff CLI
        argv, capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout


def _parse_outcome(stdout: str) -> Optional["tuple[str, str, str, dict | None]"]:
    """Extract ``(status, reason, injection_stage, queue_observation)`` from the outcome JSON.

    The handoff ``--record-format json`` path prints ``outcome.to_json()``. Scan the output for
    a JSON object carrying both ``status`` and ``reason`` (tolerant of surrounding lines). Any
    parse failure returns ``None`` (the caller fails safe).

    ``queue_observation`` (review j#106497) is the producer's full
    ``queue_enter_turn_start_observation`` dict when present — it carries the exact busy
    queued-submission proof (ADR-0002 / #15537) — and ``None`` otherwise.
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
            # Redmine #14232 review j#95333 F1: carry the producer's own injection-stage token
            # when the payload has one, so the retry decision reuses the classification the
            # sender made with full context instead of re-deriving it from two tokens.
            stage = obj.get("injection_stage")
            stage_token = (
                str(stage.get("stage") or "") if isinstance(stage, dict) else ""
            )
            observation = obj.get("queue_enter_turn_start_observation")
            if not isinstance(observation, dict):
                observation = None
            return str(obj["status"]), str(obj["reason"]), stage_token, observation
    return None


def _parse_receipt(stdout: str) -> Optional["tuple[bool, str]"]:
    """Extract ``(persisted, reason)`` from the ``--persist-delivery`` receipt JSON, or ``None``.

    With ``--record-format json --persist-delivery`` the handoff also prints the delivery
    ``DeliveryReceipt`` JSON (``persisted`` / ``reason`` / ``record_class`` — distinct from the
    outcome JSON, which carries ``status`` / ``reason``). This is **best-effort observability
    only** (#13520 review F6): it never changes the send outcome. ``None`` when no receipt line
    was printed (e.g. ``--persist-delivery`` produced no receipt) or it did not parse.
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = _json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "persisted" in obj and "record_class" in obj:
            return bool(obj["persisted"]), str(obj.get("reason", ""))
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
    #: The coordinator role->provider resolution seam (#15707). Production resolves through the
    #: repo-local role authority (:func:`_default_coordinator_provider`); a test injects a fake.
    #: Consulted ONLY for a coordinator-route row whose ``target_receiver`` is not already a
    #: valid provider token — never for any other route.
    coordinator_provider_resolver: Callable[[], str] = _default_coordinator_provider
    #: The workspace this sender is attested for (#13520 review R2-F5 / #13518 review R3-F3). When
    #: attested (non-blank), the sender routes a row ONLY when the row's workspace id EXACTLY
    #: matches — a foreign row OR a row with no workspace id is refused (fail-closed), rather than
    #: routing on the ambient cwd/env. An attested sender never delivers a workspace it cannot
    #: positively confirm the row belongs to. "" (attested blank) skips the check — the legacy /
    #: single-workspace / explicit-migration bucket, reachable in production only behind the CLI's
    #: --allow-unpartitioned-callbacks surface.
    attested_workspace_id: str = ""

    def _receiver_token(self, row: CallbackOutboxRow) -> "tuple[str, str]":
        """The ``--to`` receiver for this row: ``(token, "")`` or ``("", refusal_reason)``.

        Redmine #15707 (a): a coordinator-route row derives its receiver from the coordinator
        role authority instead of a hardcoded literal — j#108012 measured the literal ``codex``
        contradicting the claude pane the same route resolved to (a coordinator rebind,
        #13229), which the ``binds_receiver`` fence correctly refused as ``invalid_args``.
        Precedence: the row's stamped ``target_receiver`` (the binding-resolved provider
        :func:`...workspace_callback_review_return.coordinator_target_tuple` wrote at enqueue)
        when it is a valid provider token; the injected role-authority resolver ONLY when the
        stamp is BLANK (review j#108062 finding_receiverstamp: a non-blank non-provider stamp
        is a broken durable expectation and refuses typed, never silently re-derived); and a
        resolver that fails or answers outside the provider vocabulary refuses (fail-closed)
        rather than silently defaulting to ``codex``.

        Every NON-coordinator route keeps the literal ``codex`` — the #13758 R2-F2 disposition
        (a worker row's provider flows via the resolver-backed background sender, not this
        port) is deliberately preserved.
        """
        route = str(getattr(row, "callback_route", "") or "").strip()
        if route != COORDINATOR_ROUTE:
            return PROVIDER_CODEX, ""
        stamped = str(getattr(row, "target_receiver", "") or "").strip()
        if stamped:
            if stamped in _PROVIDER_TOKENS:
                return stamped, ""
            return "", RECEIVER_STAMP_INVALID
        try:
            resolved = str(self.coordinator_provider_resolver() or "").strip()
        except Exception:  # noqa: BLE001 - an unresolved role authority is a typed refusal, never a guess
            return "", RECEIVER_PROVIDER_UNRESOLVED
        if resolved in _PROVIDER_TOKENS:
            return resolved, ""
        return "", RECEIVER_PROVIDER_UNRESOLVED

    def __call__(self, row: CallbackOutboxRow) -> HandoffDeliveryResult:
        row_ws = str(getattr(row, "workspace_id", "") or "").strip()
        attested = str(self.attested_workspace_id or "").strip()
        if attested and row_ws != attested:
            # Fail-closed workspace pin (R3-F3): an attested sender routes only an EXACT-match row.
            # A foreign row (different workspace) or an unattested row (no workspace id) is refused —
            # not a deterministic dead-letter but ``blocked`` so the row stays for its correct
            # workspace's sender (never delivered here, never mis-sent on ambient env).
            reason = "workspace_mismatch" if row_ws else "workspace_unattested_row"
            return HandoffDeliveryResult("blocked", reason)
        receiver, refusal = self._receiver_token(row)
        if refusal:
            # A deterministic pre-send refusal: the coordinator route's receiver could not be
            # derived, so nothing is typed. Carrying the producer stage token classifies it as
            # a bounded-retry not-sent (#14232 j#95333 F1), never an uncertain terminal.
            return HandoffDeliveryResult("blocked", refusal, injection_stage=_STAGE_NOT_SENT)
        argv = [
            self.mozyo_bridge_bin, "handoff", "send",
            "--to", receiver,
            "--target", row.callback_route,
            "--source", "redmine",
            "--issue", row.issue,
            "--journal", row.journal,
            "--kind", "reply",
            "--mode", "standard",
            "--target-repo", "auto",
            "--record-format", "json",
            # Emit a best-effort, credential-gated Redmine delivery RECEIPT through the sanctioned
            # handoff delivery-record path (#13520 review F6). This is a human-visible notification
            # pointer, NOT the durability mechanism: the outbox row is the authoritative durable
            # record (see the module "Durability contract"). The receipt is parsed only as
            # observable evidence and never gates the delivered outcome.
            "--persist-delivery",
        ]
        try:
            rc, stdout = self.runner(argv)
        except Exception:  # noqa: BLE001 - a runner blow-up is fail-safe uncertain, never a crash/retry
            return HandoffDeliveryResult("blocked", "inject_failed")
        stdout = stdout or ""
        receipt = _parse_receipt(stdout)
        persist_ok = receipt[0] if receipt is not None else None
        persist_reason = receipt[1] if receipt is not None else ""
        parsed = _parse_outcome(stdout)
        if parsed is not None:
            return HandoffDeliveryResult(
                parsed[0],
                parsed[1],
                persist_ok=persist_ok,
                persist_reason=persist_reason,
                injection_stage=parsed[2],
                queue_enter_turn_start_observation=parsed[3],
            )
        # No parseable structured outcome: fail safe. A clean rc still cannot confirm a
        # turn-start, so treat it as uncertain (no auto-retry); a nonzero rc is likewise
        # unconfirmed. Never optimistically report delivered without the structured outcome.
        return HandoffDeliveryResult(
            "blocked", "turn_start_unconfirmed", persist_ok=persist_ok, persist_reason=persist_reason
        )


__all__ = (
    "CallbackSendRunner",
    "HandoffCallbackSendPort",
    "RECEIVER_PROVIDER_UNRESOLVED",
    "RECEIVER_STAMP_INVALID",
)
