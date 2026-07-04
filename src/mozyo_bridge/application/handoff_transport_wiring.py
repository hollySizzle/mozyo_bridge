"""Runtime transport backend wiring for the handoff send path (Redmine #13253).

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

Fail-closed (Redmine #13253 j#72318): an absent / broken config is "no herdr
selection" and resolves to the tmux default; a herdr selection whose trusted-
environment binary is unconfigured / unresolvable surfaces as a clean ``die`` —
it is never silently downgraded to tmux. Roll-back is a one-line
``terminal_transport.backend`` change plus a process restart: this resolver reads
the selection fresh per process and holds no state.
"""

from __future__ import annotations

import argparse
import functools
from typing import Any, Callable, Optional

from mozyo_bridge.application.commands_common import repo_root_from_args
from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
    RepoLocalConfigError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.transport_binding import (
    TransportBinding,
    resolve_runtime_transport_binding,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    HerdrIdentityError,
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
    TerminalTransportError,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (
    capture_pane as _tmux_capture_pane,
    run_tmux as _tmux_run_tmux,
)
from mozyo_bridge.core.state.workspace_registry import resolve_canonical_session
from mozyo_bridge.shared.errors import die


def _resolve_receiver_assigned_name(args: argparse.Namespace) -> str:
    """Mint the receiver's durable herdr assigned name for this send (fail-closed).

    The receiver's identity slot is ``(workspace_id, role, lane)`` — the same slot
    the route-identity ledger and #13247 use. ``role`` is the handoff receiver
    (``--to claude|codex``); ``workspace_id`` and ``lane`` come from the repo
    context (``resolve_canonical_session`` + the ``_resolve_workspace_lane`` probe),
    normalised exactly as #13247 prescribes (an empty lane -> ``default``). The name
    is minted with #13247 ``encode_assigned_name``; a missing role / workspace (an
    empty required component) fails closed with a clean ``die`` — a herdr send must
    not proceed without a durable receiver handle to translate its tmux target to.
    """
    role = getattr(args, "to", None)
    repo_root = repo_root_from_args(args)
    try:
        workspace_id = resolve_canonical_session(repo_root).workspace_id
    except Exception:  # pragma: no cover - defensive; unresolvable workspace context
        workspace_id = None
    # The lane probe lives in ``commands`` (git checkout facts + registered
    # canonical path); resolve it lazily to avoid an import cycle, and degrade to
    # the #13247 default lane if it cannot be derived.
    lane_id = ""
    try:
        from mozyo_bridge.application import commands

        lane_id = getattr(
            commands._resolve_workspace_lane(str(repo_root), workspace_id), "lane_id", ""
        )
    except Exception:  # pragma: no cover - defensive; lane probe is best-effort
        lane_id = ""
    try:
        return encode_assigned_name(workspace_id or "", role or "", lane_id or "")
    except HerdrIdentityError as exc:
        die(
            "terminal transport backend 'herdr' is selected but the receiver's herdr "
            f"identity could not be resolved (role={role!r}, workspace_id={workspace_id!r}): "
            f"{exc}"
        )
        raise AssertionError("unreachable")


def resolve_handoff_transport_binding(
    args: argparse.Namespace,
) -> Optional[TransportBinding]:
    """Resolve the transport binding for this send, or ``None`` for the tmux default.

    Returns ``None`` when the tmux backend is in effect (the default, an absent
    ``terminal_transport`` block, or a broken / unreadable config) so the caller
    installs nothing; returns a herdr :class:`TransportBinding` when the herdr
    backend is selected and its trusted-environment binary resolves. A herdr
    selection whose binary is unconfigured / unresolvable, or whose receiver herdr
    identity cannot be minted, fails closed with a clean ``die`` (never a silent
    tmux fallback).

    For the herdr backend the receiver's durable assigned name is minted here (from
    ``--to`` + the repo workspace/lane context) and handed to the binding so the
    shim can translate the rail's tmux target (``%N``) into a live herdr locator.
    """
    try:
        config = load_repo_local_config(repo_root_from_args(args)).terminal_transport
    except RepoLocalConfigError:
        # A present-but-broken / unreadable config is "no usable selection", not a
        # herdr opt-in — resolve to the tmux default rather than failing the send.
        return None
    if config.backend != BACKEND_HERDR:
        return None
    assigned_name = _resolve_receiver_assigned_name(args)
    try:
        return resolve_runtime_transport_binding(
            config,
            tmux_run_tmux=_tmux_run_tmux,
            tmux_capture_pane=_tmux_capture_pane,
            assigned_name=assigned_name,
        )
    except TerminalTransportError as exc:
        die(f"terminal transport backend 'herdr' is selected but unavailable: {exc}")
        raise AssertionError("unreachable")


def bind_runtime_transport(fn: Callable[..., int]) -> Callable[..., int]:
    """Install the config-selected transport binding around a handoff entry (#13253).

    Wraps :func:`orchestrate_handoff` without changing its body. For the herdr
    backend it swaps the ``commands`` module's ``run_tmux`` / ``capture_pane``
    globals for the tmux-shaped herdr shim for the duration of the send and
    restores them in a ``finally``. For the tmux default it installs nothing.
    """

    @functools.wraps(fn)
    def wrapper(args: argparse.Namespace, *rest: Any, **kwargs: Any) -> int:
        binding = resolve_handoff_transport_binding(args)
        if binding is None or binding.backend != BACKEND_HERDR:
            return fn(args, *rest, **kwargs)
        from mozyo_bridge.application import commands

        saved_run_tmux = commands.run_tmux
        saved_capture_pane = commands.capture_pane
        commands.run_tmux = binding.run_tmux
        commands.capture_pane = binding.capture_pane
        try:
            return fn(args, *rest, **kwargs)
        finally:
            commands.run_tmux = saved_run_tmux
            commands.capture_pane = saved_capture_pane

    return wrapper


__all__ = (
    "bind_runtime_transport",
    "resolve_handoff_transport_binding",
)
