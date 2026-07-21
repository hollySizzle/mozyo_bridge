"""Role-canonical agent topology + named runtime profiles (Redmine #14148).

This is the ``version: 2`` replacement for the provider-leaking ``provider_binding``
(role -> provider) and ``agent_launch`` (provider -> lane_class -> argv) blocks. It moves
the product's authority expression from *provider brand* to *workflow role*:

- a **named runtime profile** (:class:`RuntimeProfile`) is the ONLY place a provider and
  its launch argv live. ``provider`` names a **built-in adapter id** (an
  :func:`~mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile.agent_provider_ids`
  entry — the trusted executable boundary); it can never name an executable / argv[0] /
  path, so a repo config can never point the runtime at an arbitrary program. ``launch_argv``
  is a ``lane_class -> [tokens]`` table validated by the same #13425 token / reserved-flag
  rules the v1 ``agent_launch`` block used.
- a **role -> profile topology** (:class:`AgentsTopologyConfig.roles`) binds a closed
  :data:`~mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding.WORKFLOW_ROLES`
  role to a *profile name*. The role is the authority; the profile (and the provider it
  carries) is an exchangeable runtime attribute. Swapping which profile a role points at
  — or renaming a profile's ``provider`` — never changes a workflow gate.

**Canonical resolution vs. current-launch compatibility adapter (Redmine #14148 j#84267).**
The canonical v2 launch resolution is ``role -> profile -> (provider, launch_argv[lane_class])``
(:meth:`AgentsTopologyConfig.resolve_launch_argv_for_role`). The existing herdr launch
chokepoint is *provider-unit* (it launches panes by provider, not workflow role — the mzb1
identity's role segment is the provider token), so it consumes a **compatibility adapter**,
not the canonical semantics: :meth:`to_provider_binding_overrides` produces the ``role ->
provider`` map the #13157 :class:`RoleProviderBinding` consumes, and
:meth:`to_resolved_launch_argv_triples` produces the ``(provider, lane_class, tokens)`` triples
the #13425 ``AgentLaunchConfig`` consumes. The adapter is deliberately lossy — a same
``(provider, lane_class)`` argv collision fails closed at config load — and is explicitly not
the canonical form. Launch-time per-role profile selection arrives with #13647; until then a
role-distinct-but-provider-shared profile pair is a config concept the launch cannot reflect.

Boundary, kept enforced in code:

- **Trusted executable boundary.** ``provider`` must be a registered adapter id; an unknown
  provider fails closed (no arbitrary executable, no plugin load). The provider is a profile
  *value*, never the config's key axis — the v1 leakage this replaces.
- **Fail-closed, closed schema.** Unknown top-level / profile / role keys, a non-mapping
  record, an unsupported version, a boundary-shaped key (module / callable / credential /
  authority, …), a role outside the closed vocabulary, a dangling ``roles`` reference to an
  undeclared profile, or two profiles binding the same ``(provider, lane_class)`` to
  *different* launch argv all raise :class:`AgentsTopologyError`.
- **Behavior-preserving.** An absent ``agents`` block (a v2 config with none) resolves to no
  overrides and no launch argv — byte-for-byte the default topology. The built-in default is
  the compatibility :meth:`RoleProviderBinding.default` map, expressed role-canonically.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (
    agent_provider_ids,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.agent_launch_argv import (
    AgentLaunchArgvError,
    LAUNCH_ARGV_LANE_CLASSES,
    _reject_reserved_managed_flags,
    _validate_launch_argv_token,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (
    DEFAULT_PROFILE_PROVIDERS,
    DEFAULT_ROLE_PROFILES,
    WORKFLOW_ROLES,
    normalize_provider,
    normalize_role,
)

#: The ``agents`` topology block ships in schema ``version: 2``. Declaring it under a v1
#: record is a fail-closed error (the block did not exist there), and the block's own
#: optional ``version`` mirrors this.
AGENTS_TOPOLOGY_VERSION: int = 2

#: The closed top-level keys of the ``agents`` block.
AGENTS_TOPOLOGY_KEYS: frozenset[str] = frozenset({"version", "profiles", "roles"})

#: The closed keys of a single ``profiles.<name>`` entry.
RUNTIME_PROFILE_KEYS: frozenset[str] = frozenset({"provider", "launch_argv"})

#: A profile name: a leading alphanumeric then alphanumerics / ``.`` / ``_`` / ``-``. A
#: label an operator picks — it never selects an executable — but kept to a safe opaque
#: token so it can never smuggle a path, space, or shell metacharacter into any surface
#: that echoes it. Identical spirit to the launch-model token rule.
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Substrings in an ``agents`` key that cross a boundary this surface owns (code loading,
#: authority, routing, credential). The same union the repo-local schema screens, minus the
#: tokens that are *legitimate here* (``role`` is the whole point of the block, and
#: ``provider`` is a profile field name). Screened before structural parsing so the
#: rejection reads as deliberate in an audit.
_FORBIDDEN_AGENTS_KEY_PARTS: tuple[str, ...] = (
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
    "send",
    "send_safety",
    "target",
    "pane",
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


class AgentsTopologyError(ValueError):
    """The ``agents`` topology block violates the closed schema (fail-closed).

    Inherits :class:`ValueError` for the fail-closed semantics the sibling repo-local
    domain errors use. The composing repo-local config loader re-raises this as its own
    ``RepoLocalConfigError`` so a single ``except`` still catches every repo-local-config
    failure.
    """


def _reject_boundary_key(token: object, *, source: str, role: str) -> None:
    """Fail closed on an ``agents`` key that crosses an owned boundary."""
    if not isinstance(token, str):
        return
    lowered = token.lower()
    for part in _FORBIDDEN_AGENTS_KEY_PARTS:
        if part in lowered:
            raise AgentsTopologyError(
                f"{source} {role} {token!r} may not carry a boundary token: this surface "
                f"is config-only and may never load code, name a module / callable / entry "
                f"point, grant authority, or carry a credential (matched forbidden token "
                f"{part!r})."
            )


def _reject_unknown_keys(
    record: "Mapping[object, object]", *, allowed: "frozenset[str]", source: str
) -> None:
    """Fail closed on a non-string / boundary-crossing / unknown record key."""
    for key in record:
        if not isinstance(key, str) or not key:
            raise AgentsTopologyError(
                f"{source} record keys must be non-empty strings; got {key!r}"
            )
        _reject_boundary_key(key, source=source, role="record key")
        if key not in allowed:
            raise AgentsTopologyError(
                f"{source} record has unknown key {key!r}; allowed keys: {sorted(allowed)}"
            )


def _checked_version(record: "Mapping[object, object]", *, source: str) -> int:
    """Return the supported ``agents`` version, failing closed on anything else."""
    version = record.get("version", AGENTS_TOPOLOGY_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise AgentsTopologyError(
            f"{source} 'version' must be an integer, got {version!r}"
        )
    if version != AGENTS_TOPOLOGY_VERSION:
        raise AgentsTopologyError(
            f"unsupported {source} version {version!r}; this build understands version "
            f"{AGENTS_TOPOLOGY_VERSION}"
        )
    return version


@dataclass(frozen=True)
class RuntimeProfile:
    """A named runtime profile: a provider + its per-lane-class launch argv (hashable).

    ``provider`` is a built-in adapter id (the trusted executable boundary). ``launch_argv``
    is a sorted tuple of ``(lane_class, tokens)`` pairs — the same lane-class taxonomy
    (``default`` / ``sublane``) and token rules the v1 ``agent_launch.launch_argv`` used.
    """

    name: str
    provider: str
    launch_argv: "tuple[tuple[str, tuple[str, ...]], ...]" = ()

    @classmethod
    def from_record(
        cls, name: str, record: "Mapping[object, object]", *, source: str
    ) -> "RuntimeProfile":
        """Normalize one ``profiles.<name>`` entry into a typed, validated profile."""
        if not isinstance(record, Mapping):
            raise AgentsTopologyError(
                f"{source} profile {name!r} must be a mapping (a YAML table), got "
                f"{type(record).__name__}"
            )
        _reject_unknown_keys(
            record, allowed=RUNTIME_PROFILE_KEYS, source=f"{source} profile {name!r}"
        )
        provider = record.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            raise AgentsTopologyError(
                f"{source} profile {name!r} 'provider' must be a non-empty string naming a "
                f"built-in adapter id; got {provider!r}"
            )
        provider = normalize_provider(provider)
        known = agent_provider_ids()
        if provider not in known:
            raise AgentsTopologyError(
                f"{source} profile {name!r} provider {provider!r} is not a built-in adapter "
                f"id (allowed: {sorted(known)}); a config profile may only reference a "
                f"registered provider, never an arbitrary executable"
            )
        launch_argv = _parse_profile_launch_argv(
            record.get("launch_argv"), provider=provider, source=f"{source} profile {name!r}"
        )
        return cls(name=name, provider=provider, launch_argv=launch_argv)


def _parse_profile_launch_argv(
    record: object, *, provider: str, source: str
) -> "tuple[tuple[str, tuple[str, ...]], ...]":
    """Normalize a profile ``launch_argv`` (``lane_class -> [tokens]``) into sorted pairs.

    ``None`` yields ``()`` (no extra argv). Each ``lane_class`` is a
    :data:`~mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.agent_launch_argv.LAUNCH_ARGV_LANE_CLASSES`
    value; each token passes the #13425 single-argv-element check; and no token may
    re-specify a mozyo-managed flag for ``provider`` (the managed posture stays
    authoritative). Raised as :class:`AgentsTopologyError` so the block keeps one boundary.
    """
    if record is None:
        return ()
    if not isinstance(record, Mapping):
        raise AgentsTopologyError(
            f"{source} 'launch_argv' must be a mapping of lane_class -> argv list, got "
            f"{type(record).__name__}"
        )
    pairs: list = []
    for lane_class, argv in record.items():
        if not isinstance(lane_class, str) or lane_class not in LAUNCH_ARGV_LANE_CLASSES:
            raise AgentsTopologyError(
                f"{source} 'launch_argv' lane-class key must be one of "
                f"{sorted(LAUNCH_ARGV_LANE_CLASSES)}, got {lane_class!r}"
            )
        if not isinstance(argv, (list, tuple)):
            raise AgentsTopologyError(
                f"{source} 'launch_argv.{lane_class}' must be a list of argv tokens, got "
                f"{type(argv).__name__}"
            )
        tokens = tuple(argv)
        try:
            for token in tokens:
                _validate_launch_argv_token(token, source=source)
            _reject_reserved_managed_flags(provider, tokens, source=source)
        except AgentLaunchArgvError as exc:
            raise AgentsTopologyError(str(exc)) from exc
        pairs.append((lane_class, tokens))
    return tuple(sorted(pairs))


@dataclass(frozen=True)
class AgentsTopologyConfig:
    """The closed ``agents`` block: named runtime profiles + a role -> profile topology.

    The default (no profiles, no roles) resolves to no overrides and no launch argv — the
    behavior-preserving default topology. :meth:`to_provider_binding_overrides` and
    :meth:`to_resolved_launch_argv_triples` project this role-canonical surface onto the exact typed
    inputs the existing #13157 / #13425 records consume, so v2 wires into the runtime with
    no new seam.
    """

    version: int = AGENTS_TOPOLOGY_VERSION
    profiles: "tuple[RuntimeProfile, ...]" = ()
    roles: "tuple[tuple[str, str], ...]" = ()

    @classmethod
    def default(cls) -> "AgentsTopologyConfig":
        """The built-in role-canonical default topology (Redmine #14148 finding 1).

        Projected from the ONE canonical
        :data:`~mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding.DEFAULT_ROLE_PROFILES`
        / ``DEFAULT_PROFILE_PROVIDERS`` — so the default topology is expressed *as profiles*
        (role -> profile -> provider), not an empty override that silently falls back to a
        provider-literal map. The default profiles carry no launch argv, so resolving a v2
        config with no ``agents`` block is byte-for-byte the historical default launch.
        """
        profiles = tuple(
            RuntimeProfile(name=name, provider=provider, launch_argv=())
            for name, provider in sorted(DEFAULT_PROFILE_PROVIDERS.items())
        )
        roles = tuple(sorted(DEFAULT_ROLE_PROFILES.items()))
        return cls(version=AGENTS_TOPOLOGY_VERSION, profiles=profiles, roles=roles)

    def profile_by_name(self, name: str) -> "Optional[RuntimeProfile]":
        """The declared profile named ``name``, or ``None``."""
        for profile in self.profiles:
            if profile.name == name:
                return profile
        return None

    @classmethod
    def from_record(
        cls, record: "Optional[Mapping[object, object]]" = None
    ) -> "AgentsTopologyConfig":
        """Normalize an ``agents`` sub-record into a validated topology (fail-closed).

        ``None`` / an empty mapping yields the default. A non-mapping record, a boundary /
        unknown key, an unsupported version, a malformed profile, a role outside the closed
        vocabulary, a dangling ``roles`` reference, or a launch-argv provider/lane-class
        conflict fails closed with :class:`AgentsTopologyError`.
        """
        source = "agents topology config"
        if record is None or (isinstance(record, Mapping) and not record):
            # Absent or empty ``agents`` block -> the built-in role-canonical default (not an
            # empty topology that would silently defer to a provider-literal map).
            return cls.default()
        if not isinstance(record, Mapping):
            raise AgentsTopologyError(
                f"{source} record must be a mapping (a YAML table), got "
                f"{type(record).__name__}"
            )
        _reject_unknown_keys(record, allowed=AGENTS_TOPOLOGY_KEYS, source=source)
        version = _checked_version(record, source=source)

        profiles = _parse_profiles(record.get("profiles"), source=source)
        roles = _parse_roles(record.get("roles"), profiles=profiles, source=source)
        # The canonical role -> profile -> launch resolution supports two profiles on the
        # same provider (finding 2), so a valid topology is NOT rejected here. The lossy
        # provider-keyed *facade* (:meth:`to_resolved_launch_argv_triples`) fails closed only
        # where a provider-keyed consumer actually requests it.
        return cls(version=version, profiles=profiles, roles=roles)

    def resolved_role_profiles(self) -> "dict[str, str]":
        """Effective role -> profile name: the canonical default overridden by this topology.

        The canonical
        :data:`~mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding.DEFAULT_ROLE_PROFILES`
        is the base; a role this topology declares in :attr:`roles` overrides it. So a role a
        v2 config does not mention still resolves to its role-canonical default profile, never
        a bare provider fallback (Redmine #14148 finding 1).
        """
        resolved = dict(DEFAULT_ROLE_PROFILES)
        resolved.update(dict(self.roles))
        return resolved

    def resolved_profiles(self) -> "dict[str, RuntimeProfile]":
        """Effective profile registry: the canonical default profiles overridden by name.

        The canonical default profiles (``coordination`` -> codex, ``implementation`` ->
        claude, no launch argv) are the base; an operator profile with the same name
        overrides it (e.g. to attach launch argv), and a new name adds a profile. This is
        what carries named-profile identity (provider + per-lane-class launch argv) to the
        role/profile-aware launch resolution (Redmine #14148 finding 2).
        """
        registry: "dict[str, RuntimeProfile]" = {
            name: RuntimeProfile(name=name, provider=provider, launch_argv=())
            for name, provider in DEFAULT_PROFILE_PROVIDERS.items()
        }
        for profile in self.profiles:
            registry[profile.name] = profile
        return registry

    def resolve_profile_for_role(self, role: object) -> "RuntimeProfile":
        """The effective runtime profile bound to ``role`` (role -> profile), fail-closed.

        Resolves ``role -> profile name`` (:meth:`resolved_role_profiles`) then
        ``profile name -> profile`` (:meth:`resolved_profiles`). An unknown role or a
        dangling profile name fails closed with :class:`AgentsTopologyError` — never a
        guessed provider.
        """
        role_norm = normalize_role(role)
        name = self.resolved_role_profiles().get(role_norm)
        if name is None:
            raise AgentsTopologyError(
                f"workflow role {role_norm!r} has no profile binding; known roles: "
                f"{', '.join(sorted(WORKFLOW_ROLES))}"
            )
        profile = self.resolved_profiles().get(name)
        if profile is None:
            raise AgentsTopologyError(
                f"workflow role {role_norm!r} references undeclared profile {name!r}"
            )
        return profile

    def resolve_provider_for_role(self, role: object) -> str:
        """The runtime provider bound to ``role`` via ``role -> profile -> provider``."""
        return self.resolve_profile_for_role(role).provider

    def resolve_launch_argv_for_role(
        self, role: object, lane_class: str
    ) -> "list[str]":
        """The launch argv for ``role`` at ``lane_class`` via ``role -> profile -> argv``.

        The canonical v2 launch resolution (Redmine #14148 finding 2): profile identity is
        preserved, so two roles on the same provider but different profiles get different
        argv, and a lane class a profile does not configure yields ``[]`` — never inherited
        from another lane class (the #13451 sublane-has-no-model invariant).
        """
        profile = self.resolve_profile_for_role(role)
        for lane, tokens in profile.launch_argv:
            if lane == lane_class:
                return list(tokens)
        return []

    def to_resolved_launch_argv_triples(
        self,
    ) -> "tuple[tuple[str, str, tuple[str, ...]], ...]":
        """The ``(provider, lane_class, tokens)`` **current-launch compatibility adapter**.

        This is NOT the canonical v2 semantics (that is
        :meth:`resolve_launch_argv_for_role`, ``role -> profile -> launch_argv``). It is a
        deliberately lossy projection of the resolved profiles onto the provider axis, so the
        existing *provider-unit* herdr launch chokepoint (which launches panes by provider,
        not by workflow role) can consume it via the #13425 :class:`AgentLaunchConfig`
        (Redmine #14148 Design Consultation Answer j#84267 condition 1). It preserves every
        *launchable* deployed config: profiles on the same provider that differ only by lane
        class (the #13451 shape) fold cleanly.

        Runtime limitation (docs must state this; a v2 config with two profiles on the same
        provider that a role could pick between is a config-authority concept the current
        launch cannot yet reflect — that arrives with #13647's launch-time lane-role
        vocabulary): two profiles binding the **same** ``(provider, lane_class)`` to
        **different** argv is a provider-unit launch collision (one pane, two argv). Per
        j#84267 condition 2 it fails closed here — at the earliest pre-side-effect boundary,
        since the loader materializes this at config-load time — naming the two colliding
        profiles, never silently selecting / merging / inheriting.
        """
        by_key: "dict[tuple[str, str], tuple[tuple[str, ...], str]]" = {}
        for profile in sorted(self.resolved_profiles().values(), key=lambda p: p.name):
            for lane_class, tokens in profile.launch_argv:
                key = (profile.provider, lane_class)
                existing = by_key.get(key)
                if existing is not None and existing[0] != tokens:
                    prev_tokens, prev_name = existing
                    raise AgentsTopologyError(
                        f"provider-unit launch collision: profiles {prev_name!r} and "
                        f"{profile.name!r} both bind provider {profile.provider!r} lane_class "
                        f"{lane_class!r} but to different launch argv ({list(prev_tokens)} vs "
                        f"{list(tokens)}). The current launch places one pane per "
                        f"(provider, lane_class), so it cannot reflect two role-distinct "
                        f"profiles here; resolve config semantics via role -> profile "
                        f"(canonical) and see Redmine #13647 for launch-time lane-role support"
                    )
                by_key[key] = (tokens, profile.name)
        return tuple(
            sorted(
                (provider, lane_class, tokens)
                for (provider, lane_class), (tokens, _name) in by_key.items()
            )
        )

    def to_provider_binding_overrides(self) -> "dict[str, str]":
        """The effective ``role -> provider`` map (the #13157 RoleProviderBinding input).

        Resolved for every workflow role via ``role -> profile -> provider`` (canonical
        default merged with this topology). Passed as overrides onto the compatibility
        default binding, it reproduces the exact effective binding for the current
        provider-keyed consumers while the canonical authority stays role -> profile.
        """
        return {
            role: self.resolve_provider_for_role(role)
            for role in sorted(WORKFLOW_ROLES)
        }


def _parse_profiles(
    record: object, *, source: str
) -> "tuple[RuntimeProfile, ...]":
    """Normalize the ``profiles`` mapping into sorted, validated profiles."""
    if record is None:
        return ()
    if not isinstance(record, Mapping):
        raise AgentsTopologyError(
            f"{source} 'profiles' must be a mapping of name -> profile, got "
            f"{type(record).__name__}"
        )
    profiles: list[RuntimeProfile] = []
    seen: set[str] = set()
    for name, profile_record in record.items():
        if not isinstance(name, str) or not name.strip():
            raise AgentsTopologyError(
                f"{source} profile names must be non-empty strings; got {name!r}"
            )
        _reject_boundary_key(name, source=source, role="profile name")
        if not _PROFILE_NAME_RE.match(name):
            raise AgentsTopologyError(
                f"{source} profile name {name!r} must match {_PROFILE_NAME_RE.pattern} "
                f"(a leading alphanumeric then alphanumerics / '.' / '_' / '-')"
            )
        if name in seen:
            raise AgentsTopologyError(f"{source} declares duplicate profile {name!r}")
        seen.add(name)
        profiles.append(RuntimeProfile.from_record(name, profile_record, source=source))
    return tuple(sorted(profiles, key=lambda p: p.name))


def _parse_roles(
    record: object, *, profiles: "tuple[RuntimeProfile, ...]", source: str
) -> "tuple[tuple[str, str], ...]":
    """Normalize the ``roles`` (role -> profile name) map, fail-closed on a dangling ref."""
    if record is None:
        return ()
    if not isinstance(record, Mapping):
        raise AgentsTopologyError(
            f"{source} 'roles' must be a mapping of role -> profile name, got "
            f"{type(record).__name__}"
        )
    declared = {p.name for p in profiles}
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_role, profile_name in record.items():
        if not isinstance(raw_role, str) or not raw_role.strip():
            raise AgentsTopologyError(
                f"{source} 'roles' role keys must be non-empty strings; got {raw_role!r}"
            )
        role = normalize_role(raw_role)
        if role not in WORKFLOW_ROLES:
            raise AgentsTopologyError(
                f"{source} 'roles' has unknown workflow role {role!r}; known roles: "
                f"{', '.join(sorted(WORKFLOW_ROLES))}"
            )
        if role in seen:
            raise AgentsTopologyError(f"{source} 'roles' binds role {role!r} twice")
        seen.add(role)
        if not isinstance(profile_name, str) or not profile_name.strip():
            raise AgentsTopologyError(
                f"{source} 'roles' value for {role!r} must be a non-empty profile name; got "
                f"{profile_name!r}"
            )
        if profile_name not in declared:
            raise AgentsTopologyError(
                f"{source} 'roles' role {role!r} references undeclared profile "
                f"{profile_name!r}; declared profiles: {sorted(declared)}"
            )
        pairs.append((role, profile_name))
    return tuple(sorted(pairs))


__all__ = (
    "AGENTS_TOPOLOGY_VERSION",
    "AGENTS_TOPOLOGY_KEYS",
    "RUNTIME_PROFILE_KEYS",
    "AgentsTopologyError",
    "RuntimeProfile",
    "AgentsTopologyConfig",
)
