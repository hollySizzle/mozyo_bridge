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

import re
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
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.agent_launch_argv import (
    AgentLaunchArgvError,
    parse_launch_argv_record,
    validate_launch_argv,
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

#: The supported repo-local config record version. ``version`` is optional in a
#: record and defaults to this; any other value is rejected so a future,
#: not-yet-understood schema never reads as version 1.
REPO_LOCAL_CONFIG_VERSION: int = 1

#: The closed set of recognized top-level keys. Anything else fails closed.
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
    }
)

#: The closed set of recognized keys inside the ``sublane_integration`` sub-record
#: (Redmine #12604): the sublane Git worktree / retire-merge policy knob. ``version``
#: is optional and defaults to :data:`REPO_LOCAL_CONFIG_VERSION`; the three policy
#: keys carry *operational intent only* (see :class:`SublaneIntegrationConfig`) and
#: never any owner-approval / close / callback / routing / send authority — those stay
#: core-owned and the runtime preflight remains the final authority over this config.
SUBLANE_INTEGRATION_KEYS: frozenset[str] = frozenset(
    {"version", "manage_worktree", "integration_branch", "merge_on_retire"}
)

#: The behavior-preserving sublane-integration policy defaults (Redmine #12604).
#: They are the documented *default path* of the (opt-in, explicitly invoked) sublane
#: integration flow — create a worktree / branch at launch, and attempt a merge to the
#: integration branch before retirement. They do **not** change any existing command:
#: no current ``mozyo-bridge`` surface reads this block, and a repo that never invokes
#: the sublane integration flow is unaffected by the defaults.
DEFAULT_MANAGE_WORKTREE: bool = True
DEFAULT_MERGE_ON_RETIRE: bool = True

#: The closed set of recognized keys inside the ``agent_launch`` sub-record
#: (Redmine #13155): the per-role / lane managed-pane launch model knob. ``version``
#: is optional and defaults to :data:`REPO_LOCAL_CONFIG_VERSION`; ``sublane_claude_model``
#: names a single Claude model token appended as ``--model <token>`` at the sublane
#: managed-pane launch chokepoint. It carries *launch-model intent only* — never a shell
#: string, and never any routing / owner-approval / close / send authority — and an unset
#: value is byte-for-byte the historical launch command.
AGENT_LAUNCH_KEYS: frozenset[str] = frozenset(
    {"version", "sublane_claude_model", "launch_argv"}
)


# The provider / lane-class vocabulary, reserved managed-flag set, and launch_argv
# validators (Redmine #13425) live in the self-contained sibling
# :mod:`...domain.agent_launch_argv` (imported below) so this governance-config module
# stays within the module-health budget and the launch-argv rules are one cohesive unit,
# mirroring :mod:`...domain.role_provider_binding_config`.

#: The permitted shape of a launch model token (Redmine #13155). A single opaque token —
#: a leading alphanumeric then alphanumerics / ``.`` / ``_`` / ``-`` — so a config value
#: can never smuggle a space, empty value, shell metacharacter, flag, or path into the
#: launch command. Kept intentionally identical to the flag-policy module's regex
#: (:data:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_model_policy.MODEL_TOKEN_PATTERN`);
#: the two are small and deliberately duplicated so neither layer depends on the other.
_MODEL_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]+$")

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


def _checked_bool(
    record: "Mapping[object, object]", key: str, default: bool, *, source: str
) -> bool:
    """Return a strict boolean record value, failing closed on anything else.

    The key is optional and defaults to ``default``. ``int`` (including ``0`` /
    ``1``) and any non-``bool`` value are rejected so ``merge_on_retire: 0`` cannot
    silently read as ``False`` and a typo'd value never becomes a quiet policy
    change — the fail-closed boundary the rest of this schema enforces.
    """
    if key not in record:
        return default
    value = record[key]
    if not isinstance(value, bool):
        raise RepoLocalConfigError(
            f"{source} record {key!r} must be a boolean, got {value!r}"
        )
    return value


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
class SublaneIntegrationConfig:
    """The sublane Git worktree / retire-merge policy knob (Redmine #12604).

    This is the typed *field contract* for the ``sublane_integration`` block of
    ``.mozyo-bridge/config.yaml``. It carries **operational intent only** for the
    config-driven sublane lifecycle the parent (#12603) re-evaluates: whether to
    create a Git worktree / branch at sublane launch, which integration branch a
    retire-time merge targets, and whether that merge is attempted at all.

    Three policy fields:

    - :attr:`manage_worktree` — in a Git workspace, the sublane launch default path
      creates a worktree / branch (the documented #12604 default). ``False`` opts a
      lane out, e.g. for a non-Git directory scaffold the runtime preflight also
      skips worktree creation regardless of this flag.
    - :attr:`integration_branch` — the configured target branch a retire-time merge
      integrates into. ``None`` (the default) leaves the branch to runtime
      resolution (e.g. the repo default branch); a runtime that cannot resolve a
      target fails closed (``integration_blocked``) rather than guessing.
    - :attr:`merge_on_retire` — attempt a merge to :attr:`integration_branch` before
      retiring the lane (the documented #12604 default). ``merge_on_retire: false``
      is the opt-out: the merge attempt is skipped, but every other retirement gate
      (clean worktree, verification, durable record, and the owner-approval / close
      / callback / durable-anchor invariants) still applies.

    Boundary, kept enforced in code (this is *policy intent*, not authority):

    - **The runtime preflight is the final authority, never this config.** This
      record can opt *out* of a merge attempt; it can never opt out of a safety
      gate or mark a blocked retirement as ``ok``. The decision authority is the
      pure :mod:`...domain.sublane_integration_policy` preflight, which fails closed
      on a dirty worktree, a merge conflict, an unresolved target branch, a
      verification failure, or a missing invariant — *whatever this config says*.
    - **The owner-approval / close / callback / durable-anchor invariants cannot be
      disabled here.** There is deliberately no key for them: the closed
      :data:`SUBLANE_INTEGRATION_KEYS` set admits only the three operational fields,
      and a boundary-shaped key (``owner`` / ``approval`` / ``close`` / ``route`` /
      ``send`` / credential, …) is rejected by the same closed-schema check the rest
      of this module enforces.
    - **Behavior-preserving default.** The default (manage worktree, merge on
      retire, runtime-resolved branch) is the documented default *path of the opt-in
      sublane integration flow*; no existing command reads this block, so a missing
      ``sublane_integration`` block never changes default ``mozyo-bridge`` behavior.
    """

    manage_worktree: bool = DEFAULT_MANAGE_WORKTREE
    integration_branch: Optional[str] = None
    merge_on_retire: bool = DEFAULT_MERGE_ON_RETIRE

    @classmethod
    def default(cls) -> "SublaneIntegrationConfig":
        """The behavior-preserving default sublane-integration policy."""
        return cls()

    @classmethod
    def from_record(
        cls, record: "Optional[Mapping[str, object]]" = None
    ) -> "SublaneIntegrationConfig":
        """Normalize a ``sublane_integration`` sub-record into a typed policy.

        ``None`` or an empty mapping yields the behavior-preserving default. A
        non-mapping record, a boundary-crossing / unknown key, an unsupported
        version, a non-boolean ``manage_worktree`` / ``merge_on_retire``, or an
        ``integration_branch`` that is neither ``None`` nor a non-empty string fails
        closed with :class:`RepoLocalConfigError`.
        """
        if record is None:
            return cls.default()
        if not isinstance(record, Mapping):
            raise RepoLocalConfigError(
                "sublane integration config record must be a mapping (a YAML "
                f"table), got {type(record).__name__}"
            )
        _reject_boundary_and_unknown_keys(
            record,
            allowed=SUBLANE_INTEGRATION_KEYS,
            source="sublane integration config",
        )
        _checked_version(record, source="sublane integration config")
        manage_worktree = _checked_bool(
            record,
            "manage_worktree",
            DEFAULT_MANAGE_WORKTREE,
            source="sublane integration config",
        )
        merge_on_retire = _checked_bool(
            record,
            "merge_on_retire",
            DEFAULT_MERGE_ON_RETIRE,
            source="sublane integration config",
        )
        integration_branch = record.get("integration_branch")
        if integration_branch is not None:
            if not isinstance(integration_branch, str) or not integration_branch.strip():
                raise RepoLocalConfigError(
                    "sublane integration config 'integration_branch' must be a "
                    "non-empty string naming the target branch, or omitted for "
                    f"runtime resolution; got {integration_branch!r}"
                )
        return cls(
            manage_worktree=manage_worktree,
            integration_branch=integration_branch,
            merge_on_retire=merge_on_retire,
        )


@dataclass(frozen=True)
class AgentLaunchConfig:
    """The provider-agnostic per-agent x lane-class launch-argv override (#13155/#13425).

    This is the typed *field contract* for the ``agent_launch`` block of
    ``.mozyo-bridge/config.yaml``. It lets a repo pin the extra launch argv (model,
    reasoning-effort flag, …) each managed agent is started with, keyed by
    ``provider x lane_class``, without touching the launch command's byte shape when
    unset. mozyo does **not** hardcode any provider's flag spec — it appends the
    operator's tokens verbatim after ``-- {provider}`` (Redmine #13425).

    Value fields:

    - :attr:`launch_argv` — the generalized override: a frozen, sorted tuple of
      ``(provider, lane_class, tokens)`` triples parsed from the
      ``launch_argv: {provider: {lane_class: [tokens]}}`` mapping. ``provider`` is a
      :data:`LAUNCH_ARGV_PROVIDERS` launch label (never an executable / argv[0], which
      stays mozyo-controlled — #13245 posture); ``lane_class`` is ``default`` (the main
      coordinator / auditor pair) or ``sublane`` (a lane worker / gateway); each token is
      validated by :func:`_validate_launch_argv_token`. The keying axis is
      ``provider x lane_class``, deliberately NOT the ``provider_binding`` workflow-role
      axis (design consultation answer j#73949 Q1).
    - :attr:`sublane_claude_model` — the #13155 predecessor: a single Claude model *token*
      (e.g. ``claude-opus-4-8``) matching :data:`_MODEL_TOKEN_RE`. It is **folded** into the
      generalized mechanism: :meth:`resolve_launch_argv` treats it as the
      ``claude x sublane`` argv ``["--model", <token>]`` (answer j#73949 Q5). Kept as a
      distinct field for byte-for-byte backward compatibility.

    Boundary, kept enforced in code (this is *launch intent*, not authority):

    - **Config-only, never an executable.** Tokens are argv *elements* appended after the
      mozyo-controlled provider command; config can never select argv[0] / the executable
      (#13245). A path in a flag *value* is allowed; the shell-string launch surface
      ``shlex.quote``s every token.
    - **No mozyo-managed flag override.** A token that re-specifies a
      :data:`RESERVED_MANAGED_FLAGS` flag (currently Claude ``--permission-mode``) fails
      closed — the managed posture (#13360) is authoritative (answer j#73949 Q4).
    - **Old / new conflict fails closed.** Setting both ``sublane_claude_model`` and an
      explicit ``launch_argv.claude.sublane`` is a fail-closed conflict — the operator's
      intended source is ambiguous (answer j#73949 Q5).
    - **Non-retroactive.** The tokens only affect a managed pane mozyo *launches*; an
      already-running pane is untouched (same posture as the #11925 permission policy).
    - **Behavior-preserving default.** No block ⇒ empty ⇒ no extra argv, so a repo with no
      ``agent_launch`` block launches exactly as before.
    """

    version: int = REPO_LOCAL_CONFIG_VERSION
    sublane_claude_model: Optional[str] = None
    launch_argv: "tuple[tuple[str, str, tuple[str, ...]], ...]" = ()

    def __post_init__(self) -> None:
        model = self.sublane_claude_model
        if model is not None and (
            not isinstance(model, str) or not _MODEL_TOKEN_RE.match(model)
        ):
            raise RepoLocalConfigError(
                "agent launch config 'sublane_claude_model' must be a single model "
                f"token matching {_MODEL_TOKEN_RE.pattern} (no spaces, empty value, or "
                "shell metacharacters), or omitted for the historical launch command; "
                f"got {model!r}"
            )
        # Validate the generalized launch_argv (covers direct construction too — existing
        # tests build AgentLaunchConfig(...) directly, and from_record re-uses this path).
        # The sibling raises AgentLaunchArgvError; re-raise as RepoLocalConfigError so the
        # public config-failure boundary is uniform (the role_provider_binding pattern).
        try:
            validate_launch_argv(
                self.launch_argv,
                sublane_claude_model_set=model is not None,
                source="agent launch config",
            )
        except AgentLaunchArgvError as exc:
            raise RepoLocalConfigError(str(exc)) from exc

    def resolve_launch_argv(self, provider: str, lane_class: str) -> "list[str]":
        """The extra launch argv tokens for ``(provider, lane_class)`` (the single source).

        Both launch backends resolve through this method (Redmine #13425 answer j#73949
        Q6): herdr extends its ``agent start ... -- {provider}`` argv list with the
        returned tokens verbatim; the tmux shell-string surface ``shlex.quote``s them. An
        explicit :attr:`launch_argv` entry wins; otherwise the #13155
        :attr:`sublane_claude_model` is folded in for the ``claude x sublane`` slot only
        (the conflict guard in :meth:`__post_init__` means at most one source is set).
        Returns a fresh list (``[]`` when nothing is configured — byte-for-byte historical).
        """
        for entry_provider, entry_lane_class, tokens in self.launch_argv:
            if entry_provider == provider and entry_lane_class == lane_class:
                return list(tokens)
        if (
            provider == "claude"
            and lane_class == "sublane"
            and self.sublane_claude_model is not None
        ):
            return ["--model", self.sublane_claude_model]
        return []

    @classmethod
    def default(cls) -> "AgentLaunchConfig":
        """The behavior-preserving default: no launch-argv override."""
        return cls()

    @classmethod
    def from_record(
        cls, record: "Optional[Mapping[str, object]]" = None
    ) -> "AgentLaunchConfig":
        """Normalize an ``agent_launch`` sub-record into a typed policy.

        ``None`` or an empty mapping yields the behavior-preserving default. A
        non-mapping record, a boundary-crossing / unknown key, an unsupported
        version, a ``sublane_claude_model`` that is neither ``None`` nor a valid
        single model token, or a ``launch_argv`` that violates the provider /
        lane_class / token / reserved-flag / old-new-conflict rules fails closed
        with :class:`RepoLocalConfigError`.
        """
        if record is None:
            return cls.default()
        if not isinstance(record, Mapping):
            raise RepoLocalConfigError(
                "agent launch config record must be a mapping (a YAML table), got "
                f"{type(record).__name__}"
            )
        _reject_boundary_and_unknown_keys(
            record, allowed=AGENT_LAUNCH_KEYS, source="agent launch config"
        )
        version = _checked_version(record, source="agent launch config")
        try:
            launch_argv = parse_launch_argv_record(
                record.get("launch_argv"), source="agent launch config"
            )
        except AgentLaunchArgvError as exc:
            raise RepoLocalConfigError(str(exc)) from exc
        return cls(
            version=version,
            sublane_claude_model=record.get("sublane_claude_model"),
            launch_argv=launch_argv,
        )


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
        # The per-role / lane launch model knob (#13155) is parsed by its own
        # self-contained sub-record, which raises RepoLocalConfigError directly, so
        # the single fail-closed boundary holds without re-wrapping. An absent
        # ``agent_launch`` block resolves to the behavior-preserving default (no
        # ``--model`` flag), so a repo with no block launches exactly as before.
        agent_launch = AgentLaunchConfig.from_record(record.get("agent_launch"))
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
        # The role -> provider binding override knob (#13157) live-wires the #12673
        # RoleProviderBinding seam. It is parsed by its own self-contained domain schema;
        # its RoleProviderBindingConfigError is re-raised as a RepoLocalConfigError so the
        # loader keeps a single fail-closed boundary. An absent ``provider_binding`` block
        # resolves to the legacy codex/claude default, so it stays behavior-preserving.
        try:
            provider_binding = RoleProviderBindingConfig.from_record(
                record.get("provider_binding")
            )
        except RoleProviderBindingConfigError as exc:
            raise RepoLocalConfigError(
                f"provider_binding config is invalid: {exc}"
            ) from exc
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
        )


__all__ = (
    "REPO_LOCAL_CONFIG_VERSION",
    "REPO_LOCAL_CONFIG_KEYS",
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
