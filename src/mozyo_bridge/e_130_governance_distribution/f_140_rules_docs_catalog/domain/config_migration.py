"""Pure v1 -> v2 repo-local config migration (Redmine #14148).

The record-to-record transform behind the public ``config migrate`` surface. It is pure —
no file IO, no YAML — so the CLI layer owns reading / writing and this layer owns *meaning*:
turning a legacy provider-keyed v1 mapping into the role-canonical v2 ``agents`` topology
while preserving the resolved runtime behavior exactly.

Contract, all fail-closed:

- **Validate first.** The input is validated through
  :meth:`~...domain.repo_local_config.RepoLocalConfig.from_record`, so a malformed /
  unknown / boundary-shaped v1 record fails closed *before* any transform.
- **Registered-adapter requirement (finding 5).** v1 binds an *open* provider vocabulary;
  v2 profiles may only name a *registered* adapter id. A v1 provider (a binding target or a
  ``launch_argv`` key) that is not a built-in adapter id fails closed with an actionable
  ``registered adapter profile required`` diagnostic naming the role / provider — never a
  silent drop or a guessed provider.
- **Behavior-preserving via role -> profile.** The produced v2 record resolves (through
  ``from_record`` -> ``AgentsTopologyConfig``) to the same effective launch argv per
  ``(role, lane_class)`` as the v1 input. Launch argv is attached to the *canonical*
  profile of its provider so ``role -> profile -> launch_argv`` reaches it, and a lane
  class is never inherited across lane classes (the #13451 sublane-has-no-model invariant).
  Redundant ``provider_binding`` overrides that merely restate the default are dropped.
- **Idempotent + deterministic.** A record already at v2 is returned unchanged; profiles
  are canonically named and everything is sorted, so the same v1 always yields the same v2.
- **No authority widening.** Only ``provider_binding`` / ``agent_launch`` are rewritten
  into ``agents``; every other block is copied verbatim.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Optional

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (
    agent_provider_ids,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (
    DEFAULT_PROFILE_PROVIDERS,
    DEFAULT_ROLE_PROFILES,
    RoleProviderBinding,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
    REPO_LOCAL_CONFIG_V2,
    RepoLocalConfig,
    RepoLocalConfigError,
    V1_ONLY_TOP_LEVEL_KEYS,
)


class ConfigMigrationError(RepoLocalConfigError):
    """A v1 config cannot be migrated to v2 (fail-closed).

    Subclasses :class:`RepoLocalConfigError` so a caller catching the config-failure
    boundary catches migration failures too. Raised for a v1 provider that is not a
    registered adapter id (finding 5) — a valid-v1 that cannot become a trusted v2 profile.
    """


@dataclass(frozen=True)
class MigrationResult:
    """The outcome of a v1 -> v2 migration transform (pure).

    ``migrated`` is the target record (a plain dict a caller can serialize). ``changes`` is
    a human-readable, ordered plan the ``--check`` surface prints. ``already_current`` is
    ``True`` when the input was already v2 (``migrated`` is then the input, unchanged).
    """

    source_version: int
    target_version: int
    already_current: bool
    migrated: "dict[str, object]"
    changes: "tuple[str, ...]" = field(default_factory=tuple)


def _canonical_profile_name_for_provider(provider: str) -> str:
    """The canonical profile name for ``provider`` (reverse of DEFAULT_PROFILE_PROVIDERS).

    ``codex`` -> ``coordination``, ``claude`` -> ``implementation``; any other registered
    provider names its profile after itself. Using the canonical name for a default provider
    lets the migrated launch argv be reached through the canonical ``role -> profile`` map
    without emitting a role override for every role.
    """
    for name, prov in DEFAULT_PROFILE_PROVIDERS.items():
        if prov == provider:
            return name
    return provider


def migrate_record(
    record: "Optional[Mapping[str, object]]",
) -> MigrationResult:
    """Migrate a parsed repo-local config mapping from v1 to v2 (pure, fail-closed).

    ``None`` / an empty mapping migrates to a bare ``version: 2`` record. A record already
    at v2 is validated and returned unchanged (idempotent). A v1 record is validated, its
    provider(s) checked against the registered adapter ids (finding 5), then its
    ``provider_binding`` / ``agent_launch`` blocks are folded into the role-canonical
    ``agents`` topology (launch argv attached to each provider's canonical profile) and every
    other block copied verbatim.
    """
    if record is None:
        record = {}
    if not isinstance(record, Mapping):
        RepoLocalConfig.from_record(record)  # raises RepoLocalConfigError

    config = RepoLocalConfig.from_record(record)
    source_version = config.schema_version

    if source_version >= REPO_LOCAL_CONFIG_V2:
        return MigrationResult(
            source_version=source_version,
            target_version=REPO_LOCAL_CONFIG_V2,
            already_current=True,
            migrated=dict(record),
            changes=(
                f"config is already version {REPO_LOCAL_CONFIG_V2}; no migration needed",
            ),
        )

    changes: list[str] = [f"version: {source_version} -> {REPO_LOCAL_CONFIG_V2}"]

    # 1. Effective role -> provider (default merged with v1 overrides). The default binding
    #    is itself the projection of the canonical role -> profile -> provider topology.
    effective = config.provider_binding.binding.as_mapping()
    default_binding = RoleProviderBinding.default()
    for role, provider in sorted(config.provider_binding.overrides):
        if provider == default_binding.provider_for(role):
            changes.append(
                f"drop redundant provider_binding '{role}: {provider}' (equals the default)"
            )

    # 2. launch argv by provider (parsed triples + the legacy single-model knob folded into
    #    the claude x sublane slot).
    launch_by_provider: "dict[str, dict[str, list[str]]]" = {}
    for provider, lane_class, tokens in config.agent_launch.launch_argv:
        launch_by_provider.setdefault(provider, {})[lane_class] = list(tokens)
    if config.agent_launch.sublane_claude_model is not None:
        model = config.agent_launch.sublane_claude_model
        launch_by_provider.setdefault("claude", {})["sublane"] = ["--model", model]
        changes.append(
            f"fold legacy 'sublane_claude_model: {model}' into a profile launch_argv "
            f"(claude / sublane)"
        )

    # 3. Finding 5: every provider that becomes a v2 profile must be a registered adapter id.
    #    A valid-v1 open provider that is not registered fails closed with an actionable,
    #    value-non-secret diagnostic rather than a silent drop.
    known = agent_provider_ids()
    for role, provider in sorted(effective.items()):
        if provider not in known:
            raise ConfigMigrationError(
                f"cannot migrate role {role!r} bound to provider {provider!r}: a v2 profile "
                f"requires a registered adapter profile (known adapter ids: {sorted(known)}). "
                f"Register the adapter or rebind the role before migrating."
            )
    for provider in sorted(launch_by_provider):
        if provider not in known:
            raise ConfigMigrationError(
                f"cannot migrate launch_argv for provider {provider!r}: a v2 profile requires "
                f"a registered adapter profile (known adapter ids: {sorted(known)})."
            )

    # 4. Build profiles keyed by canonical profile name. Every provider that is bound by a
    #    role or carries launch argv gets a profile; launch argv attaches to that provider's
    #    canonical profile so role -> profile -> launch_argv reaches it.
    providers_needed = sorted(set(effective.values()) | set(launch_by_provider))
    profiles: "dict[str, object]" = {}
    provider_to_profile: "dict[str, str]" = {}
    for provider in providers_needed:
        name = _canonical_profile_name_for_provider(provider)
        provider_to_profile[provider] = name
        profile: "dict[str, object]" = {"provider": provider}
        argv = launch_by_provider.get(provider)
        if argv:
            profile["launch_argv"] = {lc: argv[lc] for lc in sorted(argv)}
        profiles[name] = profile

    # 5. roles: emit a role -> profile binding only where the effective profile differs from
    #    the canonical default role -> profile (so a default-provider role stays implicit and
    #    still reaches its now-argv-bearing canonical profile).
    roles: "dict[str, str]" = {}
    for role in sorted(effective):
        effective_profile = provider_to_profile[effective[role]]
        if DEFAULT_ROLE_PROFILES.get(role) != effective_profile:
            roles[role] = effective_profile

    if profiles:
        changes.append(f"create runtime profile(s): {', '.join(sorted(profiles))}")
    if roles:
        changes.append(
            "bind role -> profile: "
            + ", ".join(f"{r} -> {roles[r]}" for r in sorted(roles))
        )
    consumed = sorted(k for k in V1_ONLY_TOP_LEVEL_KEYS if k in record)
    if consumed:
        changes.append(f"replace legacy block(s) {consumed} with role-canonical 'agents'")

    # 6. Assemble the target record: version, then agents (if any), then other blocks verbatim.
    migrated: "dict[str, object]" = {"version": REPO_LOCAL_CONFIG_V2}
    if profiles or roles:
        agents_block: "dict[str, object]" = {}
        if profiles:
            agents_block["profiles"] = profiles
        if roles:
            agents_block["roles"] = roles
        migrated["agents"] = agents_block
    for key, value in record.items():
        if key == "version" or key in V1_ONLY_TOP_LEVEL_KEYS:
            continue
        migrated[key] = value

    return MigrationResult(
        source_version=source_version,
        target_version=REPO_LOCAL_CONFIG_V2,
        already_current=False,
        migrated=migrated,
        changes=tuple(changes),
    )


__all__ = (
    "ConfigMigrationError",
    "MigrationResult",
    "migrate_record",
)
