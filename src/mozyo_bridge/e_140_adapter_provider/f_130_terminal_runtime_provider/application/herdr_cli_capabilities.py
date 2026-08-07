"""Actuation-free admission for the Herdr 0.8 managed-launch CLI surface."""

from __future__ import annotations

import subprocess
import re
from dataclasses import dataclass
from typing import Mapping, Type

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    Runner,
    _bounded_detail,
)


REASON_HELP_UNAVAILABLE = "herdr_help_unavailable"
REASON_AGENT_START_SURFACE_MISMATCH = "herdr_agent_start_surface_mismatch"
REASON_PANE_SPLIT_SURFACE_MISMATCH = "herdr_pane_split_surface_mismatch"
REASON_PANE_RUN_SURFACE_MISMATCH = "herdr_pane_run_surface_mismatch"


class HerdrCliCapabilityError(RuntimeError):
    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class HerdrCliCapabilities:
    agent_start_help: str
    pane_split_help: str
    pane_run_help: str


def _option_tokens(help_text: str) -> frozenset[str]:
    """Parse complete long-option tokens; ``--no-focus`` never implies ``--focus``."""
    return frozenset(re.findall(r"(?<![A-Za-z0-9-])--[a-z][a-z0-9-]*", help_text))


def _help(
    binary: str,
    tail: list[str],
    *,
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
) -> str:
    argv = [binary, *tail, "--help"]
    try:
        completed = runner(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(env),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HerdrCliCapabilityError(
            "Herdr CLI help could not be read before managed launch",
            reason=REASON_HELP_UNAVAILABLE,
        ) from exc
    if completed.returncode != 0:
        detail = _bounded_detail(completed.stderr) or f"exit {completed.returncode}"
        raise HerdrCliCapabilityError(
            f"Herdr CLI help was refused before managed launch ({detail})",
            reason=REASON_HELP_UNAVAILABLE,
        )
    return f"{completed.stdout or ''}\n{completed.stderr or ''}"


def observe_herdr_cli_capabilities(
    binary: str,
    *,
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
) -> HerdrCliCapabilities:
    """Require the exact 0.8 pane-bound launch surface before any Herdr write.

    Version text is deliberately not the authority.  A repackaged binary can retain a
    version string while changing flags; these three help surfaces are the operations the
    launch will actually issue.
    """
    agent_help = _help(
        binary, ["agent", "start"], runner=runner, timeout=timeout, env=env
    )
    agent_options = _option_tokens(agent_help)
    agent_usage = " ".join(agent_help.split())
    required_agent = frozenset({"--kind", "--pane"})
    forbidden_agent = frozenset({"--cwd", "--workspace", "--env", "--split"})
    required_name = bool(
        re.search(r"\bUsage:\s*herdr\s+agent\s+start\s+<NAME>(?:\s|$)", agent_usage)
    )
    required_agent_tail = bool(
        re.search(r"\[\s*--\s+\[AGENT_ARG\]\.\.\.\s*\]", agent_usage)
    )
    if (
        not required_agent.issubset(agent_options)
        or agent_options & forbidden_agent
        or not required_name
        or not required_agent_tail
    ):
        raise HerdrCliCapabilityError(
            "Herdr agent start is not the required 0.8 pane-bound surface; no pane was created",
            reason=REASON_AGENT_START_SURFACE_MISMATCH,
        )

    pane_help = _help(
        binary, ["pane", "split"], runner=runner, timeout=timeout, env=env
    )
    pane_options = _option_tokens(pane_help)
    required_pane = frozenset(
        {"--direction", "--cwd", "--env", "--focus", "--no-focus"}
    )
    pane_usage = " ".join(pane_help.split())
    explicit_target = bool(
        re.search(
            r"\bUsage:\s*herdr\s+pane\s+split(?:\s+\[OPTIONS\])?"
            r"\s+\[PANE_ID\](?:\s|$)",
            pane_usage,
        )
    )
    if not required_pane.issubset(pane_options) or not explicit_target:
        raise HerdrCliCapabilityError(
            "Herdr pane split does not expose the required 0.8 placement/env surface; "
            "no pane was created",
            reason=REASON_PANE_SPLIT_SURFACE_MISMATCH,
        )

    pane_run_help = _help(
        binary, ["pane", "run"], runner=runner, timeout=timeout, env=env
    )
    pane_run_usage = " ".join(pane_run_help.split())
    if not re.search(
        r"\bUsage:\s*herdr\s+pane\s+run\s+<PANE_ID>\s+<COMMAND>\.\.\.(?:\s|$)",
        pane_run_usage,
    ):
        raise HerdrCliCapabilityError(
            "Herdr pane run does not expose the required exact-pane command surface; "
            "no pane was created",
            reason=REASON_PANE_RUN_SURFACE_MISMATCH,
        )
    return HerdrCliCapabilities(agent_help, pane_help, pane_run_help)


def require_herdr_cli_capabilities(
    binary: str,
    *,
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
    error_type: Type[Exception] = RuntimeError,
) -> HerdrCliCapabilities:
    """Translate the typed capability refusal onto the caller's public boundary."""
    try:
        return observe_herdr_cli_capabilities(
            binary, runner=runner, timeout=timeout, env=env
        )
    except HerdrCliCapabilityError as exc:
        raise error_type(f"{exc} (reason={exc.reason})") from exc


__all__ = (
    "HerdrCliCapabilities",
    "HerdrCliCapabilityError",
    "REASON_AGENT_START_SURFACE_MISMATCH",
    "REASON_HELP_UNAVAILABLE",
    "REASON_PANE_RUN_SURFACE_MISMATCH",
    "REASON_PANE_SPLIT_SURFACE_MISMATCH",
    "observe_herdr_cli_capabilities",
    "require_herdr_cli_capabilities",
)
