"""Internal-only repo-local YAML config schema (Redmine #12189).

This is the typed *schema boundary* for ``.mozyo-bridge/config.yaml`` — the
v0.9 repo-local config source. It defines the closed top-level record shape and
the small presentation-surface selection record, and composes the two existing
internal selection records:

- ``cli`` -> :class:`mozyo_bridge.e_150_quality_architecture.f_130_module_health.domain.module_registry.CliCompositionConfig`
  (Redmine #12155 / #12184): names built-in CLI families to disable.
- ``providers`` -> :class:`mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.provider_registry.ProviderSelectionConfig`
  (Redmine #12035 / #12184): names, per category, which built-in provider id to
  select.
- ``presentation`` -> :class:`PresentationSelectionConfig` (this module):
  selects which built-in projection *surface* to use.
- ``delegation`` -> :class:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_project_config.DelegationConfig`
  (Redmine #12549): the public-safe external-parent child-candidate surface that
  a delegation resolver reads. Default (no candidates) is behavior-preserving.

Boundary, kept enforced in code (this is *schema only*):

- **No file IO and no parsing happen here.** :meth:`RepoLocalConfig.from_record`
  normalizes an already-parsed mapping — the in-memory shape ``yaml.safe_load``
  of the repo-local file would yield. Reading the file from disk is Redmine
  #12190; wiring the resolved records into CLI composition is Redmine #12191.
- **Default / missing / empty config is behavior-preserving.** ``None`` and an
  empty mapping both resolve to :meth:`RepoLocalConfig.default`, whose three
  sub-records are each their own behavior-preserving default. A repo with no
  ``config.yaml`` can never change the default ``mozyo-bridge`` behavior.
- **Closed schema, fail-closed.** Any unknown top-level key, an unsupported
  version, a non-mapping record, or a key shaped like a module / callable /
  entry point / authority / credential is rejected through
  :class:`RepoLocalConfigError` — never a raw parser exception and never a
  silent normalization.
- **``presentation`` is projection-only.** It may name a built-in surface only
  (``tmux_user_option`` default, ``text`` optional). It may never carry a
  target, pane, route, send, approve, close, credential, or authority-shaped
  field, so display selection can never become routing / approval / send truth.

What this surface deliberately cannot express (kept out of the schema, not just
undocumented): external plugin loading, dynamic import, Python entry points,
callable lookup, and any workflow / owner approval / review / close / routing /
send-safety / credential configuration. Those authorities stay core-owned —
the same boundary the CLI and provider registries already enforce.

The module is pure (dataclasses + a small validation helper) and imports only
the sibling domain records, so the dependency only ever points within the
domain layer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_project_config import (
    DelegationConfig,
    DelegationConfigError,
)
from mozyo_bridge.e_150_quality_architecture.f_130_module_health.domain.module_registry import CliCompositionConfig
from mozyo_bridge.e_140_adapter_provider.f_140_presentation_provider.domain.presentation_adapter import (
    PRESENTATION_SURFACES,
    SURFACE_TMUX_USER_OPTION,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping import (
    PresentationGroupingConfig,
    PresentationGroupingConfigError,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.provider_registry import ProviderSelectionConfig

#: The supported repo-local config record version. ``version`` is optional in a
#: record and defaults to this; any other value is rejected so a future,
#: not-yet-understood schema never reads as version 1.
REPO_LOCAL_CONFIG_VERSION: int = 1

#: The closed set of recognized top-level keys. Anything else fails closed.
REPO_LOCAL_CONFIG_KEYS: frozenset[str] = frozenset(
    {"version", "cli", "providers", "presentation", "delegation"}
)

#: The closed set of recognized keys inside the ``presentation`` sub-record.
#: ``version`` / ``surface`` select the projection *surface* (#12189); the three
#: grouping keys (#12286) carry the desired presentation *grouping* config — the
#: Project Group layer (``project_groups`` / ``grouping``) and the whole-view
#: display-placement mode (``project_group_presentation``) — delegated to
#: :class:`~mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping.PresentationGroupingConfig`.
PRESENTATION_SELECTION_KEYS: frozenset[str] = frozenset(
    {
        "version",
        "surface",
        "project_groups",
        "grouping",
        "project_group_presentation",
    }
)

#: The ``presentation`` sub-keys that belong to the grouping config (forwarded to
#: :class:`~mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping.PresentationGroupingConfig`).
#: A separate set so the surface-selection keys and the grouping keys never blur.
PRESENTATION_GROUPING_SUBKEYS: frozenset[str] = frozenset(
    {"project_groups", "grouping", "project_group_presentation"}
)

#: The default projection surface — the current built-in behavior. Selecting no
#: surface (or no ``presentation`` block at all) keeps tmux user-option output.
DEFAULT_PRESENTATION_SURFACE: str = SURFACE_TMUX_USER_OPTION

#: Substrings in a config key that signal an attempt to cross a boundary this
#: surface owns: load / execute code, name a module / callable / entry point,
#: grant or alter authority / approval / routing / send safety, address a
#: target / pane / route, or carry a credential / secret. Such a key is rejected
#: with a boundary-specific message rather than the generic unknown-key error, so
#: the rejection reads as deliberate in an audit. This is the union of the CLI
#: composition record's forbidden parts (Redmine #12155) plus the
#: presentation-only additions (``target`` / ``pane`` / ``route`` / ``send``) the
#: projection-only invariant requires.
_FORBIDDEN_KEY_PARTS: tuple[str, ...] = (
    "import",
    "module",
    "path",
    "registrar",
    "callable",
    "entry",
    "plugin",
    "exec",
    "eval",
    "script",
    "load",
    "authority",
    "authorities",
    "approval",
    "approve",
    "grant",
    "owner",
    "review",
    "close",
    "routing",
    "route",
    "send",
    "send_safety",
    "target",
    "pane",
    "role",
    "secret",
    "token",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "credential",
    "auth",
    "billing",
)


class RepoLocalConfigError(ValueError):
    """The repo-local YAML config record violates the closed schema.

    Inherits :class:`ValueError` for fail-closed semantics, matching the sibling
    domain errors (:class:`~mozyo_bridge.e_150_quality_architecture.f_130_module_health.domain.module_registry.ModuleRegistryError`,
    :class:`~mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.provider_registry.ProviderRegistryError`,
    :class:`~mozyo_bridge.e_140_adapter_provider.f_140_presentation_provider.domain.presentation_adapter.PresentationRecordError`).
    """


def _reject_boundary_token(token: object, *, source: str, role: str) -> None:
    """Fail closed on a repo-local config string that crosses an owned boundary.

    ``token`` is something the YAML names — a top-level key, a provider-selection
    category, or a selected provider id. A non-string token is left for the
    downstream record to type-check; a string whose name contains a
    :data:`_FORBIDDEN_KEY_PARTS` token (code loading, module / callable / entry
    point, authority / approval / routing, target / pane / route / send, or a
    credential) is rejected here with a boundary-specific message so the
    rejection reads as deliberate in an audit.
    """
    if not isinstance(token, str):
        return
    lowered = token.lower()
    for part in _FORBIDDEN_KEY_PARTS:
        if part in lowered:
            raise RepoLocalConfigError(
                f"{source} {role} {token!r} may not carry a boundary token: this "
                f"surface is config-only and may never load code, name a module / "
                f"callable / entry point, grant authority, address a target / pane "
                f"/ route, or carry a credential (matched forbidden token {part!r})."
            )


def _reject_boundary_and_unknown_keys(
    record: "Mapping[object, object]",
    *,
    allowed: "frozenset[str]",
    source: str,
) -> None:
    """Fail closed on a non-string / boundary-crossing / unknown record key.

    Runs the same ordered checks the CLI composition record uses: keys must be
    non-empty strings; a key whose name contains a :data:`_FORBIDDEN_KEY_PARTS`
    token is rejected with a boundary-specific message (code loading, authority,
    routing, target/pane, or credential); any remaining key outside ``allowed``
    is rejected as an unknown key (closed schema / typo protection).
    """
    for key in record:
        if not isinstance(key, str) or not key:
            raise RepoLocalConfigError(
                f"{source} record keys must be non-empty strings; got {key!r}"
            )
        _reject_boundary_token(key, source=source, role="record key")
        if key not in allowed:
            raise RepoLocalConfigError(
                f"{source} record has unknown key {key!r}; allowed keys: "
                f"{sorted(allowed)}"
            )


def _reject_provider_selection_boundary_tokens(
    providers_block: object, *, source: str
) -> None:
    """Fail closed on a boundary-shaped provider-selection category / id.

    :class:`~mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.provider_registry.ProviderSelectionConfig`
    rejects only the *exact* core-owned authority names
    (``workflow_authority`` / ``owner_approval`` / ``close_approval`` /
    ``routing_authority``); the repo-local YAML schema boundary additionally
    rejects module / callable / entry point / target / pane / route / send /
    credential-shaped tokens appearing as a selection category key or a selected
    provider id, matching the same closed rule applied to top-level keys. This
    runs *before* delegation; structural validation (mapping vs pairs,
    duplicates, types, version, category/provider existence) stays with
    ``ProviderSelectionConfig`` and its registry.

    A non-mapping ``providers`` block, or a ``selections`` value of an
    unexpected shape, is left untouched so ``ProviderSelectionConfig.from_record``
    raises the precise structural error.
    """
    if not isinstance(providers_block, Mapping):
        return
    selections = providers_block.get("selections")
    if isinstance(selections, Mapping):
        pairs = list(selections.items())
    elif isinstance(selections, (list, tuple)):
        pairs = [
            (pair[0], pair[1])
            for pair in selections
            if isinstance(pair, (list, tuple)) and len(pair) == 2
        ]
    else:
        return
    for category, provider_id in pairs:
        _reject_boundary_token(category, source=source, role="selection category")
        _reject_boundary_token(
            provider_id, source=source, role="selection provider id"
        )


def _checked_version(record: "Mapping[object, object]", *, source: str) -> int:
    """Return the supported version, failing closed on anything else.

    ``version`` is optional and defaults to :data:`REPO_LOCAL_CONFIG_VERSION`.
    ``bool`` is rejected even though it is an ``int`` subclass so ``version:
    true`` does not silently read as version ``1``.
    """
    version = record.get("version", REPO_LOCAL_CONFIG_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise RepoLocalConfigError(
            f"{source} record 'version' must be an integer, got {version!r}"
        )
    if version != REPO_LOCAL_CONFIG_VERSION:
        raise RepoLocalConfigError(
            f"unsupported {source} record version {version!r}; this build "
            f"understands version {REPO_LOCAL_CONFIG_VERSION}"
        )
    return version


@dataclass(frozen=True)
class PresentationSelectionConfig:
    """Projection-only selection of a built-in surface + desired grouping.

    Two display-only concerns live under one ``presentation`` block:

    - :attr:`surface` names which built-in projection
      :data:`~mozyo_bridge.e_140_adapter_provider.f_140_presentation_provider.domain.presentation_adapter.PRESENTATION_SURFACES`
      surface to use — ``tmux_user_option`` (default) or ``text`` (#12189). It
      cannot add a surface, supply a renderer / module / callable, address a
      target / pane / route, send / approve / close anything, or grant authority.
    - :attr:`grouping` carries the desired presentation *grouping* config — the
      Project Group layer (``project_groups`` / ``grouping``) and the whole-view
      ``project_group_presentation`` display-placement mode (#12286) — parsed by
      :class:`~mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping.PresentationGroupingConfig`,
      itself display-only and fail-closed (no routing / approval / window
      guarantee).

    The default (``tmux_user_option`` surface + an empty grouping config) is the
    current built-in behavior, so a missing ``presentation`` block — or one with
    no grouping keys — never changes how attention is projected or grouped.
    """

    surface: str = DEFAULT_PRESENTATION_SURFACE
    grouping: PresentationGroupingConfig = field(
        default_factory=PresentationGroupingConfig.default
    )

    def __post_init__(self) -> None:
        if not isinstance(self.surface, str) or self.surface not in PRESENTATION_SURFACES:
            raise RepoLocalConfigError(
                f"presentation surface {self.surface!r} is not a built-in "
                f"projection surface; allowed: {sorted(PRESENTATION_SURFACES)}"
            )

    @classmethod
    def default(cls) -> "PresentationSelectionConfig":
        """The behavior-preserving default: tmux surface + empty grouping."""
        return cls()

    @classmethod
    def from_record(
        cls, record: "Optional[Mapping[str, object]]" = None
    ) -> "PresentationSelectionConfig":
        """Normalize a ``presentation`` sub-record into a typed selection.

        ``None`` or an empty mapping yields the default surface and an empty
        grouping config. A non-mapping record, a boundary-crossing / unknown key,
        an unsupported version, or a non-string / unrecognized surface fails
        closed. The grouping sub-keys (``project_groups`` / ``grouping`` /
        ``project_group_presentation``) are forwarded to
        :meth:`PresentationGroupingConfig.from_record`, whose own
        :class:`PresentationGroupingConfigError` (an undeclared group, an invalid
        placement mode, a boundary-shaped grouping key, …) is re-raised as a
        :class:`RepoLocalConfigError` so the loader's single-``except`` boundary
        still catches every repo-local-config failure.
        """
        if record is None:
            return cls.default()
        if not isinstance(record, Mapping):
            raise RepoLocalConfigError(
                "presentation config record must be a mapping (a YAML table), got "
                f"{type(record).__name__}"
            )
        _reject_boundary_and_unknown_keys(
            record, allowed=PRESENTATION_SELECTION_KEYS, source="presentation config"
        )
        _checked_version(record, source="presentation config")
        surface = record.get("surface", DEFAULT_PRESENTATION_SURFACE)
        if not isinstance(surface, str):
            raise RepoLocalConfigError(
                f"presentation config 'surface' must be a string naming a built-in "
                f"projection surface, got {type(surface).__name__}"
            )
        # Forward only the grouping sub-keys to the grouping schema, so the
        # surface-selection keys (version / surface) never reach — and are never
        # rejected as unknown by — the grouping record, and vice versa.
        grouping_record = {
            key: record[key]
            for key in PRESENTATION_GROUPING_SUBKEYS
            if key in record
        }
        try:
            grouping = PresentationGroupingConfig.from_record(
                grouping_record or None
            )
        except PresentationGroupingConfigError as exc:
            raise RepoLocalConfigError(
                f"presentation config grouping is invalid: {exc}"
            ) from exc
        return cls(surface=surface, grouping=grouping)


@dataclass(frozen=True)
class RepoLocalConfig:
    """The closed top-level ``.mozyo-bridge/config.yaml`` record (schema only).

    Composes the four configurable surfaces — :attr:`cli`, :attr:`providers`,
    :attr:`presentation`, :attr:`delegation` — each behavior-preserving by
    default. The default (no fields set) reproduces the current ``mozyo-bridge``
    behavior exactly.

    This layer does no file IO and no parsing: :meth:`from_record` normalizes an
    already-parsed mapping into typed records and fails closed on any unknown
    key, unsupported version, non-mapping record, or module- / callable- /
    entry-point- / authority- / credential-shaped field. Disk reading is Redmine
    #12190 and CLI composition wiring is Redmine #12191.
    """

    cli: CliCompositionConfig = field(default_factory=CliCompositionConfig.default)
    providers: ProviderSelectionConfig = field(
        default_factory=ProviderSelectionConfig.default
    )
    presentation: PresentationSelectionConfig = field(
        default_factory=PresentationSelectionConfig.default
    )
    delegation: DelegationConfig = field(default_factory=DelegationConfig.default)

    @classmethod
    def default(cls) -> "RepoLocalConfig":
        """The behavior-preserving default for a missing / empty config."""
        return cls()

    @classmethod
    def from_record(
        cls, record: "Optional[Mapping[str, object]]" = None
    ) -> "RepoLocalConfig":
        """Normalize a parsed ``.mozyo-bridge/config.yaml`` mapping into records.

        ``record`` is the in-memory mapping a ``yaml.safe_load`` of the
        repo-local config would produce; this layer does no file IO. ``None`` or
        an empty mapping yields the behavior-preserving default, so a missing or
        empty config can never change default behavior.

        Fail-closed, in order:

        - a non-mapping record is rejected (it is not a config record);
        - a key naming a module / callable / entry point / authority / routing /
          target / pane / credential boundary is rejected with a
          boundary-specific message;
        - any other unknown top-level key is rejected (closed schema);
        - ``version``, if present, must be the supported integer version;
        - ``cli`` / ``providers`` / ``presentation`` / ``delegation`` each
          delegate to their own sub-record :meth:`from_record`, which fail closed
          on their own shapes; ``providers`` selection categories / ids are
          additionally screened for module / callable / entry point / authority /
          routing / target / pane / send / credential-shaped tokens here, since
          the provider record itself rejects only the exact core-owned authority
          names. ``delegation``'s own ``DelegationConfigError`` is re-raised as a
          ``RepoLocalConfigError`` so the single fail-closed boundary holds.

        Whether a selected CLI family / provider actually exists is validated by
        the respective registry at resolution time (a later lane), not here; this
        method validates record *shape*.
        """
        if record is None:
            return cls.default()
        if not isinstance(record, Mapping):
            raise RepoLocalConfigError(
                "repo-local config record must be a mapping (a YAML table), got "
                f"{type(record).__name__}"
            )
        _reject_boundary_and_unknown_keys(
            record, allowed=REPO_LOCAL_CONFIG_KEYS, source="repo-local config"
        )
        _checked_version(record, source="repo-local config")

        cli = CliCompositionConfig.from_record(record.get("cli"))
        # ProviderSelectionConfig.from_record requires a mapping (it has no
        # ``None`` default), so an absent ``providers`` block resolves to the
        # behavior-preserving default here rather than being forwarded as None.
        # The schema boundary additionally screens selection category/id tokens
        # for module / callable / target / credential shapes before delegating —
        # the provider record itself only rejects the exact core-owned authority
        # names, so this is where the repo-local YAML closed rule is enforced.
        if "providers" in record:
            _reject_provider_selection_boundary_tokens(
                record["providers"], source="provider selection config"
            )
            providers = ProviderSelectionConfig.from_record(record["providers"])
        else:
            providers = ProviderSelectionConfig.default()
        presentation = PresentationSelectionConfig.from_record(
            record.get("presentation")
        )
        # The delegation child-candidate surface (#12549) is parsed by its own
        # self-contained domain schema; its DelegationConfigError is re-raised as
        # a RepoLocalConfigError so the loader keeps a single fail-closed boundary
        # for every repo-local-config failure (same pattern as the grouping
        # sub-record above). An absent ``delegation`` block resolves to the
        # no-candidate default, so it stays behavior-preserving.
        try:
            delegation = DelegationConfig.from_record(record.get("delegation"))
        except DelegationConfigError as exc:
            raise RepoLocalConfigError(
                f"delegation config is invalid: {exc}"
            ) from exc
        return cls(
            cli=cli,
            providers=providers,
            presentation=presentation,
            delegation=delegation,
        )


__all__ = (
    "REPO_LOCAL_CONFIG_VERSION",
    "REPO_LOCAL_CONFIG_KEYS",
    "PRESENTATION_SELECTION_KEYS",
    "PRESENTATION_GROUPING_SUBKEYS",
    "DEFAULT_PRESENTATION_SURFACE",
    "RepoLocalConfigError",
    "PresentationSelectionConfig",
    "RepoLocalConfig",
    "DelegationConfig",
    "DelegationConfigError",
)
