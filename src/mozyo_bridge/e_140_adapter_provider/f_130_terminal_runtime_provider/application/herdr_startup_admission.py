"""Action-time receiver startup admission — the pre-send zero-send gate (Redmine #13760).

The bug this closes (#13760, live evidence #13582 j#77917 / j#77937 / j#77939): a
fresh worktree's managed Claude worker renders a **startup screen** — a trust
confirmation, a first-run theme picker, a login prompt — instead of a request
composer. That screen is a live pane with non-blank content, so every projection the
dispatch path had said *ready*: ``sublane readiness --json`` reported ``status=ok``,
the herdr agent status was ``unknown`` both before and after the screen cleared, and
the queue-enter rail typed the Implementation Request into a screen that has no
composer and pressed Enter — which **accepted the dialog's default** instead of
submitting a turn. The request body was destroyed, and the transport recorded ``sent
/ queue_enter``, so the coordinator's durable record projected a delivered dispatch
that the worker had never seen.

The fix is a hard, pre-send admission gate, and its placement is the whole point
(Design Answer j#77947, Q2): it sits at the **shared herdr send boundary**, at
**action time** — after the target is resolved, immediately before the first
``send_text`` — not in ``sublane dispatch-worker``, which would leave every direct
``handoff send`` (the flow that actually lost j#77917) unprotected, and not at
readiness-probe time, which is a different moment than the send and can go stale
between them.

What the gate does, and what it deliberately does not do
-------------------------------------------------------
- It reads the receiver's **visible** pane once, at action time, through the caller's
  already-bound read primitive (the same herdr shim the send would have used), and
  classifies it against the *resolved receiver provider's* declared
  ``startup_blockers`` (:mod:`...f_160_provider_registry.domain.agent_provider_profile_config`).
  The classifier is pure and provider-neutral: every provider-specific string lives in
  the profile data, never here (j#77947 correction 1).
- On a match it returns :data:`ADMISSION_BLOCKED` and the caller emits a structured
  ``receiver_startup_interaction_required`` outcome. **Zero send**: no text, no keys,
  no Enter, no ACK. The transport never answers the provider's prompt — accepting a
  trust / login screen is an operator action in the provider's own UI, and this gate
  exists precisely because a blind Enter *did* accept one (#13760 境界).
- Only the **provider id and the fixed blocker id** leave this module. The pane's own
  text is never returned, logged, or carried onto an outcome / journal (j#77947
  invariant 3) — a startup screen can show a workspace path, and a durable record is
  pasteable.
- An **unreadable** pane does NOT decay to "startup clear" (j#77947 invariant 4). It
  is a transport failure, and it fails closed on the existing transport-failure path —
  which is also zero-send, because the read happens before the first injection.
- An **unknown / unprofiled** provider fails closed the same way. The gate never
  guesses that a provider it cannot describe has no startup screen.
- A receiver whose profile declares no blockers (or whose pane matches none) is
  **admitted**, and the send proceeds byte-for-byte as before. The gate adds exactly
  one visible read — the read the send path already performed as its snapshot preflight
  — so an admitted queue-enter send is unchanged (j#77947 Q3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (
    AGENT_PROVIDER_PROFILES,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (
    AgentProviderProfileRegistry,
)

#: The receiver's visible pane carries no declared startup blocker — send as before.
ADMISSION_ADMITTED = "admitted"
#: A declared startup screen is on the receiver. Zero-send; the operator owns it.
ADMISSION_BLOCKED = "startup_interaction_required"
#: The visible pane could not be read. A transport failure — never "startup clear".
ADMISSION_UNREADABLE = "receiver_unreadable"
#: The receiver resolved to a provider with no profile. Fail closed, never assume.
ADMISSION_UNKNOWN_PROVIDER = "unknown_provider"

ADMISSION_OUTCOMES: frozenset[str] = frozenset(
    {
        ADMISSION_ADMITTED,
        ADMISSION_BLOCKED,
        ADMISSION_UNREADABLE,
        ADMISSION_UNKNOWN_PROVIDER,
    }
)


class StartupAdmissionError(ValueError):
    """A startup-admission record violates the closed contract (fail-closed)."""


@dataclass(frozen=True)
class StartupAdmission:
    """The structured verdict of one action-time startup admission (never raises).

    ``outcome`` is the sole authority and is always a member of
    :data:`ADMISSION_OUTCOMES`. ``provider_id`` and ``blocker_id`` are fixed tokens —
    ``blocker_id`` is non-empty only for :data:`ADMISSION_BLOCKED`. There is
    deliberately **no** field carrying pane content: the whole record is safe to put on
    a structured outcome and a pasteable durable record.
    """

    outcome: str
    provider_id: str = ""
    blocker_id: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in ADMISSION_OUTCOMES:
            raise StartupAdmissionError(
                f"startup admission outcome {self.outcome!r} is not recognised; "
                f"allowed: {sorted(ADMISSION_OUTCOMES)}"
            )
        if self.outcome == ADMISSION_BLOCKED and not self.blocker_id:
            raise StartupAdmissionError(
                "a blocked startup admission must name the matched blocker id: it is "
                "the only thing about the screen a structured outcome may report"
            )
        if self.outcome != ADMISSION_BLOCKED and self.blocker_id:
            raise StartupAdmissionError(
                f"startup admission {self.outcome!r} must not carry a blocker id "
                f"(got {self.blocker_id!r})"
            )

    @property
    def admitted(self) -> bool:
        """True only when the send may proceed. Every other outcome is zero-send."""
        return self.outcome == ADMISSION_ADMITTED

    def to_telemetry_dict(self) -> dict:
        """Fixed tokens only — no pane text, no paths (durable-record safe)."""
        return {
            "outcome": self.outcome,
            "provider_id": self.provider_id,
            "blocker_id": self.blocker_id,
        }


def evaluate_startup_admission(
    *,
    provider_id: str,
    read_visible: Callable[[], object],
    registry: Optional[AgentProviderProfileRegistry] = None,
) -> StartupAdmission:
    """Admit or refuse a send against the receiver's live startup state (fail-closed).

    ``read_visible`` is the caller's already-bound visible-pane read (under herdr, the
    same shim primitive the send itself would use), invoked **once**, at action time,
    after target resolution and before any injection. Injecting the read keeps this
    module free of transport construction and lets the caller stay the single owner of
    the resolved target.

    Never raises: a read that fails for any reason (a transport error, a herdr binary
    fault, a ``die()``-shaped ``SystemExit`` from a tmux-era primitive) is
    :data:`ADMISSION_UNREADABLE`, and an unprofiled provider is
    :data:`ADMISSION_UNKNOWN_PROVIDER`. Both are zero-send refusals, distinct from a
    matched blocker so the caller can report the accurate cause; neither ever decays
    into an admission.
    """
    profiles = AGENT_PROVIDER_PROFILES if registry is None else registry
    resolved = str(provider_id or "").strip()
    profile = profiles.get(resolved) if resolved else None
    if profile is None:
        return StartupAdmission(
            outcome=ADMISSION_UNKNOWN_PROVIDER, provider_id=resolved
        )
    try:
        content = read_visible()
    except (Exception, SystemExit):
        # Fail closed, and stay closed: an unreadable receiver is exactly the case an
        # "assume it is fine" fallback would turn back into #13760. The read runs
        # before the first injection, so this refusal is zero-send by construction.
        return StartupAdmission(outcome=ADMISSION_UNREADABLE, provider_id=resolved)
    if not isinstance(content, str) or not content.strip():
        # A blank read is not evidence of a clear composer either (the #13760 live lane
        # saw an empty composer *after* the dialog ate the body). Treat it as unreadable.
        return StartupAdmission(outcome=ADMISSION_UNREADABLE, provider_id=resolved)
    blocker = profile.match_startup_blocker(content)
    if blocker is not None:
        return StartupAdmission(
            outcome=ADMISSION_BLOCKED,
            provider_id=resolved,
            blocker_id=blocker.blocker_id,
        )
    return StartupAdmission(outcome=ADMISSION_ADMITTED, provider_id=resolved)


def startup_admission_record_lines(admission: StartupAdmission) -> list[str]:
    """Render the additive durable-record telemetry (pure, redaction-safe).

    Follows the turn-start rail's ``turn_start_rail_record_lines`` precedent: fixed
    tokens and a verdict only — no free text, no pane content, no absolute paths — so it
    is safe in a pasteable delivery record. Emitted only for a refusal (an admitted send
    reports nothing new, keeping an admitted record byte-identical).
    """
    if admission.admitted:
        return []
    if admission.outcome == ADMISSION_BLOCKED:
        return [
            (
                "- Startup admission (pre-send): BLOCKED — receiver provider "
                f"{admission.provider_id} is showing the {admission.blocker_id} startup "
                "screen, which cannot accept a handoff body. Zero-send: no text, no "
                "keys, no Enter, no ACK. Clear the prompt in the provider's own UI, "
                "then re-issue the SAME durable anchor; the transport never answers a "
                "startup prompt on your behalf."
            )
        ]
    if admission.outcome == ADMISSION_UNREADABLE:
        return [
            (
                "- Startup admission (pre-send): UNREADABLE — the receiver's visible "
                "pane could not be read, so its startup state is unknown. Fail-closed "
                "zero-send (an unreadable pane is never treated as startup-clear)."
            )
        ]
    return [
        (
            "- Startup admission (pre-send): UNKNOWN PROVIDER — the receiver resolved "
            f"to provider {admission.provider_id!r}, which has no registered profile, "
            "so its startup screens cannot be classified. Fail-closed zero-send."
        )
    ]


__all__ = (
    "ADMISSION_ADMITTED",
    "ADMISSION_BLOCKED",
    "ADMISSION_OUTCOMES",
    "ADMISSION_UNKNOWN_PROVIDER",
    "ADMISSION_UNREADABLE",
    "StartupAdmission",
    "StartupAdmissionError",
    "evaluate_startup_admission",
    "startup_admission_record_lines",
)
