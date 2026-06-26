"""Internal-only built-in CLI module registry skeleton (Redmine #12155).

This is the parser-composition analogue of the built-in *provider* registry
(:mod:`mozyo_bridge.domain.provider_registry`, Redmine #12035). Where that
module classifies the built-in adapter providers, this one classifies the
built-in **CLI command family modules** the codebase already ships — the
families the feature-family parser split produced (Redmine #12153 / #12154):
``agents`` / ``cockpit`` / ``handoff`` / ``observability`` / ``runtime-config``
/ ``session`` / ``workspace`` / ``release`` / ``docs-scaffold`` plus the core
command set — so ``build_parser()`` can compose them from a registry instead of
an inlined hand-ordered sequence, and so the codebase has a configuration-aware
baseline (module selection / feature flags) before any external plugin surface.

It is deliberately **not** a plugin system, exactly like the provider registry:

- there is no dynamic import, no third-party entry point, and no user-script
  loading. A family is *classified* here by a pure :class:`CliFamily`
  description (name, summary, the core-owned authorities its commands
  participate in, flags). The mapping from a family name to the built-in
  registrar callable that adds its subparsers lives in the application layer
  (:mod:`mozyo_bridge.application.cli_modules`), bound to statically-imported
  built-in functions only — never to a module path supplied at runtime;
- there is no public ABI and no compatibility promise. These data shapes and
  family names are internal and may change with no deprecation window;
- **config may select modules; it may never weaken authority.** The
  configuration surface (:class:`CliCompositionConfig`) is limited to module
  selection / feature flags. The authorities core never makes configurable —
  workflow authority, owner approval, review / close approval, send safety, and
  routing — are enumerated in :data:`CORE_OWNED_AUTHORITIES`. A family that
  carries any of them (and the hard core command set) is *mandatory*: a config
  that tries to disable it is rejected, so owner approval / review / close /
  send safety can never be configured away.

The module is pure (dataclasses + an insertion-ordered in-memory mapping) and
imports no application or argparse code, so the dependency only ever points
application -> domain, exactly like ``provider_registry``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Iterator, Optional


# Authorities core never delegates and never makes configurable. A CLI family
# may *participate* in one of these (it is built-in core code), but the registry
# treats any family that does as mandatory so a config can never disable the
# commands that carry owner approval / review / close / send safety / workflow /
# routing authority. This mirrors ``provider_registry.FORBIDDEN_PROVIDER_AUTHORITIES``;
# there the set is what a provider may not *claim*, here it is what a family may
# not be configured *out of*.
CORE_OWNED_AUTHORITIES: frozenset[str] = frozenset(
    {
        "workflow_authority",
        "owner_approval",
        "review_authority",
        "close_approval",
        "send_safety",
        "routing_authority",
    }
)


class ModuleRegistryError(ValueError):
    """A CLI family description, registration, or composition config is invalid."""


def _frozen_label_set(value: object, *, field: str, family_name: str) -> frozenset[str]:
    """Normalize a label collection into a validated ``frozenset``.

    A bare ``str``/``bytes`` is **rejected** rather than normalized: both are
    iterable, so ``frozenset("send_safety")`` would silently become a set of
    single characters and slip past the :data:`CORE_OWNED_AUTHORITIES` subset
    check — exactly the kind of authority confusion this seam must prevent.
    Each entry must be a non-empty ``str``. This is the same guard the provider
    registry applies to its descriptive sets.
    """
    if isinstance(value, (str, bytes)):
        raise ModuleRegistryError(
            f"family {family_name!r} {field} must be a collection of strings, "
            f"not a bare {type(value).__name__}; a bare string is iterated "
            f"character-by-character and would bypass the authority check"
        )
    try:
        items = frozenset(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ModuleRegistryError(
            f"family {family_name!r} {field} must be an iterable of strings, "
            f"got {type(value).__name__}"
        ) from exc
    for item in items:
        if not isinstance(item, str) or not item:
            raise ModuleRegistryError(
                f"family {family_name!r} {field} entries must be non-empty "
                f"strings; got {item!r}"
            )
    return items


@dataclass(frozen=True)
class CliFamily:
    """A pure description of one built-in CLI command family — classification.

    Fields:

    - ``name``: stable internal family id (e.g. ``"handoff"``), unique within
      the registry and used as the config selection key. It is *not* required
      to equal any subcommand name — a family may register several subcommands.
    - ``summary``: one-line human description; public-safe, no private policy.
    - ``authorities``: the :data:`CORE_OWNED_AUTHORITIES` this family's commands
      participate in (e.g. ``"send_safety"`` for the families that deliver pane
      input / handoffs). Purely descriptive *and* load-bearing: a non-empty set
      makes the family :attr:`mandatory`. Each entry must be a known core-owned
      authority — a family cannot invent a new authority name here.
    - ``core``: ``True`` marks a family that belongs to the hard core command
      set (status / pane I/O / lifecycle). Core families are mandatory even when
      they carry no specific authority.
    - ``experimental``: ``True`` marks a not-yet-stable classification.

    The dataclass is frozen and holds no behavior; it is metadata about a
    family, never a handle to its registrar. The registrar binding lives in the
    application layer.
    """

    name: str
    summary: str
    authorities: frozenset[str] = field(default_factory=frozenset)
    core: bool = False
    experimental: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ModuleRegistryError("family name must be a non-empty string")
        object.__setattr__(
            self,
            "authorities",
            _frozen_label_set(self.authorities, field="authorities", family_name=self.name),
        )
        unknown = self.authorities - CORE_OWNED_AUTHORITIES
        if unknown:
            raise ModuleRegistryError(
                f"family {self.name!r} declares unknown authorities {sorted(unknown)}; "
                f"only the core-owned authorities {sorted(CORE_OWNED_AUTHORITIES)} are "
                f"expressible (a family cannot invent authority names)."
            )

    @property
    def mandatory(self) -> bool:
        """Whether config is forbidden from disabling this family.

        Mandatory iff it is part of the hard core command set or it carries any
        core-owned authority. This is the property that keeps owner approval /
        review / close / send safety out of the configurable surface.
        """
        return self.core or bool(self.authorities)


# --- Repo-local composition config record schema (internal-only) -----------
#
# A *composition config record* is the YAML/TOML-equivalent typed mapping a repo
# may carry to select built-in CLI families. It is intentionally tiny: the only
# behavior config may express is disabling non-mandatory families. The schema is
# closed (unknown keys fail closed), values are typed, and the record may never
# name a module path, a callable, an authority, or a credential — those are not
# "unknown future options", they are boundaries this surface must never cross, so
# a key that even looks like one is rejected with a boundary-specific message.
# ``CliCompositionConfig.from_record`` validates record *shape*; the registry's
# ``resolve_enabled`` validates record *meaning* (unknown / mandatory families).

#: Top-level keys a composition config record may contain. Anything else fails
#: closed (typo protection + closed schema).
COMPOSITION_RECORD_KEYS: frozenset[str] = frozenset({"version", "disabled"})

#: The only record schema version understood today. A record may omit it; if
#: present it must equal this. The version is bumped by a deliberate code + doc
#: change, never selected from config to unlock new behavior.
COMPOSITION_RECORD_VERSION: int = 1

#: A valid built-in family identifier: lowercase, digits, hyphens (e.g.
#: ``"runtime-config"``). A ``disabled`` entry must match this, so a dotted
#: module path, a filesystem path, or a secret-shaped token is rejected at the
#: record boundary, before it could ever reach family resolution.
_FAMILY_ID_RE = re.compile(r"[a-z][a-z0-9-]*\Z")

#: Substrings in a record key that signal an attempt to cross a boundary this
#: surface owns: load/execute code, name a module/callable, grant or alter
#: authority/approval/routing, or carry a credential/secret. Such a key is
#: rejected with a boundary-specific message rather than the generic unknown-key
#: error, so the rejection reads as deliberate in an audit.
_FORBIDDEN_RECORD_KEY_PARTS: tuple[str, ...] = (
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
    "send_safety",
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


@dataclass(frozen=True)
class CliCompositionConfig:
    """The configuration-aware composition surface — module selection only.

    The *only* thing config may do is name built-in families to disable
    (``disabled``). It cannot reorder, add a family, supply a registrar, or
    grant authority. Disabling a :attr:`CliFamily.mandatory` family is rejected
    at resolution, so this surface can never be used to weaken workflow
    authority, owner approval, review / close approval, or send safety.

    The default (``disabled`` empty) composes the full built-in CLI unchanged.
    """

    disabled: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "disabled",
            _frozen_label_set(self.disabled, field="disabled", family_name="<config>"),
        )

    @classmethod
    def default(cls) -> "CliCompositionConfig":
        """The behavior-preserving default: nothing disabled, full composition."""
        return cls()

    @classmethod
    def from_record(
        cls, record: "Optional[Mapping[str, object]]" = None
    ) -> "CliCompositionConfig":
        """Normalize a repo-local config *record* into a CliCompositionConfig.

        ``record`` is the in-memory mapping a YAML/TOML repo-local config would
        parse to; this layer does no file IO and no parsing — it normalizes an
        already-parsed typed record. ``None`` or an empty record yields the
        behavior-preserving default (full composition), so a missing config can
        never change the default ``mozyo-bridge`` CLI.

        Fail-closed, in order:

        - a non-mapping record is rejected (it is not a config record);
        - a key naming a module/callable/authority/credential boundary is
          rejected with a boundary-specific message — config may not load code,
          grant authority, or carry a secret;
        - any other unknown top-level key is rejected (closed schema / typo
          protection);
        - ``version``, if present, must be the supported integer version;
        - ``disabled``, if present, must be a list/tuple of family identifiers
          (lowercase letters, digits, hyphens). A bare string, a mapping, or a
          value not shaped like a family id — a module path or a secret-shaped
          token — is rejected before it could reach family resolution.

        Whether the named families actually exist and are non-mandatory is
        enforced separately by :meth:`BuiltinCliModuleRegistry.resolve_enabled`,
        which fails closed on unknown or mandatory families. This method
        validates record *shape*; the registry validates record *meaning*.
        """
        if record is None:
            return cls.default()
        if not isinstance(record, Mapping):
            raise ModuleRegistryError(
                "composition config record must be a mapping (a YAML/TOML table), "
                f"got {type(record).__name__}"
            )
        for key in record:
            if not isinstance(key, str) or not key:
                raise ModuleRegistryError(
                    "composition config record keys must be non-empty strings; "
                    f"got {key!r}"
                )
            lowered = key.lower()
            for part in _FORBIDDEN_RECORD_KEY_PARTS:
                if part in lowered:
                    raise ModuleRegistryError(
                        f"composition config record may not carry a {key!r} field: "
                        f"this surface selects built-in families only and may never "
                        f"load code, name a module/callable, grant authority, or carry "
                        f"a credential (matched forbidden token {part!r})."
                    )
            if key not in COMPOSITION_RECORD_KEYS:
                raise ModuleRegistryError(
                    f"composition config record has unknown key {key!r}; "
                    f"allowed keys: {sorted(COMPOSITION_RECORD_KEYS)}"
                )

        version = record.get("version", COMPOSITION_RECORD_VERSION)
        # ``bool`` is an ``int`` subclass — reject it so ``version: true`` does
        # not silently read as version 1.
        if isinstance(version, bool) or not isinstance(version, int):
            raise ModuleRegistryError(
                "composition config record 'version' must be an integer, got "
                f"{version!r}"
            )
        if version != COMPOSITION_RECORD_VERSION:
            raise ModuleRegistryError(
                f"unsupported composition config record version {version!r}; this "
                f"build understands version {COMPOSITION_RECORD_VERSION}"
            )

        raw_disabled = record.get("disabled", ())
        # A bare string is iterable (char-by-char) and a mapping iterates its
        # keys; both would silently normalize into the wrong set, so require an
        # explicit YAML/TOML array (list/tuple).
        if isinstance(raw_disabled, (str, bytes)) or not isinstance(
            raw_disabled, (list, tuple)
        ):
            raise ModuleRegistryError(
                "composition config record 'disabled' must be a list of family "
                f"identifiers, got {type(raw_disabled).__name__}"
            )
        for entry in raw_disabled:
            if not isinstance(entry, str) or not _FAMILY_ID_RE.match(entry):
                raise ModuleRegistryError(
                    f"composition config 'disabled' entry {entry!r} is not a valid "
                    f"family identifier (lowercase letters, digits, hyphens); a "
                    f"module path, filesystem path, or secret-shaped value is "
                    f"rejected here."
                )
        return cls(disabled=frozenset(raw_disabled))


class BuiltinCliModuleRegistry:
    """An insertion-ordered, in-memory classification of built-in CLI families.

    Registration takes a :class:`CliFamily` description only; this module never
    loads, imports, or executes a family, so it is a catalogue, not an extension
    point. Insertion order is preserved and is the composition order — unlike
    the provider registry (which sorts by id for stable listing), CLI subcommand
    order is observable in ``--help`` and must be deterministic from
    registration order. Ids are unique; re-registering is an error.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, CliFamily] = {}

    def register(self, family: CliFamily) -> CliFamily:
        """Record one built-in family description; reject duplicate names."""
        if not isinstance(family, CliFamily):
            raise ModuleRegistryError(
                "register expects a CliFamily description, not "
                f"{type(family).__name__}; the registry never loads code."
            )
        if family.name in self._by_name:
            raise ModuleRegistryError(f"duplicate family name: {family.name!r}")
        self._by_name[family.name] = family
        return family

    def get(self, name: str) -> Optional[CliFamily]:
        """Return the family named ``name``, or ``None`` if unregistered."""
        return self._by_name.get(name)

    def families(self) -> tuple[CliFamily, ...]:
        """All registered families in registration (composition) order."""
        return tuple(self._by_name.values())

    def names(self) -> tuple[str, ...]:
        """All registered family names in registration (composition) order."""
        return tuple(self._by_name.keys())

    def mandatory_names(self) -> tuple[str, ...]:
        """Names of families config can never disable, in composition order."""
        return tuple(name for name, fam in self._by_name.items() if fam.mandatory)

    def resolve_enabled(
        self, config: Optional[CliCompositionConfig] = None
    ) -> tuple[str, ...]:
        """Return the enabled family names, in composition order, for ``config``.

        Fail-closed on a config that names an unknown family (typo protection)
        or a mandatory family (authority protection): both raise
        :class:`ModuleRegistryError` rather than silently composing something
        other than what was asked. With the default config every family is
        enabled, so the composition is the full built-in CLI.
        """
        if config is None:
            config = CliCompositionConfig.default()
        unknown = config.disabled - set(self._by_name)
        if unknown:
            raise ModuleRegistryError(
                f"config disables unknown families {sorted(unknown)}; "
                f"known families: {sorted(self._by_name)}"
            )
        locked = {n for n in config.disabled if self._by_name[n].mandatory}
        if locked:
            raise ModuleRegistryError(
                f"config may not disable mandatory families {sorted(locked)}: "
                f"they carry core-owned authority or are core commands. Workflow "
                f"authority, owner approval, review / close approval, and send "
                f"safety are not configurable."
            )
        return tuple(
            name for name in self._by_name if name not in config.disabled
        )

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def __iter__(self) -> Iterator[CliFamily]:
        return iter(self.families())

    def __len__(self) -> int:
        return len(self._by_name)


__all__ = (
    "CORE_OWNED_AUTHORITIES",
    "COMPOSITION_RECORD_KEYS",
    "COMPOSITION_RECORD_VERSION",
    "BuiltinCliModuleRegistry",
    "CliCompositionConfig",
    "CliFamily",
    "ModuleRegistryError",
)
