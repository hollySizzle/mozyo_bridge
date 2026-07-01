"""Role profile template config schema boundary (Redmine #12952).

US #12388 pinned the four fixed role profile template *bodies* as inline Python
constants in :mod:`mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile`.
#12952 moves those bodies to a packaged config artifact
(``role_profile_templates.yaml``) that is the runtime source of truth, and this
module is the pure *schema boundary* that validates that artifact before the
resolver trusts it. The template bodies stay defined by the human-facing spec
``vibes/docs/specs/delegated-coordinator-role-profile.md`` (US #12387); the
packaged YAML is the machine-readable copy the resolver loads.

What this surface is, kept enforced in code, mirroring the repo-local /
delegation config boundaries (:mod:`mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config`,
:mod:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_project_config`):

- **Schema only — no IO, no parsing.** :meth:`RoleProfileConfig.from_record`
  normalizes an already-parsed mapping (the in-memory shape ``yaml.safe_load`` of
  ``role_profile_templates.yaml`` yields). It reads no file and imports no YAML;
  the packaged-resource read lives in the resolver module so this schema stays
  pure and unit-testable without touching the filesystem.
- **Fixed role vocabulary.** The config must define *exactly* the four known role
  tokens — no unknown token, and none missing. An unknown role token fails closed
  (never silently accepted as an extra template) and a dropped role fails closed
  (never silently treated as "no template" at send time).
- **Fail-closed, closed schema.** A non-mapping record, an unknown top-level or
  per-role key, a missing / empty ``version`` or ``source``, an empty / blank
  template body, or a declared ``placeholders`` list that does not match the
  ``<...>`` tokens actually present in the template are all rejected with a
  :class:`RoleProfileConfigError` — never a raw parser exception and never a
  silent normalization.

The versioned pointer contract from US #12388 is preserved: ``version`` carries
the stable ``ROLE_PROFILE_VERSION`` pointer (bump it in the YAML whenever a
template body changes) and ``source`` carries the ``ROLE_PROFILE_SOURCE`` spec
pointer, so a persisted ``profile_version`` / ``profile_source`` stays a faithful
pointer to the contract text that was sent.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


class RoleProfileConfigError(ValueError):
    """The role profile template config is malformed or unsafe (fail-closed)."""


# The fixed role profile vocabulary (US #12387 ``## role 語彙``). Kept as code
# constants — the *vocabulary* is a code invariant; only the template *bodies*
# are externalized to the packaged config.
ROLE_COORDINATOR = "coordinator"
ROLE_DELEGATED_COORDINATOR = "delegated_coordinator"
ROLE_IMPLEMENTATION_GATEWAY = "implementation_gateway"
ROLE_IMPLEMENTATION_WORKER = "implementation_worker"

# Insertion-ordered by authority, matching the spec's ``## role 語彙`` ordering.
# The config must define exactly this set; the resolved registry is built in this
# order so it never depends on mapping iteration / file ordering.
KNOWN_ROLE_TOKENS: tuple[str, ...] = (
    ROLE_COORDINATOR,
    ROLE_DELEGATED_COORDINATOR,
    ROLE_IMPLEMENTATION_GATEWAY,
    ROLE_IMPLEMENTATION_WORKER,
)

# Wheel-packaged resource (a sibling of the resolver module) that ships the
# template bodies as the config source of truth. Loaded via
# ``importlib.resources`` in the resolver module — a package-anchored resource,
# never a cwd / worktree path walk, so the "no path guessing at send time"
# invariant holds.
ROLE_PROFILE_CONFIG_RESOURCE = "role_profile_templates.yaml"

# Closed set of recognized top-level config keys.
_CONFIG_KEYS: frozenset[str] = frozenset({"version", "source", "roles"})

# Closed set of recognized keys inside one ``roles`` entry.
_ROLE_ENTRY_KEYS: frozenset[str] = frozenset({"template", "placeholders"})

# Placeholder token shape used in the templates: ``<lower_snake_case>``. Kept in
# sync with ``role_profile._PLACEHOLDER_RE`` (same shape) so a declared
# ``placeholders`` list validates against exactly the tokens the resolver will
# substitute.
_PLACEHOLDER_RE = re.compile(r"<([a-z_]+)>")


def extract_placeholders(template: str) -> tuple[str, ...]:
    """Return the ordered-unique ``<...>`` placeholder names in ``template``."""
    seen: list[str] = []
    for match in _PLACEHOLDER_RE.finditer(template):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return tuple(seen)


@dataclass(frozen=True)
class RoleProfileConfig:
    """A validated role profile template config (Redmine #12952).

    ``templates`` and ``placeholders`` are keyed by role token in
    :data:`KNOWN_ROLE_TOKENS` order. ``version`` / ``source`` carry the durable
    ``ROLE_PROFILE_VERSION`` / ``ROLE_PROFILE_SOURCE`` pointers.
    """

    version: str
    source: str
    templates: Mapping[str, str]
    placeholders: Mapping[str, tuple[str, ...]]

    @classmethod
    def from_record(cls, record: object) -> "RoleProfileConfig":
        """Validate an already-parsed config mapping, failing closed.

        Rejects, as :class:`RoleProfileConfigError`, every malformed / unsafe
        shape: a non-mapping record, an unknown top-level or per-role key, a
        missing / non-string / blank ``version`` or ``source``, a non-mapping
        ``roles`` block, an unknown role token, a missing role token, a
        non-mapping role entry, an empty / blank template body, or a declared
        ``placeholders`` list that does not match the template's ``<...>`` tokens.
        """
        if not isinstance(record, Mapping):
            raise RoleProfileConfigError(
                f"role profile config must be a mapping; got {type(record).__name__}"
            )
        unknown = set(record) - _CONFIG_KEYS
        if unknown:
            raise RoleProfileConfigError(
                f"unknown role profile config key(s): {sorted(unknown)}; "
                f"expected {sorted(_CONFIG_KEYS)}"
            )

        version = record.get("version")
        if not isinstance(version, str) or not version.strip():
            raise RoleProfileConfigError(
                "role profile config 'version' must be a non-empty string"
            )
        source = record.get("source")
        if not isinstance(source, str) or not source.strip():
            raise RoleProfileConfigError(
                "role profile config 'source' must be a non-empty string"
            )

        roles = record.get("roles")
        if not isinstance(roles, Mapping):
            raise RoleProfileConfigError(
                "role profile config 'roles' must be a mapping of role token -> entry"
            )
        role_keys = set(roles)
        known = set(KNOWN_ROLE_TOKENS)
        unknown_roles = role_keys - known
        if unknown_roles:
            raise RoleProfileConfigError(
                f"unknown role profile token(s): {sorted(unknown_roles)}; "
                f"config must define exactly {list(KNOWN_ROLE_TOKENS)}"
            )
        missing_roles = known - role_keys
        if missing_roles:
            raise RoleProfileConfigError(
                f"missing role profile template(s): {sorted(missing_roles)}; "
                f"config must define all of {list(KNOWN_ROLE_TOKENS)}"
            )

        templates: dict[str, str] = {}
        placeholders: dict[str, tuple[str, ...]] = {}
        for token in KNOWN_ROLE_TOKENS:
            entry = roles[token]
            if not isinstance(entry, Mapping):
                raise RoleProfileConfigError(
                    f"role profile entry {token!r} must be a mapping"
                )
            unknown_entry = set(entry) - _ROLE_ENTRY_KEYS
            if unknown_entry:
                raise RoleProfileConfigError(
                    f"unknown key(s) in role profile entry {token!r}: "
                    f"{sorted(unknown_entry)}; expected {sorted(_ROLE_ENTRY_KEYS)}"
                )
            template = entry.get("template")
            if not isinstance(template, str) or not template.strip():
                raise RoleProfileConfigError(
                    f"role profile entry {token!r} 'template' must be a non-empty string"
                )
            extracted = extract_placeholders(template)
            declared = entry.get("placeholders")
            if declared is not None:
                if isinstance(declared, (str, bytes)) or not isinstance(
                    declared, Sequence
                ):
                    raise RoleProfileConfigError(
                        f"role profile entry {token!r} 'placeholders' must be a list of strings"
                    )
                declared_tuple = tuple(declared)
                if any(not isinstance(item, str) for item in declared_tuple):
                    raise RoleProfileConfigError(
                        f"role profile entry {token!r} 'placeholders' must be a list of strings"
                    )
                if declared_tuple != extracted:
                    raise RoleProfileConfigError(
                        f"role profile entry {token!r} placeholder mismatch: declared "
                        f"{list(declared_tuple)} != template tokens {list(extracted)}"
                    )
            templates[token] = template
            placeholders[token] = extracted

        return cls(
            version=version,
            source=source,
            templates=templates,
            placeholders=placeholders,
        )


__all__ = (
    "RoleProfileConfigError",
    "RoleProfileConfig",
    "ROLE_COORDINATOR",
    "ROLE_DELEGATED_COORDINATOR",
    "ROLE_IMPLEMENTATION_GATEWAY",
    "ROLE_IMPLEMENTATION_WORKER",
    "KNOWN_ROLE_TOKENS",
    "ROLE_PROFILE_CONFIG_RESOURCE",
    "extract_placeholders",
)
