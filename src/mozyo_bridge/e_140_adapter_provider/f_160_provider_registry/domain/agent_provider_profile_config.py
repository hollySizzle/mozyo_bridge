"""Agent provider profile schema boundary (Redmine #13441, Design Answer j#76725).

The agent-launch layer used to carry LLM-provider knowledge as hard-coded sets
scattered across four-plus modules (``AGENT_COMMANDS`` / ``LAUNCH_ARGV_PROVIDERS``
/ ``RESERVED_MANAGED_FLAGS`` / ``AGENT_PROVIDERS``), so a CLI flag change or a new
interactive-CLI LLM meant editing source in several places. This module is the
pure *schema* for the data-driven replacement: an **agent provider profile**
declares, as trusted pure data, what mozyo needs to know to launch and recognize
one interactive-CLI agent provider.

It is the typed sibling of :mod:`.provider_registry` (which classifies *adapter*
providers — ticket / terminal-runtime / presentation), NOT an extension of it: an
agent provider is a launchable CLI, not an adapter category, so the two registries
stay separate typed shapes inside ``f_160_provider_registry`` (Design Answer
j#76715 / j#76725).

What a profile **owns** (Design Answer j#76725 "Registry ownership"):

- ``provider_id`` — the stable launch/identity token (``claude`` / ``codex``);
- ``executable`` — *trusted metadata only*: the command **basename** and the name
  of the trusted-env override variable. A committed profile may never carry a host
  absolute path, an argv, a module path, or a callable — resolution to a verified
  absolute executable happens at launch time in the trusted resolver
  (:mod:`..application.agent_provider_executable`), never from committed data;
- ``protocol`` — the interaction-protocol family. Data absorbs a *same-protocol*
  provider; a genuinely different protocol (different TUI / status semantics /
  turn-start behavior) still needs adapter code, and the closed
  :class:`InteractionProtocol` enum is what makes that boundary explicit instead
  of silently mis-launching;
- ``discovery_aliases`` / ``process_names`` — how the provider is recognized in
  pane/process discovery;
- ``capabilities`` — closed, *mechanical* capability tokens (what the launch
  mechanism may do for this provider), never an authority;
- ``managed_flags`` — the closed managed-flag **concept** map
  (concept -> flag spelling, e.g. ``permission_mode`` -> ``--permission-mode``).
  This is what turns "Claude's permission flag is called X" from a source branch
  into data, and it doubles as the reserved-flag vocabulary an operator's repo
  config may not re-specify.

What a profile may **never** own (fail-closed here, not merely documented):

- a workflow role, a provider binding, or any routing / gate / approval authority
  — those stay core-owned (:data:`FORBIDDEN_PROVIDER_AUTHORITIES` and
  :data:`FORBIDDEN_PROFILE_TOKENS`);
- a default pair / launch topology — registering a profile makes a provider
  *expressible*, never *launched*. Default topology is a separate contract;
- an arbitrary callable, module path, entry point, or host executable path — so a
  profile can never introduce foreign code or an unverified binary;
- a model / effort *semantic* schema — ``launch_argv`` model/effort tokens stay
  opaque operator-owned argv (#13425), as corrected in j#75397.

The module is pure: dataclasses + validation, no IO and no YAML import. The
packaged-artifact read lives in :mod:`.agent_provider_profile` so this schema stays
unit-testable without a filesystem, mirroring
:mod:`mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile_config`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, Optional

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.provider_registry import (
    FORBIDDEN_PROVIDER_AUTHORITIES,
)


class AgentProviderProfileError(ValueError):
    """An agent provider profile violates the closed schema (fail-closed).

    Inherits :class:`ValueError` like the sibling domain errors, so a malformed
    profile fails closed at load rather than launching a provider on a partially
    understood contract.
    """


class InteractionProtocol(str, Enum):
    """The interaction-protocol family a provider's runtime speaks.

    A closed, core-owned vocabulary. Data-driven profiles can absorb a *new
    provider of an existing family* (a claude/codex-shaped interactive CLI-TUI);
    they explicitly cannot absorb a provider whose protocol differs (a different
    TUI, different status semantics, different turn-start behavior) — that needs
    adapter code, and the honest limit is recorded in the #13441 description.
    Naming the family in data is what lets the launch mechanism *reject* an
    unsupported protocol before it spawns a pane, instead of mis-driving it.
    """

    #: The claude/codex family: an interactive terminal CLI with a TUI composer.
    INTERACTIVE_CLI_TUI = "interactive_cli_tui"


class ManagedFlagConcept(str, Enum):
    """Concepts mozyo manages at the launch chokepoint, independent of spelling.

    A profile maps the *concept* to that provider's *flag spelling*, which is what
    makes a CLI rename a data edit instead of a source edit. The concept set is
    closed and core-owned: mozyo only manages a launch concept it actually
    implements policy for, so an unknown concept in a profile fails closed rather
    than being rendered as an unvalidated flag.
    """

    #: The managed permission / approval posture (#11925 / #13360). Claude spells
    #: it ``--permission-mode``; a provider without the concept simply omits it.
    PERMISSION_MODE = "permission_mode"


class AgentCapability(str, Enum):
    """Closed, mechanical capability tokens — what the launch mechanism may do.

    Purely mechanical: these describe launch/identity mechanics, never authority.
    An authority-shaped token is rejected at construction (the same posture as
    :class:`~.provider_registry.BuiltinProvider.capabilities`), so a profile can
    never declare itself a coordinator, an auditor, or a routing authority.
    """

    #: Runs as an interactive terminal UI (composer-driven turn start).
    INTERACTIVE_TUI = "interactive_tui"
    #: Accepts operator-supplied ``launch_argv`` pass-through tokens (#13425).
    LAUNCH_ARGV_OVERRIDE = "launch_argv_override"
    #: Accepts the managed permission-mode posture (#13360).
    MANAGED_PERMISSION_MODE = "managed_permission_mode"
    #: Re-expresses injected identity as tool-shell ``-c`` overrides (Codex, #13614),
    #: because the provider applies its own env policy to spawned tool shells.
    TOOL_SHELL_ENV_OVERRIDES = "tool_shell_env_overrides"


#: Tokens a profile may never use as a provider id, capability, or managed-flag
#: concept. These name *workflow role / binding / topology* authority — the axes
#: j#76725 forbids a profile from owning. ``provider_binding`` (role -> provider)
#: and the default launch topology stay separate contracts, so a profile can never
#: promote itself into a role or into the default pair.
FORBIDDEN_PROFILE_TOKENS: frozenset[str] = frozenset(
    {
        "coordinator",
        "delegated_coordinator",
        "implementation_gateway",
        "implementation_worker",
        "auditor",
        "provider_binding",
        "role",
        "workflow_role",
        "default_pair",
        "launch_topology",
    }
) | FORBIDDEN_PROVIDER_AUTHORITIES

#: Core-owned identity sentinels a profile may never claim as a ``provider_id`` or a
#: ``discovery_alias`` (Redmine #13441 review R1-F3). ``unknown`` is the *outcome* the
#: role resolvers return when no provider is identified (``AGENT_KIND_UNKNOWN``); a
#: profile registering it would make "unidentified" indistinguishable from a real
#: provider and let an unresolved pane resolve to one. The built-in data happens not to
#: use these names — but that is a coincidence of the data, not an invariant, so it is
#: enforced here rather than assumed.
RESERVED_IDENTITY_TOKENS: frozenset[str] = frozenset({"unknown"})

#: Process basenames that identify a *host runtime*, not a provider, and so may never
#: appear in a profile's ``process_names`` (Redmine #13441 review R1-F3). Both built-in
#: CLIs are Node programs, so a ``node`` process is receiver-agnostic: claiming it would
#: let ANY node process be identified as that provider and mis-resolve a pane's role.
#: ``pane_resolver`` keeps ``node`` as a weak *agent-process* hint precisely because it
#: cannot name a role; a profile must not be able to give it one.
RECEIVER_AGNOSTIC_PROCESSES: frozenset[str] = frozenset({"node"})

#: Executable-metadata keys a profile may carry. Deliberately *not* here: any key
#: that could carry a host path, an argv, a module path, an entry point, or a
#: callable. A profile names a command basename and a trusted-env override
#: variable *name*; the value of that variable is read from the trusted
#: environment at launch, never from committed data (#13245 hostile-checkout
#: boundary, restated in j#76725).
_EXECUTABLE_KEYS: frozenset[str] = frozenset({"command", "env_override"})

_PROFILE_ENTRY_KEYS: frozenset[str] = frozenset(
    {
        "protocol",
        "executable",
        "discovery_aliases",
        "process_names",
        "capabilities",
        "managed_flags",
    }
)

_CONFIG_KEYS: frozenset[str] = frozenset({"version", "source", "profiles"})

#: Wheel-packaged resource (a sibling of the registry module) shipping the built-in
#: profiles. Read via ``importlib.resources`` in :mod:`.agent_provider_profile` — a
#: package-anchored resource, never a cwd / worktree path walk, so a hostile repo
#: checkout cannot shadow the built-in profile data.
AGENT_PROVIDER_PROFILE_RESOURCE = "agent_provider_profiles.yaml"


def _reject_forbidden_token(token: str, *, field: str, provider_id: str) -> None:
    """Fail closed when a profile token names a core-owned authority / role axis."""
    if token in FORBIDDEN_PROFILE_TOKENS:
        raise AgentProviderProfileError(
            f"agent provider profile {provider_id!r} may not name {token!r} in "
            f"{field}: workflow role, provider binding, routing / gate / approval "
            f"authority, and default launch topology stay core-owned and are never "
            f"declared by a provider profile (Redmine #13441 j#76725)"
        )


def _reject_reserved_identity(token: str, *, field: str, provider_id: str) -> None:
    """Fail closed when a profile claims a core identity sentinel (R1-F3)."""
    if token in RESERVED_IDENTITY_TOKENS:
        raise AgentProviderProfileError(
            f"agent provider profile {provider_id!r} may not claim the reserved core "
            f"identity token {token!r} in {field}: it is the sentinel the role resolvers "
            f"return for an UNidentified agent, so a provider claiming it would make "
            f"'no provider identified' resolve to a real provider"
        )


def _string_tuple(value: object, *, field: str, provider_id: str) -> tuple[str, ...]:
    """Normalize a list of non-empty strings, rejecting a bare str/bytes.

    A bare string is iterable, so ``tuple("coordinator")`` would explode into
    characters and slip past :func:`_reject_forbidden_token` — the same authority
    leak :func:`~.provider_registry._frozen_label_set` guards against. Order is
    preserved (discovery alias order is observable) and duplicates are rejected
    rather than silently deduped, so a typo cannot hide in the data.
    """
    if isinstance(value, (str, bytes)):
        raise AgentProviderProfileError(
            f"agent provider profile {provider_id!r} {field} must be a list of "
            f"strings, not a bare {type(value).__name__}; a bare string is iterated "
            f"character-by-character and would bypass the authority check"
        )
    if not isinstance(value, Sequence):
        raise AgentProviderProfileError(
            f"agent provider profile {provider_id!r} {field} must be a list of "
            f"strings, got {type(value).__name__}"
        )
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise AgentProviderProfileError(
                f"agent provider profile {provider_id!r} {field} entries must be "
                f"non-empty strings; got {item!r}"
            )
        if item in items:
            raise AgentProviderProfileError(
                f"agent provider profile {provider_id!r} {field} lists {item!r} "
                f"more than once"
            )
        _reject_forbidden_token(item, field=field, provider_id=provider_id)
        items.append(item)
    return tuple(items)


@dataclass(frozen=True)
class TrustedExecutable:
    """Trusted *metadata* for resolving a provider's executable — never a path.

    ``command`` is the executable **basename** searched on the trusted ``PATH``
    (e.g. ``claude``). ``env_override`` names the trusted-environment variable an
    operator may set to an absolute path to pin the binary. Both are pure data;
    neither is a path, and the profile never stores a resolved host path — that
    would bake one machine's layout into committed data and (worse) let a repo
    checkout name the binary that runs. The actual resolution to a verified
    absolute realpath happens in :mod:`..application.agent_provider_executable`.

    ``command`` is rejected if it looks like a path (contains a separator) or is
    absolute: a basename is the only shape the trusted PATH search may take, so a
    profile can never smuggle ``/tmp/evil`` or ``./evil`` in as a "command".
    """

    command: str
    env_override: str

    def __post_init__(self) -> None:
        if not isinstance(self.command, str) or not self.command.strip():
            raise AgentProviderProfileError(
                "agent provider executable 'command' must be a non-empty string"
            )
        if not isinstance(self.env_override, str) or not self.env_override.strip():
            raise AgentProviderProfileError(
                "agent provider executable 'env_override' must be a non-empty string"
            )
        if "/" in self.command or "\\" in self.command or self.command.startswith("."):
            raise AgentProviderProfileError(
                f"agent provider executable 'command' must be a bare basename, got "
                f"{self.command!r}: a committed profile may never carry a path "
                f"(absolute, relative, or dotted) — the executable is resolved from "
                f"the trusted environment at launch (Redmine #13245 / #13441 j#76725)"
            )

    @classmethod
    def from_record(cls, record: object, *, provider_id: str) -> "TrustedExecutable":
        """Validate an ``executable`` block, failing closed on any unknown key.

        An unknown key is rejected rather than ignored: that is what stops a
        profile from carrying a ``path`` / ``argv`` / ``module`` / ``entry_point``
        field that a future reader might honor.
        """
        if not isinstance(record, Mapping):
            raise AgentProviderProfileError(
                f"agent provider profile {provider_id!r} 'executable' must be a "
                f"mapping, got {type(record).__name__}"
            )
        unknown = set(record) - _EXECUTABLE_KEYS
        if unknown:
            raise AgentProviderProfileError(
                f"unknown 'executable' key(s) {sorted(map(repr, unknown))} in agent "
                f"provider profile {provider_id!r}; allowed: {sorted(_EXECUTABLE_KEYS)}. "
                f"An executable path / argv / module path is never accepted from data."
            )
        missing = _EXECUTABLE_KEYS - set(record)
        if missing:
            raise AgentProviderProfileError(
                f"agent provider profile {provider_id!r} 'executable' is missing "
                f"{sorted(missing)}"
            )
        return cls(
            command=record["command"],
            env_override=record["env_override"],
        )


@dataclass(frozen=True)
class AgentProviderProfile:
    """One agent provider, declared as trusted pure data (Redmine #13441).

    Frozen and behavior-free: this is metadata *about* a launchable provider,
    never a handle to one. Every collection is normalized to a frozen shape so the
    record stays hashable, and every token is checked against
    :data:`FORBIDDEN_PROFILE_TOKENS` so a profile can never declare an authority
    the core reserves.
    """

    provider_id: str
    protocol: InteractionProtocol
    executable: TrustedExecutable
    summary: str = ""
    discovery_aliases: tuple[str, ...] = ()
    process_names: tuple[str, ...] = ()
    capabilities: frozenset[AgentCapability] = field(default_factory=frozenset)
    managed_flags: tuple[tuple[ManagedFlagConcept, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, str) or not self.provider_id.strip():
            raise AgentProviderProfileError(
                "agent provider profile 'provider_id' must be a non-empty string"
            )
        _reject_forbidden_token(
            self.provider_id, field="provider_id", provider_id=self.provider_id
        )
        _reject_reserved_identity(
            self.provider_id, field="provider_id", provider_id=self.provider_id
        )
        for alias in self.discovery_aliases:
            _reject_reserved_identity(
                alias, field="discovery_aliases", provider_id=self.provider_id
            )
        for process in self.process_names:
            if process in RECEIVER_AGNOSTIC_PROCESSES:
                raise AgentProviderProfileError(
                    f"agent provider profile {self.provider_id!r} may not claim the "
                    f"receiver-agnostic host process {process!r} in process_names: both "
                    f"built-in CLIs run on it, so it identifies a runtime, not a "
                    f"provider — claiming it would let any such process be resolved as "
                    f"this provider (Redmine #13441 R1-F3)"
                )
        if not isinstance(self.protocol, InteractionProtocol):
            raise AgentProviderProfileError(
                f"agent provider profile {self.provider_id!r} 'protocol' must be an "
                f"InteractionProtocol, got {self.protocol!r}"
            )
        if not isinstance(self.executable, TrustedExecutable):
            raise AgentProviderProfileError(
                f"agent provider profile {self.provider_id!r} 'executable' must be a "
                f"TrustedExecutable, got {type(self.executable).__name__}"
            )
        for cap in self.capabilities:
            if not isinstance(cap, AgentCapability):
                raise AgentProviderProfileError(
                    f"agent provider profile {self.provider_id!r} capability must be "
                    f"an AgentCapability, got {cap!r}"
                )
        # The managed permission-mode posture is only rendered for a provider that
        # declares BOTH the capability and the flag spelling. Declaring one without
        # the other is a half-specified contract: the launch chokepoint would either
        # know a concept it cannot spell, or spell a flag it is not allowed to apply.
        # Fail closed rather than silently dropping the managed posture (a Claude
        # worker booting prompt-gated is exactly the #13360 stall).
        concepts = {concept for concept, _ in self.managed_flags}
        has_permission_flag = ManagedFlagConcept.PERMISSION_MODE in concepts
        has_permission_cap = AgentCapability.MANAGED_PERMISSION_MODE in self.capabilities
        if has_permission_flag != has_permission_cap:
            raise AgentProviderProfileError(
                f"agent provider profile {self.provider_id!r} must declare the "
                f"{AgentCapability.MANAGED_PERMISSION_MODE.value!r} capability and the "
                f"{ManagedFlagConcept.PERMISSION_MODE.value!r} managed flag together "
                f"(got capability={has_permission_cap}, flag={has_permission_flag}); a "
                f"half-declared managed posture would silently drop the flag"
            )

    @property
    def managed_flag_map(self) -> dict[str, str]:
        """The managed-flag concepts as a plain ``{concept: flag}`` dict (a copy)."""
        return {concept.value: flag for concept, flag in self.managed_flags}

    @property
    def reserved_flags(self) -> tuple[str, ...]:
        """Flag spellings an operator's repo config may not re-specify (#13425 Q4).

        Config ``launch_argv`` renders *after* the managed flag, so CLI last-wins
        would let a config token override the managed posture. The reserved
        vocabulary is therefore exactly this provider's managed-flag spellings —
        derived from the profile instead of the old hard-coded
        ``RESERVED_MANAGED_FLAGS`` table.
        """
        return tuple(flag for _, flag in self.managed_flags)

    def managed_flag(self, concept: ManagedFlagConcept) -> Optional[str]:
        """This provider's spelling of ``concept``, or ``None`` if it has none."""
        for declared, flag in self.managed_flags:
            if declared is concept:
                return flag
        return None

    def has_capability(self, capability: AgentCapability) -> bool:
        """Whether the provider declares ``capability`` (mechanical, not authority)."""
        return capability in self.capabilities

    @classmethod
    def from_record(cls, provider_id: object, record: object) -> "AgentProviderProfile":
        """Validate one already-parsed profile entry, failing closed.

        Rejects: a non-string / empty / authority-shaped ``provider_id``, a
        non-mapping entry, an unknown or missing entry key, an unknown protocol,
        an unknown capability, an unknown managed-flag concept, a non-flag-shaped
        flag spelling, and every shape :class:`TrustedExecutable` refuses.
        """
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise AgentProviderProfileError(
                f"agent provider profile id must be a non-empty string; got "
                f"{provider_id!r}"
            )
        if not isinstance(record, Mapping):
            raise AgentProviderProfileError(
                f"agent provider profile {provider_id!r} must be a mapping, got "
                f"{type(record).__name__}"
            )
        unknown = set(record) - _PROFILE_ENTRY_KEYS
        if unknown:
            raise AgentProviderProfileError(
                f"unknown key(s) {sorted(map(repr, unknown))} in agent provider "
                f"profile {provider_id!r}; allowed: {sorted(_PROFILE_ENTRY_KEYS)}. A "
                f"profile never carries a workflow role, a binding, a topology, or a "
                f"module path (Redmine #13441 j#76725)."
            )
        missing = {"protocol", "executable"} - set(record)
        if missing:
            raise AgentProviderProfileError(
                f"agent provider profile {provider_id!r} is missing required key(s) "
                f"{sorted(missing)}"
            )

        raw_protocol = record["protocol"]
        try:
            protocol = InteractionProtocol(raw_protocol)
        except ValueError as exc:
            raise AgentProviderProfileError(
                f"agent provider profile {provider_id!r} declares unsupported protocol "
                f"{raw_protocol!r}; supported: "
                f"{sorted(p.value for p in InteractionProtocol)}. A provider whose "
                f"interaction protocol differs needs adapter code, not a data profile."
            ) from exc

        raw_caps = record.get("capabilities", [])
        cap_tokens = _string_tuple(
            raw_caps, field="capabilities", provider_id=provider_id
        )
        capabilities: set[AgentCapability] = set()
        for token in cap_tokens:
            try:
                capabilities.add(AgentCapability(token))
            except ValueError as exc:
                raise AgentProviderProfileError(
                    f"agent provider profile {provider_id!r} declares unknown "
                    f"capability {token!r}; known: "
                    f"{sorted(c.value for c in AgentCapability)}"
                ) from exc

        raw_flags = record.get("managed_flags", {}) or {}
        if not isinstance(raw_flags, Mapping):
            raise AgentProviderProfileError(
                f"agent provider profile {provider_id!r} 'managed_flags' must be a "
                f"mapping of concept -> flag, got {type(raw_flags).__name__}"
            )
        managed: list[tuple[ManagedFlagConcept, str]] = []
        for raw_concept, flag in raw_flags.items():
            if not isinstance(raw_concept, str):
                raise AgentProviderProfileError(
                    f"agent provider profile {provider_id!r} managed-flag concept must "
                    f"be a string, got {raw_concept!r}"
                )
            _reject_forbidden_token(
                raw_concept, field="managed_flags", provider_id=provider_id
            )
            try:
                concept = ManagedFlagConcept(raw_concept)
            except ValueError as exc:
                raise AgentProviderProfileError(
                    f"agent provider profile {provider_id!r} declares unknown "
                    f"managed-flag concept {raw_concept!r}; known: "
                    f"{sorted(c.value for c in ManagedFlagConcept)}. mozyo only manages "
                    f"a launch concept it implements policy for."
                ) from exc
            if not isinstance(flag, str) or not flag.startswith("--"):
                raise AgentProviderProfileError(
                    f"agent provider profile {provider_id!r} managed-flag "
                    f"{raw_concept!r} must be a long-option string starting with '--', "
                    f"got {flag!r}"
                )
            managed.append((concept, flag))

        return cls(
            provider_id=provider_id,
            protocol=protocol,
            executable=TrustedExecutable.from_record(
                record["executable"], provider_id=provider_id
            ),
            discovery_aliases=_string_tuple(
                record.get("discovery_aliases", []),
                field="discovery_aliases",
                provider_id=provider_id,
            ),
            process_names=_string_tuple(
                record.get("process_names", []),
                field="process_names",
                provider_id=provider_id,
            ),
            capabilities=frozenset(capabilities),
            managed_flags=tuple(sorted(managed, key=lambda pair: pair[0].value)),
        )


class AgentProviderProfileRegistry:
    """An in-memory registry of agent provider profiles — data, not code.

    Registration takes an :class:`AgentProviderProfile` *description* only; there
    is no dynamic import, entry point, or callable, so registering a provider can
    never execute foreign code. Ids are unique (a duplicate is an error, never a
    silent overwrite), and iteration order is id-sorted so every derived
    vocabulary is deterministic.

    The registry makes a provider **expressible**, never **launched**: it holds no
    default pair, no role, and no binding, so adding a profile cannot by itself
    change what mozyo starts (Design Answer j#76725).
    """

    def __init__(self) -> None:
        self._by_id: dict[str, AgentProviderProfile] = {}

    def register(self, profile: AgentProviderProfile) -> AgentProviderProfile:
        """Record one profile; reject a non-profile or a duplicate id."""
        if not isinstance(profile, AgentProviderProfile):
            raise AgentProviderProfileError(
                "register expects an AgentProviderProfile description, not "
                f"{type(profile).__name__}; the registry never loads code."
            )
        if profile.provider_id in self._by_id:
            raise AgentProviderProfileError(
                f"duplicate agent provider profile id: {profile.provider_id!r}"
            )
        self._by_id[profile.provider_id] = profile
        return profile

    def get(self, provider_id: str) -> Optional[AgentProviderProfile]:
        """The profile for ``provider_id``, or ``None`` if unregistered."""
        return self._by_id.get(provider_id)

    def require(self, provider_id: str) -> AgentProviderProfile:
        """The profile for ``provider_id``, failing closed when unknown.

        This is the launch-path accessor: an unknown provider must never reach a
        pane / process side effect, so it raises instead of returning ``None``.
        """
        profile = self._by_id.get(provider_id)
        if profile is None:
            raise AgentProviderProfileError(
                f"unknown agent provider {provider_id!r}; known providers: "
                f"{sorted(self._by_id)}"
            )
        return profile

    def profiles(self) -> tuple[AgentProviderProfile, ...]:
        """Every registered profile, id-sorted for deterministic derived vocab."""
        return tuple(self._by_id[pid] for pid in sorted(self._by_id))

    def provider_ids(self) -> frozenset[str]:
        """The launch/identity vocabulary — the replacement for the hard-coded sets."""
        return frozenset(self._by_id)

    def commands(self) -> dict[str, str]:
        """``{provider_id: command basename}`` — the ``AGENT_COMMANDS`` replacement."""
        return {p.provider_id: p.executable.command for p in self.profiles()}

    def process_names(self) -> frozenset[str]:
        """Every process basename that identifies some registered provider."""
        names: set[str] = set()
        for profile in self.profiles():
            names.update(profile.process_names)
        return frozenset(names)

    def process_owners(self) -> dict[str, str]:
        """``{process basename: provider_id}``, exact-one across providers.

        A process basename claimed by two providers is rejected (Redmine #13441 review
        R1-F3). Consumers build a ``{process: provider}`` lookup from these profiles, so
        a duplicate would silently resolve **last-wins** — a pane running provider A's
        process would be identified as provider B purely by registration order. Discovery
        that guesses is exactly what the fail-closed posture forbids, so this is an error
        at load, not a silent pick.
        """
        owners: dict[str, str] = {}
        for profile in self.profiles():
            for process in profile.process_names:
                if process in owners and owners[process] != profile.provider_id:
                    raise AgentProviderProfileError(
                        f"process name {process!r} is claimed by both "
                        f"{owners[process]!r} and {profile.provider_id!r}; a duplicate "
                        f"would make process-based role resolution last-wins"
                    )
                owners[process] = profile.provider_id
        return owners

    def discovery_aliases(self) -> dict[str, str]:
        """``{alias: provider_id}`` over every declared discovery alias.

        A duplicate alias across two providers is rejected: an ambiguous alias
        would make pane discovery pick a provider arbitrarily, and discovery that
        guesses is exactly what the fail-closed posture forbids.
        """
        mapping: dict[str, str] = {}
        for profile in self.profiles():
            for alias in profile.discovery_aliases:
                if alias in mapping and mapping[alias] != profile.provider_id:
                    raise AgentProviderProfileError(
                        f"discovery alias {alias!r} is claimed by both "
                        f"{mapping[alias]!r} and {profile.provider_id!r}; an ambiguous "
                        f"alias would make discovery guess a provider"
                    )
                mapping[alias] = profile.provider_id
        return mapping

    def reserved_managed_flags(self) -> dict[str, tuple[str, ...]]:
        """``{provider_id: reserved flags}`` — the ``RESERVED_MANAGED_FLAGS`` replacement.

        Only providers that actually reserve a flag appear, so the shape matches
        the historical table (which listed Claude only).
        """
        return {
            p.provider_id: p.reserved_flags for p in self.profiles() if p.reserved_flags
        }

    def __contains__(self, provider_id: object) -> bool:
        return provider_id in self._by_id

    def __iter__(self) -> Iterator[AgentProviderProfile]:
        return iter(self.profiles())

    def __len__(self) -> int:
        return len(self._by_id)


@dataclass(frozen=True)
class AgentProviderProfileConfig:
    """A validated agent-provider-profile artifact (version + source + profiles).

    ``version`` / ``source`` are durable pointers to the contract the data
    implements, mirroring :class:`~...f_130_handoff_routing.domain.role_profile_config.RoleProfileConfig`.
    """

    version: str
    source: str
    profiles: tuple[AgentProviderProfile, ...]

    @classmethod
    def from_record(cls, record: object) -> "AgentProviderProfileConfig":
        """Validate an already-parsed artifact mapping, failing closed.

        Structure only — no IO. The packaged read + registry seeding live in
        :mod:`.agent_provider_profile`.
        """
        if not isinstance(record, Mapping):
            raise AgentProviderProfileError(
                f"agent provider profile config must be a mapping; got "
                f"{type(record).__name__}"
            )
        unknown = set(record) - _CONFIG_KEYS
        if unknown:
            raise AgentProviderProfileError(
                f"unknown agent provider profile config key(s) "
                f"{sorted(map(repr, unknown))}; expected {sorted(_CONFIG_KEYS)}"
            )
        version = record.get("version")
        if not isinstance(version, str) or not version.strip():
            raise AgentProviderProfileError(
                "agent provider profile config 'version' must be a non-empty string"
            )
        source = record.get("source")
        if not isinstance(source, str) or not source.strip():
            raise AgentProviderProfileError(
                "agent provider profile config 'source' must be a non-empty string"
            )
        raw_profiles = record.get("profiles")
        if not isinstance(raw_profiles, Mapping) or not raw_profiles:
            raise AgentProviderProfileError(
                "agent provider profile config 'profiles' must be a non-empty mapping "
                "of provider id -> profile"
            )
        profiles = tuple(
            AgentProviderProfile.from_record(pid, entry)
            for pid, entry in sorted(raw_profiles.items(), key=lambda kv: str(kv[0]))
        )
        return cls(version=version, source=source, profiles=profiles)

    def to_registry(self) -> AgentProviderProfileRegistry:
        """Seed a registry from this validated config (duplicate ids fail closed)."""
        registry = AgentProviderProfileRegistry()
        for profile in self.profiles:
            registry.register(profile)
        # Force BOTH ambiguity checks now, at load, rather than at the first discovery
        # call on a live pane. Checking only aliases (the pre-R1-F3 shape) left the
        # process-name axis silently last-wins.
        registry.discovery_aliases()
        registry.process_owners()
        return registry


__all__ = (
    "AGENT_PROVIDER_PROFILE_RESOURCE",
    "RECEIVER_AGNOSTIC_PROCESSES",
    "RESERVED_IDENTITY_TOKENS",
    "AgentCapability",
    "AgentProviderProfile",
    "AgentProviderProfileConfig",
    "AgentProviderProfileError",
    "AgentProviderProfileRegistry",
    "FORBIDDEN_PROFILE_TOKENS",
    "InteractionProtocol",
    "ManagedFlagConcept",
    "TrustedExecutable",
)
