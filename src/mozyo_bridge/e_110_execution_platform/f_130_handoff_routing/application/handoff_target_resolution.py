"""Handoff target-resolution preflight (Redmine #13729 tranche 5).

The ``orchestrate_handoff`` preflight in ``application/commands.py`` historically carried the
**target-resolution slice** inline: after the anchor/profile envelope is planned but before any
admission / binding gate runs, it resolves the send target onto a concrete pane record and the
canonical preflight vocabulary. That slice is one coherent step:

- resolve ``target_info`` — under the herdr backend against the live herdr inventory scoped by the
  launch-time sender identity (fail-closed to a ``target_unavailable`` / ``invalid_args`` blocked
  outcome, never a silent tmux fallback); otherwise against the tmux pane resolver, and on a
  resolver ``SystemExit`` emit ``target_unavailable``, print the best-effort ``<session>:codex``
  gateway diagnostic, and re-raise;
- project the locator ``target = target_info["id"]``;
- surface any live same-lane receiver-duplicate panes for the durable record (diagnostic only, an
  explicit no-op under herdr, and a snapshot-read failure never changes delivery);
- resolve ``--target-repo auto`` — herdr resolves to the sender's own repo root; tmux infers the
  repo root from the explicitly-named ``%pane``'s own cwd (fail-closed to ``invalid_args`` when the
  target is not an explicit ``%pane``, or ``target_repo_mismatch`` when the cwd reaches no
  workspace/repo marker); a hand-passed ``--target-repo <root>`` and a non-auto value pass through
  untouched;
- project the resolved pane onto the canonical ``PreflightTarget`` identity vocabulary
  (``project_preflight_target``), the same projection ``agents targets`` uses.

This module carves that slice into an OOP-first application use case under #12638 / #13729, the
target-resolution sibling of the herdr rail (tranche 3) and the common tmux transport rail
(tranche 4), **without** touching the anchor/profile envelope planner above it, the main-lane /
receiver-binding / session / cross-workspace / target-repo-mismatch admission gates below it, or
the transport rails after them:

- :class:`TargetResolutionRequest` is the frozen typed input — the facade-resolved ``repo_root``
  and ``herdr_send`` backend predicate, the raw ``target`` / ``target_repo`` / ``target_lane``
  scalars the resolvers read, the ``receiver``, the terminal-outcome context (``anchor`` / ``mode``
  / ``kind`` / ``source`` / ``record_format`` / ``record_command``), and the seed
  ``resolved_target_repo`` (the initial parsed ``--target-repo`` value, possibly the ``auto``
  sentinel).
- :class:`TargetResolutionResult` is the frozen typed output — everything the downstream facade
  reads from the slice: the resolved ``target_info`` pane record, the ``target`` locator, the
  ``duplicate_lane_panes`` diagnostic rows, the resolved ``resolved_target_repo``, and the
  canonical ``preflight_target`` projection. The facade never re-derives them and no gate reads a
  mutated Namespace attribute.
- :class:`TargetResolutionOps` is the port for the *side-effecting* dependencies the slice needs
  from its environment (resolve the herdr / tmux target, emit the ``<session>:codex`` gateway
  diagnostic, resolve the same-lane duplicate rows, resolve the herdr / cwd-inferred auto repo
  root, print the auto-resolution diagnostic, project the preflight target, emit the blocked
  outcome, ``die``), so :meth:`TargetResolutionUseCase.execute` is exercisable with a synthetic
  fake port and no live tmux / herdr / Redmine.
- :class:`TargetResolutionUseCase` holds the slice body: the herdr-vs-tmux resolution branch, the
  ``SystemExit`` diagnostic-then-re-raise boundary, the herdr no-op guards on the duplicate and
  auto steps, and the three ``--target-repo auto`` policy conditions (herdr self-root,
  explicit-``%pane`` requirement, cwd-must-reach-a-marker) live here as typed control flow over the
  injected effects.
- :class:`LiveTargetResolutionOps` routes every effect through the :mod:`commands` module *at call
  time* (``resolve_herdr_send_target`` / ``pane_info`` / ``herdr_auto_target_repo`` /
  ``project_preflight_target`` and the pane-resolver / project-discovery / diagnostic seams), so
  every ``commands.*`` and ``pane_resolver.*`` monkeypatch seam keeps intercepting the side effects
  unchanged and no import cycle is introduced (``commands`` imports this module at module load;
  this module imports ``commands`` only lazily inside the live adapter). The emit closure is the
  facade's per-call publishing emitter (``make_publishing_emitter``), injected through the
  constructor so publication stays a property of emitting (Redmine #13583 R3-F1).

The pure collaborators (:func:`make_outcome`, :func:`is_explicit_pane_target`, the
``AUTO_TARGET_REPO`` sentinel) are imported and called directly — they take no environment and are
already unit-covered — so the port stays scoped to the genuine side effects. The
:class:`HerdrSendEntryError` reason narrowing lives in the use case (it is policy, not an effect).
This is a pure, behavior-preserving restructuring: the resolved ``target_info`` / ``target`` /
``duplicate_lane_panes`` / ``resolved_target_repo`` / ``preflight_target``, the emitted blocked
outcomes, the printed diagnostics, the exit code, and every ``die`` message (and the re-raised
tmux ``SystemExit``) are byte-identical to the original inline block.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    PreflightTarget,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    AUTO_TARGET_REPO,
    DeliveryOutcome,
    NormalizedAnchor,
    is_explicit_pane_target,
    make_outcome,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_send_entry import (
    HerdrSendEntryError,
)


#: The per-call publishing emitter injected by the facade (``make_publishing_emitter``):
#: ``emit(outcome, record_format=..., command=...)`` — publishes then renders the delivery outcome.
PublishingEmitter = Callable[..., None]


@dataclass(frozen=True)
class TargetResolutionRequest:
    """The typed input for the handoff target-resolution preflight slice.

    Every field is the value the original inline block read from an ``orchestrate_handoff`` local:
    ``repo_root`` / ``herdr_send`` select and seed the resolvers; ``target`` / ``target_repo`` /
    ``target_lane`` are the raw resolver scalars; ``receiver`` is the resolved receiver; ``anchor``
    / ``mode`` / ``kind`` / ``source`` / ``record_format`` / ``record_command`` are the
    terminal-outcome context threaded onto every blocked outcome; ``resolved_target_repo`` is the
    seed ``--target-repo`` value (possibly the ``auto`` sentinel) the auto step resolves. Frozen:
    the slice never mutates its input — it returns the resolved values in
    :class:`TargetResolutionResult` instead of writing back onto a Namespace.
    """

    repo_root: Path
    target: Optional[str]
    target_repo: Optional[str]
    target_lane: Optional[str]
    receiver: str
    anchor: Optional[NormalizedAnchor]
    mode: str
    kind: Optional[str]
    source: str
    record_format: str
    record_command: Optional[str]
    resolved_target_repo: Optional[str]
    herdr_send: bool


@dataclass(frozen=True)
class TargetResolutionResult:
    """The typed output of the target-resolution preflight slice.

    Everything the downstream facade reads from the slice: the resolved ``target_info`` pane
    record, the ``target`` locator (``target_info["id"]``), the ``duplicate_lane_panes`` diagnostic
    rows, the resolved ``resolved_target_repo`` (the auto sentinel resolved to a concrete root, or
    the untouched hand-passed / non-auto value), and the canonical ``preflight_target`` projection.
    """

    target_info: Dict[str, str]
    target: str
    duplicate_lane_panes: List[str]
    resolved_target_repo: Optional[str]
    preflight_target: PreflightTarget


class TargetResolutionOps(Protocol):
    """Port: the side-effecting dependencies the target-resolution preflight slice needs.

    The pure collaborators (:func:`make_outcome`, :func:`is_explicit_pane_target`, the
    ``AUTO_TARGET_REPO`` sentinel) are NOT here — the use case calls them directly. Only the genuine
    side effects are ported so the slice is exercisable with a synthetic fake that records the
    calls.
    """

    def resolve_herdr_send_target(
        self,
        *,
        repo_root: Path,
        target: Optional[str],
        target_repo: Optional[str],
        target_lane: Optional[str],
        receiver: str,
    ) -> Dict[str, str]:
        """Resolve the herdr-native send target + synthesize its pane record (raises on failure)."""
        ...

    def pane_info(self, target_arg: str) -> Dict[str, str]:
        """Resolve the tmux pane record for ``target_arg`` (raises ``SystemExit`` on failure)."""
        ...

    def emit_codex_diagnostic(self, target_arg: str) -> None:
        """Best-effort ``<session>:codex`` gateway candidate diagnostic (fully swallowed)."""
        ...

    def resolve_duplicate_lane_panes(
        self, target_info: Dict[str, str], receiver: str
    ) -> List[str]:
        """Live same-lane receiver-duplicate rows (diagnostic; a snapshot-read failure -> [])."""
        ...

    def herdr_auto_target_repo(self, repo_root: Path) -> str:
        """Resolve ``--target-repo auto`` for a herdr send to the sender's own repo root."""
        ...

    def resolve_workspace_root(self, cwd: str) -> Optional[str]:
        """Walk ``cwd`` up to a workspace/repo marker root, or ``None`` when unestablished."""
        ...

    def print_auto_repo_diagnostic(
        self, *, target: str, cwd: str, root: str
    ) -> None:
        """Print the ``--target-repo auto`` resolved-root stderr audit line."""
        ...

    def project_preflight_target(self, target_info: Dict[str, str]) -> PreflightTarget:
        """Project the resolved pane onto the canonical ``PreflightTarget`` identity vocabulary."""
        ...

    def emit(
        self,
        outcome: DeliveryOutcome,
        *,
        record_format: str,
        command: Optional[str],
    ) -> None:
        """Emit (publish + render) the terminal blocked delivery outcome."""
        ...

    def die(self, message: str) -> None:
        """Terminate the send with a non-zero exit and ``message`` (raises)."""
        ...


class TargetResolutionUseCase:
    """The handoff target-resolution preflight slice.

    Resolves the send target onto a concrete pane record + the canonical preflight vocabulary:
    the herdr / tmux resolution branch, the tmux ``SystemExit`` diagnostic-then-re-raise boundary,
    the herdr no-op guards on the duplicate + auto steps, and the three ``--target-repo auto``
    policy conditions live here as typed control flow over the injected effects. Returns the typed
    result on success; every failure path emits a blocked outcome and ``die``\\ s (or re-raises the
    tmux resolver ``SystemExit``) without falling through.
    """

    def __init__(self, ops: TargetResolutionOps) -> None:
        self._ops = ops

    def _emit_blocked(
        self,
        request: TargetResolutionRequest,
        *,
        reason: str,
        target: Optional[str],
    ) -> None:
        """Emit a terminal blocked :class:`DeliveryOutcome` from the request context.

        The context threading (receiver / target / anchor / mode / kind / source) is identical
        across every preflight blocked terminal; only ``reason`` and ``target`` differ. ``reason``
        is a wire-literal constant re-narrowed to the ``Reason`` wire enum by :func:`make_outcome`'s
        signature.
        """
        self._ops.emit(
            make_outcome(
                status="blocked",
                reason=reason,  # type: ignore[arg-type]
                receiver=request.receiver,
                target=target,
                anchor=request.anchor,
                mode=request.mode,
                kind=request.kind,
                notification_marker=None,
                source=request.source,
            ),
            record_format=request.record_format,
            command=request.record_command,
        )

    def execute(self, request: TargetResolutionRequest) -> TargetResolutionResult:
        ops = self._ops
        if request.herdr_send:
            # Redmine #13261 (increment 2): pure-herdr target resolution. There is no tmux pane to
            # read, so resolve the receiver against the live herdr inventory scoped by the
            # launch-time sender identity (env + anchor) and synthesize a
            # `project_preflight_target`-compatible pane record whose `id` is the live herdr
            # locator. Fail-closed (un-attested sender / unavailable inventory / no single live
            # agent) emits a `target_unavailable` blocked outcome and dies — never a silent tmux
            # fallback.
            try:
                target_info = ops.resolve_herdr_send_target(
                    repo_root=request.repo_root,
                    target=request.target,
                    target_repo=request.target_repo,
                    target_lane=request.target_lane,
                    receiver=request.receiver,
                )
            except HerdrSendEntryError as exc:
                self._emit_blocked(
                    request,
                    reason=("invalid_args" if exc.reason == "invalid_args" else "target_unavailable"),  # #13884
                    target=None,
                )
                ops.die(str(exc))
                raise AssertionError("unreachable")
        else:
            target_arg = request.target or request.receiver
            try:
                target_info = ops.pane_info(target_arg)
            except SystemExit:
                self._emit_blocked(request, reason="target_unavailable", target=None)
                # Diagnostics only (Redmine #11776): when a `<session>:codex` gateway location
                # fails to resolve, distinguish exact tmux window-name resolution from inventory
                # agent_kind classification and list the session's Codex-like candidate panes.
                # Best-effort and additive — the original resolver failure (already printed) and
                # the blocked outcome are unchanged.
                ops.emit_codex_diagnostic(target_arg)
                raise

        target = target_info["id"]

        # Redmine #12229: surface duplicate same-lane receiver panes in the durable record so the
        # receiver pane and any stale-input duplicate stay both visible and the receiver/actor
        # record cannot silently diverge (a cockpit gateway repair can leave two same-lane Claude
        # panes, #12226 j#61213). This reads a LIVE tmux snapshot at action time
        # (`vibes/docs/logics/runtime-observability-boundary.md`), never a stored projection. It is
        # strictly diagnostic and best-effort: it never blocks the send and never replaces an
        # outcome (an explicit `--target %pane` is the documented escape hatch, and queue-enter's
        # Step 11 active-split gate already fail-closes the inactive duplicate). A snapshot read
        # failure must not change delivery, so it is swallowed to an empty list.
        #
        # Redmine #13261: this same-lane-duplicate diagnostic reads a LIVE tmux pane snapshot, which
        # has no meaning in a pure herdr session — so it is an explicit no-op under the herdr
        # backend (an empty list), not a swallowed tmux-absence error. herdr identity uniqueness is
        # enforced upstream by the assigned-name decode (a duplicate assigned name fails closed).
        duplicate_lane_panes: List[str] = []
        if not request.herdr_send:
            duplicate_lane_panes = ops.resolve_duplicate_lane_panes(
                target_info, request.receiver
            )

        # `--target-repo auto` (Redmine #11778): resolve the cross-workspace identity gate from the
        # explicitly-named pane's own cwd so the operator does not hand-run
        # `tmux display-message -p -t %pane '#{pane_current_path}'` before a safe gateway send.
        # Strictly limited to an explicit `%pane` target — never a receiver label, a
        # `session:window` location, or implicit discovery — and fail-closed when the pane cwd has
        # no inferable workspace/repo root. The resolved root then flows through the SAME
        # cross-session admission and `target_repo_mismatch` gates below (in the facade) as a
        # hand-passed `--target-repo <root>`; auto cannot weaken them.
        resolved_target_repo = request.resolved_target_repo
        if resolved_target_repo == AUTO_TARGET_REPO and request.herdr_send:
            # Redmine #13331 (j#73312 #2): herdr has no `%pane` to infer from, so `auto` resolves to
            # the sender's own repo root (the same-workspace target's repo). tmux `auto` is
            # untouched (guarded on `herdr_send`). See `herdr_auto_target_repo`.
            resolved_target_repo = ops.herdr_auto_target_repo(request.repo_root)
        elif resolved_target_repo == AUTO_TARGET_REPO:
            raw_target = request.target
            if not is_explicit_pane_target(raw_target):
                self._emit_blocked(request, reason="invalid_args", target=target)
                ops.die(
                    "`--target-repo auto` requires an explicit `%pane` target; "
                    f"target={(raw_target or '<receiver-window>')!r} is not a "
                    "`%pane` id. Auto never widens to receiver-label, "
                    "`session:window`, or discovery targets — name the exact pane, "
                    "or pass an explicit `--target-repo <root>`."
                )
                raise AssertionError("unreachable")
            auto_cwd = target_info.get("cwd") or ""
            # Prefer the real Git worktree root over a nested project-local scaffold marker
            # (Redmine #12658 j#66504): a target pane inside a monorepo project subdir that carries
            # its own `.mozyo-bridge/scaffold.json` must still resolve `--target-repo auto` to the
            # Git repo root, not the subdir, so the repo gate gates on the Git root as documented.
            # Non-git scaffold workspaces still fall back to the marker resolver (#11301).
            auto_root = ops.resolve_workspace_root(auto_cwd)
            if not auto_root:
                self._emit_blocked(
                    request, reason="target_repo_mismatch", target=target
                )
                ops.die(
                    "`--target-repo auto` could not infer a workspace/repo root "
                    f"from target_cwd={(auto_cwd or '<unknown>')!r}; identity "
                    "unestablished, fail-closed. Scaffold the target workspace so "
                    "it carries a `.mozyo-bridge/scaffold.json` / git marker, or "
                    "pass an explicit `--target-repo <root>`."
                )
                raise AssertionError("unreachable")
            # Diagnostics: record the resolved cwd and inferred root so the auto decision is
            # auditable, then hand the concrete root to the gates below.
            ops.print_auto_repo_diagnostic(
                target=target, cwd=auto_cwd, root=auto_root
            )
            resolved_target_repo = auto_root

        # Explicit-pane preflight projection (Redmine #11908): resolve the target pane onto the
        # canonical `TargetRecord` identity vocabulary (`vibes/docs/logics/unit-target-model.md`
        # "Resolver priority") via the same projection `agents targets` uses, so normal-local and
        # cockpit panes share one resolver. Pane option role/workspace/lane is primary; the window
        # name is a compatibility fallback (`role_source == window_name`); ambiguous / unknown is
        # surfaced for fail-closed handling below (in the facade).
        preflight_target = ops.project_preflight_target(target_info)

        return TargetResolutionResult(
            target_info=target_info,
            target=target,
            duplicate_lane_panes=duplicate_lane_panes,
            resolved_target_repo=resolved_target_repo,
            preflight_target=preflight_target,
        )


class LiveTargetResolutionOps:
    """Live :class:`TargetResolutionOps`.

    Every effect routes through the :mod:`commands` module *at call time*:
    ``resolve_herdr_send_target`` / ``pane_info`` / ``herdr_auto_target_repo`` /
    ``project_preflight_target`` and the pane-resolver / project-discovery / diagnostic seams.
    Resolving them from ``commands`` (and its lazily-imported collaborators) keeps every
    monkeypatch seam in force and introduces no import cycle. The emit closure is the facade's
    per-call publishing emitter, injected at construction so publication stays a property of
    emitting (Redmine #13583 R3-F1).
    """

    def __init__(self, emit: PublishingEmitter) -> None:
        self._emit = emit

    def resolve_herdr_send_target(
        self,
        *,
        repo_root: Path,
        target: Optional[str],
        target_repo: Optional[str],
        target_lane: Optional[str],
        receiver: str,
    ) -> Dict[str, str]:
        from mozyo_bridge.application import commands as _commands

        return _commands.resolve_herdr_send_target(
            repo_root=repo_root,
            target=target,
            target_repo=target_repo,
            target_lane=target_lane,
            receiver=receiver,
        )

    def pane_info(self, target_arg: str) -> Dict[str, str]:
        from mozyo_bridge.application import commands as _commands

        return _commands.pane_info(target_arg)

    def emit_codex_diagnostic(self, target_arg: str) -> None:
        # Best-effort and additive. `pane_lines()` calls `die()` (SystemExit) when tmux is absent,
        # so catch SystemExit too — a diagnostics failure must never replace the original
        # `target_unavailable` outcome (Redmine #11778).
        import sys

        try:
            if (
                ":" in target_arg
                and not target_arg.startswith("%")
                and target_arg.split(":", 1)[1].split(".", 1)[0] == "codex"
            ):
                from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain import pane_resolver as _pr
                from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
                    codex_gateway_candidates,
                )
                from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
                    target_unavailable_codex_diagnostic,
                )

                _sess = target_arg.split(":", 1)[0]
                _cands = [
                    rec.to_dict()
                    for rec in codex_gateway_candidates(_sess, _pr.pane_lines())
                ]
                _diag = target_unavailable_codex_diagnostic(_sess, "codex", _cands)
                print(_diag, file=sys.stderr)
        except (Exception, SystemExit):
            pass

    def resolve_duplicate_lane_panes(
        self, target_info: Dict[str, str], receiver: str
    ) -> List[str]:
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain import pane_resolver as _pr

        try:
            return [
                _pr.duplicate_pane_record_row(pane)
                for pane in _pr.same_lane_receiver_duplicates(
                    target_info, _pr.pane_lines(), receiver
                )
            ]
        except (Exception, SystemExit):
            return []

    def herdr_auto_target_repo(self, repo_root: Path) -> str:
        from mozyo_bridge.application import commands as _commands

        return _commands.herdr_auto_target_repo(repo_root)

    def resolve_workspace_root(self, cwd: str) -> Optional[str]:
        from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.project_discovery import (
            resolve_workspace_root as _resolve_workspace_root,
        )

        return _resolve_workspace_root(cwd)

    def print_auto_repo_diagnostic(
        self, *, target: str, cwd: str, root: str
    ) -> None:
        import sys

        print(
            f"--target-repo auto resolved: target_pane={target} "
            f"target_cwd={cwd!r} -> repo_root={root!r}",
            file=sys.stderr,
        )

    def project_preflight_target(self, target_info: Dict[str, str]) -> PreflightTarget:
        from mozyo_bridge.application import commands as _commands

        return _commands.project_preflight_target(target_info)

    def emit(
        self,
        outcome: DeliveryOutcome,
        *,
        record_format: str,
        command: Optional[str],
    ) -> None:
        self._emit(outcome, record_format=record_format, command=command)

    def die(self, message: str) -> None:
        from mozyo_bridge.application import commands as _commands

        _commands.die(message)


def run_target_resolution(
    request: TargetResolutionRequest, *, emit: PublishingEmitter
) -> TargetResolutionResult:
    """Live composition root: run the handoff target-resolution preflight for the facade.

    Constructs :class:`TargetResolutionUseCase` over :class:`LiveTargetResolutionOps` (every effect
    routed through ``commands`` at call time) and runs the slice, exactly as the original inline
    block did. ``emit`` is the facade's per-call publishing emitter.
    """
    return TargetResolutionUseCase(LiveTargetResolutionOps(emit=emit)).execute(request)


__all__ = (
    "PublishingEmitter",
    "TargetResolutionRequest",
    "TargetResolutionResult",
    "TargetResolutionOps",
    "TargetResolutionUseCase",
    "LiveTargetResolutionOps",
    "run_target_resolution",
)
