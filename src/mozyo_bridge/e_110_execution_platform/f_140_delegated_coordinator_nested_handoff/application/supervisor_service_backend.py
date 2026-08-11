"""OS scheduler backend selection for the supervisor service lifecycle (Redmine #15183 / #15192).

``workflow supervisor --service-status / --install / --restart / --uninstall`` is ONE operator-facing
contract with two host realizations:

- macOS (:mod:`...application.supervisor_launchd`) — ONE owned LaunchAgent, ``RunAtLoad`` +
  ``StartInterval``.
- Linux (:mod:`...application.supervisor_systemd`) — ONE owned systemd user service + ONE timer,
  ``OnActiveSec=0s`` + ``OnUnitActiveSec``.

Since #15192 both register exactly one bounded ``workflow supervisor --run-once`` at the same shared
portable cadence, so the **operator-visible** contract — how many registrations exist, what they run,
what the verbs mean, what status reports — is the same on both. What stays deliberately different is
the *internals*: launchd and systemd are not made to mirror each other's mechanics, and neither is
forced onto a common scheduler (no cron). Retiring the second macOS agent is what let the platform
branching below disappear: both adapters now expose the same four verbs with the same signatures, so
this module resolves *which* adapter and normalizes the envelope, and nothing else.

The envelope every verb returns is ``{action, performed, reason, backend, effect_state,
agents: [...]}`` where ``agents`` is the per-owned-service rows the host adapter produced — one row
on each supported host. The CLI renders it without branching on platform.

A host with neither adapter is a typed zero-mutation refusal, never a silent no-op.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Optional

#: Fixed-vocabulary backend tokens (machine-readable; secret-safe).
BACKEND_LAUNCHD = "launchd"
BACKEND_SYSTEMD = "systemd_user"
#: No owned scheduler adapter exists for this host (neither macOS nor Linux).
BACKEND_UNSUPPORTED = "unsupported"

#: A verb was refused because this host has no supervisor scheduler adapter at all.
REASON_NO_BACKEND = "service_backend_unsupported_platform"

#: Closed, cross-backend mutation-effect vocabulary. ``performed`` means the requested operation
#: completed; it cannot by itself distinguish a pre-effect refusal from an interrupted mutation.
EFFECT_NONE = "none"
EFFECT_PARTIAL = "partial"
EFFECT_UNCERTAIN = "uncertain"
EFFECT_COMPLETE = "complete"
_MUTATING_ACTIONS = frozenset(("install", "restart", "uninstall"))


def resolve_backend_name(platform: Optional[str] = None) -> str:
    """The backend token owning ``platform`` (default: this host's ``sys.platform``).

    macOS -> :data:`BACKEND_LAUNCHD`; Linux -> :data:`BACKEND_SYSTEMD`; anything else ->
    :data:`BACKEND_UNSUPPORTED`. Whether the resolved backend is *usable* (a reachable systemd user
    manager, a present executable) is the adapter's own fail-closed preflight — this function only
    answers which adapter would be asked.
    """
    name = platform if platform is not None else sys.platform
    if name == "darwin":
        return BACKEND_LAUNCHD
    if name.startswith("linux"):
        return BACKEND_SYSTEMD
    return BACKEND_UNSUPPORTED


def resolve_backend(platform: Optional[str] = None) -> tuple[str, Optional[ModuleType]]:
    """``(backend_token, adapter_module)`` for ``platform``; the module is ``None`` when unsupported.

    The adapter modules are imported lazily so a Linux host does not import the launchd
    ``plistlib`` machinery (and vice versa) merely to render a refusal.
    """
    backend = resolve_backend_name(platform)
    if backend == BACKEND_LAUNCHD:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            supervisor_launchd,
        )

        return backend, supervisor_launchd
    if backend == BACKEND_SYSTEMD:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            supervisor_systemd,
        )

        return backend, supervisor_systemd
    return backend, None


def unsupported_result(action: str) -> dict:
    """The typed zero-mutation refusal for a host with no scheduler adapter."""
    return {
        "action": action,
        "performed": False,
        "reason": REASON_NO_BACKEND,
        "effect_state": EFFECT_NONE,
        "backend": BACKEND_UNSUPPORTED,
        "agents": [],
    }


# ---------------------------------------------------------------------------
# The dispatched verb surface the CLI calls.
#
# Both adapters expose the same four verbs with the same signatures (#15192), so there is no
# per-backend call shape left to express here — resolve the adapter, call the verb, normalize the
# envelope. Everything downstream reads the normalized ``agents`` list.
# ---------------------------------------------------------------------------


def _envelope(action: str, backend: str, result: dict) -> dict:
    """Normalize an adapter result into the common ``agents``-list envelope."""
    payload = dict(result)
    payload.setdefault("action", action)
    if action in _MUTATING_ACTIONS:
        payload.setdefault(
            "effect_state", EFFECT_COMPLETE if payload.get("performed") else EFFECT_NONE
        )
    payload["backend"] = backend
    if "agents" not in payload:
        # Each adapter owns one service and returns one flat row; present it as a one-element roster
        # so the renderer never branches. The row keeps its own keys untouched.
        payload["agents"] = [dict(result)]
    return payload


def install(
    *, mozyo_home=None, interval_seconds: Optional[int] = None, **kwargs
) -> dict:
    """Install the owned scheduled service on this host's OS scheduler.

    ``interval_seconds`` is the OS tick cadence, and it now means the same thing on both hosts
    (#15192): the interval of the single owned registration — a launchd ``StartInterval`` or a
    systemd ``OnUnitActiveSec``. Omitted, each adapter applies the shared portable default. It is
    never the Redmine cadence, which the supervisor body gates behind its own watermark.
    """
    backend, adapter = resolve_backend()
    if adapter is None:
        return unsupported_result("install")
    extra = {} if interval_seconds is None else {"interval_seconds": int(interval_seconds)}
    return _envelope("install", backend, adapter.install(mozyo_home=mozyo_home, **extra, **kwargs))


def restart(*, mozyo_home=None, **kwargs) -> dict:
    """Re-run the owned scheduled bounded sweep now on this host's OS scheduler."""
    backend, adapter = resolve_backend()
    if adapter is None:
        return unsupported_result("restart")
    return _envelope("restart", backend, adapter.restart(mozyo_home=mozyo_home, **kwargs))


def uninstall(**kwargs) -> dict:
    """Remove exactly the owned scheduler artifacts on this host's OS scheduler."""
    backend, adapter = resolve_backend()
    if adapter is None:
        return unsupported_result("uninstall")
    return _envelope("uninstall", backend, adapter.uninstall(**kwargs))


def service_status(*, mozyo_home=None, interval_hint: Optional[int] = None, **kwargs) -> dict:
    """Read-only redacted host status of the owned service. Mutates nothing."""
    backend, adapter = resolve_backend()
    if adapter is None:
        return {
            "action": "service-status",
            "backend": BACKEND_UNSUPPORTED,
            "platform_supported": False,
            "agents": [],
        }
    extra = {} if interval_hint is None else {"interval_hint": int(interval_hint)}
    return _envelope(
        "service-status", backend,
        adapter.service_status(mozyo_home=mozyo_home, **extra, **kwargs),
    )


__all__ = (
    "BACKEND_LAUNCHD",
    "BACKEND_SYSTEMD",
    "BACKEND_UNSUPPORTED",
    "REASON_NO_BACKEND",
    "EFFECT_NONE",
    "EFFECT_PARTIAL",
    "EFFECT_UNCERTAIN",
    "EFFECT_COMPLETE",
    "resolve_backend_name",
    "resolve_backend",
    "unsupported_result",
    "install",
    "restart",
    "uninstall",
    "service_status",
)
