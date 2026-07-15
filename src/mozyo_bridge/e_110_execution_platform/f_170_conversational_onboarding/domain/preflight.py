"""Onboarding preflight assembly — the model-pre-launch hard gate (Redmine #13498).

Pure assembly of the ``OnboardingPreflight`` result from already-probed facts:
the :class:`~...domain.path_safety.PathSafety` classification, the resolved herdr
binary, whether an existing config was readable, and the on-disk receipt state.
Keeping it pure (no filesystem, no env) means the whole state matrix — adopted /
unadopted / adoption_in_progress / blocked / caution_requires_ack — is testable
without a real tree; the application layer does the probing and hands the facts
here.

The state precedence encodes the spec's hard-gate ordering:

1. a path hard block (home / ambiguous identity) → ``blocked`` — the model can
   never clear it;
2. an unreadable existing config or a broken receipt → ``blocked``;
3. a receipt recording work in progress → ``adoption_in_progress`` (bare
   ``mozyo`` reroutes to resume, never a normal launch);
4. a completed receipt, or a pre-existing adoption marker → ``adopted``;
5. a sync/cloud root that is not yet adopted → ``caution_requires_ack``;
6. otherwise → ``unadopted``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .path_safety import (
    ADOPTION_ABSENT,
    PathSafety,
)
from .receipt import RECEIPT_STATE_COMPLETE, RECEIPT_STATE_IN_PROGRESS

__all__ = (
    "STATE_ADOPTED",
    "STATE_UNADOPTED",
    "STATE_ADOPTION_IN_PROGRESS",
    "STATE_BLOCKED",
    "STATE_CAUTION_REQUIRES_ACK",
    "HERDR_RESOLVED",
    "HERDR_MISSING",
    "HERDR_AMBIGUOUS",
    "HERDR_SOURCE_ENV",
    "HERDR_SOURCE_PATH",
    "HERDR_SOURCE_NONE",
    "RECEIPT_STATE_NONE",
    "RECEIPT_STATE_BROKEN",
    "HerdrBinary",
    "OnboardingPreflight",
    "assemble_preflight",
)

STATE_ADOPTED = "adopted"
STATE_UNADOPTED = "unadopted"
STATE_ADOPTION_IN_PROGRESS = "adoption_in_progress"
STATE_BLOCKED = "blocked"
STATE_CAUTION_REQUIRES_ACK = "caution_requires_ack"

HERDR_RESOLVED = "resolved"
HERDR_MISSING = "missing"
HERDR_AMBIGUOUS = "ambiguous"
HERDR_SOURCE_ENV = "env"
HERDR_SOURCE_PATH = "path"
HERDR_SOURCE_NONE = "none"

# Receipt-state inputs to :func:`assemble_preflight` beyond the two on-disk
# receipt states: no receipt file at all, and a receipt that failed to parse.
RECEIPT_STATE_NONE = "none"
RECEIPT_STATE_BROKEN = "broken"


@dataclass(frozen=True)
class HerdrBinary:
    """Resolved herdr launch binary (from the trusted env / PATH, never config)."""

    state: str
    source: str
    path: str | None = None


@dataclass(frozen=True)
class OnboardingPreflight:
    """The closed model-pre-launch preflight result."""

    state: str
    root_kind: str
    path_risk: str
    adoption_marker: str
    herdr_binary: HerdrBinary
    hard_block_reasons: tuple[str, ...] = ()
    caution: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def is_hard_block(self) -> bool:
        return self.state == STATE_BLOCKED

    @property
    def requires_caution_ack(self) -> bool:
        return self.state == STATE_CAUTION_REQUIRES_ACK

    def as_record(self) -> dict[str, object]:
        """Machine-readable projection for ``onboarding inspect --json``."""
        return {
            "state": self.state,
            "root_kind": self.root_kind,
            "path_risk": self.path_risk,
            "adoption_marker": self.adoption_marker,
            "herdr_binary": {
                "state": self.herdr_binary.state,
                "source": self.herdr_binary.source,
                "path": self.herdr_binary.path,
            },
            "hard_block_reasons": list(self.hard_block_reasons),
            "caution": list(self.caution),
            "notes": list(self.notes),
        }


def assemble_preflight(
    safety: PathSafety,
    herdr_binary: HerdrBinary,
    *,
    receipt_state: str = RECEIPT_STATE_NONE,
    config_readable: bool = True,
) -> OnboardingPreflight:
    """Assemble the closed preflight result from probed facts.

    ``receipt_state`` is one of ``none`` / ``adoption_in_progress`` /
    ``complete`` / ``broken``. ``config_readable`` is ``False`` when an existing
    ``.mozyo-bridge/config.yaml`` is present but could not be read/parsed — a
    hard block (the spec refuses to plan over an unreadable existing config).
    """
    reasons: list[str] = []
    caution: list[str] = []
    notes: list[str] = list(safety.notes)

    # (1) path-level hard block: home / ambiguous identity.
    if safety.is_hard_block:
        reasons.extend(safety.notes or (f"path risk {safety.path_risk}",))

    # (2) unreadable existing config / broken receipt.
    if not config_readable:
        reasons.append(
            "existing .mozyo-bridge/config.yaml is present but unreadable; "
            "refusing to plan over an unreadable config"
        )
    if receipt_state == RECEIPT_STATE_BROKEN:
        reasons.append(
            "onboarding receipt is present but does not parse; refusing to "
            "resume from a broken receipt"
        )

    if reasons:
        state = STATE_BLOCKED
    elif receipt_state == RECEIPT_STATE_IN_PROGRESS:
        state = STATE_ADOPTION_IN_PROGRESS
    elif receipt_state == RECEIPT_STATE_COMPLETE:
        state = STATE_ADOPTED
    elif safety.adoption_marker != ADOPTION_ABSENT:
        # Pre-existing adoption (hand-written config / scaffold / registry
        # anchor) with no in-progress receipt is a fully adopted project.
        state = STATE_ADOPTED
    elif safety.requires_caution_ack:
        state = STATE_CAUTION_REQUIRES_ACK
        caution.extend(safety.notes or ("sync/cloud root requires human ack",))
    else:
        state = STATE_UNADOPTED

    return OnboardingPreflight(
        state=state,
        root_kind=safety.root_kind,
        path_risk=safety.path_risk,
        adoption_marker=safety.adoption_marker,
        herdr_binary=herdr_binary,
        hard_block_reasons=tuple(reasons),
        caution=tuple(caution),
        notes=tuple(notes),
    )
