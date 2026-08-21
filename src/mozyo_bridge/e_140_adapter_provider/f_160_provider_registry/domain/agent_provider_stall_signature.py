"""Provider stall-signature schema + packaged load (Redmine #15843).

The validated read of ``agent_provider_stall_signatures.yaml`` — the post-turn runtime
screens a provider renders when a started turn stops progressing. The data file's header
carries the full rationale for why this is a separate registry from
``agent_provider_profiles.yaml`` rather than another field on it; the short form is that
``startup_blockers`` feeds a zero-send refusal gate and this feeds a present-only watcher,
and the two must not share a widening surface.

Two invariants are enforced here rather than left to convention, because both are the
kind of rule that decays into a comment nobody re-reads:

- **an unrendered signature cannot assert a destructive class.** ``evidence:
  binary_read_unrendered`` is only admissible for the classes in
  :data:`UNRENDERED_ADMISSIBLE_CLASSES`, whose prescription is the same non-destructive
  patience the no-match case already receives. Promoting such a literal to, say,
  ``content_refusal`` — whose remedy discards a live session's context — would then be a
  one-line data edit, so the schema refuses it.
- **``unsent_composer`` is not declarable at all.** That class is established by composer
  evidence against the dispatched body (#15842), never by a substring on a screen. A data
  file that could assert it would invite exactly the guess #15842 was raised to remove.

Like the sibling leaf schema modules (``agent_provider_startup_blocker``,
``agent_provider_ghost_composer_signal``), this stays self-contained so the oversized
``agent_provider_profile_config`` gains nothing — here it gains not even wiring, since
this registry is loaded independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from types import MappingProxyType
from typing import Mapping, Sequence

import yaml

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_disposition import (  # noqa: E501
    CLASS_UNSENT_COMPOSER,
    EVIDENCE_BINARY_READ_UNRENDERED,
    EVIDENCE_TIERS,
    STALL_CLASSES,
    UNRENDERED_ADMISSIBLE_CLASSES,
)

#: Package-anchored resource name (never a cwd / worktree path walk, so a hostile repo
#: checkout cannot shadow the built-in signatures).
AGENT_PROVIDER_STALL_SIGNATURE_RESOURCE = "agent_provider_stall_signatures.yaml"

#: Only shape this loader understands. An unknown version fails closed rather than being
#: read on a guessed shape.
SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1"})

#: Classes a data file may never assert, whatever its evidence tier.
UNDECLARABLE_CLASSES: frozenset[str] = frozenset({CLASS_UNSENT_COMPOSER})

#: Guardrail on the AND-list. A signature with no substrings would match every screen;
#: an unbounded one is a copy of a screen rather than a signature.
MIN_SIGNATURE_SUBSTRINGS = 1
MAX_SIGNATURE_SUBSTRINGS = 6


class StallSignatureError(ValueError):
    """Raised when the packaged stall-signature artifact is malformed."""


@dataclass(frozen=True)
class StallSignature:
    """One AND of substrings that, co-located on a single screen, assert one class."""

    signature_id: str
    asserts: str
    evidence: str
    all_of: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.signature_id:
            raise StallSignatureError("stall signature requires a non-empty id")
        if self.asserts not in STALL_CLASSES:
            raise StallSignatureError(
                f"stall signature {self.signature_id!r} asserts unknown class "
                f"{self.asserts!r}"
            )
        if self.asserts in UNDECLARABLE_CLASSES:
            raise StallSignatureError(
                f"stall signature {self.signature_id!r} asserts {self.asserts!r}, which "
                f"is established by composer evidence against the dispatched body "
                f"(#15842) and is never declarable as a screen substring"
            )
        if self.evidence not in EVIDENCE_TIERS:
            raise StallSignatureError(
                f"stall signature {self.signature_id!r} declares unknown evidence tier "
                f"{self.evidence!r}"
            )
        if (
            self.evidence == EVIDENCE_BINARY_READ_UNRENDERED
            and self.asserts not in UNRENDERED_ADMISSIBLE_CLASSES
        ):
            raise StallSignatureError(
                f"stall signature {self.signature_id!r} asserts {self.asserts!r} on "
                f"{EVIDENCE_BINARY_READ_UNRENDERED!r} evidence; that tier may only assert "
                f"{sorted(UNRENDERED_ADMISSIBLE_CLASSES)}"
            )
        if not MIN_SIGNATURE_SUBSTRINGS <= len(self.all_of) <= MAX_SIGNATURE_SUBSTRINGS:
            raise StallSignatureError(
                f"stall signature {self.signature_id!r} declares {len(self.all_of)} "
                f"substrings; the bound is {MIN_SIGNATURE_SUBSTRINGS}.."
                f"{MAX_SIGNATURE_SUBSTRINGS}"
            )
        for substring in self.all_of:
            if not isinstance(substring, str) or not substring.strip():
                raise StallSignatureError(
                    f"stall signature {self.signature_id!r} declares an empty substring"
                )

    def matches(self, screen: str) -> bool:
        """True when every declared substring is present on this one screen."""
        return all(substring in screen for substring in self.all_of)


@dataclass(frozen=True)
class StallSignatureRegistry:
    """All providers' declared stall signatures, keyed by provider id.

    A provider absent from the registry, or present with an empty list, declares no
    signature — and that is the fail-safe default, not a defect. It means the classifier
    falls through to the indeterminate class, whose prescription is patience.
    """

    schema_version: str
    signatures: Mapping[str, tuple[StallSignature, ...]]

    def for_provider(self, provider_id: str) -> tuple[StallSignature, ...]:
        return self.signatures.get(provider_id, ())

    @classmethod
    def from_record(cls, record: object) -> "StallSignatureRegistry":
        if not isinstance(record, dict):
            raise StallSignatureError(
                "stall signature config must be a mapping at the top level"
            )
        version = record.get("version")
        if not isinstance(version, str) or version not in SUPPORTED_SCHEMA_VERSIONS:
            raise StallSignatureError(
                f"stall signature config version {version!r} is not one of "
                f"{sorted(SUPPORTED_SCHEMA_VERSIONS)}"
            )
        providers = record.get("providers") or {}
        if not isinstance(providers, dict):
            raise StallSignatureError("stall signature 'providers' must be a mapping")

        parsed: dict[str, tuple[StallSignature, ...]] = {}
        for provider_id, block in providers.items():
            if not isinstance(provider_id, str) or not provider_id:
                raise StallSignatureError("stall signature provider id must be a string")
            if not isinstance(block, dict):
                raise StallSignatureError(
                    f"stall signature block for {provider_id!r} must be a mapping"
                )
            entries = block.get("stall_signatures") or []
            parsed[provider_id] = _parse_signatures(provider_id, entries)
        # Read-only view: the registry is a frozen dataclass, and a consumer that could
        # add a provider's signatures at runtime would sidestep every load-time rule
        # above — including the evidence-tier gate.
        return cls(schema_version=version, signatures=MappingProxyType(parsed))


def _parse_signatures(provider_id: str, entries: object) -> tuple[StallSignature, ...]:
    if not isinstance(entries, list):
        raise StallSignatureError(
            f"stall_signatures for {provider_id!r} must be a list"
        )
    seen: set[str] = set()
    parsed: list[StallSignature] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise StallSignatureError(
                f"stall signature entry for {provider_id!r} must be a mapping"
            )
        signature_id = entry.get("id")
        if not isinstance(signature_id, str):
            raise StallSignatureError(
                f"stall signature entry for {provider_id!r} requires a string id"
            )
        if signature_id in seen:
            raise StallSignatureError(
                f"stall signature id {signature_id!r} is declared twice for "
                f"{provider_id!r}"
            )
        seen.add(signature_id)
        all_of = entry.get("all_of")
        if not isinstance(all_of, list):
            raise StallSignatureError(
                f"stall signature {signature_id!r} requires an 'all_of' list"
            )
        parsed.append(
            StallSignature(
                signature_id=signature_id,
                asserts=str(entry.get("asserts", "")),
                evidence=str(entry.get("evidence", "")),
                all_of=tuple(all_of),
            )
        )
    return tuple(parsed)


def load_stall_signature_registry() -> StallSignatureRegistry:
    """Read + validate the wheel-packaged stall-signature artifact.

    Fails closed on a malformed artifact: a watcher that silently ran with no signatures
    would report every frozen screen as indeterminate and look like it was working.
    """
    text = (
        resources.files(__package__)
        .joinpath(AGENT_PROVIDER_STALL_SIGNATURE_RESOURCE)
        .read_text(encoding="utf-8")
    )
    try:
        record = yaml.safe_load(text)
    except yaml.YAMLError as exc:  # pragma: no cover - malformed packaged artifact
        raise StallSignatureError(
            f"packaged stall signatures ({AGENT_PROVIDER_STALL_SIGNATURE_RESOURCE}) "
            f"are not valid YAML: {exc}"
        ) from exc
    return StallSignatureRegistry.from_record(record)


def first_match(
    signatures: Sequence[StallSignature], screen: str
) -> "StallSignature | None":
    """The first declared signature every substring of which is on this screen."""
    for signature in signatures:
        if signature.matches(screen):
            return signature
    return None
