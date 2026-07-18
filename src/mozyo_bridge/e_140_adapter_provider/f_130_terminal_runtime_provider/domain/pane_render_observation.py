"""Core-owned typed composer-render observation vocabulary (Redmine #14065).

#14064 proved that the current supported terminal adapter observation surface —
``agent read`` plain text (``PaneReadResult(content, truncated)``) plus the
``agent get/list`` idle/busy status — cannot tell a provider idle-placeholder
"ghost" (composer hint text) from an *exact-same-text* real unsent input: both
render byte-identical plain text (#14064 blocker j#82150 ``insufficient_observation``).
Emptying the composer on a text-only match would lose real unsent input, so
#13846 stale-worker drain stayed fail-closed / preserved.

This module is the **measurement instrument** approved by #14065 Design Answer
j#82160 (Plan A, phase 1). It carries ONLY the core-owned *closed* vocabulary and
the typed, redacted observation record a provider produces when it reads a pane's
*rendered style* (an ``agent read --format ansi`` capability, negotiated inside
the herdr adapter). Phase 1 deliberately stops at *measuring* a positive
discriminator: the provider profile schema v3, the placeholder matcher, and any
``has_pending=False`` empty-classification remain **prohibited** until live
positive evidence is green (Design Answer "Phase 2 admission rule").

Redaction is the whole point (Design Answer scope item 3, IR acceptance #3/#4):
:class:`PaneRenderObservation` exposes **no** body, hash, length, excerpt, or raw
ANSI — exactly like :class:`~...f_140...sublane_quarantine.ComposerObservation`.
It carries only closed enums and a bool, so an observation can never smuggle live
pane text into a durable record, a log, or a test failure. The style hypothesis
(a ghost placeholder is rendered *dim/faint*, real typed input *normal*) is a
measured signal here, never a decision — the decision layer does not exist yet.

Fail-closed vocabulary (Implementation Guardrail #4 of the adapter-boundary
design, ``vibes/docs/logics/plugin-ready-adapter-boundary.md``): an unsupported
``--format ansi`` flag, an absent ANSI stream, an unreadable read, an unknown
provider, an ambiguous/contradictory render, or an empty / composer-less pane all
resolve to ``readable=False`` with a specific closed ``reason`` and
``style_provenance=unknown`` — never a silent text fallback that would fabricate a
positive signal.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- style provenance of the composer body (core-owned closed set) -----------
# The rendered intensity a provider reports for the composer prompt body under
# ``agent read --format ansi``. The measured hypothesis (Design Answer j#82160):
# a ghost idle-placeholder is drawn *dim* (SGR faint / bright-black gray), a real
# unsent input is drawn *normal*. ``mixed`` is a body carrying both intensities;
# ``unknown`` is the fail-closed value whenever no confident classification exists.
STYLE_PROVENANCE_DIM = "dim"
STYLE_PROVENANCE_NORMAL = "normal"
STYLE_PROVENANCE_MIXED = "mixed"
STYLE_PROVENANCE_UNKNOWN = "unknown"

STYLE_PROVENANCES: frozenset[str] = frozenset(
    {
        STYLE_PROVENANCE_DIM,
        STYLE_PROVENANCE_NORMAL,
        STYLE_PROVENANCE_MIXED,
        STYLE_PROVENANCE_UNKNOWN,
    }
)

# --- render cursor relation (core-owned closed set) --------------------------
# Where the render cursor sits relative to the composer prompt line, when the
# provider payload reports a cursor position. ``composer`` = cursor on the
# composer prompt line; ``elsewhere`` = cursor on another line; ``absent`` = the
# payload explicitly reports no cursor; ``unknown`` = the payload carries no
# cursor information (the fail-closed default — a snapshot read need not report a
# live cursor).
CURSOR_RELATION_COMPOSER = "composer"
CURSOR_RELATION_ELSEWHERE = "elsewhere"
CURSOR_RELATION_ABSENT = "absent"
CURSOR_RELATION_UNKNOWN = "unknown"

CURSOR_RELATIONS: frozenset[str] = frozenset(
    {
        CURSOR_RELATION_COMPOSER,
        CURSOR_RELATION_ELSEWHERE,
        CURSOR_RELATION_ABSENT,
        CURSOR_RELATION_UNKNOWN,
    }
)

# --- render observation reason (core-owned closed set) -----------------------
# Exactly one of these labels every observation. ``ok`` is the only reason that
# accompanies ``readable=True`` (a classified ``dim`` / ``normal`` / ``mixed``
# provenance). Every other reason is a fail-closed ``readable=False`` outcome that
# a consumer must treat as *preserve* — never as an empty composer.
RENDER_REASON_OK = "ok"
#: The provider rejected ``--format ansi`` / ``--ansi`` (unknown-flag signature on
#: a non-zero exit): the supported binary has no ANSI render surface.
RENDER_REASON_ANSI_UNSUPPORTED = "ansi_unsupported"
#: The read succeeded but the payload carried no ANSI style stream (only plain
#: text): the capability was not exercised, so no style could be measured.
RENDER_REASON_ANSI_ABSENT = "ansi_absent"
#: The ANSI stream carried no composer prompt line to classify.
RENDER_REASON_NO_COMPOSER = "no_composer"
#: A composer prompt line was present but its body was empty (a genuinely idle,
#: empty composer): nothing to distinguish ghost from real.
RENDER_REASON_EMPTY_COMPOSER = "empty_composer"
#: A transport / spawn / exit / payload failure prevented any read.
RENDER_REASON_UNREADABLE = "unreadable"
#: The target handle or read source was malformed (fail-closed before any spawn).
RENDER_REASON_INVALID_TARGET = "invalid_target"

RENDER_OBSERVATION_REASONS: frozenset[str] = frozenset(
    {
        RENDER_REASON_OK,
        RENDER_REASON_ANSI_UNSUPPORTED,
        RENDER_REASON_ANSI_ABSENT,
        RENDER_REASON_NO_COMPOSER,
        RENDER_REASON_EMPTY_COMPOSER,
        RENDER_REASON_UNREADABLE,
        RENDER_REASON_INVALID_TARGET,
    }
)

#: The reasons that may accompany ``readable=True``. Exactly one — ``ok`` — a
#: positive, classified render observation. Every other reason is fail-closed.
_READABLE_REASONS: frozenset[str] = frozenset({RENDER_REASON_OK})


class PaneRenderObservationError(ValueError):
    """A :class:`PaneRenderObservation` violates the closed-vocabulary contract.

    ``ValueError`` for fail-closed semantics, matching the sibling adapter-boundary
    errors (:class:`~...terminal_transport.TerminalTransportError`).
    """


@dataclass(frozen=True)
class PaneRenderObservation:
    """A typed, redacted composer-render observation — closed enums / bool only.

    Deliberately carries **no** body, hash, length, excerpt, or raw ANSI — the
    same non-exposure invariant as ``ComposerObservation`` (#14064), so an
    observation can never persist live pane text. ``readable`` is the sole
    authority on whether a positive style provenance was measured: it is ``True``
    *iff* ``reason == "ok"``, and then ``style_provenance`` is one of ``dim`` /
    ``normal`` / ``mixed``. On any fail-closed outcome ``readable`` is ``False``,
    ``style_provenance`` is ``unknown``, and ``reason`` names the specific cause —
    a consumer treats every ``readable=False`` observation as *preserve*.

    ``prompt_present`` records whether a composer prompt line was found at all (a
    weak, body-free structural fact that stays useful even when the body could not
    be classified), and ``cursor_relation`` records the render-cursor position
    relative to the composer when the payload reports one.
    """

    readable: bool
    style_provenance: str = STYLE_PROVENANCE_UNKNOWN
    cursor_relation: str = CURSOR_RELATION_UNKNOWN
    reason: str = RENDER_REASON_UNREADABLE
    prompt_present: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.readable, bool):
            raise PaneRenderObservationError(
                f"render observation 'readable' must be a bool, got {self.readable!r}"
            )
        if not isinstance(self.prompt_present, bool):
            raise PaneRenderObservationError(
                f"render observation 'prompt_present' must be a bool, got "
                f"{self.prompt_present!r}"
            )
        if self.style_provenance not in STYLE_PROVENANCES:
            raise PaneRenderObservationError(
                f"unknown style_provenance {self.style_provenance!r}; expected one of "
                f"{sorted(STYLE_PROVENANCES)}"
            )
        if self.cursor_relation not in CURSOR_RELATIONS:
            raise PaneRenderObservationError(
                f"unknown cursor_relation {self.cursor_relation!r}; expected one of "
                f"{sorted(CURSOR_RELATIONS)}"
            )
        if self.reason not in RENDER_OBSERVATION_REASONS:
            raise PaneRenderObservationError(
                f"unknown render reason {self.reason!r}; expected one of "
                f"{sorted(RENDER_OBSERVATION_REASONS)}"
            )
        # readable <=> reason is the single readable reason ('ok'); and a readable
        # observation must carry a concrete (non-unknown) provenance. This is the
        # fail-closed invariant: no observation can be 'readable but unknown' or
        # 'a failure that still claims ok'.
        if self.readable and self.reason not in _READABLE_REASONS:
            raise PaneRenderObservationError(
                f"a readable render observation must carry reason 'ok', got "
                f"{self.reason!r}"
            )
        if not self.readable and self.reason in _READABLE_REASONS:
            raise PaneRenderObservationError(
                "an unreadable render observation may not carry reason 'ok'"
            )
        if self.readable and self.style_provenance == STYLE_PROVENANCE_UNKNOWN:
            raise PaneRenderObservationError(
                "a readable render observation must carry a concrete style_provenance "
                "(dim / normal / mixed), never 'unknown'"
            )

    @classmethod
    def classified(
        cls,
        style_provenance: str,
        *,
        cursor_relation: str = CURSOR_RELATION_UNKNOWN,
    ) -> "PaneRenderObservation":
        """A readable, positively-classified observation (``reason='ok'``)."""
        return cls(
            readable=True,
            style_provenance=style_provenance,
            cursor_relation=cursor_relation,
            reason=RENDER_REASON_OK,
            prompt_present=True,
        )

    @classmethod
    def failed(
        cls, reason: str, *, prompt_present: bool = False
    ) -> "PaneRenderObservation":
        """A fail-closed observation: ``readable=False`` with a specific reason."""
        return cls(
            readable=False,
            style_provenance=STYLE_PROVENANCE_UNKNOWN,
            cursor_relation=CURSOR_RELATION_UNKNOWN,
            reason=reason,
            prompt_present=prompt_present,
        )

    def to_record(self) -> dict:
        """A JSON-serializable, fully-redacted dict (the ``--json`` diagnostic shape)."""
        return {
            "readable": self.readable,
            "style_provenance": self.style_provenance,
            "cursor_relation": self.cursor_relation,
            "reason": self.reason,
            "prompt_present": self.prompt_present,
        }


__all__ = (
    "CURSOR_RELATIONS",
    "CURSOR_RELATION_ABSENT",
    "CURSOR_RELATION_COMPOSER",
    "CURSOR_RELATION_ELSEWHERE",
    "CURSOR_RELATION_UNKNOWN",
    "PaneRenderObservation",
    "PaneRenderObservationError",
    "RENDER_OBSERVATION_REASONS",
    "RENDER_REASON_ANSI_ABSENT",
    "RENDER_REASON_ANSI_UNSUPPORTED",
    "RENDER_REASON_EMPTY_COMPOSER",
    "RENDER_REASON_INVALID_TARGET",
    "RENDER_REASON_NO_COMPOSER",
    "RENDER_REASON_OK",
    "RENDER_REASON_UNREADABLE",
    "STYLE_PROVENANCES",
    "STYLE_PROVENANCE_DIM",
    "STYLE_PROVENANCE_MIXED",
    "STYLE_PROVENANCE_NORMAL",
    "STYLE_PROVENANCE_UNKNOWN",
)
