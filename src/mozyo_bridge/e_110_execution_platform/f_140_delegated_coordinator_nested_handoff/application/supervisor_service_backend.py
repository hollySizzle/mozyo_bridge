"""OS scheduler backend selection for the supervisor service lifecycle (Redmine #15183).

``workflow supervisor --service-status / --install / --restart / --uninstall`` is ONE operator-facing
contract with two host realizations, and they are deliberately **not** the same shape inside:

- macOS (:mod:`...application.supervisor_launchd`) keeps its existing owned dual-agent LaunchAgent
  pair (reconcile + drain). This module does not change it — reorganizing the macOS setup is
  explicitly out of scope for #15183.
- Linux (:mod:`...application.supervisor_systemd`) is ONE systemd user service + ONE timer running
  ``--run-once`` every 60s, with Redmine reads gated behind the supervisor body's own 300s cadence.

Making Linux mirror the macOS internals was removed from the acceptance contract, so this module
does not force a common internal shape. It normalizes only the **result envelope** the CLI renders:
every verb returns ``{action, performed, reason, backend, agents: [...]}`` where ``agents`` is the
per-owned-service rows the host adapter produced — two on macOS, one on Linux. The CLI therefore
renders both without branching on platform, while each adapter stays honest about its own shape.

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
        "backend": BACKEND_UNSUPPORTED,
        "agents": [],
    }


# ---------------------------------------------------------------------------
# The dispatched verb surface the CLI calls.
#
# macOS exposes ``*_pair`` verbs over two owned agents; Linux exposes single-service verbs. The
# per-backend call shapes below are the ONLY place that difference is expressed — everything
# downstream reads the normalized ``agents`` list.
# ---------------------------------------------------------------------------


def _envelope(action: str, backend: str, result: dict) -> dict:
    """Normalize an adapter result into the common ``agents``-list envelope."""
    payload = dict(result)
    payload.setdefault("action", action)
    payload["backend"] = backend
    if "agents" not in payload:
        # A single-service adapter returns one flat row; present it as a one-element roster so the
        # renderer never branches. The row keeps its own keys untouched.
        payload["agents"] = [dict(result)]
    return payload


def install(
    *, mozyo_home=None, interval_seconds: Optional[int] = None, **kwargs
) -> dict:
    """Install the owned scheduled service(s) on this host's OS scheduler.

    ``interval_seconds`` is the OS tick cadence. On Linux it is the single timer's interval
    (default 60s). On macOS, where the owned pair has two distinct cadences, it is ignored and the
    adapter's own reconcile / drain defaults apply — the macOS shape is out of scope for #15183.
    """
    backend, adapter = resolve_backend()
    if adapter is None:
        return unsupported_result("install")
    if backend == BACKEND_SYSTEMD:
        extra = {} if interval_seconds is None else {"interval_seconds": int(interval_seconds)}
        return _envelope("install", backend, adapter.install(mozyo_home=mozyo_home, **extra, **kwargs))
    return _envelope("install", backend, adapter.install_pair(mozyo_home=mozyo_home, **kwargs))


def restart(*, mozyo_home=None, **kwargs) -> dict:
    """Re-run the owned scheduled bounded sweep(s) now on this host's OS scheduler."""
    backend, adapter = resolve_backend()
    if adapter is None:
        return unsupported_result("restart")
    verb = adapter.restart if backend == BACKEND_SYSTEMD else adapter.restart_pair
    return _envelope("restart", backend, verb(mozyo_home=mozyo_home, **kwargs))


def uninstall(**kwargs) -> dict:
    """Remove exactly the owned scheduler artifacts on this host's OS scheduler."""
    backend, adapter = resolve_backend()
    if adapter is None:
        return unsupported_result("uninstall")
    verb = adapter.uninstall if backend == BACKEND_SYSTEMD else adapter.uninstall_pair
    return _envelope("uninstall", backend, verb(**kwargs))


def service_status(*, mozyo_home=None, interval_hint: Optional[int] = None, **kwargs) -> dict:
    """Read-only redacted host status of the owned service(s). Mutates nothing."""
    backend, adapter = resolve_backend()
    if adapter is None:
        return {
            "action": "service-status",
            "backend": BACKEND_UNSUPPORTED,
            "platform_supported": False,
            "agents": [],
        }
    if backend == BACKEND_SYSTEMD:
        extra = {} if interval_hint is None else {"interval_hint": int(interval_hint)}
        return _envelope(
            "service-status", backend,
            adapter.service_status(mozyo_home=mozyo_home, **extra, **kwargs),
        )
    return _envelope(
        "service-status", backend, adapter.service_status_pair(mozyo_home=mozyo_home, **kwargs)
    )


__all__ = (
    "BACKEND_LAUNCHD",
    "BACKEND_SYSTEMD",
    "BACKEND_UNSUPPORTED",
    "REASON_NO_BACKEND",
    "resolve_backend_name",
    "resolve_backend",
    "unsupported_result",
    "install",
    "restart",
    "uninstall",
    "service_status",
)
