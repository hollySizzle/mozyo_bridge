"""Workflow role <-> runtime provider binding (Redmine #12673).

Across the workflow runtime, ``codex`` / ``claude`` have been used in two different
senses that #12670 / #12673 deliberately separate:

- a **workflow role** is an *abstract responsibility* on the development flow —
  ``coordinator`` / ``auditor`` / ``implementer`` / ``owner`` (the #12857 runtime
  vocabulary) and the broader #12670 lane vocabulary ``root_coordinator`` /
  ``project_gateway`` / ``implementation_worker``. The DB / event schema is
  **role-canonical**: who *owns* a next action is recorded as a role, never a
  provider (``vibes/docs/logics/coordinator-sublane-development-flow.md`` ``### 設計思想``);
- a **runtime provider** is the *concrete execution surface* a role currently runs on
  — ``codex`` / ``claude`` today, possibly ``grok`` or a different Claude / Codex
  surface tomorrow. A provider is the *result* of route resolution / runtime binding,
  not an identity the workflow state is fixed to.

Before this module the only role->provider mapping was a hard-coded private dict inside
:mod:`...domain.workflow_next_action` (``_OWNER_ROLE_EXPECTED_PROVIDER``), with a comment
that "making it config-driven is #12673". This module is that config-driven boundary, kept
deliberately minimal (#12673 j#69034 scope: schema / default / override 境界を最小実装する):

- **schema** — the role vocabulary is a *closed* set (:data:`WORKFLOW_ROLES`); a binding may
  only bind a provider to a role that exists. The provider vocabulary is *open*: any
  non-empty token is accepted, so a future surface (``grok``, another Claude/Codex model)
  binds without a code change. :data:`KNOWN_PROVIDERS` only names the providers that have a
  runtime adapter *today*; it is advisory, never an allowlist that would re-fix the binding
  to ``codex`` / ``claude`` (acceptance: workflow state / route identity must not become
  provider-fixed);
- **default** — :meth:`RoleProviderBinding.default` is the compatibility baseline. It is the
  exact same mapping the old private dict carried (gateway / coordination / audit / owner ->
  ``codex``; implementation -> ``claude``), extended to the #12670 lane-role names, so
  existing ``codex`` / ``claude`` operation is unchanged when no override is supplied;
- **override** — :meth:`RoleProviderBinding.with_overrides` / :func:`parse_binding_overrides`
  merge caller-supplied bindings on top of the default, fail-closed on an unknown role or an
  empty provider. This is where an operator / config rebinds a role to a different surface
  without touching the role-canonical state.

This module is **pure**: value objects + total functions over plain strings. It opens no DB,
reads no config file, scans no tmux. Loading overrides from a CLI flag / config file and
threading the resulting binding into a command is the caller's concern; the domain just owns
the vocabulary, the default, the merge semantics, and the ``role via provider`` display.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

# The runtime owner-role vocabulary the #12857 NextAction already emits. Imported
# (not re-declared) so this binding cannot drift from the decision authority.
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (
    ROLE_AUDITOR,
    ROLE_COORDINATOR,
    ROLE_IMPLEMENTER,
    ROLE_OWNER,
)

# ---------------------------------------------------------------------------
# Runtime provider tokens (the *execution surface*). These are the providers
# with a runtime adapter today. ``KNOWN_PROVIDERS`` is advisory only: the binding
# accepts ANY non-empty provider token so a future surface binds without a code
# change. It is never used to reject an override (that would re-fix the binding to
# codex/claude, exactly the provider-lock #12673 removes).
# ---------------------------------------------------------------------------
PROVIDER_CODEX: str = "codex"
PROVIDER_CLAUDE: str = "claude"

#: Providers that have a runtime adapter today (advisory; NOT a closed allowlist).
KNOWN_PROVIDERS: frozenset[str] = frozenset({PROVIDER_CODEX, PROVIDER_CLAUDE})

# ---------------------------------------------------------------------------
# Extended #12670 lane-role vocabulary. The #12857 runtime currently emits the
# four roles imported above; #12670 names a broader lane vocabulary the binding is
# forward-looking about (acceptance condition: define the role -> provider binding
# for the named roles). Declared here as the abstract responsibilities they are.
# ---------------------------------------------------------------------------
ROLE_ROOT_COORDINATOR: str = "root_coordinator"
ROLE_PROJECT_GATEWAY: str = "project_gateway"
ROLE_IMPLEMENTATION_WORKER: str = "implementation_worker"

#: The CLOSED workflow-role vocabulary a binding may bind. A role outside this set
#: cannot be bound (fail-closed) — the role space is workflow-canonical, the
#: provider space is open. (``none`` is intentionally absent: "no owner" routes
#: nowhere and has no provider.)
WORKFLOW_ROLES: frozenset[str] = frozenset(
    {
        ROLE_COORDINATOR,
        ROLE_AUDITOR,
        ROLE_IMPLEMENTER,
        ROLE_OWNER,
        ROLE_ROOT_COORDINATOR,
        ROLE_PROJECT_GATEWAY,
        ROLE_IMPLEMENTATION_WORKER,
    }
)

# ---------------------------------------------------------------------------
# The default (compatibility) binding. Identical in spirit to the old private
# ``_OWNER_ROLE_EXPECTED_PROVIDER`` — gateway / coordination / audit / owner run on
# codex, implementation runs on claude — extended to the #12670 lane-role names so
# the broader vocabulary resolves the same way. Changing a value here changes the
# product default for EVERY workflow surface; rebinding for one run is an override,
# not an edit here.
# ---------------------------------------------------------------------------
_DEFAULT_BINDING: dict[str, str] = {
    ROLE_COORDINATOR: PROVIDER_CODEX,
    ROLE_AUDITOR: PROVIDER_CODEX,
    ROLE_OWNER: PROVIDER_CODEX,
    ROLE_IMPLEMENTER: PROVIDER_CLAUDE,
    ROLE_ROOT_COORDINATOR: PROVIDER_CODEX,
    ROLE_PROJECT_GATEWAY: PROVIDER_CODEX,
    ROLE_IMPLEMENTATION_WORKER: PROVIDER_CLAUDE,
}


class RoleProviderBindingError(ValueError):
    """A binding override is malformed (unknown role, or empty provider).

    Inherits :class:`ValueError` for the fail-closed semantics the sibling domain
    errors use: binding a provider to a role outside :data:`WORKFLOW_ROLES`, or to an
    empty provider token, raises here rather than silently producing a binding that
    would mis-resolve a route.
    """


def _norm(value: object) -> str:
    """Trim a raw token to a comparable string (``None`` -> ``""``)."""
    return str(value).strip() if value is not None else ""


def normalize_role(role: object) -> str:
    """Normalize a workflow-role token for lookup / comparison (pure)."""
    return _norm(role)


def normalize_provider(provider: object) -> str:
    """Normalize a runtime-provider token for lookup / comparison (pure)."""
    return _norm(provider)


@dataclass(frozen=True)
class RoleProviderBinding:
    """An immutable workflow-role -> runtime-provider binding (value object).

    Wraps a role->provider mapping. :meth:`default` is the compatibility baseline;
    :meth:`with_overrides` returns a new binding with caller bindings merged on top.
    :meth:`provider_for` is fail-closed (an unbound role resolves to ``None``, never a
    guessed provider). The mapping is stored read-only so a binding cannot be mutated
    after construction.
    """

    _bindings: Mapping[str, str]

    @classmethod
    def default(cls) -> "RoleProviderBinding":
        """The default (compatibility) binding — the same codex/claude map as before."""
        return cls(MappingProxyType(dict(_DEFAULT_BINDING)))

    @classmethod
    def from_overrides(
        cls,
        overrides: Mapping[str, str],
        *,
        base: "RoleProviderBinding | None" = None,
    ) -> "RoleProviderBinding":
        """Build a binding by merging ``overrides`` onto ``base`` (default if ``None``).

        Each override key must be a known :data:`WORKFLOW_ROLES` role and each value a
        non-empty provider token, else :class:`RoleProviderBindingError` (fail-closed).
        The provider value is NOT checked against :data:`KNOWN_PROVIDERS` — an open
        provider vocabulary is the point of #12673.
        """
        merged: dict[str, str] = dict((base or cls.default())._bindings)
        for raw_role, raw_provider in overrides.items():
            role = normalize_role(raw_role)
            provider = normalize_provider(raw_provider)
            if role not in WORKFLOW_ROLES:
                raise RoleProviderBindingError(
                    f"cannot bind unknown workflow role {role!r}; "
                    f"known roles: {', '.join(sorted(WORKFLOW_ROLES))}"
                )
            if not provider:
                raise RoleProviderBindingError(
                    f"role {role!r} requires a non-empty provider token"
                )
            merged[role] = provider
        return cls(MappingProxyType(merged))

    def with_overrides(self, overrides: Mapping[str, str]) -> "RoleProviderBinding":
        """Return a new binding with ``overrides`` merged on top of this one."""
        return type(self).from_overrides(overrides, base=self)

    def provider_for(self, role: object) -> str | None:
        """The runtime provider bound to ``role``, or ``None`` if unbound (fail-closed).

        An unbound / unknown role resolves to ``None`` rather than a default provider, so
        a caller (e.g. route selection) fails closed instead of pointing at a guessed
        surface.
        """
        return self._bindings.get(normalize_role(role))

    def roles(self) -> tuple[str, ...]:
        """The bound roles, in insertion order."""
        return tuple(self._bindings)

    def as_mapping(self) -> dict[str, str]:
        """A plain mutable copy of the role->provider mapping (for display / serialization)."""
        return dict(self._bindings)

    def describe(self, role: object) -> str:
        """``"<role> via <provider>"`` for ``role`` under this binding (display)."""
        return format_role_via_provider(role, self.provider_for(role))


def parse_binding_overrides(specs: Iterable[str]) -> dict[str, str]:
    """Parse ``ROLE=PROVIDER`` override specs (CLI / config boundary), fail-closed.

    Each spec is ``role=provider`` (e.g. ``auditor=grok``). A spec with no ``=``, an empty
    role, an unknown role, or an empty provider raises :class:`RoleProviderBindingError`.
    A later spec for the same role wins (last-write). Returns a plain role->provider dict
    suitable for :meth:`RoleProviderBinding.with_overrides`.
    """
    out: dict[str, str] = {}
    for raw in specs:
        spec = _norm(raw)
        if not spec:
            continue
        if "=" not in spec:
            raise RoleProviderBindingError(
                f"malformed role/provider override {raw!r}; expected ROLE=PROVIDER"
            )
        raw_role, _, raw_provider = spec.partition("=")
        role = normalize_role(raw_role)
        provider = normalize_provider(raw_provider)
        if role not in WORKFLOW_ROLES:
            raise RoleProviderBindingError(
                f"cannot bind unknown workflow role {role!r} in override {raw!r}; "
                f"known roles: {', '.join(sorted(WORKFLOW_ROLES))}"
            )
        if not provider:
            raise RoleProviderBindingError(
                f"role {role!r} requires a non-empty provider in override {raw!r}"
            )
        out[role] = provider
    return out


def format_role_via_provider(role: object, provider: object) -> str:
    """Public-safe ``"<role> via <provider>"`` display (pure).

    Renders both the abstract role and the concrete provider together
    (``auditor via codex``), per #12673's "role と provider を両方見せる". An unset role /
    provider degrades to an explicit placeholder rather than an empty token, so display
    never silently drops one side of the binding.
    """
    role_token = normalize_role(role) or "<unknown_role>"
    provider_token = normalize_provider(provider)
    return f"{role_token} via {provider_token or '<unresolved>'}"


__all__ = (
    "PROVIDER_CODEX",
    "PROVIDER_CLAUDE",
    "KNOWN_PROVIDERS",
    "ROLE_ROOT_COORDINATOR",
    "ROLE_PROJECT_GATEWAY",
    "ROLE_IMPLEMENTATION_WORKER",
    "WORKFLOW_ROLES",
    "RoleProviderBindingError",
    "normalize_role",
    "normalize_provider",
    "RoleProviderBinding",
    "parse_binding_overrides",
    "format_role_via_provider",
)
