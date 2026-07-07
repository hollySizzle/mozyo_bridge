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
   it (no launch). A duplicated assigned name (more than one live agent) fails closed
   rather than corrupting identity;
5. otherwise launch the agent as a herdr-managed pane with the durable name applied
   **at start** and the self-identity vars injected via ``--env``.

Duplicate requested ``(provider, lane)`` slots fail closed **before any side effect**
(spec §5 slot-uniqueness) so the same durable name is never minted twice.

Empty base-pane reclaim (Redmine #13330)
-----------------------------------------
A herdr workspace is *born with a ``root_pane``* — an empty base shell (measured:
``workspace create`` returns ``result.root_pane.pane_id`` on a fresh ``pane_count: 1``
workspace). On a cold start the first ``agent start`` used to auto-create the
workspace implicitly, leaving that root pane as an unused, agent-less shell beside the
launched agent panes. To reclaim it deterministically, a pure cold start now:

1. classifies every requested slot (adopt vs launch) *before* launching;
2. if any slot must launch and none of the adopted agents pin an existing workspace,
   **explicitly** ``herdr workspace create --no-focus`` and captures the returned
   ``workspace_id`` + ``root_pane.pane_id``;
3. launches every slot with ``agent start --workspace <workspace_id>`` (so herdr never
   auto-creates a second workspace); and
4. after **every** launch succeeds, ``herdr pane close <root_pane_id>`` — closing only
   that exact captured handle.

Fail-closed guarantees: only the root pane *this run created* is ever a reclaim target
(never a scanned-for shell, so a user's own shell can't be mis-closed); a launch
failure raises before any reclaim (residue left, treated as an implementation
failure); a ``pane close`` failure is recorded non-fatally (the agents are already
live and an empty base pane is only cosmetic). All-adopt and launches into an
already-existing workspace create no new base pane, so they stay byte-invariant. The
tmux path (:mod:`mozyo_bridge.application.launch_command`) is untouched.

The command is explicit opt-in and is **not** coupled to the ``terminal_transport``
backend flag: you may prepare herdr identities without selecting the herdr transport,
and vice versa (documented in ``vibes/docs/specs/herdr-native-identity.md``). In pure
herdr operation you run both.

Live-measured launch contract (herdr 0.7.1, coordinator pre-smoke)
-----------------------------------------------------------------
The ``agent start`` shape is no longer a staged assumption — it was validated against
a running herdr 0.7.1::

    herdr agent start <NAME> [--cwd PATH] [--env KEY=VALUE]... [--no-focus] -- <argv...>

- ``<NAME>`` is a required positional and is applied directly at start (probe:
  ``result.agent.name == <NAME>``), so mozyo mints the durable ``mzb1_...`` name here
  and does **not** issue a separate ``agent rename``.
- the self-identity vars ride on repeated ``--env`` flags, **not** the client process
  env: the server-spawned agent does not inherit the launching client's environment
  (coordinator-measured), so ``MOZYO_WORKSPACE_ID`` / ``MOZYO_AGENT_ROLE`` /
  ``MOZYO_LANE_ID`` are passed as ``--env KEY=VALUE``.
- ``--no-focus`` avoids stealing the operator's focus.
- output is a single JSON object on stdout; the transient locator for rebind/read is
  ``result.agent.pane_id`` under a ``result.type == "agent_started"`` envelope
  (:func:`_parse_started_locator`, fail-closed).

Tests exercise the argv + JSON parsing through an injected subprocess ``runner`` (no
live herdr binary); the end-to-end live smoke stays the coordinator's post-review step.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.core.state.workspace_registry import (
    _is_linked_worktree,
    read_anchor,
    register_workspace,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (
    InvalidPermissionMode,
    resolve_claude_permission_mode,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    _agent_locator,
    _norm,
    derive_lane_workspace_token,
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
    """The aggregate outcome of a session-start run.

    ``workspace_id`` / ``lane_id`` are the *mozyo* identities (registry anchor +
    requested lane). The base-pane fields (Redmine #13330) record the empty herdr
    root pane this run created and reclaimed on a pure cold start:

    - ``herdr_workspace_id`` — the herdr *terminal* workspace the launched agents
      live in (the one this run created, or the single workspace its adopted
      agents already occupy). Blank when nothing was launched.
    - ``base_pane_id`` — the ``root_pane.pane_id`` of the workspace this run
      **created** (blank when no workspace was created: all-adopt, dry-run, or a
      launch into an already-existing workspace). Only this exact pane is ever a
      reclaim target — never a scanned-for shell (fail-closed against closing a
      user's own shell).
    - ``base_pane_reclaimed`` — True iff that created root pane was closed.
    - ``base_pane_detail`` — a non-fatal ``pane close`` failure detail, if any
      (a failed reclaim leaves harmless cosmetic residue, never a hard failure).
    """

    workspace_id: str
    lane_id: str
    slots: list = field(default_factory=list)
    herdr_workspace_id: str = ""
    base_pane_id: str = ""
    base_pane_reclaimed: bool = False
    base_pane_detail: str = ""

    def as_payload(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "slots": [slot.as_payload() for slot in self.slots],
            "herdr_workspace_id": self.herdr_workspace_id,
            "base_pane_id": self.base_pane_id,
            "base_pane_reclaimed": self.base_pane_reclaimed,
            "base_pane_detail": self.base_pane_detail,
        }


def herdr_workspace_segment(repo_root: Path, *, home: Optional[Path] = None) -> str:
    """The mzb1 ``workspace`` segment for ``repo_root`` (Redmine #13331, design j#73357).

    The single, read-only resolver every herdr identity site shares so mint-time
    (:func:`prepare_session`) and resolve-time (cross-workspace send, retire, projection,
    lane read-back) always agree:

    - a **linked git worktree** used as a sublane herdr workspace → the deterministic
      lane-scoped token (:func:`derive_lane_workspace_token`) from its **canonical** path.
      The discriminator is git topology (:func:`_is_linked_worktree`), **not** an absent
      anchor — an unregistered standalone repo also has no anchor. The worktree inherits
      the main checkout's registry identity (#13152), which is not a distinct per-lane
      identity, so the registry is left untouched here;
    - otherwise (**standalone / main checkout**) → the registry / anchor ``workspace_id``,
      read-only (no registration), byte-for-byte the prior behaviour. ``""`` when the
      standalone checkout has no resolvable anchor (the caller decides whether that is
      fatal — :func:`prepare_session` registers + fails closed; the resolve sites treat
      ``""`` as "not a distinct target workspace").
    """
    resolved = Path(repo_root).expanduser().resolve()
    if _is_linked_worktree(resolved):
        return derive_lane_workspace_token(str(resolved))
    anchor = read_anchor(resolved)
    return _norm(anchor.get("workspace_id")) if isinstance(anchor, dict) else ""


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
    """Read the live pane locator from a herdr ``agent start`` payload (fail-closed).

    Real herdr 0.7.1 output (coordinator-measured): a single JSON object

        {"id": "cli:agent:start",
         "result": {"agent": {"pane_id": "w1:p2", "name": "...", ...},
                    "argv": [...], "type": "agent_started"}}

    The transient locator is ``result.agent.pane_id``. Returns ``None`` (so the
    caller fails closed) when the payload is not JSON, ``result.type`` is not
    ``agent_started``, or the ``pane_id`` is missing / blank — never a blank handle.
    """
    if not isinstance(stdout, str):
        return None
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return None
    if _norm(result.get("type")) != "agent_started":
        return None
    agent = result.get("agent")
    if not isinstance(agent, Mapping):
        return None
    locator = _norm(agent.get("pane_id"))
    return locator or None


def _workspace_prefix(locator: str) -> str:
    """The herdr workspace id (``wN``) of a ``wN:pM`` locator (``""`` if unparseable).

    herdr terminal locators are ``<workspace>:<pane>`` (e.g. ``w2:p3``); the part
    before the first ``:`` is the workspace the pane lives in. Returns ``""`` for a
    blank / colonless / malformed handle so the caller fails closed rather than
    guessing a launch target.
    """
    loc = _norm(locator)
    if ":" not in loc:
        return ""
    prefix = loc.split(":", 1)[0]
    return prefix if valid_target(prefix) else ""


def _launch_target_from_adopted(adopted_locators: Sequence[str]) -> str:
    """The single herdr workspace shared by adopted agents (fail-closed on a split).

    When a run launches some slots while others adopt live agents, the launches must
    land in the workspace those adopted agents already occupy (so no *new* workspace
    — hence no empty base pane — is created). Returns that single ``wN`` prefix, or
    ``""`` when nothing was adopted. Raises :class:`HerdrSessionStartError` when the
    adopted agents span more than one workspace: refusing to guess which one the
    launches belong to (Redmine #13330 auditor ruling j#73225 mixed-case gate). With
    the real 2-provider set (claude + codex) a >1 split is structurally unreachable
    — one adopt + one launch is always a single prefix — but the guard keeps the
    decision fail-closed if the provider set ever grows.
    """
    prefixes = {p for p in (_workspace_prefix(loc) for loc in adopted_locators) if p}
    if len(prefixes) > 1:
        raise HerdrSessionStartError(
            f"adopted agents span multiple herdr workspaces {sorted(prefixes)!r}; "
            "refuse to guess which one new launches belong to"
        )
    return next(iter(prefixes)) if prefixes else ""


def _parse_workspace_created(stdout: object) -> Optional[tuple[str, str]]:
    """``(workspace_id, root_pane_id)`` from a herdr ``workspace create`` payload.

    Real herdr shape (coordinator-measured, #13330 probe)::

        {"result": {"type": "workspace_created",
                    "workspace": {"workspace_id": "w3", ...},
                    "root_pane": {"pane_id": "w3:p1", ...}, ...}}

    Every fresh workspace is born with exactly this ``root_pane`` — the empty base
    shell #13330 reclaims. Returns ``None`` (so the caller fails closed and reclaims
    nothing) when the payload is not JSON, not a ``workspace_created`` envelope, or
    either id is missing / blank / malformed — never a guessed pane handle.
    """
    if not isinstance(stdout, str):
        return None
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return None
    if _norm(result.get("type")) != "workspace_created":
        return None
    workspace = result.get("workspace")
    root_pane = result.get("root_pane")
    if not isinstance(workspace, Mapping) or not isinstance(root_pane, Mapping):
        return None
    workspace_id = _norm(workspace.get("workspace_id"))
    root_pane_id = _norm(root_pane.get("pane_id"))
    if not workspace_id or not valid_target(workspace_id):
        return None
    if not root_pane_id or not valid_target(root_pane_id):
        return None
    return workspace_id, root_pane_id


def _create_workspace(
    binary: str,
    repo_root: Path,
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
) -> tuple[str, str]:
    """Explicitly create a herdr workspace; return ``(workspace_id, root_pane_id)``.

    Making the workspace ourselves (rather than letting the first ``agent start``
    auto-create it) is what turns the empty base pane into a *known* handle we can
    reclaim by id — never one we scan for. ``--no-focus`` avoids stealing the
    operator's focus. Fails closed if the response is unparseable.
    """
    completed = _invoke(
        binary,
        ["workspace", "create", "--cwd", str(repo_root), "--no-focus"],
        runner,
        timeout,
        env=dict(env),
    )
    parsed = _parse_workspace_created(completed.stdout)
    if parsed is None:
        raise HerdrSessionStartError(
            "herdr workspace create returned no parseable workspace id / root pane "
            "(expected result.workspace.workspace_id + result.root_pane.pane_id in a "
            "workspace_created payload); refuse to guess a pane to reclaim"
        )
    return parsed


def _close_base_pane(
    binary: str,
    pane_id: str,
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
) -> tuple[bool, str]:
    """Reclaim the created base pane; **never hard-fail** (cosmetic residue only).

    Returns ``(True, "")`` on a clean close, else ``(False, <detail>)``. A failed
    reclaim only leaves the harmless empty base pane behind — the agent slots are
    already live — so it is recorded, not raised (Redmine #13330 ruling j#73225).
    """
    try:
        _invoke(binary, ["pane", "close", pane_id], runner, timeout, env=dict(env))
    except HerdrSessionStartError as exc:
        return False, _bounded_detail(str(exc)) or "herdr pane close failed"
    return True, ""


@dataclass(frozen=True)
class _SlotPlan:
    """A per-provider decision (adopt / launch / dry-run plan) made before any launch.

    Classifying every slot up front lets the run pick a single launch-target
    workspace (and decide whether to create+reclaim a base pane) before it starts
    launching, so ``agent start`` can pass an explicit ``--workspace``.
    """

    provider: str
    assigned_name: str
    kind: str  # "adopt" | "launch" | "planned"
    locator: str = ""  # adopted live locator (kind == "adopt"); else ""


def prepare_session(
    *,
    repo_root: Path,
    providers: Sequence[str],
    lane_id: str,
    env: Mapping[str, str],
    runner: Optional[Runner] = None,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
    dry_run: bool = False,
    claude_permission_mode_default: Optional[str] = None,
) -> SessionStartResult:
    """Mint (or adopt) durable herdr identities for ``providers`` (fail-closed).

    Pure orchestration over the injected ``runner`` + ``env`` (no ambient I/O beyond
    ``register_workspace`` / ``read_anchor``). Raises :class:`HerdrSessionStartError`
    on any fail-closed condition (unknown provider, unconfigured binary, duplicate
    assigned name, a launch that yields no usable locator).

    ``claude_permission_mode_default`` is the launch-context policy default for the
    managed Claude permission mode (Redmine #11925 / #13360): sublane lane creation
    passes ``auto`` so lane workers are reproducibly auto (tmux parity); the
    session-start / bare ``mozyo`` paths pass ``None`` so the historical bare
    ``claude`` launch never changes silently. The ``MOZYO_CLAUDE_PERMISSION_MODE``
    env override rail wins over the default either way (resolved from ``env``).
    """
    for provider in providers:
        if provider not in AGENT_PROVIDERS:
            raise HerdrSessionStartError(
                f"unknown provider {provider!r}; expected one of {sorted(AGENT_PROVIDERS)}"
            )
    # Reject a duplicate (provider, lane) slot BEFORE any side effect (spec §5
    # slot-uniqueness). Every requested provider shares this run's lane, so a
    # repeated provider is a repeated slot: it would mint the SAME
    # `mzb1_<ws>_<role>_<lane>` name twice (two launches / two renames) — the read
    # side then fails closed with `multiple_matches`, leaving the session unusable.
    # Fail-closed rejection (not silent de-dup) matches the spec wording, so the CLI
    # can keep its repeatable `--agent` flag.
    seen_slots: set = set()
    lane_norm = _norm(lane_id)
    for provider in providers:
        slot = (provider, lane_norm)
        if slot in seen_slots:
            raise HerdrSessionStartError(
                f"duplicate requested slot for provider {provider!r} in lane "
                f"{lane_norm or 'default'!r}; each (provider, lane) may be prepared "
                "once — remove the duplicate `--agent` argument"
            )
        seen_slots.add(slot)
    binary = _resolve_binary_or_die(env)

    # Redmine #13331 (design j#73357, Opt 1): the mzb1 `workspace` segment. A linked git
    # worktree used as a per-lane herdr workspace inherits the main checkout's registry
    # identity (#13152) and has no distinct per-lane workspace_id, so it is named by a
    # deterministic path-derived token (registry untouched). A standalone / main checkout
    # is registered and named by its registry workspace_id (byte-for-byte the prior path,
    # incl. the fail-closed-on-empty guard). Both use the single shared resolver so mint
    # here and resolve at send/retire/projection agree on the same canonical path.
    resolved_root = Path(repo_root).expanduser().resolve()
    if _is_linked_worktree(resolved_root):
        workspace_id = derive_lane_workspace_token(str(resolved_root))
    else:
        register_workspace(repo_root)
        anchor = read_anchor(repo_root)
        workspace_id = _norm(anchor.get("workspace_id")) if isinstance(anchor, dict) else ""
        if not workspace_id:
            raise HerdrSessionStartError(
                "workspace has no resolvable workspace_id after registration"
            )
    lane = _norm(lane_id)

    result = SessionStartResult(workspace_id=workspace_id, lane_id=lane or "default")
    runner = runner or subprocess.run
    rows = _list_rows(binary, runner, timeout)

    # Pass 1 — classify every slot (adopt / launch / dry-run plan) before launching,
    # failing closed on a duplicate live name. Classifying up front is what lets us
    # pick ONE launch-target workspace (and decide whether to create+reclaim a base
    # pane) before the first `agent start`.
    plans: list = []
    for provider in providers:
        assigned_name = encode_assigned_name(workspace_id, provider, lane)
        existing = _find_named_agent(rows, assigned_name)
        if len(existing) > 1:
            raise HerdrSessionStartError(
                f"{len(existing)} live agents already carry {assigned_name!r}; herdr "
                "names must be unique — refuse to launch / rename over a duplicate"
            )
        if len(existing) == 1:
            plans.append(
                _SlotPlan(provider, assigned_name, "adopt", _agent_locator(existing[0]))
            )
        elif dry_run:
            plans.append(_SlotPlan(provider, assigned_name, "planned"))
        else:
            plans.append(_SlotPlan(provider, assigned_name, "launch"))

    # Resolve the launch-target workspace (Redmine #13330). Nothing to launch (all
    # adopt / dry-run) means no workspace create and no reclaim — byte-invariant.
    # Launches into an already-adopted workspace reuse it (no new base pane). A pure
    # cold start (launches, no adopted workspace) creates the workspace explicitly so
    # its empty root pane is a known handle to reclaim, not one we scan for.
    launch_plans = [p for p in plans if p.kind == "launch"]
    target_workspace = ""
    if launch_plans:
        target_workspace = _launch_target_from_adopted(
            [p.locator for p in plans if p.kind == "adopt"]
        )
        if not target_workspace:
            target_workspace, base_pane_id = _create_workspace(
                binary, repo_root, runner, timeout, env
            )
            result.base_pane_id = base_pane_id
        result.herdr_workspace_id = target_workspace

    # Pass 2 — execute each slot's decision (adopt row, dry-run plan, or launch into
    # the resolved target workspace). A launch failure raises here, before reclaim.
    for plan in plans:
        result.slots.append(
            _execute_slot(
                plan,
                repo_root=repo_root,
                workspace_id=workspace_id,
                lane=result.lane_id,
                target_workspace=target_workspace,
                binary=binary,
                env=env,
                runner=runner,
                timeout=timeout,
                claude_permission_mode_default=claude_permission_mode_default,
            )
        )

    # Reclaim the empty base pane we created — only after EVERY launch succeeded
    # (a launch failure raised above, so reaching here means all agents are live and
    # the workspace is safe to keep with just its agent panes). Close only the exact
    # root pane id we captured; a close failure is non-fatal cosmetic residue.
    if result.base_pane_id:
        reclaimed, detail = _close_base_pane(
            binary, result.base_pane_id, runner, timeout, env
        )
        result.base_pane_reclaimed = reclaimed
        result.base_pane_detail = detail
    return result


def _execute_slot(
    plan: _SlotPlan,
    *,
    repo_root: Path,
    workspace_id: str,
    lane: str,
    target_workspace: str,
    binary: str,
    env: Mapping[str, str],
    runner: Runner,
    timeout: float,
    claude_permission_mode_default: Optional[str] = None,
) -> SlotResult:
    if plan.kind == "adopt":
        return SlotResult(
            provider=plan.provider,
            assigned_name=plan.assigned_name,
            outcome=SLOT_ADOPTED,
            locator=plan.locator,
            detail="live agent already carries the durable name; adopted",
        )
    if plan.kind == "planned":
        return SlotResult(
            provider=plan.provider,
            assigned_name=plan.assigned_name,
            outcome=SLOT_PLANNED,
            detail="would launch (dry-run)",
        )
    # Launch the agent with the durable name applied at start (herdr 0.7.1 real
    # syntax: `agent start <NAME> [--cwd] [--workspace ID] [--env K=V]... [--no-focus]
    # -- <argv>`). The NAME positional applies directly (probe: result.agent.name ==
    # NAME), so no separate `agent rename` is needed. `--workspace` pins placement into
    # the resolved target workspace (Redmine #13330) so herdr never auto-creates a new
    # workspace — the source of the empty base pane. The self-identity vars ride on
    # repeated `--env` flags, NOT the client process env — the server-spawned agent
    # does not inherit the client env (coordinator-measured). `--no-focus` avoids
    # stealing the operator's focus. The client env is still passed through for PATH etc.
    #
    # MOZYO_HERDR_BINARY (Redmine #13331 j#73312 scope addition): the launched agent is
    # itself a mozyo operator (a lane worker / gateway that runs its own `handoff send`),
    # and every herdr code path resolves the herdr binary ONLY from the trusted
    # environment (`_resolve_binary_or_die`, `herdr_transport._resolve_binary`) — never a
    # repo-local binary. The three self-identity vars were the only injected env, so a
    # launched agent knew *who* it was but not *how* to reach herdr, forcing an inline
    # `MOZYO_HERDR_BINARY=$(command -v herdr)` before every send (coordinator-measured,
    # j#73312 finding #1). Injecting the already-resolved binary here propagates the
    # trusted value the same way the identity vars are propagated: as an `--env` flag on
    # the server-spawned agent, from a value the launcher already resolved from ITS trusted
    # env (never widened to a repo-local path).
    launch_argv = [
        "agent",
        "start",
        plan.assigned_name,
        "--cwd",
        str(repo_root),
        "--workspace",
        target_workspace,
        "--env",
        f"{MOZYO_WORKSPACE_ID_ENV}={workspace_id}",
        "--env",
        f"{MOZYO_AGENT_ROLE_ENV}={plan.provider}",
        "--env",
        f"{MOZYO_LANE_ID_ENV}={lane}",
        "--env",
        f"{HERDR_BINARY_ENV}={binary}",
        "--no-focus",
        "--",
        plan.provider,
    ]
    # Reproducible permission mode for managed Claude agents (Redmine #11925 /
    # #13360): the tmux managed-pane chokepoint has always appended
    # `--permission-mode <mode>`; without the same suffix here every herdr lane
    # worker boots prompt-gated and stalls on its first gated command
    # (coordinator-measured, 2026-07-07: all four wave workers blocked). The pure
    # policy resolver keeps the precedence identical to tmux (env override >
    # launch-context default > none) and never renders a flag for Codex. An
    # invalid mode fails the launch closed instead of silently booting a
    # default-permission agent the operator did not intend.
    try:
        permission_mode = resolve_claude_permission_mode(
            plan.provider,
            policy_default=claude_permission_mode_default,
            env=env,
        )
    except InvalidPermissionMode as exc:
        raise HerdrSessionStartError(str(exc)) from exc
    if permission_mode:
        launch_argv.extend(["--permission-mode", permission_mode])
    started = _invoke(
        binary,
        launch_argv,
        runner,
        timeout,
        env=dict(env),
    )
    locator = _parse_started_locator(started.stdout)
    if not locator or not valid_target(locator):
        raise HerdrSessionStartError(
            f"herdr agent start for {plan.provider!r} returned no usable live locator "
            "(expected result.agent.pane_id in an agent_started payload); refuse to "
            "return a blank handle"
        )
    # Verify the launch actually landed in the requested workspace (Redmine #13330
    # review j#73231). Passing `--workspace` is what keeps herdr from auto-creating a
    # second workspace (with its own empty base pane); if the returned locator is in a
    # DIFFERENT workspace (herdr ignored the flag / spec drift), trusting it would let
    # us close our created root pane while an auto-created base pane survives elsewhere,
    # unseen — exactly the failure this US must prevent. Fail closed instead (before
    # any reclaim), so the mislocated launch is surfaced rather than papered over.
    landed = _workspace_prefix(locator)
    if landed != target_workspace:
        raise HerdrSessionStartError(
            f"herdr agent start for {plan.provider!r} landed in workspace "
            f"{landed or '<none>'!r} but --workspace {target_workspace!r} was requested; "
            "refuse to trust a mislocated launch (herdr may have auto-created another "
            "workspace with its own base pane)"
        )
    return SlotResult(
        provider=plan.provider,
        assigned_name=plan.assigned_name,
        outcome=SLOT_LAUNCHED,
        locator=locator,
        detail="launched with the durable name and self-identity env (--env) at start",
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
    if result.base_pane_id:
        state = (
            "reclaimed"
            if result.base_pane_reclaimed
            else f"reclaim-failed ({result.base_pane_detail})"
        )
        lines.append(f"base pane {result.base_pane_id}: {state}")
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
    "herdr_workspace_segment",
    "prepare_session",
)
