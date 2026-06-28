"""Port boundary over the tmux pane user-option write surface (Redmine #12749 / #12638).

Second OOP-first port for the ``commands.py`` decomposition (after
``tmux_control_port`` in the tmux-config tranche). The ``agents
attention-project`` command projects a re-derivable attention cache onto each
target pane's tmux user options via ``tmux set-option``. The old procedural
handler issued those writes with a naked ``run_tmux(*argv, check=False)`` call
inside its candidate loop, and its test patched
``mozyo_bridge.application.commands.run_tmux`` to capture the argv — a
function-monkeypatch seam that mixed the side-effecting boundary with discovery
and presentation.

This module defines :class:`TmuxOptionWriterPort` — the narrow "apply one
pane-scoped ``set-option`` argv" operation the attention-projection use case
depends on — with a live adapter (:class:`LiveTmuxOptionWriter`) that delegates
to the real ``run_tmux`` wrapper. The use case takes the port by injection so
its specification test drives a fake writer (no real tmux, no function patch).

Scope: the *projection cache* write only (``set-option -p``). This is not the
send-keys / paste-buffer routing path (``tmux-send-safety-contract``); the
attention projection is explicitly a non-routing boundary and the port exposes
no key-send operation.
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (
    run_tmux as _run_tmux,
)


@runtime_checkable
class TmuxOptionWriterPort(Protocol):
    """Apply a single pane-scoped tmux ``set-option`` invocation."""

    def set_option(self, argv: Sequence[str]) -> bool:
        """Run one ``set-option`` ``argv``; return ``True`` on success.

        Best-effort: a failed write returns ``False`` rather than raising, so the
        projection-cache posture (a failed option write never aborts the run) is
        owned by the caller, not by a thrown exception.
        """
        ...


class LiveTmuxOptionWriter:
    """Live adapter delegating to the real ``run_tmux`` wrapper.

    Holds the subprocess dependency for the option-write boundary; the use case
    stays free of any naked ``run_tmux`` call.
    """

    def set_option(self, argv: Sequence[str]) -> bool:
        return _run_tmux(*argv, check=False).returncode == 0
