"""Repo-local role -> provider binding override config (Redmine #13157).

#12673 shipped the pure :class:`~...domain.role_provider_binding.RoleProviderBinding`
value object (the closed workflow-role vocabulary, the open provider vocabulary, the
compatibility default, and the fail-closed override merge) as a *staged seam*: the
binding existed but no config surface fed a rebind into it, so every workflow-aware
command resolved through :meth:`RoleProviderBinding.default` unconditionally. This module
is the closed-schema config sub-record that live-wires that seam — the typed *field
contract* for the ``provider_binding`` block of ``.mozyo-bridge/config.yaml``.

Shape (a closed sub-record, mirroring the sibling repo-local knobs
:mod:`...domain.work_unit_granularity` / :class:`...domain.repo_local_config.AgentLaunchConfig`)::

    provider_binding:
      version: 1
      bindings:
        auditor: claude
        implementer: claude

Boundary, kept enforced in code:

- **Closed schema / fail-closed.** The only recognized sub-keys are
  :data:`PROVIDER_BINDING_CONFIG_KEYS` (``version`` / ``bindings``); ``version`` must be
  the supported integer; ``bindings`` must be a mapping. Anything else raises
  :class:`RoleProviderBindingConfigError`, which the composing repo-local loader re-raises
  as its own ``RepoLocalConfigError`` so a single ``except`` still catches every
  repo-local-config failure.
- **Role vocabulary closed, provider vocabulary open.** Each ``bindings`` key must be a
  known :data:`~...domain.role_provider_binding.WORKFLOW_ROLES` role (an unknown role
  fails closed at load, never a silently ignored typo); each value is any non-empty
  provider token, so a future surface (``grok``, another Claude/Codex model) rebinds with
  no code change. Validation is delegated to :meth:`RoleProviderBinding.from_overrides`, so
  the config surface can never accept a binding the domain would reject, and the two never
  drift.
- **Behavior-preserving default.** A missing / empty ``provider_binding`` block resolves to
  :meth:`RoleProviderBinding.default` — the exact legacy codex/claude map — so a repo with
  no block resolves every role exactly as before (#13157 characterization requirement).
- **Advisory, never authority.** This block *rebinds* the runtime surface a role resolves
  to; it never grants owner-approval / close / routing / send authority, and it cannot
  disable a workflow gate. In particular an operator may bind the auditor and the
  implementer to the same provider (collapsing the cross-provider audit separation); that
  is an **advisory warning** (:meth:`RoleProviderBindingConfig.advisory_warnings`), never a
  hard block — the workflow still runs, the config surface only flags the reduced
  independence.

This module is pure (a dataclass + a small validation helper) and imports only the sibling
binding domain and the role vocabulary, so the dependency only ever points within the
domain layer (config -> role_provider_binding -> workflow_runtime).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (
    RoleProviderBinding,
    RoleProviderBindingError,
    normalize_provider,
    normalize_role,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (
    ROLE_AUDITOR,
    ROLE_IMPLEMENTER,
)

#: The supported ``provider_binding`` config record version. Optional in a record and
#: defaults to this; any other value is rejected so a future, not-yet-understood schema
#: never reads as version 1 (mirrors the repo-local config version rule).
PROVIDER_BINDING_CONFIG_VERSION: int = 1

#: The closed set of recognized keys in the ``provider_binding:`` block. ``bindings`` is
#: the role -> provider override mapping; ``version`` is the optional schema version.
PROVIDER_BINDING_CONFIG_KEYS: frozenset[str] = frozenset({"version", "bindings"})


class RoleProviderBindingConfigError(ValueError):
    """The ``provider_binding`` config record violates the closed schema.

    Inherits :class:`ValueError` for fail-closed semantics, matching the sibling
    repo-local domain errors (``WorkUnitGranularityError`` / ``DelegationConfigError``).
    The composing repo-local config loader re-raises this as its own
    ``RepoLocalConfigError`` so the loader keeps a single fail-closed boundary.
    """


def _checked_version(record: "Mapping[object, object]") -> int:
    """Return the supported version, failing closed on anything else.

    ``version`` is optional and defaults to :data:`PROVIDER_BINDING_CONFIG_VERSION`.
    ``bool`` is rejected even though it is an ``int`` subclass so ``version: true`` does
    not silently read as version ``1``.
    """
    version = record.get("version", PROVIDER_BINDING_CONFIG_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise RoleProviderBindingConfigError(
            f"provider_binding config 'version' must be an integer, got {version!r}"
        )
    if version != PROVIDER_BINDING_CONFIG_VERSION:
        raise RoleProviderBindingConfigError(
            f"unsupported provider_binding config version {version!r}; this build "
            f"understands version {PROVIDER_BINDING_CONFIG_VERSION}"
        )
    return version


@dataclass(frozen=True)
class RoleProviderBindingConfig:
    """The closed ``provider_binding:`` block of ``.mozyo-bridge/config.yaml``.

    Stores only the caller-supplied ``bindings`` :attr:`overrides` (a frozen, sorted tuple
    of ``(role, provider)`` pairs), so the record stays **hashable** — the top-level
    :class:`~...domain.repo_local_config.RepoLocalConfig` is hashed in tests, and the domain
    :class:`RoleProviderBinding` wraps an unhashable mapping, so it is deliberately not a
    stored field. The resolved binding (the compatibility default with the overrides merged
    on top) is the computed :attr:`binding` property. The default (no overrides) is
    byte-for-byte the legacy codex/claude map, so a repo with no ``provider_binding`` block
    resolves every role exactly as before.
    """

    version: int = PROVIDER_BINDING_CONFIG_VERSION
    overrides: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        # Validate through the domain merge so a directly-constructed config (bypassing
        # from_record) still fails closed on an unknown role / empty provider, and the
        # config surface can never hold a binding the domain would reject.
        try:
            RoleProviderBinding.default().with_overrides(dict(self.overrides))
        except RoleProviderBindingError as exc:
            raise RoleProviderBindingConfigError(str(exc)) from exc

    @property
    def binding(self) -> RoleProviderBinding:
        """The resolved binding: the compatibility default with :attr:`overrides` merged in.

        Computed (not stored) so the dataclass stays hashable. With no overrides this is
        byte-for-byte :meth:`RoleProviderBinding.default`.
        """
        return RoleProviderBinding.default().with_overrides(dict(self.overrides))

    @classmethod
    def default(cls) -> "RoleProviderBindingConfig":
        """The behavior-preserving default: the legacy codex/claude binding."""
        return cls()

    @classmethod
    def from_record(
        cls, record: "Optional[Mapping[str, object]]" = None
    ) -> "RoleProviderBindingConfig":
        """Normalize a parsed ``provider_binding:`` mapping into a typed config.

        ``None`` / an empty mapping (and an absent ``bindings`` key) yields the default
        binding. A non-mapping record, an unknown top-level key, an unsupported version, a
        non-mapping ``bindings``, an unknown role key, or an empty provider value fails
        closed with :class:`RoleProviderBindingConfigError`. Role / provider validation is
        delegated to :meth:`RoleProviderBinding.from_overrides` so the config surface never
        accepts a binding the domain would reject.
        """
        if record is None:
            return cls.default()
        if not isinstance(record, Mapping):
            raise RoleProviderBindingConfigError(
                "provider_binding config record must be a mapping (a YAML table), got "
                f"{type(record).__name__}"
            )
        for key in record:
            if not isinstance(key, str) or not key:
                raise RoleProviderBindingConfigError(
                    "provider_binding config record keys must be non-empty strings; "
                    f"got {key!r}"
                )
            if key not in PROVIDER_BINDING_CONFIG_KEYS:
                raise RoleProviderBindingConfigError(
                    f"provider_binding config record has unknown key {key!r}; allowed "
                    f"keys: {sorted(PROVIDER_BINDING_CONFIG_KEYS)}"
                )
        version = _checked_version(record)
        overrides = record.get("bindings")
        if overrides is None:
            return cls.default()
        if not isinstance(overrides, Mapping):
            raise RoleProviderBindingConfigError(
                "provider_binding config 'bindings' must be a mapping of "
                f"role -> provider, got {type(overrides).__name__}"
            )
        # Reject a non-string role key up front so the delegated normaliser never sees a
        # coerced ``str(None)`` / ``str(5)`` role token.
        for role_key in overrides:
            if not isinstance(role_key, str) or not role_key.strip():
                raise RoleProviderBindingConfigError(
                    "provider_binding 'bindings' role keys must be non-empty strings; "
                    f"got {role_key!r}"
                )
        try:
            # Validate against the domain (fail-closed on an unknown role / empty provider)
            # so the config surface never accepts a binding the domain would reject.
            RoleProviderBinding.default().with_overrides(overrides)
        except RoleProviderBindingError as exc:
            # Re-raise as the config error so the closed-schema boundary is uniform (an
            # unknown role / empty provider surfaces as a provider_binding config failure).
            raise RoleProviderBindingConfigError(
                f"provider_binding config 'bindings' is invalid: {exc}"
            ) from exc
        # Store only the (normalized) configured overrides — not the full merged map — so
        # the record stays hashable and the resolved binding is recomputed on demand.
        stored = tuple(
            sorted(
                (normalize_role(role), normalize_provider(provider))
                for role, provider in overrides.items()
            )
        )
        return cls(version=version, overrides=stored)

    def advisory_warnings(self) -> tuple[str, ...]:
        """Advisory (non-blocking) warnings about the resolved binding.

        The one flagged condition (#13157): the auditor and the implementer resolve to the
        **same** provider. That is a valid, deliberately-permitted configuration — the
        workflow is not blocked — but it collapses the cross-provider separation between the
        surface that *implements* a change and the surface that *reviews* it, so the config
        surface flags it as an advisory warning the caller can surface. Every other binding
        (including the default codex/claude, where the two differ) yields no warning.
        """
        auditor = self.binding.provider_for(ROLE_AUDITOR)
        implementer = self.binding.provider_for(ROLE_IMPLEMENTER)
        if auditor and implementer and auditor == implementer:
            return (
                f"provider_binding: auditor and implementer both resolve to provider "
                f"{auditor!r}; the reviewer and the implementer share a runtime surface, "
                "so cross-provider audit separation is reduced (advisory only, not blocked)",
            )
        return ()


__all__ = (
    "PROVIDER_BINDING_CONFIG_VERSION",
    "PROVIDER_BINDING_CONFIG_KEYS",
    "RoleProviderBindingConfigError",
    "RoleProviderBindingConfig",
)
