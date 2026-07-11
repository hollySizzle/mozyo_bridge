"""herdr session-start one-command — the durable-name write side (Redmine #13261).

`mozyo-bridge herdr session-start` is the opt-in helper that prepares a **pure herdr
session** for mozyo handoff routing. Nothing in the codebase ever *wrote* a durable
herdr name before this (the #13175 PoC did ``agent rename`` by hand); this command
mints them so the herdr-native target resolution (#13261 read side) has stable
identities to resolve against.

Flow (per requested provider agent, ``claude`` / ``codex``):

1. resolve the herdr binary from the **trusted environment** — the explicit
   ``MOZYO_HERDR_BINARY`` then an executable ``herdr`` on the trusted ``PATH``
   (Redmine #13496; absolute PATH components only, realpath / executable verified);
   unresolvable / ambiguous fails closed (never a repo-local or cwd binary);
2. ensure the workspace is registered (``register_workspace`` / anchor reuse) and take
   its ``workspace_id`` — the workspace_registry schema is unchanged (#11425);
3. mint the durable name ``encode_assigned_name(workspace_id, provider, lane)`` (#13247);
4. **idempotency + composite liveness:** if a *live* agent already carries that exact
   assigned name, *adopt* it (no launch). Liveness is a composite judgment, not a bare name
   match (Redmine #13518 j#75329): a host-restart shell residue (name survives, no detected
   agent) is classified :data:`SLOT_STALE` and surfaced read-only, never blind-adopted. A
   duplicated assigned name (more than one live agent) fails closed rather than corrupting;
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

Placement: dedicated sublane host workspace (Redmine #13380)
------------------------------------------------------------
The mzb1 identity model is #13377's (design j#73613): a lane's slots are
``mzb1_<project-ws>_<role>_<lane>`` and the workspace segment stays the project
identity. The herdr *placement* however splits (#13380, owner intent #13377
j#73654 "サブレーン専用ウィンドウ"): the default lane (coordinator pair) lives in
the project workspace while every lane slot lands in a single dedicated sublane
host workspace — a constant "project 1 + host 1", never scaling with lanes. The
join rule lives in :func:`_launch_target_for_lane`; the host is minted on demand
with an operator-readable label (:func:`_host_workspace_label`, cosmetic only)
and needs no retire-side disposal: herdr auto-closes a workspace with its last
pane (live-measured), so a lane-zero host vanishes by itself.

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
  (:func:`herdr_lane_topology._parse_started_agent`, fail-closed).

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
from typing import TYPE_CHECKING, Mapping, Optional, Sequence

if TYPE_CHECKING:
    from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
        AgentLaunchConfig,
    )

from mozyo_bridge.core.state.workspace_registry import (
    _is_linked_worktree,
    read_anchor,
    register_workspace,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (
    COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
    InvalidPermissionMode,
    resolve_claude_permission_mode,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    DEFAULT_LANE,
    _agent_locator,
    _norm,
    derive_lane_workspace_token,
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (
    SLOT_STALE as LIVENESS_STALE,
    classify_named_slot,
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
    resolve_herdr_binary,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    TerminalTransportError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (
    HerdrSessionStartError,
    _host_workspace_label,
    _lane_live_slot_tabs,
    _launch_target_for_lane,
    _parse_started_agent,
    _parse_tab_created,
    _parse_workspace_created,
    _tab_target_for_lane,
    _workspace_prefix,
    herdr_workspace_segment,
)
from mozyo_bridge.shared.errors import die

# Per-slot outcome tokens.
SLOT_ADOPTED = "adopted"
SLOT_LAUNCHED = "launched"
SLOT_PLANNED = "planned"
# A host-restart shell / name residue: surfaced read-only (#13518 j#75329; see herdr_slot_liveness).
SLOT_STALE = LIVENESS_STALE


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

    The tab fields (Redmine #13411) are the lane=tab analogue: a non-default lane
    lands in its OWN dedicated herdr tab inside the sublane host workspace, its
    gateway + worker split inside it. The default lane never uses a tab, so these
    stay blank for it (byte-invariant coordinator path):

    - ``herdr_tab_id`` — the herdr tab the launched lane agents live in (the one
      this run created, or the tab its adopted slots already occupy). Blank for
      the default lane / all-adopt / nothing launched.
    - ``tab_pane_id`` — the ``root_pane.pane_id`` of the tab this run **created**
      (blank when no tab was created: default lane, all-adopt, or a heal that
      rejoined an existing tab). Only this exact pane is ever a reclaim target.
    - ``tab_pane_reclaimed`` — True iff that created tab root pane was closed.
    - ``tab_pane_detail`` — a non-fatal tab root ``pane close`` failure detail.
    """

    workspace_id: str
    lane_id: str
    slots: list = field(default_factory=list)
    herdr_workspace_id: str = ""
    base_pane_id: str = ""
    base_pane_reclaimed: bool = False
    base_pane_detail: str = ""
    herdr_tab_id: str = ""
    tab_pane_id: str = ""
    tab_pane_reclaimed: bool = False
    tab_pane_detail: str = ""

    def as_payload(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "slots": [slot.as_payload() for slot in self.slots],
            "herdr_workspace_id": self.herdr_workspace_id,
            "base_pane_id": self.base_pane_id,
            "base_pane_reclaimed": self.base_pane_reclaimed,
            "base_pane_detail": self.base_pane_detail,
            "herdr_tab_id": self.herdr_tab_id,
            "tab_pane_id": self.tab_pane_id,
            "tab_pane_reclaimed": self.tab_pane_reclaimed,
            "tab_pane_detail": self.tab_pane_detail,
        }


def _resolve_binary_or_die(env: Mapping[str, str]) -> str:
    """The absolute herdr binary this launch injects, via the shared resolver.

    Shares the single :func:`resolve_herdr_binary` trusted-environment order
    (``MOZYO_HERDR_BINARY`` → trusted-PATH ``herdr``, realpath / executable
    verified) so a launch never resolves a different binary than the send / read
    paths (Redmine #13496). The resolved absolute path is what rides on the
    launched agent's ``--env MOZYO_HERDR_BINARY=<path>`` (see :func:`_execute_slot`).
    A fail-closed resolution is re-raised as :class:`HerdrSessionStartError` so the
    session-start caller keeps its single error type.
    """
    try:
        return resolve_herdr_binary(env).path
    except TerminalTransportError as exc:
        raise HerdrSessionStartError(str(exc)) from exc


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


def _lane_id_from_metadata(resolved_root: Path) -> str:
    """The recorded lane id for a lane worktree (``""`` when unrecorded).

    Shared project workspace model (Redmine #13377): a lane worktree's slots are
    ``mzb1_<project-ws>_<role>_<lane>``, so a relaunch from the worktree must
    recover the SAME lane segment ``sublane create`` launched with. The lane
    metadata record — keyed on the worktree's stable per-path token — carries it
    (``lane_id``, falling back to ``lane_label`` for a record written before the
    column existed). Read-only and fail-open to ``""`` (the caller fails closed:
    a lane slot is never minted with a guessed lane).
    """
    from mozyo_bridge.core.state.lane_metadata import load_lane_records

    token = derive_lane_workspace_token(str(resolved_root))
    record = load_lane_records().get(token)
    if record is None:
        return ""
    return _norm(getattr(record, "lane_id", "")) or _norm(
        getattr(record, "lane_label", "")
    )


def _create_workspace(
    binary: str,
    repo_root: Path,
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
    label: str = "",
) -> tuple[str, str]:
    """Explicitly create a herdr workspace; return ``(workspace_id, root_pane_id)``.

    Making the workspace ourselves (rather than letting the first ``agent start``
    auto-create it) is what turns the empty base pane into a *known* handle we can
    reclaim by id — never one we scan for. ``--no-focus`` avoids stealing the
    operator's focus. ``label`` (Redmine #13380) names a minted sublane host
    workspace for the operator — cosmetic only, never a join key. Fails closed if
    the response is unparseable.
    """
    argv = ["workspace", "create", "--cwd", str(repo_root)]
    if label:
        argv.extend(["--label", label])
    argv.append("--no-focus")
    completed = _invoke(
        binary,
        argv,
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


def _create_tab(
    binary: str,
    workspace_id: str,
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
    label: str = "",
) -> tuple[str, str]:
    """Explicitly create a herdr tab in ``workspace_id``; return ``(tab_id, root_pane_id)``.

    Lane=tab subdivision (Redmine #13411): a non-default lane gets its OWN tab in
    the sublane host workspace, its gateway + worker placed as a split pair inside
    it. Minting the tab ourselves turns its empty root pane into a *known* handle
    to reclaim by id (the tab analogue of the #13330 workspace base pane), never
    one we scan for. ``--label`` (the lane label) is cosmetic and operator-readable
    only — every join decision keys on the live ``tab_id``, never the label.
    ``--no-focus`` avoids stealing the operator's focus. Fails closed if the
    response is unparseable.
    """
    argv = ["tab", "create", "--workspace", workspace_id]
    if label:
        argv.extend(["--label", label])
    argv.append("--no-focus")
    completed = _invoke(binary, argv, runner, timeout, env=dict(env))
    parsed = _parse_tab_created(completed.stdout)
    if parsed is None:
        raise HerdrSessionStartError(
            "herdr tab create returned no parseable tab id / root pane "
            "(expected result.tab.tab_id + result.root_pane.pane_id in a "
            "tab_created payload); refuse to guess a pane to reclaim"
        )
    return parsed


def _close_base_pane(
    binary: str,
    pane_id: str,
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
) -> tuple[bool, str]:
    """Reclaim a created root pane; **never hard-fail** (cosmetic residue only).

    Used for both the #13330 workspace base pane and the #13411 lane tab root
    pane. Returns ``(True, "")`` on a clean close, else ``(False, <detail>)``. A
    failed reclaim only leaves the harmless empty root pane behind — the agent
    slots are already live — so it is recorded, not raised (Redmine #13330 ruling
    j#73225).
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
    kind: str  # "adopt" | "launch" | "planned" | "stale"
    locator: str = ""  # adopted live locator (kind == "adopt") / stale residue pane (kind == "stale"); else ""


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
    agent_launch: "Optional[AgentLaunchConfig]" = None,
) -> SessionStartResult:
    """Mint (or adopt) durable herdr identities for ``providers`` (fail-closed).

    Pure orchestration over the injected ``runner`` + ``env`` (no ambient I/O beyond
    ``register_workspace`` / ``read_anchor``). Raises :class:`HerdrSessionStartError`
    on any fail-closed condition (unknown provider, unconfigured binary, duplicate
    assigned name, a launch that yields no usable locator).

    ``agent_launch`` (Redmine #13425) is the repo-local launch-argv override the launch
    site resolved from ``.mozyo-bridge/config.yaml``. When provided, each launched slot's
    ``-- {provider}`` argv is extended with
    ``agent_launch.resolve_launch_argv(provider, lane_class)`` — the config's per-agent x
    lane-class tokens (model, reasoning-effort flag, …) appended verbatim (mozyo hardcodes
    no provider flag spec). ``lane_class`` is derived from the resolved lane: ``default``
    for the coordinator pair (no-lane session), ``sublane`` for a lane worker / gateway.
    ``None`` (the default) appends nothing — byte-for-byte the pre-#13425 launch, so the
    ``sublane_claude_model`` regression fix is opt-in on the launch site passing a config.

    ``claude_permission_mode_default`` is the launch-context policy default for the
    managed Claude permission mode (Redmine #11925 / #13360 / #13397): sublane lane
    creation passes ``auto`` so lane workers are reproducibly auto (tmux parity), and
    the bare ``mozyo`` coordinator-pair launch (``herdr_launch_command``) also passes
    ``auto`` so the coordinator Claude has the same headless-capable posture as its lane
    workers (Redmine #13397 finding 3 — the pre-#13397 flagless coordinator booted
    prompt-gated in an external project). A caller that passes ``None`` still gets the
    historical flagless bare ``claude`` launch. The ``MOZYO_CLAUDE_PERMISSION_MODE``
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
    # Resolve (and validate) the Claude permission policy BEFORE any side effect
    # (review j#73404): the lane chokepoint requests (codex, claude), so a
    # validation that only fires inside the claude slot's launch would leave the
    # codex gateway already started — a partial lane — when the env override is
    # invalid. Resolving once up front fails closed with zero workspace create /
    # agent start, and `_execute_slot` receives the resolved mode verbatim.
    claude_permission_mode: Optional[str] = None
    if "claude" in providers:
        try:
            claude_permission_mode = resolve_claude_permission_mode(
                "claude", policy_default=claude_permission_mode_default, env=env
            )
        except InvalidPermissionMode as exc:
            raise HerdrSessionStartError(str(exc)) from exc
    binary = _resolve_binary_or_die(env)

    # Redmine #13377 (design j#73613, Opt3 — shared project workspace): the mzb1
    # `workspace` segment. A linked git worktree (a sublane lane checkout) inherits the
    # main checkout's registry identity (#13152), and its slots are launched INTO the
    # project workspace as `mzb1_<project-ws>_<role>_<lane>` — the lane segment, not a
    # per-lane workspace, is the discriminant (supersedes the #13331 j#73357 `wt_<hash>`
    # per-lane workspace; that token survives only as the legacy/compat + metadata key).
    # A standalone / main checkout is registered and named by its registry workspace_id
    # (byte-for-byte the prior path, incl. the fail-closed-on-empty guard). Both use the
    # single shared resolver so mint here and resolve at send/retire/projection agree.
    resolved_root = Path(repo_root).expanduser().resolve()
    lane = _norm(lane_id)
    if _is_linked_worktree(resolved_root):
        workspace_id = herdr_workspace_segment(resolved_root)
        if not workspace_id:
            raise HerdrSessionStartError(
                "linked worktree's main checkout has no registered workspace "
                "identity to inherit; run `mozyo-bridge workspace register` from "
                "the main checkout first (Redmine #13152 / #13377)"
            )
        if not lane:
            # A lane worktree's slots carry a non-default lane segment. Recover the
            # recorded lane rather than minting the project workspace's DEFAULT
            # slots (those are the coordinator pair) from a lane checkout.
            lane = _lane_id_from_metadata(resolved_root)
            if not lane:
                raise HerdrSessionStartError(
                    "a linked worktree lane requires an explicit lane id (its slots "
                    "are `mzb1_<project-ws>_<role>_<lane>`); pass --lane or create "
                    "the lane via `sublane create` so its lane metadata record "
                    "carries the lane id (Redmine #13377)"
                )
    else:
        register_workspace(repo_root)
        anchor = read_anchor(repo_root)
        workspace_id = _norm(anchor.get("workspace_id")) if isinstance(anchor, dict) else ""
        if not workspace_id:
            raise HerdrSessionStartError(
                "workspace has no resolvable workspace_id after registration"
            )

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
            # Composite liveness (Redmine #13518 j#75329): a host-restart shell residue (name
            # matches, no detected agent) is classified stale and surfaced, never blind-adopted.
            kind = "stale" if classify_named_slot(existing[0]) == LIVENESS_STALE else "adopt"
            plans.append(
                _SlotPlan(provider, assigned_name, kind, _agent_locator(existing[0]))
            )
        elif dry_run:
            plans.append(_SlotPlan(provider, assigned_name, "planned"))
        else:
            plans.append(_SlotPlan(provider, assigned_name, "launch"))

    # Resolve the launch-target workspace (Redmine #13330 / #13377 / #13380). Nothing
    # to launch (all adopt / dry-run) means no workspace create and no reclaim —
    # byte-invariant. Placement is lane-aware (#13380 dedicated sublane host): a
    # lane's own live/adopted slots pin the target first (a heal never splits a
    # pair); otherwise a lane slot joins the sublane host workspace the other lane
    # slots occupy (never the coordinator's), and the default lane joins only its
    # own pins — one mozyo workspace thus occupies a constant "project 1 + host 1"
    # herdr workspaces. When nothing pins a target the workspace is created
    # explicitly (labelled for a lane slot) so its empty root pane is a known
    # handle to reclaim, not one we scan for.
    launch_plans = [p for p in plans if p.kind == "launch"]
    target_workspace = ""
    if launch_plans:
        target_workspace = _launch_target_for_lane(
            rows,
            workspace_id,
            result.lane_id,
            [p.locator for p in plans if p.kind == "adopt"],
        )
        if not target_workspace:
            target_workspace, base_pane_id = _create_workspace(
                binary,
                repo_root,
                runner,
                timeout,
                env,
                label=(
                    _host_workspace_label(resolved_root)
                    if result.lane_id != DEFAULT_LANE
                    else ""
                ),
            )
            result.base_pane_id = base_pane_id
        result.herdr_workspace_id = target_workspace

    # Resolve the launch-target tab within the host workspace (Redmine #13411,
    # lane=tab). Only a non-default lane subdivides: its gateway + worker live in
    # ONE dedicated tab, so a host with N lanes shows N tabs instead of 2N loose
    # panes. The lane's own live/adopted slots pin their tab (a heal rejoins the
    # SAME tab). When nothing pins a tab, mint one explicitly ONLY for a FRESH lane
    # (no own live/adopted slots) — labelled with the lane key (cosmetic) so its
    # empty root pane is a known handle to reclaim. A heal of a legacy pre-#13411
    # lane whose live slots are LOOSE panes (own slots present, no tab pinned)
    # launches loose too, keeping the pair together (it migrates to a tab on a full
    # relaunch, the #13380 cohabiting precedent). The default lane never uses a tab,
    # so the coordinator path stays byte-invariant.
    #
    # The fresh-vs-loose decision keys on the lane's WHOLE live inventory in the
    # target workspace (`_lane_live_slot_tabs`), NOT this run's requested `plans`
    # (review j#74433 finding 1): a single-provider heal requests only one provider,
    # so the lane's OTHER live slot is in the inventory but never in `plans` —
    # counting requested adopts alone would mint a fresh tab for a live loose sibling
    # (splitting the pair).
    target_tab = ""
    lane_slot_tabs: list = []
    if launch_plans and result.lane_id != DEFAULT_LANE:
        lane_slot_tabs = _lane_live_slot_tabs(
            rows, workspace_id, target_workspace, result.lane_id
        )
        target_tab = _tab_target_for_lane(
            rows, workspace_id, target_workspace, result.lane_id
        )
        if not target_tab and not lane_slot_tabs:
            target_tab, tab_pane_id = _create_tab(
                binary, target_workspace, runner, timeout, env, label=result.lane_id
            )
            result.tab_pane_id = tab_pane_id
        result.herdr_tab_id = target_tab

    # Config-driven launch argv (Redmine #13425): the lane_class is `default` for the
    # coordinator pair (no-lane session) and `sublane` for a lane worker / gateway. The
    # per-slot argv comes from the single-source resolver; `None` config yields `[]`
    # everywhere, so an unconfigured launch is byte-for-byte the pre-#13425 command.
    lane_class = "default" if result.lane_id == DEFAULT_LANE else "sublane"

    # Split placement inside the lane tab (Redmine #13411): the first slot to land
    # in a tab occupies it; every subsequent slot — the second of a fresh pair, or a
    # healing launch beside an already-live sibling — is placed with `--split right`.
    # The tab starts occupied by this lane's live slots ALREADY IN target_tab, read
    # from the whole inventory (review j#74433 finding 1) — not just this run's
    # requested adopts — so a single-provider heal beside a live tabbed sibling still
    # splits. A freshly minted tab has no such slots, so its first launch does not
    # split and its second does.
    tab_occupancy = (
        sum(1 for tab in lane_slot_tabs if tab == target_tab) if target_tab else 0
    )

    # Pass 2 — execute each slot's decision (adopt row, dry-run plan, or launch into
    # the resolved target workspace/tab). A launch failure raises here, before reclaim.
    for plan in plans:
        launch_argv_extra = (
            agent_launch.resolve_launch_argv(plan.provider, lane_class)
            if agent_launch is not None
            else []
        )
        split_tab = bool(target_tab) and plan.kind == "launch" and tab_occupancy > 0
        result.slots.append(
            _execute_slot(
                plan,
                repo_root=repo_root,
                workspace_id=workspace_id,
                lane=result.lane_id,
                target_workspace=target_workspace,
                target_tab=target_tab,
                split_tab=split_tab,
                binary=binary,
                env=env,
                runner=runner,
                timeout=timeout,
                claude_permission_mode=claude_permission_mode,
                launch_argv_extra=launch_argv_extra,
            )
        )
        if plan.kind == "launch" and target_tab:
            tab_occupancy += 1

    # Reclaim the empty root panes we created — only after EVERY launch succeeded
    # (a launch failure raised above, so reaching here means all agents are live and
    # the workspace/tab is safe to keep with just its agent panes). Close only the
    # exact root pane ids we captured; a close failure is non-fatal cosmetic residue.
    # The workspace base pane (#13330) and the lane tab root pane (#13411) are
    # distinct handles — reclaim each independently, never one guessed for the other.
    if result.base_pane_id:
        reclaimed, detail = _close_base_pane(
            binary, result.base_pane_id, runner, timeout, env
        )
        result.base_pane_reclaimed = reclaimed
        result.base_pane_detail = detail
    if result.tab_pane_id:
        reclaimed, detail = _close_base_pane(
            binary, result.tab_pane_id, runner, timeout, env
        )
        result.tab_pane_reclaimed = reclaimed
        result.tab_pane_detail = detail
    return result


def _execute_slot(
    plan: _SlotPlan,
    *,
    repo_root: Path,
    workspace_id: str,
    lane: str,
    target_workspace: str,
    target_tab: str = "",
    split_tab: bool = False,
    binary: str,
    env: Mapping[str, str],
    runner: Runner,
    timeout: float,
    claude_permission_mode: Optional[str] = None,
    launch_argv_extra: Sequence[str] = (),
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
    if plan.kind == "stale":
        # A host-restart shell-residue slot (Redmine #13518 j#75329): surfaced read-only with
        # its residue locator so an owner-approved recovery (j#75331) can close that exact pane
        # and relaunch the same slot — this run performs no destructive side effect.
        return SlotResult(
            provider=plan.provider,
            assigned_name=plan.assigned_name,
            outcome=SLOT_STALE,
            locator=plan.locator,
            detail=(
                "durable name held by a shell-residue pane with no live agent; requires an "
                "owner-approved close + same-slot relaunch (dirty worktree preserved)"
            ),
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
    # Lane=tab placement (Redmine #13411): a non-default lane's gateway + worker
    # land in ONE dedicated herdr tab inside the host workspace. `--tab` pins the
    # tab the same way `--workspace` pins the workspace; `--split right` places the
    # second slot beside the first (or a healing launch beside its live sibling) so
    # the pair shares the tab. Inserted right after `--workspace <ws>`; the default
    # lane passes no `target_tab`, so its argv is byte-for-byte the pre-#13411 shape.
    if target_tab:
        insert_at = launch_argv.index("--workspace") + 2
        tab_flags = ["--tab", target_tab]
        if split_tab:
            tab_flags.extend(["--split", "right"])
        launch_argv[insert_at:insert_at] = tab_flags
    # Reproducible permission mode for managed Claude agents (Redmine #11925 /
    # #13360): the tmux managed-pane chokepoint has always appended
    # `--permission-mode <mode>`; without the same suffix here every herdr lane
    # worker boots prompt-gated and stalls on its first gated command
    # (coordinator-measured, 2026-07-07: all four wave workers blocked). The mode
    # arrives pre-resolved (and pre-validated) from `prepare_session` — resolving
    # here, mid-launch-sequence, is exactly what left a partial lane on an invalid
    # env override (review j#73404). Codex never gets the flag.
    if plan.provider == "claude" and claude_permission_mode:
        launch_argv.extend(["--permission-mode", claude_permission_mode])
    # Config-driven launch argv (Redmine #13425): appended AFTER the mozyo-managed
    # `--permission-mode` flag (answer j#73949 Q4 render order) so the managed posture
    # keeps its position; the config schema fail-closes on a token that re-specifies a
    # managed flag, so a config value can never override it. herdr passes each token as a
    # distinct `agent start ... -- {provider}` argv element (no shell), so the tokens are
    # extended verbatim — no quoting needed on this list surface.
    if launch_argv_extra:
        launch_argv.extend(launch_argv_extra)
    started = _invoke(
        binary,
        launch_argv,
        runner,
        timeout,
        env=dict(env),
    )
    started_agent = _parse_started_agent(started.stdout)
    if started_agent is None:
        raise HerdrSessionStartError(
            f"herdr agent start for {plan.provider!r} returned no usable live locator "
            "(expected result.agent.pane_id in an agent_started payload); refuse to "
            "return a blank handle"
        )
    locator, landed_tab = started_agent
    if not valid_target(locator):
        raise HerdrSessionStartError(
            f"herdr agent start for {plan.provider!r} returned an invalid live locator "
            f"{locator!r}; refuse to return a malformed handle"
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
    # Verify the launch actually landed in the requested TAB (Redmine #13411 review
    # j#74434 finding 2) — the tab-axis analogue of the workspace guard above. When
    # a lane tab is requested (`--tab`, non-default lane), the `agent_started`
    # envelope returns the landed `tab_id` (live probe #13411 j#74434); if herdr
    # ignored / misplaced `--tab` and landed in a DIFFERENT tab of the same
    # workspace, trusting it would leave the pair split and let us reclaim this run's
    # tab root pane against a mislocated launch. A missing tab id is equally
    # unverifiable, so both fail closed before any reclaim. The default lane passes
    # no `target_tab`, so this guard is skipped and its behaviour is byte-invariant.
    if target_tab and landed_tab != target_tab:
        raise HerdrSessionStartError(
            f"herdr agent start for {plan.provider!r} landed in tab "
            f"{landed_tab or '<none>'!r} but --tab {target_tab!r} was requested; "
            "refuse to trust a mislocated launch (the gateway/worker pair must "
            "share one dedicated lane tab)"
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
    if result.herdr_tab_id:
        lines[0] += f" tab={result.herdr_tab_id}"
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
    if result.tab_pane_id:
        state = (
            "reclaimed"
            if result.tab_pane_reclaimed
            else f"reclaim-failed ({result.tab_pane_detail})"
        )
        lines.append(f"tab root pane {result.tab_pane_id}: {state}")
    return "\n".join(lines)


def cmd_herdr_session_start(args: argparse.Namespace) -> int:
    """CLI entry: prepare durable herdr identities for the workspace's agents."""
    from mozyo_bridge.application.commands_common import repo_root_from_args

    from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config
    from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
        RepoLocalConfigError,
    )

    repo_root = repo_root_from_args(args)
    agents = getattr(args, "agent", None) or [PROVIDER_CLAUDE, PROVIDER_CODEX]
    lane_id = getattr(args, "lane", None) or ""
    dry_run = bool(getattr(args, "dry_run", False))
    # Config-driven launch argv (Redmine #13425): resolved from the repo the command runs
    # in. lane_class is derived inside `prepare_session` from the resolved lane.
    try:
        agent_launch = load_repo_local_config(repo_root).agent_launch
    except RepoLocalConfigError as exc:
        die(f"herdr session-start failed: invalid agent_launch config: {exc}")
        raise AssertionError("unreachable")
    try:
        result = prepare_session(
            repo_root=repo_root,
            providers=list(agents),
            lane_id=lane_id,
            env=os.environ,
            dry_run=dry_run,
            claude_permission_mode_default=COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
            agent_launch=agent_launch,
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
    "SLOT_STALE",
    "HerdrSessionStartError",
    "SessionStartResult",
    "SlotResult",
    "cmd_herdr_session_start",
    "herdr_workspace_segment",
    "prepare_session",
)
