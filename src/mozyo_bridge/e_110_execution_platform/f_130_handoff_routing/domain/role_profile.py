"""Role profile template resolution for handoff prompt expansion (Redmine #12388).

US #12388 / Task #12396 implements the send-side resolution of the fixed role
profile templates defined by US #12387
(``vibes/docs/specs/delegated-coordinator-role-profile.md`` ``## 固定 role
profile template``). That spec is the human-facing source of truth for the
template *bodies*; this module is the runtime resolver that

- loads the fixed templates from a wheel-packaged, schema-validated config
  artifact (``role_profile_templates.yaml``) so resolution stays self-contained
  and fail-closed (a package-anchored resource read via ``importlib.resources``,
  never a cwd / worktree path guess at send time),
- substitutes the ``<...>`` placeholders from handoff structured fields,
- carries the structured ``role_profile`` / ``profile_source`` /
  ``profile_version`` fields so the receiver never has to discover the template
  path itself, and
- fails closed when an unknown role profile is requested (template missing).

Per the #12387 design the role profile is the receiver's *custom instruction*
and stays separate from the handoff *structured fields*: this module never
mutates the routing landing marker. The resolved contract is carried in the
durable delivery record and a compact single-line pointer in the pane body; the
durable anchor remains the source of truth.

The template bodies and the ``ROLE_PROFILE_VERSION`` / ``ROLE_PROFILE_SOURCE``
pointers now live in the packaged ``role_profile_templates.yaml``, validated by
:class:`~mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile_config.RoleProfileConfig`;
when a template body changes, bump ``version`` there so the persisted
``profile_version`` stays a faithful pointer to the resolved contract text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from typing import Iterable, Mapping, Optional

import yaml

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile_config import (
    ROLE_COORDINATOR,
    ROLE_DELEGATED_COORDINATOR,
    ROLE_IMPLEMENTATION_GATEWAY,
    ROLE_IMPLEMENTATION_WORKER,
    ROLE_PROFILE_CONFIG_RESOURCE,
    RoleProfileConfig,
    RoleProfileConfigError,
)


class RoleProfileError(ValueError):
    """A role profile could not be resolved (e.g. unknown / missing template)."""


def load_packaged_role_profile_config() -> RoleProfileConfig:
    """Load + validate the wheel-packaged role profile template config.

    Reads ``role_profile_templates.yaml`` from this package via
    :mod:`importlib.resources` — a package-anchored resource, never a cwd /
    worktree path walk — and validates it through
    :meth:`RoleProfileConfig.from_record`. The send-time invariant holds: the
    template registry is resolved from a fixed packaged artifact with no
    filesystem path guessing, and a malformed / missing artifact fails closed
    (:class:`RoleProfileConfigError`) at import rather than sending a partial or
    unvalidated role contract.
    """
    text = (
        resources.files(__package__)
        .joinpath(ROLE_PROFILE_CONFIG_RESOURCE)
        .read_text(encoding="utf-8")
    )
    return RoleProfileConfig.from_record(yaml.safe_load(text))


# The validated config is the runtime source of truth. Loaded once at import so
# the resolver stays synchronous and self-contained (no per-send IO); a packaging
# error surfaces immediately and loudly rather than mid-handoff.
_CONFIG = load_packaged_role_profile_config()

# Stable identifier for the template set. Sourced from the packaged config's
# ``version``; bump it there on any template-body edit so a persisted
# ``profile_version`` always points at the contract text that was sent.
ROLE_PROFILE_VERSION = _CONFIG.version

# Repo-relative pointer to the human-facing source of truth for the template
# bodies. Persisted as ``profile_source`` so the receiver reads the role
# contract without guessing a path.
ROLE_PROFILE_SOURCE = _CONFIG.source

# Insertion-ordered so CLI ``choices`` and help text list the roles top-down by
# authority, matching the spec's ``## role 語彙`` ordering (the config schema
# rebuilds the registry in :data:`KNOWN_ROLE_TOKENS` order).
ROLE_PROFILE_TEMPLATES: dict[str, str] = dict(_CONFIG.templates)

ROLE_PROFILE_TOKENS: tuple[str, ...] = tuple(ROLE_PROFILE_TEMPLATES.keys())

# Placeholder token shape used in the templates above: ``<lower_snake_case>``.
_PLACEHOLDER_RE = re.compile(r"<([a-z_]+)>")


def template_placeholders(role: str) -> tuple[str, ...]:
    """Return the placeholder field names a role template expects, in order.

    Fails closed (:class:`RoleProfileError`) when the role has no builtin
    template, so callers never silently treat an unknown role as "no fields".
    """
    template = ROLE_PROFILE_TEMPLATES.get(role)
    if template is None:
        raise RoleProfileError(
            f"unknown role profile: {role!r}; expected one of {list(ROLE_PROFILE_TOKENS)}"
        )
    seen: list[str] = []
    for match in _PLACEHOLDER_RE.finditer(template):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return tuple(seen)


@dataclass(frozen=True)
class RoleProfileResolution:
    """Resolved role profile carried by a handoff (Redmine #12388).

    ``resolved_text`` is the template body with every supplied placeholder
    substituted; any placeholder without a value is left as its literal
    ``<name>`` token and reported in ``unresolved_placeholders`` so a partial
    resolution is explicit rather than silently dropped (the explicit-fallback
    posture for missing *fields*; a missing *template* fails closed earlier).

    The structured pointer fields (:attr:`role_profile`, :attr:`profile_source`,
    :attr:`profile_version`, :attr:`unresolved_placeholders`) carry no
    operator-supplied free text and are always durable-record safe.
    :attr:`resolved_text` may embed supplied field values, so callers keep it to
    the stdout / pasteable record and out of any unvetted auto-persist body,
    mirroring the ``--record-command`` precedent.
    """

    role_profile: str
    profile_source: str
    profile_version: str
    resolved_text: str
    unresolved_placeholders: tuple[str, ...]

    def to_structured_dict(self) -> dict[str, object]:
        """Structured, free-text-free pointer fields for the handoff payload."""
        return {
            "role_profile": self.role_profile,
            "profile_source": self.profile_source,
            "profile_version": self.profile_version,
            "unresolved_placeholders": list(self.unresolved_placeholders),
        }

    def pointer_clause(self) -> str:
        """Compact single-line clause for the pane notification body.

        Single line by construction (no newlines): the body is delivered via a
        single ``tmux send-keys -l`` and the landing-marker gate greps the line,
        so the full multi-line contract stays in the durable delivery record,
        not the pane. Names the role, the source path (so the receiver does not
        guess a template path), and the version, and points at the durable
        record for the fully resolved contract.
        """
        clause = (
            f"role profile: {self.role_profile} "
            f"(source: {self.profile_source}, version: {self.profile_version}; "
            "fully resolved contract is in the durable delivery record)"
        )
        if self.unresolved_placeholders:
            clause += (
                " [unresolved fields: "
                + ", ".join(self.unresolved_placeholders)
                + "]"
            )
        return clause


def resolve_role_profile(
    role: str,
    fields: Optional[Mapping[str, str]] = None,
) -> RoleProfileResolution:
    """Resolve a builtin role profile template, substituting structured fields.

    Fails closed with :class:`RoleProfileError` when ``role`` has no builtin
    template (the "template missing" contract from US #12388 / #12387). Supplied
    ``fields`` substitute the matching ``<name>`` placeholders; unsupplied
    placeholders are left as literal ``<name>`` tokens and reported in
    :attr:`RoleProfileResolution.unresolved_placeholders`.

    The function is pure and deterministic over its inputs.
    """
    template = ROLE_PROFILE_TEMPLATES.get(role)
    if template is None:
        raise RoleProfileError(
            f"unknown role profile: {role!r}; expected one of {list(ROLE_PROFILE_TOKENS)}"
        )

    supplied = dict(fields or {})
    placeholders = template_placeholders(role)

    resolved = template
    unresolved: list[str] = []
    for name in placeholders:
        value = supplied.get(name)
        if value is None or value == "":
            unresolved.append(name)
            continue
        resolved = resolved.replace(f"<{name}>", value)

    return RoleProfileResolution(
        role_profile=role,
        profile_source=ROLE_PROFILE_SOURCE,
        profile_version=ROLE_PROFILE_VERSION,
        resolved_text=resolved,
        unresolved_placeholders=tuple(unresolved),
    )


def parse_profile_fields(pairs: Optional[Iterable[str]]) -> dict[str, str]:
    """Parse ``KEY=VALUE`` CLI pairs into a profile-field mapping.

    Fails closed (:class:`RoleProfileError`) on a pair without ``=`` or with an
    empty key so a malformed ``--profile-field`` never silently drops a value.
    The first ``=`` splits the pair, so values may themselves contain ``=``.
    """
    result: dict[str, str] = {}
    for raw in pairs or ():
        if "=" not in raw:
            raise RoleProfileError(
                f"--profile-field must be KEY=VALUE; got {raw!r}"
            )
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise RoleProfileError(
                f"--profile-field key must be non-empty; got {raw!r}"
            )
        result[key] = value
    return result


__all__: Iterable[str] = (
    "RoleProfileError",
    "RoleProfileConfigError",
    "load_packaged_role_profile_config",
    "ROLE_PROFILE_VERSION",
    "ROLE_PROFILE_SOURCE",
    "ROLE_COORDINATOR",
    "ROLE_DELEGATED_COORDINATOR",
    "ROLE_IMPLEMENTATION_GATEWAY",
    "ROLE_IMPLEMENTATION_WORKER",
    "ROLE_PROFILE_TEMPLATES",
    "ROLE_PROFILE_TOKENS",
    "RoleProfileResolution",
    "template_placeholders",
    "resolve_role_profile",
    "parse_profile_fields",
)
