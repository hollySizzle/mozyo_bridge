"""The provider-neutral onboarding conversation loop (Redmine #13497).

Drives a :class:`~..domain.conversation_port.ConversationProvider` toward a
decided, validated :class:`~..domain.intent.OnboardingIntent` — and nothing more.
The loop is where the closed-schema authority is enforced:

- every provider turn is either shown to the human (``Explain``) or validated
  against the closed schema via :func:`validate_onboarding_intent`
  (``IntentCandidate``);
- an invalid / unknown / over-reaching candidate is **rejected back into the
  conversation** as a structured error — never mutated on, never fatal;
- a provider that cannot operate safely raises ``ConversationProviderError`` and
  the loop aborts fail-closed (no mutation);
- the loop returns ``Ready`` only for a validated, decided intent whose action is
  ``confirm_plan``. Building the concrete plan and the *visible-plan* human
  confirmation stay model-external, in the bare-entry driver.

Human I/O is behind an injectable :class:`ConversationIO` seam so the loop is
testable with a scripted human + a fake provider (no real stdin / subprocess).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.conversation_port import (
    ConversationContext,
    ConversationProvider,
    ConversationProviderError,
    Explain,
    IntentCandidate,
    sanitize_display_text,
)
from ..domain.intent import IntentError, OnboardingIntent, validate_onboarding_intent

__all__ = (
    "ConversationIO",
    "ConversationOutcome",
    "Ready",
    "Cancelled",
    "Aborted",
    "DEFAULT_MAX_TURNS",
    "run_onboarding_conversation",
)

#: A generous ceiling so a pathological / looping provider cannot spin forever;
#: a real MVP onboarding converges in a handful of turns.
DEFAULT_MAX_TURNS = 24


class ConversationIO(Protocol):
    """The human side of the conversation (display + read one line)."""

    def show(self, text: str) -> None: ...

    def prompt(self) -> str | None:
        """Read one human line; ``None`` means EOF / the human cancelled."""
        ...


@dataclass(frozen=True)
class Ready:
    """The conversation produced a validated, decided intent ready to plan."""

    intent: OnboardingIntent


@dataclass(frozen=True)
class Cancelled:
    """The human (or the model on their behalf) cancelled the conversation."""

    reason: str = "cancelled"


@dataclass(frozen=True)
class Aborted:
    """The conversation aborted fail-closed (no mutation)."""

    code: str
    message: str


ConversationOutcome = Ready | Cancelled | Aborted


def run_onboarding_conversation(
    provider: ConversationProvider,
    context: ConversationContext,
    io: ConversationIO,
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> ConversationOutcome:
    """Run the loop to a :class:`ConversationOutcome` (never mutates)."""
    for _ in range(max_turns):
        try:
            turn = provider.converse(context)
        except ConversationProviderError as exc:
            return Aborted(exc.code, exc.message)

        if isinstance(turn, Explain):
            # Sanitize at the universal render boundary too, so any provider's
            # untrusted display text is escaped before it reaches the terminal
            # (Redmine #13497 j#74970 F2), not only the CLI binding's.
            safe_text = sanitize_display_text(turn.text)
            io.show(safe_text)
            reply = io.prompt()
            if reply is None:
                return Cancelled()
            context = context.with_assistant(safe_text).with_human(reply)
            continue

        if isinstance(turn, IntentCandidate):
            try:
                intent = validate_onboarding_intent(turn.intent)
            except IntentError as exc:
                # Reject back into the conversation — structured error, no mutation.
                context = context.with_error(exc.as_record())
                continue

            if intent.action == "cancel":
                return Cancelled()
            if intent.action == "confirm_plan" and not intent.preset_undecided:
                return Ready(intent)

            # A decided-but-not-confirmed / undecided / explain / propose / revise
            # intent cannot be planned yet: reflect why and gather more from the
            # human, without ever mutating.
            reason = (
                "preset is undecided; choose a preset"
                if intent.preset_undecided
                else f"action is {intent.action!r}; confirm the plan to proceed"
            )
            context = context.with_error(
                {"error": "intent_not_ready", "message": reason, "field": "action"}
            )
            io.show(f"(need more before planning: {reason})")
            reply = io.prompt()
            if reply is None:
                return Cancelled()
            context = context.with_human(reply)
            continue

        # A provider that returned neither closed turn is a protocol breach.
        return Aborted(
            "conversation_provider_protocol",
            f"provider returned a non-turn value: {type(turn).__name__}",
        )

    return Aborted(
        "conversation_did_not_converge",
        f"conversation did not converge within {max_turns} turns",
    )
