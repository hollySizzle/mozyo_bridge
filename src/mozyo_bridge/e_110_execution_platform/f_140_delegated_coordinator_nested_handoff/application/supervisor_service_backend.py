"""OS scheduler backend selection for the supervisor service lifecycle (Redmine #15183).

``workflow supervisor --service-status / --install / --restart / --uninstall`` is ONE service
lifecycle contract with two host realizations: the macOS LaunchAgent pair
(:mod:`...application.supervisor_launchd`) and the Linux systemd user service+timer pair
(:mod:`...application.supervisor_systemd`). Both expose the identical ``*_pair`` verb surface, so
the CLI does not branch on platform — it asks here which adapter owns this host and drives it.

Why dispatch and not an explicit ``--backend`` flag: the acceptance contract is "the same service
lifecycle on Linux", so an operator's install command must not change per host. The resolved backend
is still *visible* — every result and status projection carries a ``backend`` token — so a reader can
always tell which adapter answered, and a host with no adapter is a typed zero-mutation refusal, not
a silent no-op.
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
    manager, a present executable, a ready credential) is the adapter's own fail-closed preflight —
    this function only answers which adapter would be asked.
    """
    name = platform if platform is not None else sys.platform
    if name == "darwin":
        return BACKEND_LAUNCHD
    if name.startswith("linux"):
        return BACKEND_SYSTEMD
    return BACKEND_UNSUPPORTED


def resolve_backend(platform: Optional[str] = None) -> tuple[str, Optional[ModuleType]]:
    """``(backend_token, adapter_module)`` for ``platform``; the module is ``None`` when unsupported.

    The adapter modules are imported lazily so a host that will never use launchd does not import
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


def unsupported_status() -> dict:
    """The read-only status projection for a host with no scheduler adapter (mutates nothing)."""
    return {
        "action": "service-status",
        "backend": BACKEND_UNSUPPORTED,
        "platform_supported": False,
        "agents": [],
    }


# ---------------------------------------------------------------------------
# The dispatched verb surface every consumer should call.
#
# Importing an adapter module directly binds a consumer to one OS for the life of the process — the
# exact defect Redmine #15183 fixes in the CLI, and the reason these wrappers exist rather than a
# "resolve then call" idiom repeated at each call site. Each wrapper stamps the resolved ``backend``
# token into the result so a reader can always tell which adapter answered.
# ---------------------------------------------------------------------------


def _dispatch(action: str, verb: str, kwargs: dict) -> dict:
    backend, adapter = resolve_backend()
    if adapter is None:
        return unsupported_result(action)
    result = dict(getattr(adapter, verb)(**kwargs))
    result["backend"] = backend
    return result


def install_pair(**kwargs) -> dict:
    """Install the owned scheduled pair on this host's OS scheduler (atomic-or-nothing)."""
    return _dispatch("install", "install_pair", kwargs)


def restart_pair(**kwargs) -> dict:
    """Re-run the owned scheduled bounded sweeps now on this host's OS scheduler."""
    return _dispatch("restart", "restart_pair", kwargs)


def uninstall_pair(**kwargs) -> dict:
    """Remove exactly the owned scheduler artifacts on this host's OS scheduler."""
    return _dispatch("uninstall", "uninstall_pair", kwargs)


def service_status_pair(**kwargs) -> dict:
    """Read-only redacted host status of the owned pair. Mutates nothing."""
    backend, adapter = resolve_backend()
    if adapter is None:
        return unsupported_status()
    status = dict(adapter.service_status_pair(**kwargs))
    status["backend"] = backend
    return status


__all__ = (
    "BACKEND_LAUNCHD",
    "BACKEND_SYSTEMD",
    "BACKEND_UNSUPPORTED",
    "REASON_NO_BACKEND",
    "resolve_backend_name",
    "resolve_backend",
    "unsupported_result",
    "unsupported_status",
    "install_pair",
    "restart_pair",
    "uninstall_pair",
    "service_status_pair",
)
