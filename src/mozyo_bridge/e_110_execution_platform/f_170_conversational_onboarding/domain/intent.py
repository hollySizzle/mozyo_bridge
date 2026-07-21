"""Closed ``OnboardingIntent`` schema + validation (Redmine #13498 / #13501).

The conversation actor's *only* output is an ``OnboardingIntent``: a closed,
fixed-key record the model fills in to describe what the human wants. It is the
one bridge between free-form language and the deterministic tools, so it is
validated fail-closed:

- unknown keys, unknown enum values, missing required fields → structured error;
- ``free_text_summary`` is display-only and is never fed into any mutation;
- any model-supplied shell command, file content, or credential-shaped value in
  a field that should be a closed enum → rejected.

The validator returns either a typed :class:`OnboardingIntent` or an
:class:`IntentError` carrying a machine-readable ``code`` and the offending
field, so the conversation layer can render a structured error and ask again.
``undecided`` preset is accepted (the conversation may keep asking) but the plan
builder refuses to plan an undecided preset — that gate lives in the planner.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping

__all__ = (
    "ONBOARDING_INTENT_SCHEMA_VERSION",
    "INTENT_ACTIONS",
    "INTENT_PRESETS",
    "INTENT_BACKENDS",
    "INTENT_GIT_MODES",
    "INTENT_RULES_STORES",
    "PRESET_UNDECIDED",
    "GIT_MODE_INITIALIZE",
    "OnboardingIntent",
    "IntentError",
    "validate_onboarding_intent",
)

ONBOARDING_INTENT_SCHEMA_VERSION = 1

INTENT_ACTIONS: frozenset[str] = frozenset(
    {"explain", "propose", "confirm_plan", "revise", "cancel"}
)
PRESET_UNDECIDED = "undecided"
# Preset enum uses the underscore spelling in the closed conversation schema;
# the planner maps it to the hyphenated scaffold preset name. ``undecided`` is a
# conversation-only value (no scaffold preset) that permits further questions.
INTENT_PRESETS: frozenset[str] = frozenset(
    {
        "none",
        "asana",
        "redmine",
        "redmine_governed",
        "redmine_rails",
        "redmine_rails_governed",
        PRESET_UNDECIDED,
    }
)
INTENT_BACKENDS: frozenset[str] = frozenset({"herdr"})
GIT_MODE_INITIALIZE = "initialize"
INTENT_GIT_MODES: frozenset[str] = frozenset({"existing", "none", GIT_MODE_INITIALIZE})
INTENT_RULES_STORES: frozenset[str] = frozenset({"central", "repo_local"})

# The closed key set. Any other key in the record fails closed.
_REQUIRED_KEYS: tuple[str, ...] = (
    "schema_version",
    "action",
    "preset",
    "backend",
    "git_mode",
    "rules_store",
    "free_text_summary",
)
_ALLOWED_KEYS: frozenset[str] = frozenset(_REQUIRED_KEYS)

# A field that must be a closed enum must not smuggle a shell command, file
# content, or credential-shaped value. These are cheap, conservative shape
# guards: enum fields are matched exactly against their allow-set anyway, but
# the guard produces a specific ``code`` (``field_shaped_like_injection``) so a
# model that tries to pass e.g. ``"none; rm -rf /"`` gets an unambiguous reject
# rather than a generic ``unknown_enum``.
_INJECTION_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"[;&|`$]"),  # shell metacharacters
    re.compile(r"\n"),  # multi-line / embedded content
    re.compile(r"\b(rm|curl|wget|sudo|chmod|eval|exec)\b", re.IGNORECASE),
    re.compile(r"(?i)(secret|token|password|api[_-]?key)\s*[:=]"),
    re.compile(r"-----BEGIN [A-Z ]+-----"),  # PEM key block
)

_ENUM_FIELDS: dict[str, frozenset[str]] = {
    "action": INTENT_ACTIONS,
    "preset": INTENT_PRESETS,
    "backend": INTENT_BACKENDS,
    "git_mode": INTENT_GIT_MODES,
    "rules_store": INTENT_RULES_STORES,
}


@dataclass(frozen=True)
class OnboardingIntent:
    """A validated, closed onboarding intent (never carries free mutation input)."""

    action: str
    preset: str
    backend: str
    git_mode: str
    rules_store: str
    free_text_summary: str
    schema_version: int = ONBOARDING_INTENT_SCHEMA_VERSION

    @property
    def preset_undecided(self) -> bool:
        return self.preset == PRESET_UNDECIDED


@dataclass(frozen=True)
class IntentError(Exception):
    """A structured validation error the conversation layer can render + retry."""

    code: str
    message: str
    field: str | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        where = f" (field={self.field})" if self.field else ""
        return f"[{self.code}]{where} {self.message}"

    def as_record(self) -> dict[str, object]:
        return {"error": self.code, "message": self.message, "field": self.field}


def _reject_injection(name: str, value: str) -> None:
    for pattern in _INJECTION_SHAPES:
        if pattern.search(value):
            raise IntentError(
                code="field_shaped_like_injection",
                message=(
                    f"{name} must be a closed enum value, not a shell command / "
                    "file content / credential-shaped string"
                ),
                field=name,
            )


def validate_onboarding_intent(record: Mapping[str, object]) -> OnboardingIntent:
    """Validate a raw intent mapping into a typed :class:`OnboardingIntent`.

    Fails closed with :class:`IntentError` on: a non-mapping record, unknown or
    missing keys, an unsupported ``schema_version``, a non-string enum field, an
    enum value outside its allow-set, or an enum field shaped like an injection.
    """
    if not isinstance(record, Mapping):
        raise IntentError(
            code="not_a_mapping",
            message="OnboardingIntent must be a mapping of the closed schema keys",
        )

    unknown = [key for key in record if key not in _ALLOWED_KEYS]
    if unknown:
        raise IntentError(
            code="unknown_key",
            message=f"unknown OnboardingIntent key(s): {sorted(map(str, unknown))}",
            field=str(sorted(map(str, unknown))[0]),
        )

    missing = [key for key in _REQUIRED_KEYS if key not in record]
    if missing:
        raise IntentError(
            code="missing_field",
            message=f"missing required OnboardingIntent field(s): {missing}",
            field=missing[0],
        )

    version = record["schema_version"]
    if version != ONBOARDING_INTENT_SCHEMA_VERSION:
        raise IntentError(
            code="unsupported_schema_version",
            message=(
                f"OnboardingIntent schema_version {version!r} is not supported "
                f"(expected {ONBOARDING_INTENT_SCHEMA_VERSION})"
            ),
            field="schema_version",
        )

    for name, allowed in _ENUM_FIELDS.items():
        value = record[name]
        if not isinstance(value, str):
            raise IntentError(
                code="non_string_enum",
                message=f"{name} must be a string enum value, got {type(value).__name__}",
                field=name,
            )
        _reject_injection(name, value)
        if value not in allowed:
            raise IntentError(
                code="unknown_enum",
                message=f"{name}={value!r} is not one of {sorted(allowed)}",
                field=name,
            )

    summary = record["free_text_summary"]
    if not isinstance(summary, str):
        raise IntentError(
            code="non_string_summary",
            message="free_text_summary must be a string (display-only)",
            field="free_text_summary",
        )

    return OnboardingIntent(
        schema_version=ONBOARDING_INTENT_SCHEMA_VERSION,
        action=str(record["action"]),
        preset=str(record["preset"]),
        backend=str(record["backend"]),
        git_mode=str(record["git_mode"]),
        rules_store=str(record["rules_store"]),
        free_text_summary=summary,
    )
