"""Process execution seam for the macOS supervisor adapter (Redmine #15192).

This module is the only launchd-adapter leaf that starts a process. It turns typed arguments into
structured ``launchctl`` argv and delegates them to an injected runner; it never reads or writes an
owned plist. Keeping this boundary separate makes the neighbouring agent/text and filesystem
modules' non-mutation claims structurally true instead of relying on callers to avoid a generic
process helper (review j#102843 finding r15f4).

Lifecycle policy remains in :mod:`supervisor_launchd`: this seam does not decide whether
``bootstrap``, ``bootout`` or ``kickstart`` is authorized. Tests inject a runner, so importing this
module never touches the real host service manager.
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_launchd_agent import (  # noqa: E501
    SUPERVISOR_AGENT,
    SupervisorAgent,
)

#: The manager binary. Always invoked as structured argv; no shell is involved.
LAUNCHCTL = "launchctl"

#: Injection seam used by lifecycle and read-only probe code.
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    """Run structured argv for real. The launchd adapter's only process-spawning function."""
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def gui_domain() -> str:
    """The per-user launchd domain a LaunchAgent lives in."""
    return f"gui/{os.getuid()}"


def service_target(agent: SupervisorAgent = SUPERVISOR_AGENT) -> str:
    """``<domain>/<label>`` — how launchctl names one service."""
    return f"{gui_domain()}/{agent.label}"


def launchctl(runner: Runner, args: Sequence[str]):
    """Build a structured launchctl argv and hand it to ``runner``."""
    return runner([LAUNCHCTL, *args])


__all__ = (
    "LAUNCHCTL",
    "Runner",
    "default_runner",
    "gui_domain",
    "service_target",
    "launchctl",
)
