"""Built-in text attention presentation provider (Redmine #12185).

The second built-in
:class:`~mozyo_bridge.e_140_adapter_provider.f_140_presentation_provider.domain.presentation_adapter.PresentationProvider`, added
after the tmux provider (Redmine #12156). Until now the ``text`` surface was
core-owned vocabulary with no provider; this module fills it with the smallest
possible projection-only implementation, kept read / projection-first exactly
like the tmux provider.

Like :mod:`tmux_attention_presentation_provider`, it converts an
already-derived :class:`~mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.domain.attention.AttentionRecord` into a
normalized :class:`~mozyo_bridge.e_140_adapter_provider.f_140_presentation_provider.domain.presentation_adapter.SurfaceProjection`,
but for the ``text`` surface: stable, human-readable label keys instead of tmux
``@mozyo_attention_*`` option names. The two providers project the *same* four
logical attention cells in the *same* order, so a text rendering and a tmux
projection of one record never disagree about which facts are shown.

It also exposes :func:`render_surface_text`, a pure renderer that turns *any*
:class:`SurfaceProjection` (tmux or text) into a deterministic ``key: value``
text block. That is the "text output" half of the design doc's presentation MVP
("tmux user options and text output"): given an existing projection, produce a
stable text rendering with no extra derivation.

What this provider deliberately does **not** do, because core owns it:

- it defines no workflow truth, owner approval, or routing authority — the
  projection and its rendering are display only;
- it invents no surface — ``text`` is a core-recognized surface;
- it performs no tmux / file / network I/O; both ``project`` and
  :func:`render_surface_text` are pure over their inputs.

There is no dynamic provider loading and no public plugin contract; this is a
built-in classification, not an extension point. To honour Redmine #12185's
non-goal of minimising #12184 merge risk, this module does **not** register the
provider in :mod:`provider_registry`; it stands on its own as a built-in
projection and adds no provider-registry surface.
"""

from __future__ import annotations

from mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.domain.attention import AttentionRecord
from mozyo_bridge.e_140_adapter_provider.f_140_presentation_provider.domain.presentation_adapter import (
    SURFACE_TEXT,
    ProjectionField,
    SurfaceProjection,
)

PROVIDER_NAME = "text-presentation"

# Stable, human-readable text-surface field labels. They are display labels, not
# the tmux option names and — critically — not core-owned authorities, so a
# text projection carries the same four attention cells as the tmux projection
# without ever asserting workflow / owner / close / routing truth. The order
# mirrors ``attention_projection.ATTENTION_OPTION_NAMES`` so the two surfaces
# cannot drift in which facts they show or in what order.
TEXT_STATE_LABEL = "state"
TEXT_SEVERITY_LABEL = "severity"
TEXT_REASON_LABEL = "reason"
TEXT_UPDATED_AT_LABEL = "updated_at"

TEXT_FIELD_LABELS = (
    TEXT_STATE_LABEL,
    TEXT_SEVERITY_LABEL,
    TEXT_REASON_LABEL,
    TEXT_UPDATED_AT_LABEL,
)


class TextAttentionPresentationProvider:
    """Project a core :class:`AttentionRecord` onto the ``text`` surface."""

    name = PROVIDER_NAME
    surface = SURFACE_TEXT

    def project(self, record: AttentionRecord) -> SurfaceProjection:
        """Normalize one :class:`AttentionRecord` into a text :class:`SurfaceProjection`.

        The field keys are the stable :data:`TEXT_FIELD_LABELS`; the values come
        straight off the derived record. ``source_unit_id`` carries the record's
        unit for provenance only — it is never used to pick a target. An absent
        ``observed_at`` projects as ``""`` (the same empty-timestamp behaviour as
        the tmux provider). Pure; no I/O.
        """
        fields = (
            ProjectionField(TEXT_STATE_LABEL, record.attention_state),
            ProjectionField(TEXT_SEVERITY_LABEL, record.severity),
            ProjectionField(TEXT_REASON_LABEL, record.reason_code),
            ProjectionField(TEXT_UPDATED_AT_LABEL, record.observed_at or ""),
        )
        return SurfaceProjection(
            provider=self.name,
            surface=self.surface,
            source_unit_id=record.unit_id,
            fields=fields,
        )


def render_surface_text(projection: SurfaceProjection) -> str:
    """Render any :class:`SurfaceProjection` into a deterministic text block.

    One ``"<key>: <value>"`` line per field, in the projection's field order,
    joined by newlines. A projection with no fields renders as ``""``. An empty
    field value renders as ``"<key>: "`` (a trailing space), so an absent
    timestamp stays visible and the rendering is stable. Pure: it reads only the
    already-built projection and runs no derivation or I/O, so it works for the
    tmux projection and the text projection alike.
    """
    return "\n".join(f"{cell.key}: {cell.value}" for cell in projection.fields)


# Stateless singleton; the provider holds no per-call state.
TEXT_ATTENTION_PRESENTATION_PROVIDER = TextAttentionPresentationProvider()


__all__ = (
    "PROVIDER_NAME",
    "TEXT_ATTENTION_PRESENTATION_PROVIDER",
    "TEXT_FIELD_LABELS",
    "TEXT_REASON_LABEL",
    "TEXT_SEVERITY_LABEL",
    "TEXT_STATE_LABEL",
    "TEXT_UPDATED_AT_LABEL",
    "TextAttentionPresentationProvider",
    "render_surface_text",
)
