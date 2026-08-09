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
import contextlib
import functools
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from mozyo_bridge.application.commands_common import repo_root_from_args
from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config
from mozyo_bridge.core.state.workspace_registry import read_anchor
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.main_lane_guard_gate import (
    resolve_coordinator_provider,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
    RepoLocalConfigError,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    is_explicit_pane_target,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_send_entry import (
    PROJECT_GATEWAY_TARGET_CAPABILITY_PURPOSE,
    RESOLVED_TARGET_CAPABILITY_ARG,
    ResolvedHerdrTargetCapability,
    validate_resolved_target_capability,
    verify_project_gateway_target_effect,
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
    REASON_TRANSPORT_ERROR,
    TerminalTransportError,
    TransportResult,
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


@dataclass(frozen=True)
class HandoffTransportContext:
    """The exact inputs the transport-backend selection reads (Redmine #15149).

    The wiring below used to read all six values off an ``argparse.Namespace``,
    which made the backend switch — the last Namespace-bound step on the send
    path after #13729 — unreachable for a non-CLI caller. They are all flat
    scalars already carried by the typed
    :class:`~...domain.handoff_command_input.HandoffCommandInput` plus the
    facade's resolved repo root and the stashed project-gateway capability, so
    this frozen record is the Namespace-free statement of the same inputs.

    :meth:`coerce` keeps every existing Namespace caller (and the
    ``resolve_handoff_transport_runtime`` monkeypatch seam) working unchanged:
    the resolvers take either shape and normalize here.
    """

    repo_root: Path
    to: str | None = None
    target: str | None = None
    target_repo: str | None = None
    target_lane: str | None = None
    resolved_target_capability: Any = None

    @classmethod
    def from_namespace(cls, args: argparse.Namespace) -> "HandoffTransportContext":
        """Capture the six reads the wiring made off the parsed Namespace."""
        return cls(
            repo_root=repo_root_from_args(args),
            to=getattr(args, "to", None),
            target=getattr(args, "target", None),
            target_repo=getattr(args, "target_repo", None),
            target_lane=getattr(args, "target_lane", None),
            resolved_target_capability=getattr(
                args, RESOLVED_TARGET_CAPABILITY_ARG, None
            ),
        )

    @classmethod
    def coerce(cls, source: "Any") -> "HandoffTransportContext":
        """``source`` as a context: pass one through, convert a Namespace."""
        if isinstance(source, cls):
            return source
        return cls.from_namespace(source)


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
    ctx: HandoffTransportContext, config
) -> TransportBinding:
    """Resolve the herdr :class:`TransportBinding` for an already-herdr ``config``.

    Extracted so the config is read once and shared with the turn-start rail
    resolution (Redmine #13255, auditor j#72602 decision 6: reuse the resolution,
    do not add a second config read). Fail-closed ``die`` when the binary is
    unconfigured / unresolvable (never a silent tmux fallback).
    """
    repo_root = ctx.repo_root
    receiver = ctx.to or ""
    coordinator_provider = resolve_coordinator_provider(repo_root)
    try:
        lister = resolve_agent_lister(config)
        if lister is None:  # defensive: herdr_enabled implies non-None
            die("herdr backend selected but no agent lister could be resolved")
            raise AssertionError("unreachable")
        resolved_target_capability = ctx.resolved_target_capability
        if resolved_target_capability is not None:
            capability = validate_resolved_target_capability(
                resolved_target_capability,
                repo_root=repo_root,
                target=ctx.target,
                target_repo=ctx.target_repo,
                target_lane=ctx.target_lane,
                receiver=receiver,
            )
            # The generic herdr rail normally derives an assigned name from the launch-time sender
            # identity.  The external proxy has no such identity; it supplies the already-resolved
            # target capability instead.  The transport binding still rebinds this durable name
            # against a fresh inventory snapshot before every effect.
            resolver = lambda: capability.assigned_name
        else:
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


class _ProjectGatewayEffectGuardTransport:
    """Guard a standard-rail transport immediately before each mutating effect.

    ``HerdrTurnStartRail`` is intentionally transport-injected.  Wiring replaces
    only that injected port for the internal project-gateway capability; reads,
    state snapshots, waits, and every non-project send remain unchanged.  A
    generation/provider/backend drift becomes ``transport_error`` before the
    delegate sees body bytes or keys, preserving the rail's structured
    ``inject_failed`` zero-send path.
    """

    def __init__(self, delegate: object, verifier: Callable[[], None]) -> None:
        self._delegate = delegate
        self._verifier = verifier
        self.backend = getattr(delegate, "backend", BACKEND_HERDR)

    def _guard(self) -> Optional[TransportResult]:
        try:
            self._verifier()
        except Exception:  # noqa: BLE001 - any attestation failure blocks the effect
            return TransportResult.failure(
                REASON_TRANSPORT_ERROR,
                "project-gateway target capability changed before the send effect",
            )
        return None

    def send_text(self, target: str, text: str) -> TransportResult:
        refused = self._guard()
        if refused is not None:
            return refused
        return self._delegate.send_text(target, text)

    def send_keys(self, target: str, keys: str) -> TransportResult:
        refused = self._guard()
        if refused is not None:
            return refused
        return self._delegate.send_keys(target, keys)

    def __getattr__(self, name: str):
        # Read-only capabilities such as ``read_pane`` / ``read_pane_render``
        # remain the exact resolved transport implementation.
        return getattr(self._delegate, name)


def _guard_project_gateway_binding_effects(
    binding: TransportBinding,
    verifier: Callable[[], None],
) -> TransportBinding:
    """Guard each non-standard shim send effect before it reaches the binding."""

    unguarded_run_tmux = binding.run_tmux

    def guarded_run_tmux(*args: str, check: bool = True):
        # Every mapped Herdr mutation enters through ``send-keys -t``: body,
        # Enter, or C-u rollback.  Re-attest before each one so a generation swap
        # cannot inherit a later key effect after an earlier body check.
        if len(args) >= 3 and args[0] == "send-keys" and args[1] == "-t":
            try:
                verifier()
            except Exception as exc:  # noqa: BLE001 - effect guard is fail-closed
                raise TransportBindingError(
                    "project-gateway target capability changed before the send effect"
                ) from exc
        return unguarded_run_tmux(*args, check=check)

    return TransportBinding(
        backend=binding.backend,
        run_tmux=guarded_run_tmux,
        capture_pane=binding.capture_pane,
    )


def _guard_project_gateway_standard_rail_effects(
    rail: HerdrTurnStartRail,
    verifier: Callable[[], None],
) -> HerdrTurnStartRail:
    """Install the effect guard on the standard rail's injected transport port."""

    transport = getattr(rail, "_transport", None)
    if transport is None:
        raise TransportBindingError(
            "project-gateway send rail exposes no transport effect boundary; refusing to send"
        )
    rail._transport = _ProjectGatewayEffectGuardTransport(transport, verifier)
    return rail


def _project_gateway_capability(
    ctx: HandoffTransportContext,
) -> Optional[ResolvedHerdrTargetCapability]:
    """Return the exact internal project-gateway capability, if present."""

    capability = ctx.resolved_target_capability
    if (
        type(capability) is ResolvedHerdrTargetCapability
        and capability.purpose == PROJECT_GATEWAY_TARGET_CAPABILITY_PURPOSE
    ):
        return capability
    return None


def _require_project_gateway_herdr_transport_frame(
    ctx: HandoffTransportContext,
    source_config,
) -> Optional[ResolvedHerdrTargetCapability]:
    """Refuse a project capability before any source-to-target backend fallback.

    Project-gateway inventory is selected from ``--target-repo`` while the legacy
    handoff decorator selects its rail from the sender repo.  Until those configs
    are proven to name the same Herdr backend, the capability must never fall
    through to the sender's tmux default.  The send-effect verifier repeats this
    backend join immediately before every later mutation.
    """

    capability = _project_gateway_capability(ctx)
    if capability is None:
        return None
    try:
        target_config = load_repo_local_config(
            Path(capability.target_repo_root)
        ).terminal_transport
    except Exception:  # noqa: BLE001 - target config is an IO boundary
        die(
            "project-gateway target transport config is unreadable; refusing a "
            "cross-backend fallback before delivery"
        )
        raise AssertionError("unreachable")
    if (
        source_config.backend != BACKEND_HERDR
        or target_config.backend != BACKEND_HERDR
        or target_config != source_config
    ):
        die(
            "project-gateway sender/target terminal backends do not resolve to the "
            "same Herdr transport; refusing cross-backend fallback before delivery"
        )
        raise AssertionError("unreachable")
    return capability


def resolve_handoff_transport_binding(
    source: "argparse.Namespace | HandoffTransportContext",
) -> Optional[TransportBinding]:
    """Resolve the transport binding for this send, or ``None`` for the tmux default.

    Redmine #15149: takes either the parsed Namespace (every existing CLI caller)
    or the typed :class:`HandoffTransportContext` the shared application API
    builds, and normalizes to the context. The selection logic is identical for
    both, so the CLI and a non-CLI caller cannot resolve different backends.

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
    ctx = HandoffTransportContext.coerce(source)
    capability = _project_gateway_capability(ctx)
    try:
        config = load_repo_local_config(ctx.repo_root).terminal_transport
    except RepoLocalConfigError:
        if capability is not None:
            die(
                "project-gateway sender transport config is unreadable; refusing a "
                "cross-backend fallback before delivery"
            )
            raise AssertionError("unreachable")
        # A present-but-broken / unreadable config is "no usable selection", not a
        # herdr opt-in — resolve to the tmux default rather than failing the send.
        return None
    _require_project_gateway_herdr_transport_frame(ctx, config)
    if config.backend != BACKEND_HERDR:
        return None
    # Redmine #13320 (a-narrow, j#73114): an explicit tmux `%pane` target routes on
    # the tmux rail even under `backend: herdr`, so install no herdr binding for it —
    # the same target-kind narrowing `orchestrate_handoff` applies via
    # `herdr_effective_backend_selected`. `orchestrate_handoff` then runs the tmux
    # path (`require_tmux()` / `pane_info()`), which fails closed on an unresolvable
    # pane exactly as under `backend: tmux` — never a silent herdr fallback.
    if is_explicit_pane_target(ctx.target):
        return None
    return _resolve_herdr_binding(ctx, config)


def resolve_handoff_transport_runtime(
    source: "argparse.Namespace | HandoffTransportContext",
) -> "tuple[Optional[TransportBinding], Optional[HerdrTurnStartRail]]":
    """Resolve the transport binding **and** the herdr turn-start rail in one config read.

    Redmine #15149: like :func:`resolve_handoff_transport_binding`, this takes
    either the parsed Namespace or the typed :class:`HandoffTransportContext`, so
    the backend selection on the send path is reachable without a CLI parse.

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
    ctx = HandoffTransportContext.coerce(source)
    capability = _project_gateway_capability(ctx)
    try:
        config = load_repo_local_config(ctx.repo_root).terminal_transport
    except RepoLocalConfigError:
        if capability is not None:
            die(
                "project-gateway sender transport config is unreadable; refusing a "
                "cross-backend fallback before delivery"
            )
            raise AssertionError("unreachable")
        return None, None
    _require_project_gateway_herdr_transport_frame(ctx, config)
    if config.backend != BACKEND_HERDR:
        return None, None
    # Redmine #13320 (a-narrow, j#73114): the decorator's branch point. An explicit
    # tmux `%pane` target must install NEITHER the herdr binding NOR the herdr
    # turn-start rail — it rides the tmux rail (same predicate `orchestrate_handoff`
    # reads via `herdr_effective_backend_selected`). Narrowing here and in
    # `orchestrate_handoff` together keeps the split whole: a `%pane` send neither
    # gets the herdr shim/rail nor skips `require_tmux()`.
    if is_explicit_pane_target(ctx.target):
        return None, None
    binding = _resolve_herdr_binding(ctx, config)
    # The binding resolution above already died if the binary is unconfigured /
    # unresolvable, so the rail resolution here rides the same resolved binary and
    # cannot raise for a binary reason; any unexpected TerminalTransportError still
    # fails closed rather than silently downgrading to tmux.
    try:
        rail = resolve_turn_start_rail(config)
    except TerminalTransportError as exc:
        die(f"terminal transport backend 'herdr' is selected but unavailable: {exc}")
        raise AssertionError("unreachable")
    resolved_target_capability = capability
    if resolved_target_capability is not None:
        repo_root = Path(ctx.repo_root).expanduser().resolve()
        verifier = functools.partial(
            verify_project_gateway_target_effect,
            resolved_target_capability,
            repo_root=repo_root,
        )
        binding = _guard_project_gateway_binding_effects(binding, verifier)
        if rail is None:
            die(
                "project-gateway capability resolved under Herdr but no standard send rail "
                "was installed; refusing to send"
            )
            raise AssertionError("unreachable")
        rail = _guard_project_gateway_standard_rail_effects(rail, verifier)
    return binding, rail


@contextlib.contextmanager
def runtime_transport_binding(
    source: "argparse.Namespace | HandoffTransportContext",
) -> "Iterator[None]":
    """Install the config-selected transport binding for one send (#13253 / #15149).

    For the herdr backend it swaps the ``commands`` module's ``run_tmux`` /
    ``capture_pane`` globals for the tmux-shaped herdr shim for the duration of
    the send, and (Redmine #13255) stashes the resolved event-driven turn-start
    rail on ``commands.active_herdr_turn_start_rail`` so the herdr+standard
    branch of the orchestration can drive it in place of the capture-based
    observation; all three are restored in a ``finally``. For the tmux default it
    installs nothing (and leaves the rail slot ``None``), so the send is
    byte-for-byte the current behaviour and any test-patched ``commands.run_tmux``
    stays in force (the #12932 monkeypatch seam is untouched).

    Redmine #15149 turned the former decorator body into this context manager and
    moved the installation *inside* the Namespace-free orchestration core, so the
    backend switch is part of the shared application processing rather than a
    CLI-entry wrapper. The selection still runs before any send gate, and it still
    resolves through the module-level :func:`resolve_handoff_transport_runtime`
    seam.
    """
    binding, turn_start_rail = resolve_handoff_transport_runtime(source)
    if binding is None or binding.backend != BACKEND_HERDR:
        yield
        return
    from mozyo_bridge.application import commands

    saved_run_tmux = commands.run_tmux
    saved_capture_pane = commands.capture_pane
    saved_rail = commands.active_herdr_turn_start_rail
    commands.run_tmux = binding.run_tmux
    commands.capture_pane = binding.capture_pane
    commands.active_herdr_turn_start_rail = turn_start_rail
    try:
        yield
    finally:
        commands.run_tmux = saved_run_tmux
        commands.capture_pane = saved_capture_pane
        commands.active_herdr_turn_start_rail = saved_rail


def bind_runtime_transport(fn: Callable[..., int]) -> Callable[..., int]:
    """Decorate a Namespace-taking handoff entry with :func:`runtime_transport_binding`.

    Retained as the Namespace-entry form of the same installation. The
    orchestration core installs the binding itself (Redmine #15149), so this is
    no longer applied to ``orchestrate_handoff``; it stays available for any
    other Namespace-shaped entry that needs the backend swap.
    """

    @functools.wraps(fn)
    def wrapper(args: argparse.Namespace, *rest: Any, **kwargs: Any) -> int:
        with runtime_transport_binding(args):
            return fn(args, *rest, **kwargs)

    return wrapper


__all__ = (
    "HandoffTransportContext",
    "bind_runtime_transport",
    "resolve_handoff_transport_binding",
    "resolve_handoff_transport_runtime",
    "runtime_transport_binding",
)
