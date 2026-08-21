"""The queue-enter causal series' deterministic submit proof (Redmine #15842).

#15842 j#109739 recorded the defect this module closes. A ``sublane create`` launched a
fresh Codex gateway and dispatched into it in one operation; the provider TUI had not
reached submit-ready, so the Enter was swallowed by the startup UI and the marker+body
stayed parked in the composer, unsent, with ``Context 0% used`` — yet the rail reported
``submitted_confirmed``. The dispatcher then yielded on that ACK and the lane sat idle
for over an hour until an operator noticed and pressed Enter by hand. One Enter
recovered it, which is the decisive evidence: the body was correctly in the composer and
only the submit was missing.

Why the existing gate let it through
------------------------------------
On the idle / turn-ended series the rail's causal claim rested on three facts: an
**armed** working-transition wait fired (``event_wait_kind == "changed"``), the pre-Enter
baseline was idle, and the launch generation stayed coherent. Arming before the Enter is
what normally makes the transition attributable — but it cannot distinguish the provider
process becoming busy because it *started this turn* from it becoming busy because it is
still *starting up*. A fresh pane reads ``awaiting_input`` while its banner is on screen,
so the baseline check passed too, and provider startup activity supplied the transition.
Every gate was satisfied by an Enter that never submitted anything.

The proof this module adds
--------------------------
Busy-ness is an inference; a cleared composer is an observation. The busy series has
proven submission that way since ADR-0002 / #15537 (``queued_submission_confirmed``), and
this module extends the same evidence to the causal series: a ``changed`` event confirms a
turn start **only when the injected body has verifiably left the current composer**. The
observation is the rail's existing resend gate — it already re-reads identity, the visible
pane, the declared startup-screen blockers, the current composer tail
(``current_composer_retains_body``, which structurally refuses to match scrollback), and
the runtime state — so no new port, no new transport primitive, and no new provider
literal is introduced. This module only *classifies* the token that gate returns.

The classification is a three-way partition of that closed token set, and each arm exists
because it needs a different disposition:

- :data:`SUBMIT_PROOF_COMPOSER_CLEARED` (``body_absent``) — the composer released the
  body: submission is proven and the causal confirmation may stand. This is the ordinary
  successful dispatch, and its behaviour is unchanged: a real submission clears the
  composer *before* the working transition, so the proof is already true by the time the
  armed wait fires.
- :data:`SUBMIT_PROOF_BODY_RETAINED` (``RESEND_SKIP_NONE``) — the gate positively
  established the opposite: identity holds, the pane reads, no startup screen is up, the
  runtime state is injectable, and the body is *still in the current composer*. This is
  the #15841 shape. It is not merely "unconfirmed": it is specific, recoverable evidence
  that the Enter did not take, so the session is authorised to spend its bounded
  Enter-only budget on it. ADR-0002 governs here — the body is never re-typed, and
  stopping is worse than pressing Enter again into a receiver whose identity still checks
  out.
- :data:`SUBMIT_PROOF_UNPROVEN` (every other token: unreadable pane, startup screen,
  identity drift, non-injectable state) — the gate could not establish either fact. Not a
  confirmation and not a retry authorisation: fail closed in both directions, and carry
  the refusal token so the durable record says which read fell short.

Fail-closed by construction: an unrecognised or malformed token classifies as
:data:`SUBMIT_PROOF_UNPROVEN`, so a future skip reason cannot default into either a
confirmation or an extra keypress.

Everything published is a fixed lower-snake-case token, so the telemetry is safe verbatim
in a pasteable durable record — the same posture as ``injection_stage``'s projection.
"""
from __future__ import annotations

from dataclasses import dataclass

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_resend_gate import (
    RESEND_SKIP_BODY_ABSENT,
    RESEND_SKIP_NONE,
)

#: No causal event was observed, so no submit proof was attempted. The busy series
#: (ADR-0002 / #15537) also stays here: it proves submission on its own
#: ``queued_submission_confirmed`` field and makes no causal claim.
SUBMIT_PROOF_UNEVALUATED = "unevaluated"

#: The injected body left the current composer: the Enter actually submitted.
SUBMIT_PROOF_COMPOSER_CLEARED = "composer_cleared"

#: The body is positively still in the current composer: the Enter did not take.
SUBMIT_PROOF_BODY_RETAINED = "body_retained"

#: Neither fact could be established from the read.
SUBMIT_PROOF_UNPROVEN = "unproven"

SUBMIT_PROOF_KINDS: frozenset[str] = frozenset(
    {
        SUBMIT_PROOF_UNEVALUATED,
        SUBMIT_PROOF_COMPOSER_CLEARED,
        SUBMIT_PROOF_BODY_RETAINED,
        SUBMIT_PROOF_UNPROVEN,
    }
)


@dataclass(frozen=True)
class QueueEnterSubmitProof:
    """One causal-series submit verdict and the refusal token behind it."""

    kind: str = SUBMIT_PROOF_UNEVALUATED
    refusal: str = RESEND_SKIP_NONE

    @property
    def submitted(self) -> bool:
        """Whether submission was positively proven by the composer clearing."""
        return self.kind == SUBMIT_PROOF_COMPOSER_CLEARED

    @property
    def enter_retryable(self) -> bool:
        """Whether the body is provably parked and a bounded Enter may be spent."""
        return self.kind == SUBMIT_PROOF_BODY_RETAINED

    def telemetry(self) -> dict[str, str]:
        """The additive, redaction-safe observation fields (empty when unevaluated)."""
        if self.kind == SUBMIT_PROOF_UNEVALUATED:
            return {}
        fields = {"submit_proof": self.kind}
        if self.kind == SUBMIT_PROOF_UNPROVEN:
            fields["submit_proof_refusal"] = self.refusal
        return fields


def classify_submit_proof(skip_reason: object) -> QueueEnterSubmitProof:
    """Classify a resend-gate skip reason into a submit verdict (pure, fail-closed).

    Only the producer's exact ``str`` tokens are read. Normalising a non-string to
    ``""`` would collapse it onto :data:`RESEND_SKIP_NONE` — the one token that
    *authorises a keypress* — so ``None`` or a wire-shaped value from a future / broken
    gate would silently buy an extra Enter. This is the same exact-value posture
    ``injection_stage`` applies to the busy-path bools, and it fails in the direction
    that costs nothing.
    """
    if not isinstance(skip_reason, str):
        return QueueEnterSubmitProof(SUBMIT_PROOF_UNPROVEN)
    if skip_reason == RESEND_SKIP_BODY_ABSENT:
        return QueueEnterSubmitProof(SUBMIT_PROOF_COMPOSER_CLEARED)
    if skip_reason == RESEND_SKIP_NONE:
        return QueueEnterSubmitProof(SUBMIT_PROOF_BODY_RETAINED)
    return QueueEnterSubmitProof(SUBMIT_PROOF_UNPROVEN, skip_reason)


__all__ = (
    "QueueEnterSubmitProof",
    "SUBMIT_PROOF_BODY_RETAINED",
    "SUBMIT_PROOF_COMPOSER_CLEARED",
    "SUBMIT_PROOF_KINDS",
    "SUBMIT_PROOF_UNEVALUATED",
    "SUBMIT_PROOF_UNPROVEN",
    "classify_submit_proof",
)
