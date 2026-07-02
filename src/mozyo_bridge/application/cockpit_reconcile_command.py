"""Cockpit reconcile boundary: identity projection + confirm-gated handler (#13008).

Two helpers historically lived as procedural bodies in
:mod:`mozyo_bridge.application.commands`, mixing the *side effects* (the live
``window_layout`` / pane-geometry reads, the tmux availability gate, the #12136
swap + relayout executor, the post-apply column re-read) with *pure decision /
rendering logic* (the pane-identity projection, the preview cell report, and
the preview / json / confirm output selection):

- ``_cockpit_pane_identity`` — read the live cockpit panes and project the
  ``{pane_id: {workspace_id, lane_id, role}}`` identity map the reconcile
  planner groups Units by (#12136; retired as a ``commands`` name by this
  carve — the projection lives here as :func:`project_pane_identity`).
- ``_handle_cockpit_reconcile`` — the ``mozyo cockpit reconcile`` handler:
  non-mutating preview / ``--json`` / ``--dry-run`` vs the explicit
  ``--confirm`` structural repair (order-preserving flatten of nested
  top-level cells into per-Unit columns).

This module carves that into an OOP-first boundary under #12638, following the
#12989 reset carve (``cockpit_reset_command.py``) and the #12987 adopt carve:

- :func:`project_pane_identity` / :func:`render_reconcile_preview_lines` are
  the pure projection / rendering over already-read panes and an already-built
  plan — no :mod:`commands` dependency.
- :class:`CockpitReconcileOps` is the port for the side effects the use case
  needs — the ``window_layout`` and pane-geometry reads, the tmux availability
  gate, the #12136 reconcile executor, the post-apply column re-read, the
  fail-closed abort, and the stdout line sink.
  :class:`LiveCockpitReconcileOps` resolves every target *through the*
  :mod:`commands` *module at call time* (never at import), so the reconcile
  characterization tests that patch ``commands._read_cockpit_window_layout`` /
  ``commands._read_cockpit_geometry`` / ``commands.require_tmux`` /
  ``commands.run_tmux`` keep intercepting, and no import cycle is introduced.
- :class:`CockpitReconcileUseCase` composes the port and the pure domain
  (``parse_window_layout`` / ``build_cockpit_reconcile_plan``) into the
  confirm-gated :meth:`handle`. Like the reset carve it renders through the
  port's ``emit`` sink (the flatten prints interleave with the executor run)
  and returns the plain ``int`` exit code (there is no attach tail; the die
  paths raise via the port and never return).

The thin ``_handle_cockpit_reconcile`` wrapper stays in :mod:`commands` with an
unchanged signature (the ``cmd_cockpit`` reconcile dispatch is preserved);
``_cockpit_pane_identity`` had no remaining callers and is retired.
Behavior-preserving: the fail-closed unparseable-layout refusal (never a
"clean" no-op, never a mutation), the preview / json / confirm output, the
benign absent-cockpit no-op, the ``--dry-run`` / ``--json`` precedence over
``--confirm``, and the post-apply rebalance hint are unchanged from the
original command bodies. This module reuses the #12136 executor via the port;
it never reimplements executor semantics and never builds plans itself.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable


# --- Pure projection / rendering over read panes and a built plan. -------------


def project_pane_identity(panes: Any) -> dict:
    """``{pane_id: {workspace_id, lane_id, role}}`` from listed cockpit panes (#12136).

    Pure projection over an already-read pane list (each pane a mapping with
    ``pane_id`` and the ``@mozyo_*`` identity fields) — the identity map the
    reconcile planner groups Units by. Panes without a ``pane_id`` are skipped;
    missing identity fields default to ``""``. A falsy pane list (absent
    cockpit window) yields ``{}``.
    """
    if not panes:
        return {}
    return {
        p["pane_id"]: {
            "workspace_id": p.get("workspace_id", ""),
            "lane_id": p.get("lane_id", ""),
            "role": p.get("role", ""),
        }
        for p in panes
        if p.get("pane_id")
    }


def render_reconcile_preview_lines(plan: Any, session: str) -> list:
    """The non-mutating ``mozyo cockpit reconcile`` preview lines (#12136).

    Pure renderer over the built plan — the exact lines the handler prints
    (indentation included), byte-for-byte the former preview branch of
    ``_handle_cockpit_reconcile``: the header, one line per top-level cell
    (with the tangled / unidentified annotations), then the blocked reason,
    the clean no-op note, or the target columns + tmux command plan with the
    ``--confirm`` hint.
    """
    import shlex as _shlex

    lines = [
        f"cockpit reconcile (preview; no tmux changes): session={session} "
        f"cells={plan.cell_count} units={len(plan.units_in_order)}"
    ]
    for cell in plan.cells:
        names = ", ".join(f"{ws}/{lane}" for ws, lane in cell.unit_keys) or "-"
        flag = " [tangled: >1 Unit in one cell]" if cell.tangled else ""
        extra = (
            f" unidentified={list(cell.unidentified_panes)}"
            if cell.unidentified_panes
            else ""
        )
        lines.append(f"  cell {cell.index} (x={cell.x} w={cell.width}): {names}{flag}{extra}")
    if plan.blocked_reason:
        lines.append(f"  cannot reconcile: {plan.blocked_reason}")
    elif plan.clean:
        lines.append("  already one Unit per top-level column — nothing to reconcile.")
    else:
        units = " | ".join(f"{ws}/{lane}" for ws, lane in plan.units_in_order)
        lines.append(
            f"  target Unit columns (left-to-right, order preserved): {units}"
        )
        lines.append(
            "  reconcile plan (swap-pane order fix + checksum select-layout; "
            "no pane killed, identity untouched):"
        )
        for cmd in plan.commands:
            lines.append(
                "    tmux " + " ".join(_shlex.quote(tok) for tok in cmd.argv)
            )
        lines.append("  run `mozyo cockpit reconcile --confirm` to apply.")
    return lines


# --- Port + live adapter over the ``commands`` seams. -------------------------


@runtime_checkable
class CockpitReconcileOps(Protocol):
    """Port: the side effects the cockpit-reconcile use case needs from its environment.

    ``read_window_layout`` / ``read_geometry`` feed the plan build (the layout
    tree + the pane identity); ``require_tmux`` gates the mutable path;
    ``execute_reconcile`` runs the #12136 swap + relayout executor;
    ``rebalance_columns`` is the post-apply top-level column re-read behind the
    "reconciled" summary; ``die`` is the fail-closed abort; ``emit`` is the
    stdout line sink (injected so a fake can capture the exact line
    interleaving around the executor run).
    """

    def read_window_layout(self, session: str) -> Optional[str]: ...

    def read_geometry(self, session: str) -> Any: ...

    def require_tmux(self) -> None: ...

    def execute_reconcile(self, plan: Any) -> None: ...

    def rebalance_columns(self, session: str) -> tuple: ...

    def die(self, message: str) -> None: ...

    def emit(self, text: str) -> None: ...


class LiveCockpitReconcileOps:
    """Live :class:`CockpitReconcileOps` over the real ``commands`` seams.

    Every method resolves its target *through the* :mod:`commands` *module at
    call time* rather than binding it at import time, so the reconcile tests
    that patch ``commands._read_cockpit_window_layout`` /
    ``commands._read_cockpit_geometry`` / ``commands.require_tmux`` /
    ``commands.run_tmux`` keep intercepting, and the #12136
    ``execute_cockpit_reconcile_plan`` executor + ``commands.die`` abort are
    reached unchanged.
    """

    @staticmethod
    def _commands() -> Any:
        from mozyo_bridge.application import commands

        return commands

    def read_window_layout(self, session: str) -> Optional[str]:
        return self._commands()._read_cockpit_window_layout(session)

    def read_geometry(self, session: str) -> Any:
        return self._commands()._read_cockpit_geometry(session)

    def require_tmux(self) -> None:
        return self._commands().require_tmux()

    def execute_reconcile(self, plan: Any) -> None:
        commands = self._commands()
        commands.execute_cockpit_reconcile_plan(plan, commands.run_tmux)

    def rebalance_columns(self, session: str) -> tuple:
        return self._commands()._cockpit_rebalance_columns(session)

    def die(self, message: str) -> None:
        return self._commands().die(message)

    def emit(self, text: str) -> None:
        print(text)


# --- Use case: compose the port + pure domain into the reconcile flow. ---------


class CockpitReconcileUseCase:
    """Preview / apply a cockpit structural reconcile through the injected port.

    Composes the layout / identity reads, the pure domain
    (``parse_window_layout`` / ``build_cockpit_reconcile_plan``), and the
    confirm-gated handler. Byte-for-byte equivalent to the original command
    body, including the fail-closed unparseable-layout refusal, the benign
    absent-cockpit no-op, and the post-apply rebalance hint. All paths return
    exit code ``0``; the fail-closed aborts raise via the port's ``die``.
    """

    def __init__(self, ops: CockpitReconcileOps) -> None:
        self._ops = ops

    def pane_identity(self, session: str) -> dict:
        """The live cockpit's ``{pane_id: identity}`` map, via the port read (#12136)."""
        return project_pane_identity(self._ops.read_geometry(session))

    def handle(
        self,
        session: str,
        *,
        confirm: bool,
        json_output: bool,
        dry_run: bool,
        codex_ratio: int = 70,
    ) -> int:
        """`mozyo cockpit reconcile` — preview/confirm structural layout-tree repair (#12136).

        Reads the live cockpit ``window_layout`` tree and pane identity, and
        plans an order-preserving flatten of any nested top-level cell (a 2x2 /
        mixed-Unit drift) into clean per-Unit columns via `swap-pane` + a
        checksum-valid `select-layout`. ``codex_ratio`` (CLI ``--ratio``) sizes
        the codex-over-claude vertical split of each rebuilt column. Safety
        contract: the default path and `--dry-run` / `--json` are non-mutating
        previews; only an explicit `--confirm` (and not `--dry-run` / `--json`)
        runs the plan. It never kills a pane and never re-infers identity from
        geometry. Fails closed on an unidentified pane (#12133 scope), a Unit
        split across cells, a duplicate same-role pane, or an unparseable
        layout. A cockpit already one-Unit-per-column, or absent, is a benign
        no-op.
        """
        import json as _json

        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
            build_cockpit_reconcile_plan,
            parse_window_layout,
        )

        layout = self._ops.read_window_layout(session)
        present = layout is not None
        root = parse_window_layout(layout) if layout else None
        # Fail closed on an unparseable-but-present layout: a non-empty layout string
        # that did not parse must NOT be reported as "clean" (a no-op success); the
        # safe outcome is a blocked preview / refusal, never a mutation.
        if present and root is None:
            message = (
                f"cockpit reconcile: could not parse the live `window_layout` for "
                f"session {session!r}; refusing to reconcile (fail-closed). Re-read "
                f"and retry, or inspect the cockpit manually."
            )
            if json_output:
                # Same audit-field contract as the normal JSON branch (#12136 j#59881):
                # an unparseable layout has no cells/target, so those are empty/None.
                self._ops.emit(_json.dumps(
                    {"command": "cockpit reconcile", "executes": False,
                     "would_execute": False, "confirm": confirm, "session": session,
                     "cockpit_present": True, "drift": False, "clean": False,
                     "blocked_reason": message, "current_layout": layout,
                     "current_cells": [], "target_layout": None,
                     "target_layout_checksum": None, "plan": None},
                    ensure_ascii=False, indent=2, sort_keys=True,
                ))
                return 0
            self._ops.die(message) if confirm and not dry_run else self._ops.emit(message)
            return 0
        identity = self.pane_identity(session)
        plan = build_cockpit_reconcile_plan(
            root, identity, session=session, codex_ratio=codex_ratio
        )

        would_execute = bool(
            confirm and not dry_run and present and plan.drift and not plan.blocked_reason
        )

        if json_output:
            target = plan.target_layout
            payload = {
                "command": "cockpit reconcile",
                "executes": False,
                "would_execute": would_execute,
                "confirm": confirm,
                "session": session,
                "cockpit_present": present,
                "drift": plan.drift,
                "clean": plan.clean,
                # Normalized audit fields: `blocked_reason` matches `plan.blocked_reason`;
                # `current_layout` / `current_cells` are the observed before-state and
                # `target_layout` / `target_layout_checksum` the planned after-state.
                "blocked_reason": plan.blocked_reason,
                "current_layout": layout,
                "current_cells": [c.as_dict() for c in plan.cells],
                "target_layout": target,
                "target_layout_checksum": (
                    target.split(",", 1)[0] if target else None
                ),
                "plan": plan.as_dict(),
            }
            self._ops.emit(
                _json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            )
            return 0

        if not present:
            self._ops.emit(
                f"cockpit reconcile: no cockpit window for session {session!r} — "
                "nothing to reconcile."
            )
            return 0

        # Preview: bare command (no `--confirm`) or `--dry-run`. No mutation.
        if dry_run or not confirm:
            for line in render_reconcile_preview_lines(plan, session):
                self._ops.emit(line)
            return 0

        # Confirm-gated execution: the only path that mutates tmux.
        if plan.blocked_reason:
            self._ops.die(plan.blocked_reason)
        if plan.clean or not plan.commands:
            self._ops.emit(
                f"cockpit reconcile: session {session!r} already one Unit per "
                "top-level column — nothing to do."
            )
            return 0

        self._ops.require_tmux()
        self._ops.emit(
            f"cockpit reconcile: flattening nested cells into {len(plan.units_in_order)} "
            f"per-Unit columns in cockpit {session!r}..."
        )
        self._ops.execute_reconcile(plan)
        _, after = self._ops.rebalance_columns(session)
        self._ops.emit(
            f"  reconciled: {len(after)} top-level columns now align with Units; "
            "run `mozyo cockpit rebalance` to even widths."
        )
        return 0
