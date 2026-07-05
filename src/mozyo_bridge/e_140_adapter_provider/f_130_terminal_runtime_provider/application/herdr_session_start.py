"""herdr session-start one-command — the durable-name write side (Redmine #13261).

`mozyo-bridge herdr session-start` is the opt-in helper that prepares a **pure herdr
session** for mozyo handoff routing. Nothing in the codebase ever *wrote* a durable
herdr name before this (the #13175 PoC did ``agent rename`` by hand); this command
mints them so the herdr-native target resolution (#13261 read side) has stable
identities to resolve against.

Flow (per requested provider agent, ``claude`` / ``codex``):

1. resolve the herdr binary from the **trusted environment** (``MOZYO_HERDR_BINARY``);
   unset / unresolvable fails closed (never a repo-local binary);
2. ensure the workspace is registered (``register_workspace`` / anchor reuse) and take
   its ``workspace_id`` — the workspace_registry schema is unchanged (#11425);
3. mint the durable name ``encode_assigned_name(workspace_id, provider, lane)`` (#13247);
4. **idempotency:** if a live agent already carries that exact assigned name, *adopt*
   it (no launch, no rename). A duplicated assigned name (more than one live agent)
   fails closed rather than corrupting identity;
5. otherwise launch the agent as a herdr-managed pane pinned to the repo root, with the
   self-identity env (``MOZYO_WORKSPACE_ID`` / ``MOZYO_AGENT_ROLE`` / ``MOZYO_LANE_ID``)
   injected into its process, then rename the fresh live locator to the minted name.

The command is explicit opt-in and is **not** coupled to the ``terminal_transport``
backend flag: you may prepare herdr identities without selecting the herdr transport,
and vice versa (documented in ``vibes/docs/specs/herdr-native-identity.md``). In pure
herdr operation you run both.

Staged actuator, fake-tested: the herdr ``agent start`` / ``agent rename`` argv shapes
are built here and exercised through an injected subprocess ``runner`` — no live herdr
binary runs in tests. The precise ``agent start`` argv (cwd / command handoff) is the
one detail the PoC log left unconfirmed (E6 recorded only its existence); it is treated
as a staged assumption and confirmed against a live binary in the coordinator's
post-review smoke, exactly like the sibling #13245-series seams.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.core.state.workspace_registry import read_anchor, register_workspace
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    _agent_locator,
    _norm,
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (
    AGENT_PROVIDERS,
    MOZYO_AGENT_ROLE_ENV,
    MOZYO_LANE_ID_ENV,
    MOZYO_WORKSPACE_ID_ENV,
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    valid_target,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_state import (
    _extract_list_rows,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    COMMAND_TIMEOUT_SECONDS,
    HERDR_BINARY_ENV,
    Runner,
    _bounded_detail,
    _resolve_binary,
)
from mozyo_bridge.shared.errors import die

# Per-slot outcome tokens.
SLOT_ADOPTED = "adopted"
SLOT_LAUNCHED = "launched"
SLOT_PLANNED = "planned"


class HerdrSessionStartError(ValueError):
    """A herdr session-start step cannot proceed (fail-closed)."""


@dataclass(frozen=True)
class SlotResult:
    """The outcome of preparing one provider slot's durable herdr identity."""

    provider: str
    assigned_name: str
    outcome: str
    locator: str = ""
    detail: str = ""

    def as_payload(self) -> dict:
        return {
            "provider": self.provider,
            "assigned_name": self.assigned_name,
            "outcome": self.outcome,
            "locator": self.locator,
            "detail": self.detail,
        }


@dataclass
class SessionStartResult:
    """The aggregate outcome of a session-start run."""

    workspace_id: str
    lane_id: str
    slots: list = field(default_factory=list)

    def as_payload(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "slots": [slot.as_payload() for slot in self.slots],
        }


def _resolve_binary_or_die(env: Mapping[str, str]) -> str:
    raw = env.get(HERDR_BINARY_ENV)
    binary = raw.strip() if isinstance(raw, str) else ""
    if not binary:
        raise HerdrSessionStartError(
            f"no herdr binary is configured in the trusted environment "
            f"({HERDR_BINARY_ENV})"
        )
    resolved = _resolve_binary(binary, env)
    if resolved is None:
        raise HerdrSessionStartError(
            f"herdr binary {binary!r} (from {HERDR_BINARY_ENV}) was not found as an "
            f"executable file or on the trusted environment PATH"
        )
    return resolved


def _list_rows(binary: str, runner: Runner, timeout: float) -> Sequence[Mapping[str, object]]:
    """Run herdr ``agent list`` and return raw rows (fail-closed)."""
    completed = _invoke(binary, ["agent", "list"], runner, timeout, env=None)
    rows = _extract_list_rows(completed.stdout)
    if rows is None:
        raise HerdrSessionStartError(
            "herdr agent list payload was not a recognised JSON array or agents object"
        )
    return rows


def _invoke(
    binary: str,
    tail: Sequence[str],
    runner: Runner,
    timeout: float,
    *,
    env: Optional[Mapping[str, str]],
) -> "subprocess.CompletedProcess[str]":
    """Run ``binary tail...`` fail-closed; raise on any mechanical / non-zero failure."""
    argv = [binary, *tail]
    try:
        completed = runner(
            argv, capture_output=True, text=True, timeout=timeout, env=env
        )
    except FileNotFoundError:
        raise HerdrSessionStartError(f"herdr binary not found: {binary!r}")
    except subprocess.TimeoutExpired:
        raise HerdrSessionStartError(f"herdr command timed out: {list(tail)!r}")
    except OSError as exc:
        raise HerdrSessionStartError(
            f"herdr command failed ({exc.__class__.__name__}): {list(tail)!r}"
        )
    if completed.returncode != 0:
        raise HerdrSessionStartError(
            _bounded_detail(completed.stderr)
            or f"herdr {list(tail)!r} exited {completed.returncode}"
        )
    return completed


def _find_named_agent(
    rows: Sequence[Mapping[str, object]], assigned_name: str
) -> list:
    """Rows whose durable name equals ``assigned_name`` (fail-closed on duplicates)."""
    return [
        row
        for row in rows
        if isinstance(row, Mapping) and _norm(row.get(AGENT_KEY_NAME)) == assigned_name
    ]


def _parse_started_locator(stdout: object) -> Optional[str]:
    """Best-effort: read the live pane locator from an ``agent start`` payload.

    Defensive (the exact schema is a staged assumption): a JSON object contributes
    the transient locator (``pane_id`` / ``pane`` / ``location``); anything else
    yields ``None`` so the caller fails closed rather than renaming a blank handle.
    """
    if not isinstance(stdout, str):
        return None
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    locator = _agent_locator(payload)
    return locator or None


def prepare_session(
    *,
    repo_root: Path,
    providers: Sequence[str],
    lane_id: str,
    env: Mapping[str, str],
    runner: Optional[Runner] = None,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
    dry_run: bool = False,
) -> SessionStartResult:
    """Mint (or adopt) durable herdr identities for ``providers`` (fail-closed).

    Pure orchestration over the injected ``runner`` + ``env`` (no ambient I/O beyond
    ``register_workspace`` / ``read_anchor``). Raises :class:`HerdrSessionStartError`
    on any fail-closed condition (unknown provider, unconfigured binary, duplicate
    assigned name, a launch that yields no usable locator).
    """
    for provider in providers:
        if provider not in AGENT_PROVIDERS:
            raise HerdrSessionStartError(
                f"unknown provider {provider!r}; expected one of {sorted(AGENT_PROVIDERS)}"
            )
    binary = _resolve_binary_or_die(env)

    register_workspace(repo_root)
    anchor = read_anchor(repo_root)
    workspace_id = anchor.get("workspace_id") if isinstance(anchor, dict) else None
    workspace_id = _norm(workspace_id)
    if not workspace_id:
        raise HerdrSessionStartError(
            "workspace has no resolvable workspace_id after registration"
        )
    lane = _norm(lane_id)

    result = SessionStartResult(workspace_id=workspace_id, lane_id=lane or "default")
    rows = _list_rows(binary, runner or subprocess.run, timeout)
    for provider in providers:
        assigned_name = encode_assigned_name(workspace_id, provider, lane)
        result.slots.append(
            _prepare_slot(
                provider=provider,
                assigned_name=assigned_name,
                repo_root=repo_root,
                workspace_id=workspace_id,
                lane=result.lane_id,
                rows=rows,
                binary=binary,
                env=env,
                runner=runner or subprocess.run,
                timeout=timeout,
                dry_run=dry_run,
            )
        )
    return result


def _prepare_slot(
    *,
    provider: str,
    assigned_name: str,
    repo_root: Path,
    workspace_id: str,
    lane: str,
    rows: Sequence[Mapping[str, object]],
    binary: str,
    env: Mapping[str, str],
    runner: Runner,
    timeout: float,
    dry_run: bool,
) -> SlotResult:
    existing = _find_named_agent(rows, assigned_name)
    if len(existing) > 1:
        raise HerdrSessionStartError(
            f"{len(existing)} live agents already carry {assigned_name!r}; herdr names "
            "must be unique — refuse to launch / rename over a duplicate"
        )
    if len(existing) == 1:
        return SlotResult(
            provider=provider,
            assigned_name=assigned_name,
            outcome=SLOT_ADOPTED,
            locator=_agent_locator(existing[0]),
            detail="live agent already carries the durable name; adopted",
        )
    if dry_run:
        return SlotResult(
            provider=provider,
            assigned_name=assigned_name,
            outcome=SLOT_PLANNED,
            detail="would launch + rename (dry-run)",
        )
    # Launch the agent with the self-identity env injected, then mint its name.
    launch_env = dict(env)
    launch_env[MOZYO_WORKSPACE_ID_ENV] = workspace_id
    launch_env[MOZYO_AGENT_ROLE_ENV] = provider
    launch_env[MOZYO_LANE_ID_ENV] = lane
    started = _invoke(
        binary,
        ["agent", "start", "--cwd", str(repo_root), "--", provider],
        runner,
        timeout,
        env=launch_env,
    )
    locator = _parse_started_locator(started.stdout)
    if not locator or not valid_target(locator):
        raise HerdrSessionStartError(
            f"herdr agent start for {provider!r} returned no usable live locator; "
            "refuse to rename a blank handle"
        )
    _invoke(
        binary,
        ["agent", "rename", locator, assigned_name],
        runner,
        timeout,
        env=None,
    )
    return SlotResult(
        provider=provider,
        assigned_name=assigned_name,
        outcome=SLOT_LAUNCHED,
        locator=locator,
        detail="launched with self-identity env and renamed to the durable name",
    )


def _render_text(result: SessionStartResult) -> str:
    lines = [
        f"herdr session-start: workspace={result.workspace_id} lane={result.lane_id}"
    ]
    for slot in result.slots:
        lines.append(
            f"  - {slot.provider}: {slot.outcome} name={slot.assigned_name}"
            + (f" locator={slot.locator}" if slot.locator else "")
        )
    return "\n".join(lines)


def cmd_herdr_session_start(args: argparse.Namespace) -> int:
    """CLI entry: prepare durable herdr identities for the workspace's agents."""
    from mozyo_bridge.application.commands_common import repo_root_from_args

    repo_root = repo_root_from_args(args)
    agents = getattr(args, "agent", None) or [PROVIDER_CLAUDE, PROVIDER_CODEX]
    lane_id = getattr(args, "lane", None) or ""
    dry_run = bool(getattr(args, "dry_run", False))
    try:
        result = prepare_session(
            repo_root=repo_root,
            providers=list(agents),
            lane_id=lane_id,
            env=os.environ,
            dry_run=dry_run,
        )
    except HerdrSessionStartError as exc:
        die(f"herdr session-start failed: {exc}")
        raise AssertionError("unreachable")
    if getattr(args, "json", False):
        print(json.dumps(result.as_payload(), ensure_ascii=False, sort_keys=True))
    else:
        print(_render_text(result))
    return 0


__all__ = (
    "SLOT_ADOPTED",
    "SLOT_LAUNCHED",
    "SLOT_PLANNED",
    "HerdrSessionStartError",
    "SessionStartResult",
    "SlotResult",
    "cmd_herdr_session_start",
    "prepare_session",
)
