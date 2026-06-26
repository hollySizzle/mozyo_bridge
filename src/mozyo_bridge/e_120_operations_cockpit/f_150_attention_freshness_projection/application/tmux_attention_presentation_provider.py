"""Built-in tmux attention presentation provider (Redmine #12156).

The first — and, per the adapter-boundary design (Redmine #12001), for v0.8 the
*only* — concrete
:class:`~mozyo_bridge.e_140_adapter_provider.f_140_presentation_provider.domain.presentation_adapter.PresentationProvider`. It is
the "Candidate 2" presentation slice, kept read / projection-first: it converts
an already-derived :class:`~mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.domain.attention.AttentionRecord` into a
normalized :class:`~mozyo_bridge.e_140_adapter_provider.f_140_presentation_provider.domain.presentation_adapter.SurfaceProjection`
for the ``tmux_user_option`` surface.

It lives in the application layer next to :mod:`attention_projection` because it
is pure plan/record building — no tmux is executed here — and it reuses that
module's pane user-option names (``@mozyo_attention_*``) as the single source of
truth, so the classified projection and the executed
``build_attention_option_plan`` cannot drift apart. The argv mechanics
(``set-option -p -t <pane> ...``) stay in :mod:`attention_projection`; this
provider only produces the normalized, pane-agnostic record.

What this provider deliberately does **not** do, because core owns it:

- it defines no workflow truth, owner approval, or routing authority — the
  projection is display only, and the option values are a re-derivable cache the
  design doc pins as never consulted for routing / handoff preflight;
- it invents no surface — ``tmux_user_option`` is a core-recognized surface;
- it performs no tmux I/O; the projection is pure over the supplied record.

There is no dynamic provider loading and no public plugin contract; this is a
built-in classification, not an extension point.
"""

from __future__ import annotations

from mozyo_bridge.application.attention_projection import (
    ATTENTION_REASON_OPTION,
    ATTENTION_SEVERITY_OPTION,
    ATTENTION_STATE_OPTION,
    ATTENTION_UPDATED_AT_OPTION,
)
from mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.domain.attention import AttentionRecord
from mozyo_bridge.e_140_adapter_provider.f_140_presentation_provider.domain.presentation_adapter import (
    SURFACE_TMUX_USER_OPTION,
    ProjectionField,
    SurfaceProjection,
)

PROVIDER_NAME = "tmux-presentation"


class TmuxAttentionPresentationProvider:
    """Project a core :class:`AttentionRecord` onto tmux pane user options."""

    name = PROVIDER_NAME
    surface = SURFACE_TMUX_USER_OPTION

    def project(self, record: AttentionRecord) -> SurfaceProjection:
        """Normalize one :class:`AttentionRecord` into a :class:`SurfaceProjection`.

        The field keys are the canonical ``@mozyo_attention_*`` option names from
        :mod:`attention_projection`; the values come straight off the derived
        record. ``source_unit_id`` carries the record's unit for provenance only
        — it is never used to pick a target. Pure; no tmux, no I/O.
        """
        fields = (
            ProjectionField(ATTENTION_STATE_OPTION, record.attention_state),
            ProjectionField(ATTENTION_SEVERITY_OPTION, record.severity),
            ProjectionField(ATTENTION_REASON_OPTION, record.reason_code),
            ProjectionField(ATTENTION_UPDATED_AT_OPTION, record.observed_at or ""),
        )
        return SurfaceProjection(
            provider=self.name,
            surface=self.surface,
            source_unit_id=record.unit_id,
            fields=fields,
        )


# Stateless singleton; the provider holds no per-call state.
TMUX_ATTENTION_PRESENTATION_PROVIDER = TmuxAttentionPresentationProvider()


__all__ = (
    "PROVIDER_NAME",
    "TMUX_ATTENTION_PRESENTATION_PROVIDER",
    "TmuxAttentionPresentationProvider",
)
