"""Doctor persist-delivery credential-resolution section boundary (Redmine #13262 / #15698).

The opt-in ``--persist-delivery`` live-write rail is gated by an explicit env
opt-in plus two credentials:

- ``MOZYO_REDMINE_DELIVERY_WRITE`` — the explicit live-write opt-in; unset -> no
  transport injected -> the sink fails closed with ``write_optin_unset``. The
  opt-in is **environment-only** (Redmine #15692 left its semantics unchanged),
  so this section still reports it as a set/unset presence boolean;
- the trusted Redmine base URL and API key — since Redmine #15692 the write
  transport resolves both through :func:`resolve_redmine_credentials` (env
  first, per-field fallback to the home-scoped, owner-only credential file).
  Missing/invalid while the opt-in is set -> the transport fails closed with
  ``base_url_unset`` / ``credential_missing``.

Redmine #15698: before this issue the section reported env **presence** only
(``base_url_set`` / ``api_key_set``), so a file-supplied configuration showed
``False`` while writes succeeded — informational drift between doctor and the
resolver. The section now reports the resolver's per-field outcome instead:
``base_url_source`` / ``api_key_source`` carry ``"env"`` / ``"file"`` /
``"unresolved"``, matching what the write transport will actually use.

Hard boundary (``vibes/docs/rules/public-private-boundary.md``): the base URL and
the API key are credentials. This section reports **only the source labels and
the opt-in boolean** — it never reads back, prints, logs, or otherwise exposes
any credential value, and it never auto-enables anything. It is strictly
informational: ``status`` is always ``"ok"`` so it can never drag the aggregate
doctor verdict (the verdict is a health signal; an unset opt-in or unresolved
credentials are valid, common configurations, not faults).

This module has NO direct I/O in its policy: :func:`evaluate_delivery_env_section`
is pure over the opt-in boolean and a ``{field: source}`` map, and
:class:`LiveDeliveryEnvReads` is the thin adapter that reads ``os.environ``
presence for the opt-in and the resolver's ``source`` provenance (never the
resolved values). That keeps the policy exercisable with synthetic inputs and
free of any real environment coupling.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_credentials import (
    resolve_redmine_credentials,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_note_transport import (
    DELIVERY_WRITE_ENV,
)

# Resolver provenance labels the section may report. Anything else (including a
# missing entry) collapses to "unresolved" so a resolver refactor can never leak
# an unexpected token — let alone a value — into the doctor output.
CREDENTIAL_SOURCES: tuple[str, ...] = ("env", "file")
UNRESOLVED = "unresolved"

# The two resolver-backed credential fields, in render order (base URL first,
# matching the fail-closed receipt order base_url_unset -> credential_missing).
CREDENTIAL_FIELDS: tuple[str, ...] = ("base_url", "api_key")


def evaluate_delivery_env_section(
    write_optin_set: bool,
    credential_sources: Mapping[str, str | None],
) -> dict[str, Any]:
    """Pure policy: derive the delivery-env section from resolver provenance.

    ``credential_sources`` maps ``base_url`` / ``api_key`` to the resolver's
    ``source`` label (``"env"`` / ``"file"`` / ``None``). The section carries
    only the opt-in boolean and normalized source labels — never a value — and
    always reports ``status="ok"`` because an unset opt-in or an unresolved
    credential is a valid configuration, not a health fault.
    """

    def normalized(field: str) -> str:
        source = credential_sources.get(field)
        return source if source in CREDENTIAL_SOURCES else UNRESOLVED

    return {
        "status": "ok",
        "write_optin_set": bool(write_optin_set),
        "base_url_source": normalized("base_url"),
        "api_key_source": normalized("api_key"),
    }


@runtime_checkable
class DeliveryEnvReads(Protocol):
    """Port: report the opt-in presence and resolver provenance (no values)."""

    def write_optin_set(self) -> bool:
        ...

    def credential_sources(self) -> dict[str, str | None]:
        ...


class LiveDeliveryEnvReads:
    """Live adapter: opt-in presence from ``os.environ``, provenance from the resolver.

    The opt-in reads as set only when ``MOZYO_REDMINE_DELIVERY_WRITE`` has a
    non-empty (stripped) value, so an empty assignment reads as unset — the same
    presence rule the section always had. The credential fields come from
    :func:`resolve_redmine_credentials`: only its ``source`` provenance dict is
    returned; the resolved values are dropped on the floor here and can never
    reach the section. ``home`` is injectable for hermetic tests and defaults to
    the resolver's own ``MOZYO_BRIDGE_HOME`` handling.
    """

    def __init__(self, home: Path | None = None) -> None:
        self._home = home

    def write_optin_set(self) -> bool:
        return bool((os.environ.get(DELIVERY_WRITE_ENV) or "").strip())

    def credential_sources(self) -> dict[str, str | None]:
        return dict(resolve_redmine_credentials(self._home).source)


class DeliveryEnvSectionUseCase:
    """Use case: read via the port, apply the pure policy."""

    def __init__(self, reads: DeliveryEnvReads) -> None:
        self._reads = reads

    def execute(self) -> dict[str, Any]:
        return evaluate_delivery_env_section(
            self._reads.write_optin_set(), self._reads.credential_sources()
        )


__all__ = [
    "CREDENTIAL_FIELDS",
    "CREDENTIAL_SOURCES",
    "DeliveryEnvReads",
    "DeliveryEnvSectionUseCase",
    "LiveDeliveryEnvReads",
    "UNRESOLVED",
    "evaluate_delivery_env_section",
]
