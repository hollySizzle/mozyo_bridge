"""Runtime transport backend wiring for the handoff send path (Redmine #13253 / #13261).

The single injection point that switches ``orchestrate_handoff``'s send/capture
primitives between the tmux runtime (default) and the opt-in **herdr** backend,
**without changing** ``orchestrate_handoff``'s body. It is a thin application-layer
seam kept out of the already-oversized ``application/commands.py`` (module-health
gate) so the handoff command module does not grow.

Two pieces:

- :func:`resolve_handoff_transport_binding` reads the repo-local
  ``terminal_transport`` selection once and returns the
  :class:`~...transport_binding.TransportBinding` (herdr) or ``None`` (tmux
  default / absent / broken config). This is the *only* place the selection is
  read on the send path.
- :func:`bind_runtime_transport` decorates the handoff entry: for the herdr
  backend it swaps the ``commands`` module's ``run_tmux`` / ``capture_pane``
  globals for the tmux-shaped herdr shim for the duration of the send and restores
  them in a ``finally``; for the tmux default it installs nothing, so the send is
  byte-for-byte the current behaviour and any test-patched ``commands.run_tmux``
  stays in force (the #12932 monkeypatch seam is untouched).

herdr-native target resolution (Redmine #13261)
-----------------------------------------------
For a **pure herdr session** (no tmux server / ``TMUX`` unset / isolated socket) the
#13253 approach — deriving the target's durable herdr name from a tmux **target
pane** (``project_preflight_target(pane_info(%N))``) — has no pane to read. #13261
replaces it: the target is resolved against the **live herdr inventory**
(``agent list`` decode) scoped by the **launch-time sender identity** env
(``MOZYO_WORKSPACE_ID`` / ``MOZYO_AGENT_ROLE`` / ``MOZYO_LANE_ID``). Sender env is the
workspace scope + coordinator-binding context only — never the target's authority
(auditor answer j#72519). See ``vibes/docs/specs/herdr-native-identity.md``.

The rail still hands the shim a tmux ``%N`` target (``orchestrate_handoff`` resolves
it), but under herdr the translator's ``resolve_assigned_name`` **ignores** that
handle and resolves the receiver label against the inventory instead; the resulting
assigned name is then re-bound against a fresh snapshot (existing translator path).

Fail-closed (Redmine #13253 j#72318 / #13261): an absent / broken config is "no
herdr selection" and resolves to the tmux default; a herdr selection whose trusted-
environment binary is unconfigured / unresolvable, or whose sender identity is
missing / mismatched against the repo anchor, or whose receiver does not resolve to a
single live agent, surfaces as a clean ``die`` — never a silent downgrade to tmux and
never a send to a guessed target. Roll-back is a one-line
``terminal_transport.backend`` change plus a process restart: this resolver reads the
selection fresh per process and holds no state.
"""

from __future__ import annotations

import argparse
import functools
import os
from pathlib import Path
from typing import Any, Callable, Optional

from mozyo_bridge.application.commands_common import repo_root_from_args
from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config
from mozyo_bridge.core.state.workspace_registry import read_anchor
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.main_lane_guard_gate import (
    resolve_coordinator_provider,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
    RepoLocalConfigError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.transport_binding import (
    TransportBinding,
    TransportBindingError,
    resolve_runtime_transport_binding,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (
    HerdrAgentDiscoveryPort,
    resolve_herdr_target,
    resolve_sender_identity,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
    TerminalTransportError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_rail import (
    HerdrTurnStartRail,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_discovery import (
    resolve_agent_lister,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_turn_start import (
    resolve_turn_start_rail,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (
    capture_pane as _tmux_capture_pane,
    run_tmux as _tmux_run_tmux,
)
from mozyo_bridge.shared.errors import die


def _herdr_native_assigned_name(
    *,
    receiver: str,
    repo_root: str,
    coordinator_provider: str,
    lister: HerdrAgentDiscoveryPort,
) -> str:
    """Resolve the target receiver's live herdr assigned name (fail-closed, #13261).

    A lazy *fallback* resolver for the translator: since increment 2 resolves the
    target herdr-natively at the orchestrate entry and hands the rail the live locator
    directly (``valid_target`` passes through unchanged), this is only reached if a
    non-herdr-valid target ever survives to the shim. It resolves the sender identity
    (env + anchor) and the receiver against the live inventory scoped to the sender's
    workspace + provider role; any failure (un-attested sender, unknown receiver,
    coordinator binding unresolved, no / multiple match, missing locator) raises
    :class:`TransportBindingError` before any send — never a guessed target.
    """
    try:
        anchor = read_anchor(Path(repo_root))
    except (OSError, ValueError):
        anchor = None
    anchor_ws = anchor.get("workspace_id") if isinstance(anchor, dict) else None
    sender_res = resolve_sender_identity(os.environ, anchor_workspace_id=anchor_ws)
    if not sender_res.ok or sender_res.identity is None:
        raise TransportBindingError(
            "herdr sender identity is not attested "
            f"(reason={sender_res.reason}): {sender_res.detail}"
        )
    rows = lister.list_agent_rows()
    resolution = resolve_herdr_target(
        receiver, sender_res.identity, rows, coordinator_provider=coordinator_provider
    )
    if resolution.is_fail:
        raise TransportBindingError(
            f"herdr target resolution failed for receiver {receiver!r} in workspace "
            f"{sender_res.identity.workspace_id!r} (reason={resolution.reason}): "
            f"{resolution.detail}"
        )
    return resolution.assigned_name


def _resolve_herdr_binding(
    args: argparse.Namespace, config
) -> TransportBinding:
    """Resolve the herdr :class:`TransportBinding` for an already-herdr ``config``.

    Extracted so the config is read once and shared with the turn-start rail
    resolution (Redmine #13255, auditor j#72602 decision 6: reuse the resolution,
    do not add a second config read). Fail-closed ``die`` when the binary is
    unconfigured / unresolvable (never a silent tmux fallback).
    """
    repo_root = repo_root_from_args(args)
    receiver = getattr(args, "to", None) or ""
    coordinator_provider = resolve_coordinator_provider(repo_root)
    try:
        lister = resolve_agent_lister(config)
        if lister is None:  # defensive: herdr_enabled implies non-None
            die("herdr backend selected but no agent lister could be resolved")
            raise AssertionError("unreachable")
        resolver = functools.partial(
            _herdr_native_assigned_name,
            receiver=receiver,
            repo_root=repo_root,
            coordinator_provider=coordinator_provider,
            lister=lister,
        )
        return resolve_runtime_transport_binding(
            config,
            tmux_run_tmux=_tmux_run_tmux,
            tmux_capture_pane=_tmux_capture_pane,
            # The rail's tmux target is ignored by the herdr-native resolver, so the
            # translator's ``resolve_assigned_name`` accepts (and drops) it.
            resolve_assigned_name=lambda _target: resolver(),
            list_agents=lister.list_agent_rows,
        )
    except TerminalTransportError as exc:
        die(f"terminal transport backend 'herdr' is selected but unavailable: {exc}")
        raise AssertionError("unreachable")


def resolve_handoff_transport_binding(
    args: argparse.Namespace,
) -> Optional[TransportBinding]:
    """Resolve the transport binding for this send, or ``None`` for the tmux default.

    Returns ``None`` when the tmux backend is in effect (the default, an absent
    ``terminal_transport`` block, or a broken / unreadable config) so the caller
    installs nothing; returns a herdr :class:`TransportBinding` when the herdr
    backend is selected and its trusted-environment binary resolves.

    For the herdr backend (Redmine #13261) the binding is handed a herdr-native
    ``resolve_assigned_name`` resolver: it resolves the ``--to`` receiver against the
    live herdr inventory scoped to the **launch-time sender identity** (env +
    anchor), not a tmux target pane. Fail-closed ``die`` when the binary is
    unconfigured / unresolvable, the sender identity is un-attested, or the receiver
    does not resolve to a single live agent (never a silent tmux fallback).
    """
    try:
        config = load_repo_local_config(repo_root_from_args(args)).terminal_transport
    except RepoLocalConfigError:
        # A present-but-broken / unreadable config is "no usable selection", not a
        # herdr opt-in — resolve to the tmux default rather than failing the send.
        return None
    if config.backend != BACKEND_HERDR:
        return None
    return _resolve_herdr_binding(args, config)


def resolve_handoff_transport_runtime(
    args: argparse.Namespace,
) -> "tuple[Optional[TransportBinding], Optional[HerdrTurnStartRail]]":
    """Resolve the transport binding **and** the herdr turn-start rail in one config read.

    Redmine #13255 (auditor j#72602 decision 6): under ``terminal_transport.backend:
    herdr`` the standard rail is driven by the event-driven
    :class:`~...domain.turn_start_rail.HerdrTurnStartRail` instead of the capture-based
    ``_observe_standard_turn_start``. That rail is resolved here, alongside the
    transport binding, from the *same* repo-local ``terminal_transport`` config load
    (so there is no second config read on the send path) using the same trusted-env
    binary posture as the binding (``resolve_turn_start_rail``: real
    subprocess/Popen in production, injected fakes in tests via patched
    ``subprocess.run`` / ``subprocess.Popen``).

    Returns ``(None, None)`` for the tmux default / absent / broken config; returns
    ``(binding, rail)`` for the herdr backend. The rail is resolved for every herdr
    send (it runs no subprocess at resolution time) but is only *used* by the
    herdr+standard branch in ``orchestrate_handoff`` — queue-enter / pending herdr
    sends ignore it and stay on the shim-backed choreography (decision 5).
    """
    try:
        config = load_repo_local_config(repo_root_from_args(args)).terminal_transport
    except RepoLocalConfigError:
        return None, None
    if config.backend != BACKEND_HERDR:
        return None, None
    binding = _resolve_herdr_binding(args, config)
    # The binding resolution above already died if the binary is unconfigured /
    # unresolvable, so the rail resolution here rides the same resolved binary and
    # cannot raise for a binary reason; any unexpected TerminalTransportError still
    # fails closed rather than silently downgrading to tmux.
    try:
        rail = resolve_turn_start_rail(config)
    except TerminalTransportError as exc:
        die(f"terminal transport backend 'herdr' is selected but unavailable: {exc}")
        raise AssertionError("unreachable")
    return binding, rail


def bind_runtime_transport(fn: Callable[..., int]) -> Callable[..., int]:
    """Install the config-selected transport binding around a handoff entry (#13253).

    Wraps :func:`orchestrate_handoff` without changing its body. For the herdr
    backend it swaps the ``commands`` module's ``run_tmux`` / ``capture_pane``
    globals for the tmux-shaped herdr shim for the duration of the send, and (Redmine
    #13255) stashes the resolved event-driven turn-start rail on
    ``commands.active_herdr_turn_start_rail`` so the herdr+standard branch of
    ``orchestrate_handoff`` can drive it in place of the capture-based observation;
    all three are restored in a ``finally``. For the tmux default it installs nothing
    (and leaves the rail slot ``None``).
    """

    @functools.wraps(fn)
    def wrapper(args: argparse.Namespace, *rest: Any, **kwargs: Any) -> int:
        binding, turn_start_rail = resolve_handoff_transport_runtime(args)
        if binding is None or binding.backend != BACKEND_HERDR:
            return fn(args, *rest, **kwargs)
        from mozyo_bridge.application import commands

        saved_run_tmux = commands.run_tmux
        saved_capture_pane = commands.capture_pane
        saved_rail = commands.active_herdr_turn_start_rail
        commands.run_tmux = binding.run_tmux
        commands.capture_pane = binding.capture_pane
        commands.active_herdr_turn_start_rail = turn_start_rail
        try:
            return fn(args, *rest, **kwargs)
        finally:
            commands.run_tmux = saved_run_tmux
            commands.capture_pane = saved_capture_pane
            commands.active_herdr_turn_start_rail = saved_rail

    return wrapper


__all__ = (
    "bind_runtime_transport",
    "resolve_handoff_transport_binding",
    "resolve_handoff_transport_runtime",
)
