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
    TransportBindingError,
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
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    project_preflight_target,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver import (
    pane_info as _pane_info,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (
    capture_pane as _tmux_capture_pane,
    run_tmux as _tmux_run_tmux,
)
from mozyo_bridge.shared.errors import die


def _resolve_target_assigned_name(target: str, *, receiver: str) -> str:
    """Project *the target pane's* stable identity to its herdr assigned name (fail-closed).

    Redmine #13253 j#72373: the translation identity must come from the **target
    pane**, not the sender / current-repo context. This resolves the concrete pane
    the rail is sending to (``pane_info(target)``) and reuses the rail's own
    projection (``project_preflight_target`` — the #11822 role resolver + the
    ``(workspace_id, lane)`` fields the pane carries) to derive the durable
    ``(workspace_id, role, lane)`` slot, then mints the #13247 assigned name from it.

    It is called *lazily* by the translator the first time the shim sees the target's
    ``%N``, so it runs after ``orchestrate_handoff`` has resolved the concrete target
    pane.

    The identity is only minted when the target pane **strongly and non-ambiguously
    binds the receiver** — reusing the rail's own
    :meth:`PreflightTarget.binds_receiver` predicate (role == receiver, ``confidence
    == strong``, ``not ambiguous``, Redmine #13253 j#72381). A pane whose role is
    only *weakly* inferred (a bare ``node`` / process-basename signal without a
    ``@mozyo_agent_role`` option or an agent window name), ambiguous, cross-bound to
    the other role, or missing a ``workspace_id`` (an unregistered pane) fails closed
    with a :class:`TransportBindingError` *before* any send — a herdr send never
    re-binds against a guessed, weakly-typed, or sender-context handle.
    """
    preflight = project_preflight_target(_pane_info(target))
    if not preflight.binds_receiver(receiver) or not preflight.workspace_id:
        raise TransportBindingError(
            "herdr target-pane identity could not be strongly bound for target "
            f"{target!r} (role={preflight.role!r}, receiver={receiver!r}, "
            f"confidence={preflight.confidence!r}, ambiguous={preflight.ambiguous}, "
            f"workspace_id={preflight.workspace_id!r}); refusing to send to an "
            "un-translatable / weakly-identified target"
        )
    try:
        return encode_assigned_name(
            preflight.workspace_id, preflight.role, preflight.lane_id
        )
    except HerdrIdentityError as exc:
        raise TransportBindingError(
            f"herdr target-pane {target!r} identity could not be minted: {exc}"
        )


def resolve_handoff_transport_binding(
    args: argparse.Namespace,
) -> Optional[TransportBinding]:
    """Resolve the transport binding for this send, or ``None`` for the tmux default.

    Returns ``None`` when the tmux backend is in effect (the default, an absent
    ``terminal_transport`` block, or a broken / unreadable config) so the caller
    installs nothing; returns a herdr :class:`TransportBinding` when the herdr
    backend is selected and its trusted-environment binary resolves. A herdr
    selection whose binary is unconfigured / unresolvable fails closed with a clean
    ``die`` (never a silent tmux fallback).

    For the herdr backend the binding is handed the lazy
    :func:`_resolve_target_assigned_name` resolver (curried with the ``--to``
    receiver, so it can require the target pane to *strongly bind that receiver*),
    so the shim mints the assigned name from the **target pane's** stable identity
    (not the sender context, Redmine #13253 j#72373) the first time it sees the
    rail's tmux target.
    """
    try:
        config = load_repo_local_config(repo_root_from_args(args)).terminal_transport
    except RepoLocalConfigError:
        # A present-but-broken / unreadable config is "no usable selection", not a
        # herdr opt-in — resolve to the tmux default rather than failing the send.
        return None
    if config.backend != BACKEND_HERDR:
        return None
    receiver = getattr(args, "to", None) or ""
    try:
        return resolve_runtime_transport_binding(
            config,
            tmux_run_tmux=_tmux_run_tmux,
            tmux_capture_pane=_tmux_capture_pane,
            resolve_assigned_name=functools.partial(
                _resolve_target_assigned_name, receiver=receiver
            ),
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
