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
- ``work_unit`` -> :class:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.work_unit_granularity.WorkUnitGranularityConfig`
  (Redmine #13002): the governed work-unit granularity for sublane dispatch
  (``epic`` / ``feature`` / ``user_story`` / ``leaf_issue``). Default
  (``user_story``) is the standard governed unit.
- ``provider_binding`` -> :class:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding_config.RoleProviderBindingConfig`
  (Redmine #13157): role -> runtime-provider binding overrides that live-wire the
  #12673 ``RoleProviderBinding`` seam. The role vocabulary is closed (an unknown
  role fails closed) while the provider vocabulary stays open. Default (no
  overrides) is the legacy codex/claude map, so it is behavior-preserving.
- ``terminal_transport`` -> :class:`mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport.TerminalTransportConfig`
  (Redmine #13245): selects the runtime terminal-transport backend (``tmux``
  default = herdr off / ``herdr`` opt-in). The herdr *binary* is not a config
  field — it comes only from the trusted environment — so this block can never
  point the runtime at an arbitrary executable. Default (``tmux``) is
  behavior-preserving.

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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.work_unit_granularity import (
    WorkUnitGranularityConfig,
    WorkUnitGranularityError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding_config import (
    RoleProviderBindingConfig,
    RoleProviderBindingConfigError,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config_records import (
    AGENT_LAUNCH_KEYS,
    AgentLaunchConfig,
    DEFAULT_MANAGE_WORKTREE,
    DEFAULT_MERGE_ON_RETIRE,
    REPO_LOCAL_CONFIG_VERSION,
    RepoLocalConfigError,
    SUBLANE_INTEGRATION_KEYS,
    SublaneIntegrationConfig,
    _checked_bool,
    _checked_version,
    _reject_boundary_and_unknown_keys,
    _reject_boundary_token,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.agents_topology import (
    AgentsTopologyConfig,
    AgentsTopologyError,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.lane_placement import (
    LanePlacementConfig,
    LanePlacementError,
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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    TerminalTransportConfig,
    TerminalTransportError,
)


#: The role-canonical config record version (Redmine #14148). v2 replaces the
#: provider-keyed ``provider_binding`` / ``agent_launch`` blocks with the ``agents``
#: topology (named runtime profiles + role -> profile bindings). v1 stays parseable
#: during the deprecation window.
REPO_LOCAL_CONFIG_V2: int = 2

#: The versions this build understands. A future, not-yet-understood version is
#: rejected so it never silently reads as v1 / v2.
SUPPORTED_REPO_LOCAL_CONFIG_VERSIONS: frozenset[int] = frozenset(
    {REPO_LOCAL_CONFIG_VERSION, REPO_LOCAL_CONFIG_V2}
)

#: Top-level keys valid only in v2 (the role-canonical topology). Declaring them
#: under v1 fails closed — the block did not exist there.
V2_ONLY_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"agents"})

#: Top-level keys valid only in v1 (the provider-keyed legacy blocks superseded by
#: ``agents`` in v2). Declaring them under v2 fails closed with a migration pointer.
V1_ONLY_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"provider_binding", "agent_launch"})

# --- v1 deprecation / removal lifecycle (Redmine #14148 finding 4) -----------------
# v2 is the role-canonical schema; v1 stays *parseable* through a fixed compatibility
# window so no repo breaks on upgrade. The window is concrete (not "a future minor") so an
# operator and a future maintainer can both plan against it, and the removal is gated on an
# observable condition, not a date.

#: The release that introduces schema v2 (the current line is 0.12.x -> next minor).
V2_INTRODUCED_VERSION: str = "0.13.0"

#: The minimum number of minor releases v1 stays parseable *after* v2 ships. Two minors
#: (0.13.x and 0.14.x) is the compatibility floor: v1 configs keep loading with a warning.
V1_COMPAT_MINIMUM_MINORS: int = 2

#: The earliest release in which v1 parse support MAY be removed (the end of the window).
V1_EARLIEST_REMOVAL_VERSION: str = "0.15.0"

#: The observable gate that must hold before v1 support is actually dropped in / after
#: :data:`V1_EARLIEST_REMOVAL_VERSION`: the packaged v1 fixtures are retired only once no v1
#: config is expected in the field (dogfood + operator repos migrated). Removal is a
#: guardrail change (its own reviewed issue), never a silent minor-bump drop.
V1_REMOVAL_GATE: str = (
    "no v1 config remains in dogfood / operator repos (migrated) AND the v1 round-trip "
    "fixtures are retired in the same reviewed change"
)

#: The deprecation notice surfaced for a v1 config that still carries migratable
#: provider-keyed content. v1 keeps working through the window; the notice points at the
#: public migration and states the concrete removal boundary.
V1_DEPRECATION_NOTICE: str = (
    "repo-local config is version 1 (provider-keyed 'provider_binding' / 'agent_launch'). "
    f"Version 1 is deprecated as of {V2_INTRODUCED_VERSION}; it stays parseable for at "
    f"least {V1_COMPAT_MINIMUM_MINORS} more minor releases and may be removed no earlier "
    f"than {V1_EARLIEST_REMOVAL_VERSION} (and only once {V1_REMOVAL_GATE}). Run "
    "`mozyo-bridge config migrate --check` to preview the role-canonical version 2, then "
    "`--write` to upgrade."
)

#: The closed set of recognized top-level keys across all supported versions.
#: Cross-version validity is enforced separately (:func:`_reject_cross_version_keys`)
#: once the version is known; membership here only gates the unknown-key check.
REPO_LOCAL_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "version",
        "cli",
        "providers",
        "presentation",
        "delegation",
        "sublane_integration",
        "work_unit",
        "agent_launch",
        "lane_placement",
        "provider_binding",
        "terminal_transport",
        "agents",
    }
)





# The provider / lane-class vocabulary, reserved managed-flag set, and launch_argv
# validators (Redmine #13425) live in the self-contained sibling
# :mod:`...domain.agent_launch_argv` (imported below) so this governance-config module
# stays within the module-health budget and the launch-argv rules are one cohesive unit,
# mirroring :mod:`...domain.role_provider_binding_config`.


#: The closed set of recognized keys inside the ``presentation`` sub-record.
#: ``version`` / ``surface`` select the projection *surface* (#12189); the
#: grouping keys (#12286) carry the desired presentation *grouping* config — the
#: Project Group layer (``project_groups`` / ``grouping``), the whole-view
#: display-placement mode (``project_group_presentation``), and the
#: delegated-tree / sublane window-separation policy
#: (``delegation_window_policy``, #12467 display + #13015 launcher placement) —
#: delegated to
#: :class:`~mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping.PresentationGroupingConfig`.
PRESENTATION_SELECTION_KEYS: frozenset[str] = frozenset(
    {
        "version",
        "surface",
        "project_groups",
        "grouping",
        "project_group_presentation",
        "delegation_window_policy",
    }
)

#: The ``presentation`` sub-keys that belong to the grouping config (forwarded to
#: :class:`~mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping.PresentationGroupingConfig`).
#: A separate set so the surface-selection keys and the grouping keys never blur.
PRESENTATION_GROUPING_SUBKEYS: frozenset[str] = frozenset(
    {
        "project_groups",
        "grouping",
        "project_group_presentation",
        "delegation_window_policy",
    }
)

#: The default projection surface — the current built-in behavior. Selecting no
#: surface (or no ``presentation`` block at all) keeps tmux user-option output.
DEFAULT_PRESENTATION_SURFACE: str = SURFACE_TMUX_USER_OPTION









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




def _checked_top_level_version(record: "Mapping[object, object]") -> int:
    """Return the supported top-level version (1 or 2), failing closed on anything else.

    Unlike :func:`_checked_version` (which each sub-record uses to pin its own ``version``
    at exactly 1), the top-level record accepts either the legacy v1 or the role-canonical
    v2. ``version`` is optional and defaults to :data:`REPO_LOCAL_CONFIG_VERSION`. ``bool``
    is rejected even though it is an ``int`` subclass so ``version: true`` never reads as 1.
    """
    version = record.get("version", REPO_LOCAL_CONFIG_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise RepoLocalConfigError(
            f"repo-local config record 'version' must be an integer, got {version!r}"
        )
    if version not in SUPPORTED_REPO_LOCAL_CONFIG_VERSIONS:
        raise RepoLocalConfigError(
            f"unsupported repo-local config record version {version!r}; this build "
            f"understands versions {sorted(SUPPORTED_REPO_LOCAL_CONFIG_VERSIONS)}"
        )
    return version


def _reject_cross_version_keys(
    record: "Mapping[object, object]", version: int
) -> None:
    """Fail closed on a block that is not valid for the record's version.

    A v2 record may not carry the provider-keyed ``provider_binding`` / ``agent_launch``
    legacy blocks (they are folded into ``agents``); a v1 record may not carry the
    ``agents`` topology (it did not exist there). Keeping the two shapes disjoint makes the
    version boundary — and the migration — unambiguous. Keys are already validated as
    strings by the unknown-key screen that runs first.
    """
    present = set(record)
    if version >= REPO_LOCAL_CONFIG_V2:
        legacy = sorted(V1_ONLY_TOP_LEVEL_KEYS & present)
        if legacy:
            raise RepoLocalConfigError(
                f"repo-local config version {version} does not support legacy block(s) "
                f"{legacy}: they are folded into the 'agents' topology in v2. Run "
                f"`mozyo-bridge config migrate` to upgrade a v1 config."
            )
    else:
        future = sorted(V2_ONLY_TOP_LEVEL_KEYS & present)
        if future:
            raise RepoLocalConfigError(
                f"repo-local config version {version} does not support block(s) {future}: "
                f"the 'agents' topology requires version {REPO_LOCAL_CONFIG_V2}. Set "
                f"'version: {REPO_LOCAL_CONFIG_V2}' (see `mozyo-bridge config migrate`)."
            )




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
      Project Group layer (``project_groups`` / ``grouping``), the whole-view
      ``project_group_presentation`` display-placement mode (#12286), and the
      ``delegation_window_policy`` window-separation knob (#12467 / #13015) —
      parsed by
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
        ``project_group_presentation`` / ``delegation_window_policy``) are
        forwarded to
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

    Composes the configurable surfaces — :attr:`cli`, :attr:`providers`,
    :attr:`presentation`, :attr:`delegation`, :attr:`sublane_integration`,
    :attr:`work_unit`, :attr:`agent_launch`, :attr:`lane_placement`,
    :attr:`provider_binding` — each behavior-preserving by default. The default (no
    fields set) reproduces the current ``mozyo-bridge`` behavior exactly.

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
    sublane_integration: SublaneIntegrationConfig = field(
        default_factory=SublaneIntegrationConfig.default
    )
    work_unit: WorkUnitGranularityConfig = field(
        default_factory=WorkUnitGranularityConfig.default
    )
    agent_launch: AgentLaunchConfig = field(
        default_factory=AgentLaunchConfig.default
    )
    lane_placement: LanePlacementConfig = field(
        default_factory=LanePlacementConfig.default
    )
    provider_binding: RoleProviderBindingConfig = field(
        default_factory=RoleProviderBindingConfig.default
    )
    terminal_transport: TerminalTransportConfig = field(
        default_factory=TerminalTransportConfig.default
    )
    #: The role-canonical topology (Redmine #14148). In a v2 config this is the source the
    #: :attr:`provider_binding` / :attr:`agent_launch` fields are *resolved from* (named
    #: runtime profiles + role -> profile bindings); in a v1 config it is the empty default
    #: and those two fields are parsed from their own legacy blocks. Carried so the
    #: migration / round-trip surface can serialize the topology back out.
    agents: AgentsTopologyConfig = field(default_factory=AgentsTopologyConfig.default)
    #: The schema version this record was parsed from (1 legacy / 2 role-canonical). The
    #: default record (missing / empty config) carries the legacy version but no migratable
    #: content, so :meth:`deprecation_warnings` stays silent for it.
    schema_version: int = REPO_LOCAL_CONFIG_VERSION

    def deprecation_warnings(self) -> "tuple[str, ...]":
        """Advisory (non-blocking) deprecation notices for this config.

        A v1 config that still carries provider-keyed ``provider_binding`` overrides or an
        ``agent_launch`` block is deprecated: it keeps working, but the notice points at the
        public ``config migrate`` surface. A v1 config with no migratable content (the
        behavior-preserving default, or one that only sets unrelated blocks) is silent —
        there is nothing to migrate — and a v2 config is silent.
        """
        if self.schema_version >= REPO_LOCAL_CONFIG_V2:
            return ()
        migratable = (
            bool(self.provider_binding.overrides)
            or bool(self.agent_launch.launch_argv)
            or self.agent_launch.sublane_claude_model is not None
        )
        return (V1_DEPRECATION_NOTICE,) if migratable else ()

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
        version = _checked_top_level_version(record)
        _reject_cross_version_keys(record, version)

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
        # The sublane integration policy knob (#12604) is parsed by its own
        # self-contained sub-record, which raises RepoLocalConfigError directly, so
        # the single fail-closed boundary holds without re-wrapping. An absent
        # ``sublane_integration`` block resolves to the behavior-preserving default.
        sublane_integration = SublaneIntegrationConfig.from_record(
            record.get("sublane_integration")
        )
        # The governed work-unit granularity knob (#13002) is parsed by its own
        # self-contained domain schema; its WorkUnitGranularityError is re-raised
        # as a RepoLocalConfigError so the loader keeps a single fail-closed
        # boundary. An absent ``work_unit`` block resolves to the ``user_story``
        # standard-unit default, so it stays behavior-preserving.
        try:
            work_unit = WorkUnitGranularityConfig.from_record(record.get("work_unit"))
        except WorkUnitGranularityError as exc:
            raise RepoLocalConfigError(
                f"work_unit config is invalid: {exc}"
            ) from exc
        # The lane-class pane-pair placement knob (#13646) declares the herdr split
        # direction / provider order per lane class. It is parsed by its own
        # self-contained domain schema (the sibling that also owns the vocabulary); its
        # LanePlacementError is re-raised as a RepoLocalConfigError so the loader keeps a
        # single fail-closed boundary. An absent ``lane_placement`` block resolves to the
        # behavior-preserving default (no split / order override), so a repo with no block
        # launches exactly as before. The ``pane``-shaped key screen already ran above.
        try:
            lane_placement = LanePlacementConfig.from_record(
                record.get("lane_placement")
            )
        except LanePlacementError as exc:
            raise RepoLocalConfigError(
                f"lane_placement config is invalid: {exc}"
            ) from exc
        # The role -> provider binding (#13157) and per-role launch argv (#13155/#13425)
        # are version-gated (Redmine #14148):
        #  - v1 parses the provider-keyed legacy ``provider_binding`` / ``agent_launch``
        #    blocks (behavior-preserving; an absent block is the legacy default);
        #  - v2 parses the role-canonical ``agents`` topology (named runtime profiles +
        #    role -> profile bindings) and *resolves* it into the same two typed records, so
        #    every downstream consumer is unchanged.
        # The cross-version key gate above guaranteed a disjoint block set, so exactly one
        # path runs. Each sub-schema error is re-raised as RepoLocalConfigError so the loader
        # keeps a single fail-closed boundary.
        if version >= REPO_LOCAL_CONFIG_V2:
            try:
                agents = AgentsTopologyConfig.from_record(record.get("agents"))
                provider_binding = RoleProviderBindingConfig(
                    overrides=tuple(
                        sorted(agents.to_provider_binding_overrides().items())
                    )
                )
                # AgentLaunchConfig.__post_init__ already re-raises AgentLaunchArgvError as
                # RepoLocalConfigError, so it needs no wrapping here.
                agent_launch = AgentLaunchConfig(
                    launch_argv=agents.to_resolved_launch_argv_triples()
                )
            except (AgentsTopologyError, RoleProviderBindingConfigError) as exc:
                raise RepoLocalConfigError(
                    f"agents topology is invalid: {exc}"
                ) from exc
        else:
            agents = AgentsTopologyConfig.default()
            # The role -> provider binding override knob (#13157) live-wires the #12673
            # RoleProviderBinding seam; an absent ``provider_binding`` block resolves to the
            # legacy default, so it stays behavior-preserving.
            try:
                provider_binding = RoleProviderBindingConfig.from_record(
                    record.get("provider_binding")
                )
            except RoleProviderBindingConfigError as exc:
                raise RepoLocalConfigError(
                    f"provider_binding config is invalid: {exc}"
                ) from exc
            # The per-role / lane launch model knob (#13155); an absent ``agent_launch``
            # block resolves to the behavior-preserving default (no ``--model`` flag).
            agent_launch = AgentLaunchConfig.from_record(record.get("agent_launch"))
        # The terminal-transport backend selection (#13245) picks the runtime
        # transport backend (default ``tmux`` = herdr off). It is parsed by its
        # own self-contained domain schema; its TerminalTransportError is
        # re-raised as a RepoLocalConfigError so the loader keeps a single
        # fail-closed boundary. An absent ``terminal_transport`` block resolves to
        # the tmux default, so it stays behavior-preserving. The herdr *binary*
        # is deliberately not a config field — it comes only from the trusted
        # environment (see TerminalTransportConfig).
        try:
            terminal_transport = TerminalTransportConfig.from_record(
                record.get("terminal_transport")
            )
        except TerminalTransportError as exc:
            raise RepoLocalConfigError(
                f"terminal_transport config is invalid: {exc}"
            ) from exc
        return cls(
            cli=cli,
            providers=providers,
            presentation=presentation,
            delegation=delegation,
            sublane_integration=sublane_integration,
            work_unit=work_unit,
            agent_launch=agent_launch,
            lane_placement=lane_placement,
            provider_binding=provider_binding,
            terminal_transport=terminal_transport,
            agents=agents,
            schema_version=version,
        )


__all__ = (
    "REPO_LOCAL_CONFIG_VERSION",
    "REPO_LOCAL_CONFIG_V2",
    "SUPPORTED_REPO_LOCAL_CONFIG_VERSIONS",
    "V1_ONLY_TOP_LEVEL_KEYS",
    "V2_ONLY_TOP_LEVEL_KEYS",
    "V1_DEPRECATION_NOTICE",
    "V2_INTRODUCED_VERSION",
    "V1_COMPAT_MINIMUM_MINORS",
    "V1_EARLIEST_REMOVAL_VERSION",
    "V1_REMOVAL_GATE",
    "REPO_LOCAL_CONFIG_KEYS",
    "AgentsTopologyConfig",
    "AgentsTopologyError",
    "PRESENTATION_SELECTION_KEYS",
    "PRESENTATION_GROUPING_SUBKEYS",
    "DEFAULT_PRESENTATION_SURFACE",
    "SUBLANE_INTEGRATION_KEYS",
    "DEFAULT_MANAGE_WORKTREE",
    "DEFAULT_MERGE_ON_RETIRE",
    "AGENT_LAUNCH_KEYS",
    "RepoLocalConfigError",
    "PresentationSelectionConfig",
    "SublaneIntegrationConfig",
    "AgentLaunchConfig",
    "LanePlacementConfig",
    "LanePlacementError",
    "RoleProviderBindingConfig",
    "RoleProviderBindingConfigError",
    "TerminalTransportConfig",
    "TerminalTransportError",
    "RepoLocalConfig",
    "DelegationConfig",
    "DelegationConfigError",
    "WorkUnitGranularityConfig",
    "WorkUnitGranularityError",
)
