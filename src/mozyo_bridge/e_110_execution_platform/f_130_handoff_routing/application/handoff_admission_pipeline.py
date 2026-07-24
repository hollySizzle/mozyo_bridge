"""Handoff admission-pipeline preflight (Redmine #13729 tranche 6).

The ``orchestrate_handoff`` preflight in ``application/commands.py`` historically carried the
**admission-pipeline slice** inline: after the send target is resolved onto a concrete pane record
and the canonical preflight vocabulary (tranche 5), but before the pre-send envelope / startup
admission / transport rails, it runs the stateful sequence of die-able admission gates that decide
whether the send is *allowed to type at all*. That slice is one coherent step — a fixed-order
sequence of fail-closed gates, each either falling through or emitting a terminal blocked outcome
and ``die``\\ ing (or re-raising the agent-gate ``SystemExit``):

1. **main-lane implementation-dispatch guard** (Redmine #12441/#13174): an
   ``implementation_request`` addressed to the repo's default/main-lane implementer fails closed in
   every mode unless an explicit ``--main-lane-exception`` references an owner decision;
2. **receiver binding** (Redmine #11779/#11822/#11908): under queue-enter (or any
   receiver-locked wrapper) the explicit ``--target`` must bind to the receiver via the canonical
   role projection, else ``invalid_args``;
3. **session binding** (Redmine #11301/#13261): under queue-enter the target must live in the
   sender's tmux session, or be a constrained cross-session target (explicit pane + ``--target-repo``
   gate); an explicit no-op under herdr;
4. **cross-workspace ``--to claude`` gate** (Redmine #10332): a cross-session ``--to claude``
   is rejected (``cross_session_claude``) with a best-effort Codex-gateway diagnostic; an explicit
   no-op under herdr;
5. **gateway-route enforcement** (Redmine #12918): a governed ``implementation_request`` /
   ``review_result`` sent ``--to claude`` cross-lane fails closed through the f_140 gate;
6. **``--target-repo`` identity gate** (Redmine #11625/#12658): the target pane's cwd must
   resolve (Unicode-normalized) to the expected Git repo root, else ``target_repo_mismatch``;
7. **project-scope gate** (Redmine #12658): layered *under* the repo gate — the target must
   resolve to the expected adopted project scope (and requires an explicit ``--target-repo``), else
   ``invalid_args`` / ``target_project_mismatch``;
8. **standard_target_admission** (Redmine #12597): resolve the admission/activation policy and,
   under queue-enter to an inactive split, either **plan** the activation (``activate_inactive_target``
   — the actual ``select-pane`` is deferred to the facade, after every die-able gate + startup
   admission) or fail closed (``invalid_args`` with a recovery command);
9. **foreground-agent binding** (Redmine #12597): under queue-enter the pane's foreground process
   must match the receiver agent (``target_not_agent``); otherwise the generic ``ensure_agent_target``
   agent gate runs and re-raises its ``SystemExit`` after emitting ``target_not_agent``.

This module carves that slice into an OOP-first application use case under #12638 / #13729, the
admission-pipeline sibling of the herdr rail (tranche 3), the common tmux transport rail (tranche 4),
and the target-resolution preflight (tranche 5), **without** touching the target-resolution slice
above it or the anchor/profile envelope + startup admission + transport rails after it:

- :class:`AdmissionPipelineRequest` is the frozen typed input — the resolved ``target`` locator /
  ``target_info`` pane record / ``preflight_target`` projection from the target-resolution slice,
  the ``herdr_send`` backend predicate and ``resolved_target_repo``, the ``receiver`` / ``mode`` /
  the terminal-outcome context (``anchor`` / ``kind`` / ``source`` / ``record_format`` /
  ``record_command``), and the raw entry scalars the gates read (``raw_target`` /
  ``require_receiver_binding`` / ``has_main_lane_exception`` / ``allow_direct_worker`` /
  ``target_project`` / ``no_target_activation`` / ``restore_previous_active`` / ``force``).
- :class:`AdmissionPipelineResult` is the frozen typed output — the two values the downstream facade
  reads back from the slice: the resolved ``admission_policy`` (its ``restore_previous_active`` is
  threaded onto the final delivery outcome) and ``activate_inactive_target`` (the deferred-activation
  plan the facade actuates after startup admission). Every other gate is terminal-or-fallthrough, so
  nothing else crosses the boundary.
- :class:`AdmissionPipelineOps` is the port for the *side-effecting* dependencies the slice needs
  from its environment (resolve the tmux session name, run the config-resolving main-lane guard and
  gateway-route gate, resolve the workspace root + project scope, run the generic agent gate, emit
  the best-effort cross-session gateway diagnostic, emit the blocked outcome, ``die``), so
  :meth:`AdmissionPipelineUseCase.execute` is exercisable with a synthetic fake port and no live
  tmux / herdr / Redmine / repo-local config.
- :class:`AdmissionPipelineUseCase` holds the slice body: the fixed evaluation order of the nine
  gates, their herdr no-op guards, the ``--target-repo auto``-already-resolved identity comparison,
  the layered project-scope discovery (with its fail-closed ``except`` boundary), the inactive-split
  activation-plan-vs-fail-closed branch, and the queue-enter-vs-standard foreground split live here
  as typed control flow over the injected effects and the pure collaborators.
- :class:`LiveAdmissionPipelineOps` routes the two monkeypatched seams (``current_session_name`` /
  ``ensure_agent_target``) through the :mod:`commands` module *at call time* so those seams keep
  intercepting unchanged, and imports every other effect directly from the f_110 / f_140 / f_120
  module the inline block imported it from (none of those are ``commands.*`` monkeypatch seams), so
  no import cycle is introduced. The emit closure is the facade's per-call publishing emitter
  (``make_publishing_emitter``), injected through the constructor so publication stays a property of
  emitting (Redmine #13583 R3-F1).

The pure collaborators (:func:`make_outcome`, :func:`resolve_standard_target_admission_policy`,
:func:`evaluate_standard_target_admission`, :func:`is_receiver_agent_process`,
:func:`build_inactive_pane_fallback_command`, :func:`normalize_path_unicode`,
:func:`path_under_repo_relative`) are imported and called directly — they take no environment and
are already unit-covered — so the port stays scoped to the genuine side effects. This is a pure,
behavior-preserving restructuring: the evaluation order, the emitted blocked outcomes (reason /
extras / ``recovery_command``), the printed diagnostics, the exit code, every ``die`` message, and
the re-raised agent-gate ``SystemExit`` are byte-identical to the original inline block.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.project_scope import (
    ProjectScope,
    path_under_repo_relative,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    PreflightTarget,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver import (
    is_receiver_agent_process,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_send_semantics import (  # noqa: E501
    SEND_SEMANTIC_PROJECT_REPO,
    send_semantic_gap,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    MODE_QUEUE_ENTER,
    DeliveryOutcome,
    NormalizedAnchor,
    StandardTargetAdmissionPolicy,
    build_inactive_pane_fallback_command,
    evaluate_standard_target_admission,
    make_outcome,
    resolve_standard_target_admission_policy,
)
from mozyo_bridge.shared.paths import normalize_path_unicode


#: The per-call publishing emitter injected by the facade (``make_publishing_emitter``):
#: ``emit(outcome, record_format=..., command=..., recovery_command=...)`` — publishes then renders
#: the delivery outcome. ``recovery_command`` defaults to ``None`` (byte-identical to omitting it).
PublishingEmitter = Callable[..., None]


@dataclass(frozen=True)
class AdmissionPipelineRequest:
    """The typed input for the handoff admission-pipeline preflight slice.

    Every field is a value the original inline block read from an ``orchestrate_handoff`` local:
    ``target`` / ``target_info`` / ``preflight_target`` are the resolved-target outputs of the
    tranche-5 slice; ``herdr_send`` selects the tmux no-op guards; ``resolved_target_repo`` is the
    (auto-)resolved ``--target-repo`` value the identity gates read; ``receiver`` / ``mode`` /
    ``anchor`` / ``kind`` / ``source`` / ``record_format`` / ``record_command`` are the
    terminal-outcome context threaded onto every blocked outcome; the remaining scalars are the raw
    entry inputs the individual gates read (``raw_target`` is the pre-resolution ``--target`` value
    the session/auto gates read as ``bool(inp.target)``). Frozen: the slice never mutates its input.
    """

    receiver: str
    kind: Optional[str]
    mode: str
    source: str
    anchor: Optional[NormalizedAnchor]
    target: str
    target_info: Dict[str, str]
    preflight_target: PreflightTarget
    herdr_send: bool
    resolved_target_repo: Optional[str]
    record_format: str
    record_command: Optional[str]
    raw_target: Optional[str]
    require_receiver_binding: bool
    has_main_lane_exception: bool
    allow_direct_worker: bool
    target_project: Optional[str]
    no_target_activation: bool
    restore_previous_active: bool
    force: bool


@dataclass(frozen=True)
class AdmissionPipelineResult:
    """The typed output of the admission-pipeline preflight slice.

    The two values the downstream facade reads from the slice: the resolved ``admission_policy``
    (its ``restore_previous_active`` is threaded onto the final delivery outcome) and
    ``activate_inactive_target`` — the deferred inactive-split activation plan the facade actuates
    (via ``select-pane``) only after every die-able gate above AND the startup-admission gate pass.
    Every other gate is terminal-or-fallthrough, so nothing else crosses the boundary.
    """

    activate_inactive_target: bool
    admission_policy: StandardTargetAdmissionPolicy


class AdmissionPipelineOps(Protocol):
    """Port: the side-effecting dependencies the admission-pipeline preflight slice needs.

    The pure collaborators (:func:`make_outcome`, :func:`resolve_standard_target_admission_policy`,
    :func:`evaluate_standard_target_admission`, :func:`is_receiver_agent_process`,
    :func:`build_inactive_pane_fallback_command`, :func:`normalize_path_unicode`,
    :func:`path_under_repo_relative`) are NOT here — the use case calls them directly. Only the
    genuine side effects (tmux reads, config-resolving gates, filesystem project discovery, the
    generic agent gate, and the emit/die terminals) are ported so the slice is exercisable with a
    synthetic fake that records the calls.
    """

    def current_session_name(self) -> Optional[str]:
        """The sender's current tmux session name (``None`` when invoked outside tmux)."""
        ...

    def main_lane_guard_blocked(
        self,
        *,
        receiver: str,
        kind: Optional[str],
        preflight_target: PreflightTarget,
        has_main_lane_exception: bool,
    ) -> bool:
        """Apply the #12441 main-lane guard with the implementer role resolved by binding (config IO)."""
        ...

    def enforce_gateway_route(
        self,
        *,
        kind: Optional[str],
        receiver: str,
        preflight_target: PreflightTarget,
        source: str,
        mode: str,
        anchor: Optional[NormalizedAnchor],
        target: str,
        record_format: str,
        record_command: Optional[str],
        allow_direct_worker: bool,
        sender_lane_unit: Optional[Tuple[Optional[str], Optional[str]]],
    ) -> None:
        """Apply the #12918 gateway-route gate; on a block it emits + ``die``\\ s (never returns)."""
        ...

    def resolve_workspace_root(self, cwd: str) -> Optional[str]:
        """Walk ``cwd`` up to a Git-worktree-preferring workspace/repo marker root, or ``None``."""
        ...

    def project_scope_for_cwd(
        self, cwd: str, git_root: str
    ) -> Optional[ProjectScope]:
        """Resolve the adopted project scope ``cwd`` belongs to under ``git_root`` (or ``None``)."""
        ...

    def cross_session_gateway_diagnostic(self, target_session: str) -> str:
        """Best-effort ``cross_session_claude`` Codex-gateway hint (``""`` on any failure)."""
        ...

    def ensure_agent_target(
        self, target_info: Dict[str, str], receiver: str, *, force: bool
    ) -> None:
        """Generic agent gate for the non-queue-enter rail (raises ``SystemExit`` on a mismatch)."""
        ...

    def emit(
        self,
        outcome: DeliveryOutcome,
        *,
        record_format: str,
        command: Optional[str],
        recovery_command: Optional[str] = None,
    ) -> None:
        """Emit (publish + render) the terminal blocked delivery outcome."""
        ...

    def die(self, message: str) -> None:
        """Terminate the send with a non-zero exit and ``message`` (raises)."""
        ...


class AdmissionPipelineUseCase:
    """The handoff admission-pipeline preflight slice.

    Runs the fixed-order sequence of nine fail-closed admission gates over the resolved target.
    Returns the typed result (the resolved admission policy + the deferred inactive-split activation
    plan) on success; every gate either falls through or emits a blocked outcome and ``die``\\ s (or
    re-raises the agent gate's ``SystemExit``) without falling through.
    """

    def __init__(self, ops: AdmissionPipelineOps) -> None:
        self._ops = ops

    def _emit_blocked(
        self,
        request: AdmissionPipelineRequest,
        *,
        reason: str,
        recovery_command: Optional[str] = None,
    ) -> None:
        """Emit a terminal blocked :class:`DeliveryOutcome` from the request context.

        The context threading (receiver / target / anchor / mode / kind / source) is identical
        across every admission blocked terminal; only ``reason`` (and the inactive-split
        ``recovery_command``) differ. ``reason`` is a wire-literal constant re-narrowed to the
        ``Reason`` wire enum by :func:`make_outcome`'s signature.
        """
        self._ops.emit(
            make_outcome(
                status="blocked",
                reason=reason,  # type: ignore[arg-type]
                receiver=request.receiver,
                target=request.target,
                anchor=request.anchor,
                mode=request.mode,
                kind=request.kind,
                notification_marker=None,
                source=request.source,
            ),
            record_format=request.record_format,
            command=request.record_command,
            recovery_command=recovery_command,
        )

    def execute(self, request: AdmissionPipelineRequest) -> AdmissionPipelineResult:
        ops = self._ops
        receiver = request.receiver
        kind = request.kind
        mode = request.mode
        target = request.target
        target_info = request.target_info
        preflight_target = request.preflight_target
        herdr_send = request.herdr_send

        # Main-lane implementation-dispatch guard (Redmine #12441; prevention note
        # #12438 j#63436; role-based since Redmine #13174). In the managed cockpit /
        # sublane operating model (epic #12366;
        # `vibes/docs/logics/coordinator-sublane-development-flow.md`) the main-unit
        # implementer surface is not where implementation runs; implementation-shaped
        # work defaults to a cockpit-visible sublane, so a direct `implementation_request`
        # into the cockpit's default/main-lane implementer pane is a process gap (#12438
        # j#63432/j#63434). The guard reasons about the implementer *role*: the boundary
        # resolves the implementer's runtime provider from the repo-local RoleProviderBinding
        # (#12673/#13157; default -> `claude`, byte-identical) and fails closed in EVERY mode
        # (the resolved target's lane/view is known here, before the mode-scoped binding gate
        # below) unless an explicit `--main-lane-exception` references an owner/operator
        # decision. Deliberately scoped to cockpit panes: a plain `normal_window` agent, a
        # same-lane *sublane* implementer (non-`default` lane), a dispatch to any non-implementer
        # provider (the gateway route), and any non-`implementation_request` notification are all
        # unaffected. The binding wiring lives in the f_140 `main_lane_guard_gate` seam.
        if ops.main_lane_guard_blocked(
            receiver=receiver,
            kind=kind,
            preflight_target=preflight_target,
            has_main_lane_exception=request.has_main_lane_exception,
        ):
            self._emit_blocked(request, reason="main_lane_implementation_blocked")
            ops.die(
                "blocked: `--to claude --kind implementation_request` resolved to "
                f"the repo's default/main lane (pane {target}, lane="
                f"{preflight_target.lane_id!r}). Implementation-shaped work defaults "
                "to a cockpit-visible sublane — \"pane already open\" is not an "
                "exception. Dispatch through the target-lane Codex gateway "
                "(`--to codex --target <session>:codex --target-repo <root>`), which "
                "performs the same-lane Claude handoff, or — only with a genuine "
                "owner/operator decision recorded in the durable anchor — pass "
                "`--main-lane-exception <journal-ref>`. A same-lane sublane Claude "
                "dispatch (non-default lane) is unaffected."
            )
            raise AssertionError("unreachable")

        if (mode == MODE_QUEUE_ENTER or request.require_receiver_binding) and not preflight_target.binds_receiver(receiver):
            # Step 9 (v0.2; role-aware since Redmine #11822, projection since #11908;
            # mode-independent for receiver-locked wrappers since Redmine #11779).
            # Under the relaxed queue-enter rail, marker miss does NOT roll back, so
            # an explicit `--target %X` that resolves to a different agent would
            # silently press Enter into the wrong receiver's pane. The agent gate
            # (`ensure_agent_target`) only verifies the pane is running *some* agent
            # process (claude / codex / node) and does not bind the pane to the
            # intended receiver. `binds_receiver` binds the explicit target to the
            # receiver via the canonical projection: a strong, non-ambiguous role ==
            # receiver from either the `@mozyo_agent_role` pane option (cockpit /
            # `cockpit_pane` view) or the `<agent>` window name (normal `mozyo` /
            # `normal_window` view). A cockpit pane no longer needs `--force`; a weak
            # / ambiguous / mismatched signal stays fail-closed, matching the
            # contract's "Allowed Targets".
            #
            # `require_receiver_binding` extends this gate to `standard` / `pending`
            # for wrappers whose contract fixes the receiver (cross-workspace
            # consult): without it, `--mode standard` / `--mode pending` would skip
            # the binding and let an explicit foreign-Claude `%pane` be typed into
            # under a `to=codex` marker.
            observed_window = preflight_target.window_name or "<unknown>"
            observed_role = preflight_target.pane_option_role or "<none>"
            self._emit_blocked(request, reason="invalid_args")
            gate_label = (
                f"--mode {MODE_QUEUE_ENTER}"
                if mode == MODE_QUEUE_ENTER
                else "this handoff primitive"
            )
            ops.die(
                f"{gate_label} requires the explicit --target pane to resolve "
                f"to the receiver; --to={receiver!r} but pane {target} resolved to "
                f"role={preflight_target.role!r} (source={preflight_target.role_source}, "
                f"confidence={preflight_target.confidence}, "
                f"ambiguous={preflight_target.ambiguous}, view={preflight_target.view_kind}; "
                f"window={observed_window!r}, @mozyo_agent_role={observed_role!r}). "
                "Drop --target to use role resolution, or pass a pane that resolves "
                "to the receiver."
            )
            raise AssertionError("unreachable")

        if mode == MODE_QUEUE_ENTER and not herdr_send:
            # Redmine #13261: this session-binding gate binds the target to the sender's
            # *tmux session* — a concept that does not exist in a pure herdr session. It
            # is an explicit no-op under the herdr backend: the herdr target is addressed
            # by its live locator and is already scoped to the sender's workspace + role
            # by the inventory decode (WU1), which supersedes tmux-session binding.
            #
            # Step 10 (v0.3; constrained cross-session admission added in
            # Redmine #11301): session binding. queue-enter is bound to the
            # sender's tmux session by default — under marker miss it does not roll
            # back, so an explicit `--target %X` in a foreign session could
            # otherwise land in a different repo's agent, and tmux-outside
            # invocations have no sender session to compare against.
            #
            # A cross-session target is admitted ONLY under the constrained rail:
            # both sender and target sessions must be resolvable, `--target` must
            # be an explicit pane / tmux target (not receiver auto-discovery), and
            # `--target-repo` must be supplied so the workspace identity gate runs.
            # When admitted, the request still flows through every downstream gate:
            # the cross-session `--to claude` gate keeps Claude on the codex-gateway
            # path, the `--target-repo` gate fails closed on identity mismatch, and
            # Steps 11 / 12 bind the active pane and the foreground agent process.
            # This lets a configured workspace skip the manual `--mode standard`
            # fallback while ambiguous / unconfigured states stay fail-closed.
            sender_session = ops.current_session_name()
            target_location = target_info.get("location") or ""
            target_session = (
                target_location.split(":", 1)[0] if ":" in target_location else ""
            )
            same_session = (
                bool(sender_session)
                and bool(target_session)
                and sender_session == target_session
            )
            if not same_session:
                both_sessions_known = bool(sender_session) and bool(target_session)
                explicit_target = bool(request.raw_target)
                has_target_repo = bool(request.resolved_target_repo)
                cross_session_admitted = (
                    both_sessions_known and explicit_target and has_target_repo
                )
                if not cross_session_admitted:
                    self._emit_blocked(request, reason="invalid_args")
                    ops.die(
                        "--mode queue-enter requires the target pane to live in the "
                        "sender's tmux session, or a constrained cross-session "
                        "target (an explicit --target pane id plus --target-repo "
                        "identity gate); "
                        f"sender_session={(sender_session or '<unset>')!r} "
                        f"target_session={(target_session or '<unknown>')!r} "
                        f"explicit_target={explicit_target} "
                        f"target_repo={'set' if has_target_repo else 'unset'}. "
                        "Run `mozyo-bridge` from inside the receiver's tmux "
                        "session, pass an explicit pane id together with "
                        "--target-repo, or use `--mode standard`."
                    )
                    raise AssertionError("unreachable")

        # Cross-Workspace Handoff Gate (Redmine #10332).
        #
        # When the resolved target lives in a different tmux session from the
        # sender, ``--to claude`` is rejected at the CLI. The cross-workspace
        # path must route through the target session's Codex window so the
        # target workspace's audit boundary is preserved; an origin Codex typing
        # directly into another workspace's Claude pane bypasses that boundary.
        #
        # Same-session ``--to claude`` is unaffected (existing window-only
        # resolver). Cross-session ``--to codex`` is the explicit gateway path.
        # When the sender is outside tmux (`sender_session` is None) the check
        # is skipped because we cannot prove cross-session intent; the
        # queue-enter rail's own session check below still applies in that
        # mode. The optional ``--target-repo`` check below adds repo-mismatch
        # fail-closed on top of this gate.
        # Redmine #13261: the cross-session `--to claude` gate compares tmux session
        # names. Under the herdr backend there is no tmux session (sender_session_xw is
        # empty), so the gate below is an explicit no-op — the herdr target's audit
        # boundary is enforced by the workspace-scoped inventory decode, not tmux-session
        # membership.
        sender_session_xw = "" if herdr_send else (ops.current_session_name() or "")
        target_location_xw = target_info.get("location") or ""
        target_session_xw = (
            target_location_xw.split(":", 1)[0] if ":" in target_location_xw else ""
        )
        if (
            sender_session_xw
            and target_session_xw
            and sender_session_xw != target_session_xw
            and receiver == "claude"
        ):
            self._emit_blocked(request, reason="cross_session_claude")
            # Diagnostics only (Redmine #11776): point the operator at the safe
            # Codex gateway route with concrete candidate pane(s). Best-effort —
            # any discovery failure falls back to the boundary message unchanged,
            # and the cross-session `--to claude` block itself is untouched.
            gateway_hint = ops.cross_session_gateway_diagnostic(target_session_xw)
            ops.die(
                "cross-session handoff to Claude is not allowed; "
                f"sender_session={sender_session_xw!r} target_session={target_session_xw!r}. "
                "Naming a foreign workspace's Claude pane directly bypasses its "
                "audit boundary. Route through the target session's Codex window "
                "with `--to codex --target <target_session>:codex --target-repo "
                "<target_workspace_root>` and ask that Codex to perform the local "
                "Claude handoff. With an explicit --target and a passing "
                "--target-repo identity gate, that gateway send is admitted on the "
                "default queue-enter rail (Redmine #11301); `--mode standard` (or "
                "`--mode pending`) remains an available fallback, e.g. when you "
                "cannot assert --target-repo. See the Cross-Workspace Handoff rule "
                "in the agent workflow."
                + (f"\n\n{gateway_hint}" if gateway_hint else "")
            )
            raise AssertionError("unreachable")

        # Gateway Route Enforcement Gate (Redmine #12918): fail closed when a governed
        # implementation_request / review_result is sent `--to claude` directly to a
        # worker in a different lane than the sender, bypassing that lane's Codex
        # gateway. The whole gate (policy + emit + die) lives in the f_140
        # `application/gateway_route_gate` seam so this oversized module keeps only the
        # one call.
        # Redmine #13261 (increment 4): under the herdr backend resolve the sender lane
        # Unit from the env-derived SenderIdentity (already resolved for the target above)
        # so the gate enforces on the env sender lane and makes ZERO tmux calls; under tmux
        # `sender_lane_unit` is None and the gate keeps its `current_pane_lane_unit()` path
        # byte-identical.
        herdr_sender_lane_unit = (
            (target_info.get("herdr_sender_workspace_id"), target_info.get("herdr_sender_lane_id"))
            if herdr_send
            else None
        )
        ops.enforce_gateway_route(
            kind=kind,
            receiver=receiver,
            preflight_target=preflight_target,
            source=request.source,
            mode=mode,
            anchor=request.anchor,
            target=target,
            record_format=request.record_format,
            record_command=request.record_command,
            allow_direct_worker=request.allow_direct_worker,
            sender_lane_unit=herdr_sender_lane_unit,
        )

        expected_target_repo = request.resolved_target_repo
        if expected_target_repo:
            expected_resolved = str(Path(expected_target_repo).expanduser().resolve())
            # Prefer the real Git worktree root over a nested project-local scaffold
            # marker (Redmine #12658 j#66504) so a target pane inside a monorepo
            # project subdir (which may carry its own `.mozyo-bridge/scaffold.json`)
            # still gates against the Git repo root — otherwise an explicit
            # `--target-repo <Git root>` would fail closed before the project gate can
            # run. Non-git scaffold workspaces still fall back to the marker resolver.
            observed_repo = ops.resolve_workspace_root(target_info.get("cwd") or "")
            # Identity comparison goes through the shared Unicode normalization
            # (Redmine #11625): an NFC-spelled --target-repo must match an NFD
            # pane cwd for the same directory instead of fail-closing on bytes.
            if observed_repo is None or normalize_path_unicode(
                observed_repo
            ) != normalize_path_unicode(expected_resolved):
                self._emit_blocked(request, reason="target_repo_mismatch")
                if observed_repo is None:
                    # Identity could not be established at all: the target cwd does
                    # not walk up to any git / pyproject / scaffold marker. Keep
                    # fail-closed, but hand back a concrete setup action instead of
                    # forcing the operator to reason about repo-root heuristics.
                    setup_hint = (
                        "the target workspace has no identity marker reachable "
                        f"from target_cwd={(target_info.get('cwd') or '<unknown>')!r}. "
                        "For a non-git workspace, scaffold it so it carries "
                        "`.mozyo-bridge/scaffold.json` (run `mozyo-bridge scaffold "
                        f"apply <preset> --target {expected_resolved}`), then retry. "
                        "Or drop `--target-repo` to skip the check."
                    )
                else:
                    setup_hint = (
                        f"target pane resolves to repo root {observed_repo!r}. "
                        "Pass a target pane whose cwd resolves under the expected "
                        "repo root, or drop `--target-repo` to skip the check."
                    )
                ops.die(
                    "target pane is not in the expected repo; "
                    f"expected={expected_resolved!r} "
                    f"observed={(observed_repo or '<unknown>')!r} "
                    f"target_cwd={(target_info.get('cwd') or '<unknown>')!r}. "
                    + setup_hint
                )
                raise AssertionError("unreachable")

        # Project-Scope Handoff Gate (Redmine #12658). LAYERED ON TOP of the Git
        # `--target-repo` gate above, never replacing it: the repo gate stays the
        # fail-closed Git-repo-root identity check, and this adds an additional
        # constraint that the target resolve to a specific adopted project scope. A
        # target in the correct Git repository but OUTSIDE the expected project path
        # fails closed here. `--target-repo auto` is not repurposed to resolve project
        # paths (it still gates on the Git repo root); the project scope is derived
        # separately from the target pane's cwd via the bounded project discovery, or
        # read from a stamped `@mozyo_project_scope` pane option when present.
        expected_project = request.target_project
        if expected_project:
            target_cwd = target_info.get("cwd") or ""
            # Project scope is layered UNDER the Git repo identity and is never a
            # substitute for repo preflight (Redmine #12658 review j#66481 blocker 2):
            # `--target-project` requires an explicit `--target-repo` (incl. `auto`)
            # gate so the same adopted project id in an unrelated repo can never become
            # the sole identity gate. `--target-repo` has already been validated above
            # when present.
            if send_semantic_gap(
                target_project=expected_project, target_repo=expected_target_repo
            ) == SEND_SEMANTIC_PROJECT_REPO:
                self._emit_blocked(request, reason="invalid_args")
                ops.die(
                    "`--target-project` requires an explicit `--target-repo` "
                    "(or `--target-repo auto`) Git-repo gate; project scope is layered "
                    "under workspace identity and must not be the sole identity gate. "
                    f"target_project={expected_project!r} was given without "
                    "`--target-repo`. Add `--target-repo <root>` / `--target-repo auto`."
                )
                raise AssertionError("unreachable")

            observed_scope: Optional[str] = None
            observed_path: Optional[str] = None
            # Default to the explicit repo gate value so the fail-closed die() message
            # below always has a concrete git_repo_root, even if discovery raises.
            git_root = str(Path(expected_target_repo).expanduser().resolve())
            try:
                # The project path is repo-relative to the real Git worktree root, so
                # the stamped cwd-under-project check resolves the Git root (preferring
                # it over a nested project-local scaffold marker, #12658 j#66499). The
                # repo gate above already enforced `--target-repo`.
                git_root = ops.resolve_workspace_root(target_cwd) or git_root
                stamped_scope = (target_info.get("project_scope") or "").strip()
                stamped_path = (target_info.get("project_path") or "").strip()
                # A stamped pane option is a projection cache, not authority: it is only
                # trusted when the pane's cwd is actually under the stamped project path
                # within the verified Git repo root (Redmine #12658 review j#66481
                # blocker 1) — a stale / wrong option can never bypass the
                # cwd-under-project condition. Otherwise (or on no stamp) the scope is
                # re-derived from the live project.yaml sources, which is itself
                # cwd-under-project by construction and fail-closes on cache drift.
                if stamped_scope and stamped_path and path_under_repo_relative(
                    target_cwd, repo_root=git_root, project_path=stamped_path
                ):
                    observed_scope = stamped_scope
                    observed_path = stamped_path
                else:
                    resolved = ops.project_scope_for_cwd(target_cwd, git_root)
                    if resolved is not None:
                        observed_scope = resolved.scope
                        observed_path = resolved.path
            except Exception:  # noqa: BLE001 - fail closed below on any discovery error
                observed_scope = None
                observed_path = None

            if observed_scope != expected_project:
                self._emit_blocked(request, reason="target_project_mismatch")
                ops.die(
                    "target pane is not in the expected project scope; "
                    f"expected_project={expected_project!r} "
                    f"observed_project={(observed_scope or '<none>')!r} "
                    f"observed_project_path={(observed_path or '<none>')!r} "
                    f"git_repo_root={(git_root or '<unknown>')!r} "
                    f"target_cwd={(target_cwd or '<unknown>')!r}. "
                    "The target must be inside the expected adopted project (its cwd "
                    "under the project path) with a passing Git repo gate. A target in "
                    "the correct repo but outside the project path fails closed. Pass "
                    "a pane whose cwd is under the project, ensure the project carries "
                    "a `runtime_identity.enabled: true` opt-in, or drop "
                    "`--target-project` to gate on the Git repo root only."
                )
                raise AssertionError("unreachable")

        # Step 11 (v0.5, Redmine #12597): standard_target_admission. Replaces the
        # v0.3 unconditional active-split fail-closed. tmux delivers keystrokes to
        # the pane addressed by `-t` even when it is an inactive split, so the old
        # gate's concern was visibility (the receiver agent is, by construction, not
        # the foreground process the operator is looking at), not deliverability.
        # The owner (j#65493) judged the hard block over-strict: an inactive
        # registered agent pane that passes the minimal admission contract (live
        # pane + strong role match + workspace_id + unambiguous target) is now
        # *activated* by the rail (via `tmux select-pane` — pane selection only,
        # never raw key injection) and delivered to, with the active化 / restore
        # facts recorded in the durable record. `lane_id` / the Step 12 foreground
        # allowlist / repo-cwd checks stay as additional hardening, not minimal
        # admission conditions, so a git-less / non-scaffolded unit is not broken.
        # The policy is config-driven through the single
        # `resolve_standard_target_admission_policy` seam (constants + optional CLI
        # overrides), not scattered per caller/wrapper.
        admission_policy = resolve_standard_target_admission_policy(
            activate_inactive=(
                False if request.no_target_activation else None
            ),
            restore_previous_active=(
                True if request.restore_previous_active else None
            ),
        )
        admission = evaluate_standard_target_admission(
            target_info, receiver=receiver, preflight=preflight_target
        )
        activate_inactive_target = False
        if mode == MODE_QUEUE_ENTER and target_info.get("pane_active") != "1":
            if admission.admitted and admission_policy.activate_inactive:
                # Admitted inactive split: defer the actual `select-pane` until just
                # before typing (after the remaining die-able gates) so we never
                # steal focus for a send that then fails a later gate.
                activate_inactive_target = True
            else:
                observed_active = target_info.get("pane_active") or "<unknown>"
                # Concrete strict-rail recovery (Redmine #12162). `target` is the
                # already-resolved pane id (an explicit `%pane`), so `--target-repo
                # auto` can pin its identity, and the command carries the same
                # receiver / source / kind / anchor.
                recovery_command = build_inactive_pane_fallback_command(
                    receiver=receiver,
                    kind=kind,
                    target=target,
                    anchor=request.anchor,
                )
                self._emit_blocked(
                    request, reason="invalid_args", recovery_command=recovery_command
                )
                if not admission_policy.activate_inactive:
                    reason_clause = (
                        "target-pane activation is disabled by policy "
                        "(--no-target-activation), so an inactive split stays "
                        "fail-closed exactly like the pre-#12597 active-split gate"
                    )
                else:
                    reason_clause = (
                        "standard_target_admission did not admit the inactive "
                        "split; unmet minimal conditions: "
                        f"{', '.join(admission.unmet_conditions()) or '—'} "
                        "(register the workspace so the pane carries a workspace_id, "
                        "or use a pane that resolves strongly to the receiver)"
                    )
                if recovery_command:
                    fallback_hint = (
                        " The safest retry is the strict rail, which does not "
                        "require the receiver pane to be the active split (it "
                        f"observes the landing marker instead): `{recovery_command}`"
                    )
                else:
                    fallback_hint = (
                        " As a fallback you can pin the pane and re-check identity "
                        "with `--target %pane --target-repo auto` and retry under "
                        "`--mode standard`, which does not require the active split."
                    )
                ops.die(
                    "--mode queue-enter requires the target pane to be the active "
                    "split of its window or to pass standard_target_admission; pane "
                    f"{target} has pane_active={observed_active!r} and "
                    f"{reason_clause}. Activate the receiver pane in tmux, or drop "
                    "--target to use window-name resolution." + fallback_hint
                )
                raise AssertionError("unreachable")

        if mode == MODE_QUEUE_ENTER:
            # Step 12 (v0.3): per-receiver foreground process binding. Stricter
            # than the generic `ensure_agent_target` agent gate. Literal basenames
            # (`claude` / `node` for `claude`, `codex` for `codex`) give strong
            # receiver identity. Versioned native binary basenames give only weak
            # identity (receiver-agnostic regex) — see
            # `is_receiver_agent_process` and Open Question 8 in the contract.
            # The CLI does not advertise the weak case as strong; it just admits
            # under Step 9 + Layer A discipline.
            pane_command = target_info.get("command") or ""
            if not is_receiver_agent_process(pane_command, receiver):
                observed_command = Path(pane_command).name or "<none>"
                self._emit_blocked(request, reason="target_not_agent")
                ops.die(
                    "--mode queue-enter requires the foreground process to match "
                    f"the {receiver} agent; pane {target} has process "
                    f"{observed_command!r}. Restart the receiver agent in the "
                    "pane, or pass a pane that is running the agent."
                )
                raise AssertionError("unreachable")
        else:
            try:
                ops.ensure_agent_target(target_info, receiver, force=request.force)
            except SystemExit:
                self._emit_blocked(request, reason="target_not_agent")
                raise

        return AdmissionPipelineResult(
            activate_inactive_target=activate_inactive_target,
            admission_policy=admission_policy,
        )


class LiveAdmissionPipelineOps:
    """Live :class:`AdmissionPipelineOps`.

    Routes the two monkeypatched seams (``current_session_name`` / ``ensure_agent_target``) through
    the :mod:`commands` module *at call time* so those seams keep intercepting unchanged, and
    imports every other effect directly from the f_110 / f_140 / f_120 module the inline block
    imported it from (none of those are ``commands.*`` monkeypatch seams), so no import cycle is
    introduced (``commands`` imports this module at module load; this module imports ``commands``
    only lazily inside the two routed methods). The emit closure is the facade's per-call publishing
    emitter, injected at construction so publication stays a property of emitting (Redmine #13583
    R3-F1), and the gateway-route gate is handed that same emitter.
    """

    def __init__(self, emit: PublishingEmitter) -> None:
        self._emit = emit

    def current_session_name(self) -> Optional[str]:
        from mozyo_bridge.application import commands as _commands

        return _commands.current_session_name()

    def main_lane_guard_blocked(
        self,
        *,
        receiver: str,
        kind: Optional[str],
        preflight_target: PreflightTarget,
        has_main_lane_exception: bool,
    ) -> bool:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.main_lane_guard_gate import (
            main_lane_guard_blocked,
        )

        return main_lane_guard_blocked(
            receiver=receiver,
            kind=kind,
            preflight_target=preflight_target,
            has_main_lane_exception=has_main_lane_exception,
        )

    def enforce_gateway_route(
        self,
        *,
        kind: Optional[str],
        receiver: str,
        preflight_target: PreflightTarget,
        source: str,
        mode: str,
        anchor: Optional[NormalizedAnchor],
        target: str,
        record_format: str,
        record_command: Optional[str],
        allow_direct_worker: bool,
        sender_lane_unit: Optional[Tuple[Optional[str], Optional[str]]],
    ) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.gateway_route_gate import (
            enforce_gateway_route,
        )

        enforce_gateway_route(
            kind=kind,
            receiver=receiver,
            preflight_target=preflight_target,
            source=source,
            mode=mode,
            anchor=anchor,
            target=target,
            record_format=record_format,
            record_command=record_command,
            emit=self._emit,
            allow_direct_worker=allow_direct_worker,
            sender_lane_unit=sender_lane_unit,
        )

    def resolve_workspace_root(self, cwd: str) -> Optional[str]:
        from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.project_discovery import (
            resolve_workspace_root as _resolve_workspace_root,
        )

        return _resolve_workspace_root(cwd)

    def project_scope_for_cwd(
        self, cwd: str, git_root: str
    ) -> Optional[ProjectScope]:
        from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.project_discovery import (
            project_scope_for_cwd as _project_scope_for_cwd,
        )

        return _project_scope_for_cwd(cwd, git_root)

    def cross_session_gateway_diagnostic(self, target_session: str) -> str:
        # Best-effort: point the operator at the safe Codex gateway route with concrete
        # candidate pane(s). `pane_lines()` calls `die()` (SystemExit) when tmux is absent,
        # so catch SystemExit too — a diagnostics failure must never pre-empt the
        # `cross_session_claude` boundary message that must be the command's terminal
        # output (Redmine #11778).
        try:
            from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain import pane_resolver as _pr
            from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
                codex_gateway_candidates,
            )
            from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
                cross_session_gateway_hint,
            )

            _cands = [
                rec.to_dict()
                for rec in codex_gateway_candidates(target_session, _pr.pane_lines())
            ]
            return cross_session_gateway_hint(target_session, _cands)
        except (Exception, SystemExit):
            return ""

    def ensure_agent_target(
        self, target_info: Dict[str, str], receiver: str, *, force: bool
    ) -> None:
        from mozyo_bridge.application import commands as _commands

        _commands.ensure_agent_target(target_info, receiver, force=force)

    def emit(
        self,
        outcome: DeliveryOutcome,
        *,
        record_format: str,
        command: Optional[str],
        recovery_command: Optional[str] = None,
    ) -> None:
        self._emit(
            outcome,
            record_format=record_format,
            command=command,
            recovery_command=recovery_command,
        )

    def die(self, message: str) -> None:
        from mozyo_bridge.application import commands as _commands

        _commands.die(message)


def run_admission_pipeline(
    request: AdmissionPipelineRequest, *, emit: PublishingEmitter
) -> AdmissionPipelineResult:
    """Live composition root: run the handoff admission pipeline for the facade.

    Constructs :class:`AdmissionPipelineUseCase` over :class:`LiveAdmissionPipelineOps` (the two
    monkeypatched seams routed through ``commands`` at call time, every other effect imported
    directly) and runs the slice, exactly as the original inline block did. ``emit`` is the facade's
    per-call publishing emitter (also handed to the gateway-route gate).
    """
    return AdmissionPipelineUseCase(LiveAdmissionPipelineOps(emit=emit)).execute(request)


__all__ = (
    "PublishingEmitter",
    "AdmissionPipelineRequest",
    "AdmissionPipelineResult",
    "AdmissionPipelineOps",
    "AdmissionPipelineUseCase",
    "LiveAdmissionPipelineOps",
    "run_admission_pipeline",
)
