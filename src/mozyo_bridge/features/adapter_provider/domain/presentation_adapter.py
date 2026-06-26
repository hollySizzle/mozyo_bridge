"""Core-facing presentation adapter boundary seam (Redmine #12156).

This is the first concrete cut of the built-in *presentation* adapter boundary
from ``vibes/docs/logics/plugin-ready-adapter-boundary.md`` (Redmine #12001),
the v0.8 "Candidate 2" slice. It follows the same shape as the ticket adapter
seam (Redmine #12034): a small pure record plus a provider ``Protocol`` plus the
core-owned invariants a provider is forbidden from crossing.

Boundary, restated from the design doc so it stays enforced in code:

- **Core owns** the projection record vocabulary — ``TargetRecord`` /
  ``UnitRecord`` / ``AttentionRecord`` and the event envelope remain core
  shapes — the recognized projection *surfaces*, the command preview / confirm
  semantics, and the public / private presentation boundary.
- **Providers own** *how* to render onto a surface: colour, badge, pane title,
  border, WebViewer UI, the option / text mechanics, and local-only operator
  preferences.

The defining constraint is that a presentation adapter is **read / projection
first**: it consumes already-derived core records and emits a projection. It
must not define workflow truth, owner approval, or routing authority. The
:class:`PresentationProvider` protocol therefore exposes only a ``project``
method (no send / route / approve), and :class:`SurfaceProjection` rejects any
field that names a core-owned authority — so a provider cannot smuggle routing
or approval truth into a display projection. This mirrors the ticket seam, where
``classify_workflow_gate`` / ``owner_approval`` are core functions a provider can
never own.

This module is pure (dataclasses + a ``Protocol``) and imports no provider
implementation, so the dependency only ever points provider -> core.

Non-goals (kept explicit so the seam does not drift into a plugin API):

- no third-party / arbitrary-code provider loading; one built-in projection
  provider (tmux) exists for now;
- no public ABI or long-term compatibility promise for these record shapes;
- no presentation-defined workflow truth, owner approval, or routing authority;
- no UI state becomes a routing key or a completion gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

# The core never delegates these decisions to a provider; reuse the single
# authority vocabulary from the provider registry seam (Redmine #12035) so a
# presentation projection and a registered provider are checked against the same
# core-owned list and the two can never drift apart.
from mozyo_bridge.domain.provider_registry import FORBIDDEN_PROVIDER_AUTHORITIES

# --- recognized projection surfaces (core-owned vocabulary) ------------------
# The design doc's presentation MVP names "tmux user options and text output" as
# the first built-in projection surfaces. New surfaces are added here in core,
# never supplied by a provider. An unrecognized surface is rejected at
# construction so a provider cannot invent surface semantics. Like the empty
# provider categories in the registry skeleton, a surface listed here without a
# provider yet (``text``) is still a valid, expressible classification.
SURFACE_TMUX_USER_OPTION = "tmux_user_option"
SURFACE_TEXT = "text"

PRESENTATION_SURFACES: frozenset[str] = frozenset(
    {SURFACE_TMUX_USER_OPTION, SURFACE_TEXT}
)

# A presentation projection may not carry a field that asserts a core-owned
# authority. This is the projection-only invariant expressed as data: display
# state never becomes workflow / owner / close / routing truth.
FORBIDDEN_PROJECTION_FIELDS: frozenset[str] = FORBIDDEN_PROVIDER_AUTHORITIES


class PresentationRecordError(ValueError):
    """A provider produced a presentation projection that violates the contract."""


@dataclass(frozen=True)
class ProjectionField:
    """One ``key -> value`` cell of a surface projection.

    Both are strings: a projection is display data, not structured authority.
    ``key`` must be non-empty; ``value`` may be empty (e.g. an absent timestamp
    projects as ``""``). The forbidden-key check lives on
    :class:`SurfaceProjection` so the whole projection is validated as a unit.
    """

    key: str
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise PresentationRecordError(
                f"projection field key must be a non-empty string, got {self.key!r}"
            )
        if not isinstance(self.value, str):
            raise PresentationRecordError(
                f"projection field {self.key!r} value must be a string, "
                f"got {type(self.value).__name__}"
            )


@dataclass(frozen=True)
class SurfaceProjection:
    """A read-only projection of core records onto one presentation surface.

    ``provider`` names the built-in projection provider that produced it (e.g.
    ``"tmux-presentation"``); ``surface`` is one of :data:`PRESENTATION_SURFACES`;
    ``source_unit_id`` is the core unit the projection is *about* (provenance,
    not a routing key); ``fields`` are the rendered cells; ``source_ref`` is an
    optional pointer back to the durable / derived source.

    The two invariants are enforced in :meth:`__post_init__`, so even direct
    construction cannot bypass them:

    - the surface must be core-recognized (a provider cannot invent one);
    - no field key may name a core-owned authority
      (:data:`FORBIDDEN_PROJECTION_FIELDS`) — a projection is display only and
      can never assert workflow / owner / close / routing truth.
    """

    provider: str
    surface: str
    source_unit_id: str
    fields: tuple[ProjectionField, ...] = field(default_factory=tuple)
    source_ref: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.provider:
            raise PresentationRecordError("provider must be a non-empty string")
        if self.surface not in PRESENTATION_SURFACES:
            raise PresentationRecordError(
                f"unknown presentation surface: {self.surface!r}; expected one of "
                f"{sorted(PRESENTATION_SURFACES)}"
            )
        # Accept any iterable of ProjectionField but store an immutable tuple.
        fields = tuple(self.fields)
        for cell in fields:
            if not isinstance(cell, ProjectionField):
                raise PresentationRecordError(
                    "fields entries must be ProjectionField instances, got "
                    f"{type(cell).__name__}"
                )
        object.__setattr__(self, "fields", fields)
        leaked = {cell.key for cell in fields} & FORBIDDEN_PROJECTION_FIELDS
        if leaked:
            raise PresentationRecordError(
                f"presentation projection from {self.provider!r} may not carry a "
                f"core-owned authority as a field: {sorted(leaked)}. Presentation "
                f"is projection only (see plugin-ready-adapter-boundary.md)."
            )

    def as_mapping(self) -> dict[str, str]:
        """Return the projection fields as an ordered ``key -> value`` dict."""
        return {cell.key: cell.value for cell in self.fields}


@runtime_checkable
class PresentationProvider(Protocol):
    """The built-in presentation provider boundary — read / projection first.

    Implementations are *built-in* providers only — there is no dynamic loading
    and no public extension contract (see the module docstring and the
    adapter-boundary design doc). A provider takes an already-derived core record
    and returns a :class:`SurfaceProjection` for its surface. The protocol
    deliberately exposes *only* projection: there is no ``send``, ``route``, or
    ``approve`` method, because a presentation adapter must never define workflow
    truth, owner approval, or routing authority.
    """

    name: str
    surface: str

    def project(self, record: object) -> SurfaceProjection:
        """Project one core record onto this provider's surface."""
        ...


__all__ = (
    "FORBIDDEN_PROJECTION_FIELDS",
    "PRESENTATION_SURFACES",
    "PresentationProvider",
    "PresentationRecordError",
    "ProjectionField",
    "SURFACE_TEXT",
    "SURFACE_TMUX_USER_OPTION",
    "SurfaceProjection",
)
