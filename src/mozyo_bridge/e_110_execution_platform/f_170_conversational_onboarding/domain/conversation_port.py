"""Provider-neutral onboarding conversation port (Redmine #13497).

The conversation actor's *only* job is to turn a human's natural language into a
closed :class:`~..domain.intent.OnboardingIntent`. This module is the
provider-neutral boundary of that job:

- :class:`SanitizedFacts` — the *only* preflight facts a model may see. Absolute
  paths, file hashes, the herdr binary realpath, and any secret are stripped; the
  model sees classifications (state / root_kind / path_risk / adoption_marker /
  herdr availability) and a fixed caution reason, never raw notes (which can
  embed a path).
- :class:`ConversationContext` — sanitized facts + the closed intent schema + the
  closed tool surface + the structured errors from prior rejected turns.
- :class:`ConversationTurn` — the closed union a provider may return: an
  :class:`Explain` (ask/explain, display-only) or an :class:`IntentCandidate`
  (a proposed intent mapping the loop validates fail-closed).
- :class:`ConversationProvider` — the protocol a concrete binding implements.

No provider (Claude / Codex / a test double) is named here; concrete bindings
live in the application ``onboarding_providers`` package so the provider name
never leaks into the domain contract (Redmine #13497 j#74915 / j#74919 R2).
Everything here is pure — no filesystem, subprocess, env, or clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping, Protocol, Sequence, Union, runtime_checkable

from .intent import (
    INTENT_ACTIONS,
    INTENT_BACKENDS,
    INTENT_GIT_MODES,
    INTENT_PRESETS,
    INTENT_RULES_STORES,
    ONBOARDING_INTENT_SCHEMA_VERSION,
)

__all__ = (
    "SanitizedFacts",
    "Explain",
    "IntentCandidate",
    "ConversationTurn",
    "ConversationContext",
    "ConversationProvider",
    "ConversationProviderError",
    "PROVIDER_UNAVAILABLE",
    "build_intent_schema",
    "build_intent_json_schema",
    "build_turn_json_schema",
    "build_tool_schema",
    "sanitize_facts",
    "sanitize_display_text",
)

#: Fail-closed code a binding raises when the provider cannot be operated safely
#: (missing binary, timeout, non-zero exit, malformed output). The bare-entry
#: driver renders it and mutates nothing (Redmine #13497 j#74915).
PROVIDER_UNAVAILABLE = "conversation_provider_unavailable"

# Model-authored display text is untrusted terminal output: newline / tab are
# kept, but every other C0 control, DEL, the C1 range, and the Unicode bidi /
# direction overrides are rendered as a visible escape so a provider can never
# emit raw escape sequences that drive the terminal (title / clipboard / cursor)
# or spoof the human-confirmation UI with reordered text (Redmine #13497 j#74970 F2).
_DISPLAY_ALLOWED_CONTROLS: frozenset[str] = frozenset({"\n", "\t"})
_DISPLAY_BIDI_CONTROLS: frozenset[int] = frozenset(
    {0x200E, 0x200F, 0x061C, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
     0x2066, 0x2067, 0x2068, 0x2069}
)


def sanitize_display_text(text: str) -> str:
    """Escape control / direction characters in untrusted model display text.

    Keeps ``\\n`` / ``\\t``; replaces every other C0 control, ``DEL``, the C1
    range, and the bidi / direction overrides with a visible ``\\xNN`` / ``\\uNNNN``
    escape (never silently dropped). Printable text is returned unchanged.
    """
    out: list[str] = []
    for ch in text:
        if ch in _DISPLAY_ALLOWED_CONTROLS:
            out.append(ch)
            continue
        cp = ord(ch)
        if cp < 0x20 or cp == 0x7F or 0x80 <= cp <= 0x9F:
            out.append(f"\\x{cp:02x}")
        elif cp in _DISPLAY_BIDI_CONTROLS:
            out.append(f"\\u{cp:04x}")
        else:
            out.append(ch)
    return "".join(out)


@dataclass(frozen=True)
class SanitizedFacts:
    """The redacted preflight facts a conversation provider may see.

    Deliberately excludes the canonical root path, existing-file hashes, the
    herdr binary realpath, and the raw preflight notes (which can embed a path).
    Only closed classifications and a fixed caution reason are exposed.
    """

    state: str
    root_kind: str
    path_risk: str
    adoption_marker: str
    herdr_available: bool
    caution_reason: str | None = None

    def as_prompt_facts(self) -> dict[str, object]:
        return {
            "state": self.state,
            "root_kind": self.root_kind,
            "path_risk": self.path_risk,
            "adoption_marker": self.adoption_marker,
            "herdr_available": self.herdr_available,
            "caution_reason": self.caution_reason,
        }


@dataclass(frozen=True)
class Explain:
    """A display-only conversational turn (the model asks or explains)."""

    text: str


@dataclass(frozen=True)
class IntentCandidate:
    """A proposed intent mapping the loop validates against the closed schema."""

    intent: Mapping[str, object]


#: The closed set of turns a provider may return. Anything else is a protocol
#: violation the binding must surface as a provider error, not a mutation.
ConversationTurn = Union[Explain, IntentCandidate]


#: A transcript turn the provider is shown. ``role`` is ``human`` or
#: ``assistant``; the transcript lives only in memory (never repo/ticket).
ROLE_HUMAN = "human"
ROLE_ASSISTANT = "assistant"


@dataclass(frozen=True)
class ConversationContext:
    """Everything a provider is handed for one turn — nothing more.

    ``messages`` is the in-memory transcript (the human's utterances and the
    model's prior replies) the provider reasons over; because a safe binding is
    stateless per call (no session persistence), the driver replays it each turn.
    ``errors`` accumulates the structured rejections from prior turns so the
    provider can correct itself without the loop ever mutating on a bad turn.
    """

    facts: SanitizedFacts
    intent_schema: Mapping[str, object]
    tool_schema: Sequence[Mapping[str, object]]
    messages: tuple[Mapping[str, str], ...] = ()
    errors: tuple[Mapping[str, object], ...] = ()
    notes: tuple[str, ...] = ()

    def with_human(self, text: str) -> "ConversationContext":
        return replace(
            self, messages=self.messages + ({"role": ROLE_HUMAN, "text": text},)
        )

    def with_assistant(self, text: str) -> "ConversationContext":
        return replace(
            self, messages=self.messages + ({"role": ROLE_ASSISTANT, "text": text},)
        )

    def with_error(self, error: Mapping[str, object]) -> "ConversationContext":
        return replace(self, errors=self.errors + (dict(error),))

    def with_note(self, note: str) -> "ConversationContext":
        return replace(self, notes=self.notes + (note,))


@runtime_checkable
class ConversationProvider(Protocol):
    """A provider-neutral conversation binding.

    A binding maps one :class:`ConversationContext` to one
    :class:`ConversationTurn`. It must never mutate the filesystem, run a tool,
    or persist a transcript. On any inability to operate safely it raises
    :class:`ConversationProviderError` (fail-closed), it does not fabricate an
    intent.
    """

    def converse(self, context: ConversationContext) -> ConversationTurn: ...


class ConversationProviderError(Exception):
    """A structured, fail-closed conversation-provider failure (never mutates)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_record(self) -> dict[str, object]:
        return {"error": self.code, "message": self.message}


def build_intent_schema() -> dict[str, object]:
    """Project the closed ``OnboardingIntent`` schema for the model prompt.

    Enumerates the fixed keys and their allow-sets so the provider is told the
    exact closed shape it must produce — and nothing outside it is accepted by
    :func:`~..domain.intent.validate_onboarding_intent`.
    """
    return {
        "schema_version": ONBOARDING_INTENT_SCHEMA_VERSION,
        "required_keys": [
            "schema_version",
            "action",
            "preset",
            "backend",
            "git_mode",
            "rules_store",
            "free_text_summary",
        ],
        "enums": {
            "action": sorted(INTENT_ACTIONS),
            "preset": sorted(INTENT_PRESETS),
            "backend": sorted(INTENT_BACKENDS),
            "git_mode": sorted(INTENT_GIT_MODES),
            "rules_store": sorted(INTENT_RULES_STORES),
        },
        "notes": [
            "free_text_summary is display-only and never used as mutation input",
            "emit no key outside required_keys; unknown keys/enums are rejected",
            "you have no shell / file / network / YAML / MCP tool authority",
        ],
    }


def build_intent_json_schema() -> dict[str, object]:
    """JSON Schema for the closed ``OnboardingIntent`` object (generation-time).

    Fed into a provider that supports structured-output constraint (e.g. the
    Claude CLI ``--json-schema``) so the model *cannot* generate a key or enum
    value outside the closed schema. It mirrors — and never widens —
    :func:`~..domain.intent.validate_onboarding_intent`, which stays the single
    fail-closed authority after generation.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "action",
            "preset",
            "backend",
            "git_mode",
            "rules_store",
            "free_text_summary",
        ],
        "properties": {
            "schema_version": {"const": ONBOARDING_INTENT_SCHEMA_VERSION},
            "action": {"enum": sorted(INTENT_ACTIONS)},
            "preset": {"enum": sorted(INTENT_PRESETS)},
            "backend": {"enum": sorted(INTENT_BACKENDS)},
            "git_mode": {"enum": sorted(INTENT_GIT_MODES)},
            "rules_store": {"enum": sorted(INTENT_RULES_STORES)},
            "free_text_summary": {"type": "string"},
        },
    }


def build_turn_json_schema() -> dict[str, object]:
    """JSON Schema for the closed :data:`ConversationTurn` envelope (generation-time).

    Constrains the provider to emit exactly one closed turn — an ``explain`` text
    or an ``intent`` object matching :func:`build_intent_json_schema` — so an
    out-of-band tool call, free prose, or an extra key is refused at generation
    rather than only at parse time.
    """
    return {
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["turn", "text"],
                "properties": {
                    "turn": {"const": "explain"},
                    "text": {"type": "string"},
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["turn", "intent"],
                "properties": {
                    "turn": {"const": "intent"},
                    "intent": build_intent_json_schema(),
                },
            },
        ]
    }


def build_tool_schema() -> list[dict[str, object]]:
    """Project the *closed* deterministic tool surface the model may reason about.

    Names + mutation levels only — the model can neither call these nor pass
    anything but a closed ``OnboardingIntent`` to the orchestrator.
    """
    return [
        {"name": "onboarding.inspect", "mutation": "none", "actor": "orchestrator"},
        {"name": "onboarding.plan", "mutation": "none", "actor": "orchestrator"},
        {"name": "onboarding.apply", "mutation": "bounded", "actor": "orchestrator"},
        {"name": "onboarding.resume", "mutation": "one_step", "actor": "orchestrator"},
    ]


def sanitize_facts(preflight, *, caution_reason: str | None = None) -> SanitizedFacts:
    """Redact an :class:`~..domain.preflight.OnboardingPreflight` for the model.

    Only closed classifications survive; the herdr realpath, file hashes,
    canonical path, and free-form notes are dropped. ``caution_reason`` is a
    fixed, path-free reason string chosen by the caller (never the raw notes).
    """
    herdr = getattr(preflight, "herdr_binary", None)
    herdr_available = bool(getattr(herdr, "state", None) == "resolved")
    return SanitizedFacts(
        state=preflight.state,
        root_kind=preflight.root_kind,
        path_risk=preflight.path_risk,
        adoption_marker=preflight.adoption_marker,
        herdr_available=herdr_available,
        caution_reason=caution_reason,
    )
