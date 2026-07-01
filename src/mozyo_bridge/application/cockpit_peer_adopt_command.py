"""Cockpit peer-adopt resolver boundary: pane runtime read + resolver + handler (#12978).

Four helpers historically lived as procedural bodies in
:mod:`mozyo_bridge.application.commands`, each mixing a *side effect* (a
read-only ``tmux display-message`` pane query, a registry / anchor cwd
resolution, or the confirm-gated apply) with *pure decision logic* (parsing the
tab-separated output, composing the fail-closed planner's
:class:`PeerAdoptCandidate` / :class:`PeerAdoptTarget` inputs, and selecting the
CLI preview / json / apply output):

- ``_read_cockpit_pane_runtime`` — read one cockpit pane's cwd / foreground
  process / lane label (#12133, read-only; tolerant of any tmux failure).
- ``_resolve_peer_adopt_candidate`` — resolve the role-less candidate pane's
  preflight facts, walking its live cwd through the registry -> anchor -> lane
  chain so the pure planner can fail-closed on a contradicting checkout / agent.
- ``_resolve_peer_adopt_target`` — build the destination :class:`PeerAdoptTarget`,
  mirroring the Unit's opposite-role peer's lane label onto the adopted pane.
- ``_handle_cockpit_peer_adopt`` — the ``mozyo cockpit peer-adopt`` handler:
  parse / validate the flags, read geometry, resolve candidate + target, run the
  fail-closed planner, and preview / json / confirm-gated apply.

This module carves that into an OOP-first boundary under #12638:

- The module-level ``project_pane_runtime`` / ``read_pane_runtime`` /
  ``missing_flags`` / ``split_peer_unit`` helpers are the pure projection /
  parsing: they own the tmux ``-F`` field template, the ``display-message`` line
  parse (byte-for-byte), the required-flag check, and the ``workspace/lane`` split,
  with no :mod:`commands` dependency.
- :class:`CockpitPeerAdoptOps` is the port for the side effects the use case
  needs — the geometry read, the pane runtime read seam, the cwd -> identity
  resolution, the ``require_tmux`` availability gate, the #12972
  ``execute_peer_adopt_plan`` executor, and the ``die`` fail-closed abort.
  :class:`LiveCockpitPeerAdoptOps` is the live adapter: it resolves every target
  *through the* :mod:`commands` *module at call time* (never at import), so the
  characterization tests that patch ``commands._read_cockpit_geometry`` /
  ``commands._read_cockpit_pane_runtime`` / ``commands.require_tmux`` /
  ``commands.run_tmux`` keep intercepting, and this module never imports
  :mod:`commands` at module scope (no import cycle).
- :class:`CockpitPeerAdoptUseCase` composes the port and the pure planner and
  returns a :class:`PeerAdoptOutcome` (exit code + rendered text or json payload).
  The thin ``_read_cockpit_pane_runtime`` / ``_resolve_peer_adopt_candidate`` /
  ``_resolve_peer_adopt_target`` / ``_handle_cockpit_peer_adopt`` wrappers in
  :mod:`commands` build the live ops and run the use case, preserving the seams
  the rest of the cockpit code (``_cockpit_unit_repo_root``) and the tests call.

Behavior-preserving: the read tolerance (any tmux failure -> empty runtime), the
unresolvable-cwd tolerance (empty ids = "unknown", never a contradiction), the
fail-closed guard order, the confirm-gated apply, and the ``cockpit peer-adopt``
CLI output + exit conventions are unchanged from the original command bodies.
This module reuses the #12972 ``execute_peer_adopt_plan`` executor; it never
reimplements executor semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


# --- Pure projection / parsing: field template, line parse, flag checks. -------

# The ``tmux display-message -F`` field template for :func:`read_pane_runtime`:
# the pane's live cwd, its foreground process, and its written lane label. Kept
# as a constant so the read and the tests share one source.
PANE_RUNTIME_FIELDS = (
    "#{pane_current_path}\t#{pane_current_command}\t#{@mozyo_lane_label}"
)


def project_pane_runtime(stdout: str) -> dict:
    """Parse a pane runtime read's tab-separated ``display-message`` output.

    Returns ``{cwd, process, lane_label}``; a short / empty line right-pads to
    three fields so a missing trailing field reads as ``""`` without
    ``IndexError``. Pure over the stdout string (no tmux dependency).
    """

    line = (stdout or "").splitlines()
    parts = ((line[0] if line else "").split("\t") + ["", "", ""])[:3]
    return {"cwd": parts[0], "process": parts[1], "lane_label": parts[2]}


def read_pane_runtime(run_tmux: Any, pane_id: str) -> dict:
    """Read one cockpit pane's cwd / foreground process / lane label (#12133).

    A tmux pane id (``%id``) is globally unique, so ``display-message`` targets it
    directly (no window qualifier). Tolerant: any tmux failure — a missing binary
    / server (raise) or a non-zero exit — degrades to empty facts so the planner
    treats them as "unknown" (never a fabricated match). ``run_tmux`` is the
    read-only tmux callable (the module-level ``run_tmux`` or a fake).
    """

    try:
        result = run_tmux(
            "display-message",
            "-p",
            "-t",
            pane_id,
            "-F",
            PANE_RUNTIME_FIELDS,
            check=False,
        )
    except (Exception, SystemExit):
        return {"cwd": "", "process": "", "lane_label": ""}
    if getattr(result, "returncode", 1) != 0:
        return {"cwd": "", "process": "", "lane_label": ""}
    return project_pane_runtime(getattr(result, "stdout", "") or "")


def missing_flags(pane_id: Any, unit_arg: Any, role: Any) -> list[str]:
    """The required ``peer-adopt`` flags that are absent, in flag order.

    ``--pane`` / ``--unit`` / ``--role`` are all mandatory; a falsy value (unset
    or empty) is reported. Pure over the raw arg values.
    """

    return [
        flag
        for flag, value in (("--pane", pane_id), ("--unit", unit_arg), ("--role", role))
        if not value
    ]


def split_peer_unit(unit_arg: str) -> tuple[str, str]:
    """Split a ``--unit`` token into ``(workspace_id, lane_token)``.

    ``workspace/lane`` splits on the last ``/`` (a workspace id never contains one
    in this position); a bare ``workspace`` yields an empty lane token (the caller
    normalizes it to the default lane). Pure over the token.
    """

    if "/" in unit_arg:
        workspace_id, lane_token = unit_arg.rsplit("/", 1)
    else:
        workspace_id, lane_token = unit_arg, ""
    return workspace_id, lane_token


# --- Outcome: the caller-facing result of the confirm-gated handler. -----------


@dataclass(frozen=True)
class PeerAdoptOutcome:
    """The result of :meth:`CockpitPeerAdoptUseCase.handle`.

    ``exit_code`` is the process exit status (``0`` applicable / applied, ``1``
    fail-closed blocked). Exactly one render channel is populated: ``json_payload``
    for ``--json`` (the thin wrapper dumps it), else ``text`` (the already-rendered
    multi-line human output, printed as-is). The die paths raise via the port and
    never return an outcome.
    """

    exit_code: int
    text: str = ""
    json_payload: dict | None = None


# --- Port + live adapter over the ``commands`` seams. -------------------------


@runtime_checkable
class CockpitPeerAdoptOps(Protocol):
    """Port: the side effects the peer-adopt use case needs from its environment.

    ``read_geometry`` is the read-only cockpit geometry read; ``read_pane_runtime``
    is the per-pane runtime read seam (candidate preflight + peer lane label);
    ``resolve_cwd_identity`` walks a live cwd through the registry -> anchor -> lane
    chain into ``(workspace_id, lane_id)``; ``require_tmux`` is the mutable-server
    availability gate (apply only); ``execute_peer_adopt`` runs the #12972 executor;
    ``die`` is the fail-closed abort. The live adapter routes every one through the
    :mod:`commands` module at call time, so the monkeypatched characterization tests
    still intercept and this module never imports :mod:`commands` at module scope.
    """

    def read_geometry(self, session: str) -> Any: ...

    def read_pane_runtime(self, session: str, pane_id: str) -> dict: ...

    def resolve_cwd_identity(self, cwd: str) -> tuple[str, str]: ...

    def require_tmux(self) -> None: ...

    def execute_peer_adopt(self, plan: Any) -> None: ...

    def die(self, message: str) -> None: ...


class LiveCockpitPeerAdoptOps:
    """Live :class:`CockpitPeerAdoptOps` over the real ``commands`` seams.

    Every method resolves its target *through the* :mod:`commands` *module at call
    time* rather than binding it at import time, so the tests that patch
    ``commands._read_cockpit_geometry`` / ``commands._read_cockpit_pane_runtime``
    (geometry + runtime feeds) / ``commands.require_tmux`` / ``commands.run_tmux``
    (the apply path) keep intercepting, and the #12972 ``execute_peer_adopt_plan``
    executor + ``commands.die`` abort are reached unchanged.
    """

    @staticmethod
    def _commands() -> Any:
        from mozyo_bridge.application import commands

        return commands

    def read_geometry(self, session: str) -> Any:
        return self._commands()._read_cockpit_geometry(session)

    def read_pane_runtime(self, session: str, pane_id: str) -> dict:
        return self._commands()._read_cockpit_pane_runtime(session, pane_id)

    def resolve_cwd_identity(self, cwd: str) -> tuple[str, str]:
        """Resolve a live cwd through the registry -> anchor -> lane chain (#12133).

        Only ids are carried forward — never the absolute path (privacy boundary).
        Tolerant: an unresolvable cwd (or any registry failure) yields empty ids
        ("unknown", not a contradiction), so the planner never fabricates a match.
        """

        from pathlib import Path

        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
            normalize_lane,
        )

        commands = self._commands()
        try:
            repo_root = str(Path(cwd).expanduser().resolve())
            canon = commands.resolve_canonical_session(repo_root)
            workspace_id = getattr(canon, "workspace_id", None) or ""
            lane_id = ""
            if workspace_id:
                lane = commands._resolve_workspace_lane(repo_root, workspace_id)
                lane_id = normalize_lane(getattr(lane, "lane_id", None))
            return workspace_id, lane_id
        except (Exception, SystemExit):
            return "", ""

    def require_tmux(self) -> None:
        return self._commands().require_tmux()

    def execute_peer_adopt(self, plan: Any) -> None:
        commands = self._commands()
        commands.execute_peer_adopt_plan(plan, commands.run_tmux)

    def die(self, message: str) -> None:
        return self._commands().die(message)


# --- Use case: compose the port + pure planner into the caller-facing shapes. --


class CockpitPeerAdoptUseCase:
    """Resolve + preview / apply a cockpit peer-adopt through the injected port.

    Composes the read seams, the cwd -> identity resolution, and the pure
    fail-closed planner (:func:`plan_peer_adopt`) into the resolver helpers
    (:meth:`resolve_candidate` / :meth:`resolve_target`) and the confirm-gated
    handler (:meth:`handle`). Byte-for-byte equivalent to the original command
    bodies; it reuses the #12972 executor via the port and never reimplements
    executor semantics.
    """

    def __init__(self, ops: CockpitPeerAdoptOps) -> None:
        self._ops = ops

    def resolve_candidate(self, session: str, pane_id: str):
        """Resolve the role-less candidate pane's preflight facts (#12133).

        Reads the candidate's live cwd / foreground process (via the runtime seam)
        and resolves the cwd through the port's identity chain, so the pure planner
        can fail-closed when the checkout / running agent contradicts the
        destination. An unresolvable cwd yields empty ids ("unknown", never a
        contradiction).
        """

        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_geometry import (
            PeerAdoptCandidate,
        )
        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
            ROLES,
        )

        runtime = self._ops.read_pane_runtime(session, pane_id)
        cwd = (runtime.get("cwd") or "").strip()
        process = (runtime.get("process") or "").strip()
        process_role = process if process in ROLES else ""

        cwd_workspace_id = ""
        cwd_lane_id = ""
        if cwd:
            cwd_workspace_id, cwd_lane_id = self._ops.resolve_cwd_identity(cwd)
        return PeerAdoptCandidate(
            pane_id=pane_id,
            cwd_workspace_id=cwd_workspace_id,
            cwd_lane_id=cwd_lane_id,
            process_role=process_role,
            process_name=process,
        )

    def resolve_target(self, session: str, diagnosis, workspace_id: str, lane_id: str, role: str):
        """Build the destination :class:`PeerAdoptTarget`, mirroring its peer (#12133).

        The lane label is read off the Unit's existing opposite-role peer pane so
        the adopted pane stamps the same human-facing lane label as its sibling;
        the display label defaults to the workspace id. When the Unit / peer cannot
        be found the planner blocks anyway, so missing metadata is harmless.
        """

        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_geometry import (
            PeerAdoptTarget,
        )
        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
            ROLE_CLAUDE,
            normalize_lane,
        )

        target_lane = normalize_lane(lane_id)
        lane_label = None
        unit = next(
            (
                u
                for u in diagnosis.units
                if u.workspace_id == workspace_id
                and normalize_lane(u.lane_id) == target_lane
            ),
            None,
        )
        if unit is not None:
            peer_panes = unit.codex_panes if role == ROLE_CLAUDE else unit.claude_panes
            if peer_panes:
                label = (
                    self._ops.read_pane_runtime(session, peer_panes[0]).get("lane_label") or ""
                ).strip()
                lane_label = label or None
        return PeerAdoptTarget(
            workspace_id=workspace_id,
            lane_id=target_lane,
            lane_label=lane_label,
            label=workspace_id,
        )

    def handle(
        self, session: str, args, *, json_output: bool, dry_run: bool
    ) -> PeerAdoptOutcome:
        """`mozyo cockpit peer-adopt` — bind a role-less pane as a Unit's peer (#12133).

        The first safe repair slice of US #12132: it adopts the role-less cockpit
        pane named by ``--pane`` as the ``--role`` peer of the Unit named by
        ``--unit workspace/lane``, binding that pane's identity options only — never
        a pane move / kill / split / rebalance. Fail-closed: the pure planner
        (:func:`plan_peer_adopt`) must clear every guard, and the mutation runs only
        with ``--confirm``. ``--dry-run`` / ``--json`` and a bare invocation preview
        without mutating and never gate on a mutable tmux server. Returns a
        :class:`PeerAdoptOutcome`: exit ``0`` when applicable (and applied, when
        confirmed); ``1`` when fail-closed blocked.
        """

        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_geometry import (
            diagnose_cockpit_geometry,
            format_peer_adopt_text,
            plan_peer_adopt,
        )
        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
            normalize_lane,
        )

        pane_id = getattr(args, "peer_pane", None)
        unit_arg = getattr(args, "peer_unit", None)
        role = getattr(args, "peer_role", None)
        confirm = bool(getattr(args, "confirm", False))

        missing = missing_flags(pane_id, unit_arg, role)
        if missing:
            self._ops.die(
                "cockpit peer-adopt requires "
                + ", ".join(missing)
                + " (e.g. `mozyo cockpit peer-adopt --pane %123 --unit video/default "
                "--role claude`)."
            )

        workspace_id, lane_token = split_peer_unit(unit_arg)
        workspace_id = workspace_id.strip()
        target_lane = normalize_lane(lane_token)
        if not workspace_id:
            self._ops.die("cockpit peer-adopt --unit needs a workspace id (e.g. `video/default`).")

        panes = self._ops.read_geometry(session)
        diagnosis = diagnose_cockpit_geometry(session=session, panes=panes)
        candidate = self.resolve_candidate(session, pane_id)
        target = self.resolve_target(session, diagnosis, workspace_id, target_lane, role)
        decision = plan_peer_adopt(
            diagnosis=diagnosis,
            target=target,
            pane_id=pane_id,
            role=role,
            candidate=candidate,
        )

        will_apply = decision.ok and confirm and not dry_run and not json_output

        if json_output:
            payload = decision.as_dict()
            payload["applied"] = False
            return PeerAdoptOutcome(exit_code=0 if decision.ok else 1, json_payload=payload)

        if not decision.ok:
            return PeerAdoptOutcome(exit_code=1, text=format_peer_adopt_text(decision))

        if not will_apply:
            lines = [format_peer_adopt_text(decision, applied=False)]
            if not confirm:
                lines.append(
                    "  (preview only — re-run with `--confirm` to bind the pane "
                    "identity.)"
                )
            return PeerAdoptOutcome(exit_code=0, text="\n".join(lines))

        self._ops.require_tmux()
        self._ops.execute_peer_adopt(decision.plan)
        lines = [
            format_peer_adopt_text(decision, applied=True),
            "  smoke: re-run `mozyo cockpit doctor-geometry` and `mozyo agents targets` "
            "to confirm the missing-peer / role-less finding is resolved.",
        ]
        return PeerAdoptOutcome(exit_code=0, text="\n".join(lines))
