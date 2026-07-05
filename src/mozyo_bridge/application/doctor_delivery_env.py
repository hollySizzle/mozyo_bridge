"""Doctor persist-delivery env-presence section boundary (Redmine #13262).

The opt-in ``--persist-delivery`` live-write rail is gated by three environment
variables read from the trusted daemon environment:

- ``MOZYO_REDMINE_DELIVERY_WRITE`` — the explicit live-write opt-in; unset -> no
  transport injected -> the sink fails closed with ``write_optin_unset``;
- ``MOZYO_REDMINE_URL`` — the trusted Redmine base URL; missing/invalid while the
  opt-in is set -> the transport fails closed with ``base_url_unset``;
- ``MOZYO_REDMINE_API_KEY`` — the API key; missing while the opt-in is set -> the
  transport fails closed with ``credential_missing``.

Redmine #13262 split the former single ``provider_unavailable`` reason into the
distinct ``write_optin_unset`` / ``base_url_unset`` reasons so an operator can tell
"the opt-in was never set" apart from "the opt-in is set but the base URL is
missing/invalid". This doctor section is the companion read-side surface: it reports
which of the three gates is **set vs unset** so an operator can reconcile a
fail-closed persist receipt with the environment.

Hard boundary (``vibes/docs/rules/public-private-boundary.md``): the base URL and
the API key are credentials. This section reports **only set/unset booleans** — it
never reads, prints, logs, or otherwise exposes any value, and it never
auto-enables anything. It is strictly informational: ``status`` is always ``"ok"``
so it can never drag the aggregate doctor verdict (the verdict is a health signal;
an unset opt-in is a valid, common configuration, not a fault).

This module has NO direct I/O in its policy: :func:`evaluate_delivery_env_section`
is pure over a ``{env_name: is_set}`` presence map, and :class:`LiveDeliveryEnvReads`
is the thin adapter that reads ``os.environ`` (presence only). That keeps the policy
exercisable with synthetic presence maps and free of any real environment coupling.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, Protocol, runtime_checkable

from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_note_transport import (
    DELIVERY_WRITE_ENV,
)
from mozyo_bridge.redmine_context import API_KEY_ENV, BASE_URL_ENV

# The three gates, in the order they are evaluated on the live-write rail
# (opt-in -> base URL -> API key). Order fixed so the rendered section is stable.
DELIVERY_ENV_VARS: tuple[str, ...] = (DELIVERY_WRITE_ENV, BASE_URL_ENV, API_KEY_ENV)


def evaluate_delivery_env_section(present: Mapping[str, bool]) -> dict[str, Any]:
    """Pure policy: derive the delivery-env section from a presence map.

    ``present`` maps each env var name to a bool (set vs unset). The section carries
    only booleans — never a value — and always reports ``status="ok"`` because an
    unset opt-in is a valid configuration, not a health fault.
    """
    return {
        "status": "ok",
        "write_optin_set": bool(present.get(DELIVERY_WRITE_ENV, False)),
        "base_url_set": bool(present.get(BASE_URL_ENV, False)),
        "api_key_set": bool(present.get(API_KEY_ENV, False)),
    }


@runtime_checkable
class DeliveryEnvReads(Protocol):
    """Port: report which delivery-env vars are set (presence only, no values)."""

    def env_presence(self) -> dict[str, bool]:
        ...


class LiveDeliveryEnvReads:
    """Live adapter: read ``os.environ`` presence for the three gates.

    Presence is ``True`` only when the var is set to a non-empty (stripped) value,
    so an empty assignment reads as unset. The *value* is never returned, echoed,
    or compared beyond emptiness — no credential can leak through this adapter.
    """

    def env_presence(self) -> dict[str, bool]:
        return {name: bool((os.environ.get(name) or "").strip()) for name in DELIVERY_ENV_VARS}


class DeliveryEnvSectionUseCase:
    """Use case: read presence via the port, apply the pure policy."""

    def __init__(self, reads: DeliveryEnvReads) -> None:
        self._reads = reads

    def execute(self) -> dict[str, Any]:
        return evaluate_delivery_env_section(self._reads.env_presence())


__all__ = [
    "DELIVERY_ENV_VARS",
    "DeliveryEnvReads",
    "DeliveryEnvSectionUseCase",
    "LiveDeliveryEnvReads",
    "evaluate_delivery_env_section",
]
