"""Port boundary over the live tmux control surface (Redmine #12749 / #12638).

The OOP-first architecture policy
(``vibes/docs/logics/object-oriented-architecture-policy.md``) requires that
tmux / Redmine / Git / subprocess boundaries be expressed as a ``Protocol``
port with a live adapter and a test fake, instead of being called as naked
free functions inside procedural command handlers. ``commands.py`` historically
reached straight for the module-level ``require_tmux`` / ``source_tmux_conf``
bindings (and tests patched them via ``mozyo_bridge.application.commands.*``),
which mixed the external boundary with CLI parsing and presentation.

This module defines the narrow :class:`TmuxControlPort` the tmux-config /
tmux-ui command family depends on, plus the live adapter
:class:`LiveTmuxControlPort` that delegates to the real tmux CLI wrappers in
``infrastructure.tmux_client``. A use case (``ApplyTmuxConfigUseCase`` in
``commands_tmux_ui``) takes the port by constructor injection, so its unit test
drives a fake port (``FakeTmuxControlPort``) and never shells out to tmux or
relies on function monkeypatch — the first fake-port boundary established for
the ``commands.py`` OOP-first decomposition (#12638 / #12785).

Scope: this is the tmux *control* surface used by the config-load path
(availability check + ``tmux source-file``). It is intentionally NOT the
send-keys / paste-buffer safety path (``tmux-send-safety-contract``); the
delivery rail keeps its own dedicated, characterized seam.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (
    require_tmux as _require_tmux,
    source_tmux_conf as _source_tmux_conf,
)


@runtime_checkable
class TmuxControlPort(Protocol):
    """The tmux control operations the tmux-config command family depends on.

    Kept deliberately small (interface segregation): only the availability
    guard and the config-source operation the config-load use case needs. The
    install / uninstall / status commands operate on the host tmux conf file
    through the ``tmux_ui`` domain and do not require a live tmux server, so
    they do not depend on this port.
    """

    def require_available(self) -> None:
        """Fail closed (``SystemExit``) when tmux is not installed / on PATH."""
        ...

    def source_conf(self, path: str, *, optional: bool = False) -> bool:
        """Source ``path`` into the running tmux server.

        Returns ``True`` when ``tmux source-file`` was invoked. With
        ``optional=True`` a missing file is a no-op returning ``False``.
        """
        ...


class LiveTmuxControlPort:
    """Live adapter delegating to the real tmux CLI wrappers.

    Holds all environment / subprocess dependency for the tmux control
    surface; the use case stays free of naked ``require_tmux`` /
    ``source_tmux_conf`` calls.
    """

    def require_available(self) -> None:
        _require_tmux()

    def source_conf(self, path: str, *, optional: bool = False) -> bool:
        return _source_tmux_conf(path, optional=optional)
