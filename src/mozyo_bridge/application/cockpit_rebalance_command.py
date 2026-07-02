"""Cockpit rebalance boundary: preview rendering + confirm-gated handler (#13009).

``_handle_cockpit_rebalance`` historically lived as a procedural body in
:mod:`mozyo_bridge.application.commands`, mixing the *side effects* (the live
``window_layout`` column read, the tmux availability gate, the #12135
``resize-pane`` executor, the fail-closed abort) with *pure decision /
rendering logic* (the would-execute projection, the preview / json / confirm
output selection, and the per-column preview report).

This module carves that into an OOP-first boundary under #12638, following the
#12989 reset carve (``cockpit_reset_command.py``):

- :func:`render_rebalance_preview_lines` is the pure preview renderer over an
  already-built :class:`CockpitRebalancePlan` — no :mod:`commands` dependency.
- :class:`CockpitRebalanceOps` is the port for the side effects the use case
  needs — the top-level column read behind the plan (and the post-apply
  re-read), the tmux availability gate, the #12135 rebalance executor, the
  fail-closed abort, and the stdout line sink.
  :class:`LiveCockpitRebalanceOps` resolves every target *through the*
  :mod:`commands` *module at call time* (never at import), so the rebalance /
  reconcile characterization tests that patch
  ``commands._read_cockpit_window_layout`` / ``commands.require_tmux`` /
  ``commands.run_tmux`` keep intercepting, and no import cycle is introduced.
- :class:`CockpitRebalanceUseCase` composes the port and the pure domain
  (``build_cockpit_rebalance_plan``) into the confirm-gated :meth:`handle`.
  Like the reset carve it renders through the port's ``emit`` sink and returns
  the process exit code; the die paths raise via the port and never return.

The thin ``_handle_cockpit_rebalance`` wrapper stays in :mod:`commands` with an
unchanged signature (the ``cmd_cockpit`` rebalance dispatch is preserved), and
the shared ``_read_cockpit_window_layout`` / ``_cockpit_rebalance_columns``
readers stay in :mod:`commands` — ``_handle_cockpit_reconcile`` still consumes
them directly, so this boundary reaches them only through the port.
Behavior-preserving: the #12135 safety contract (preview-first, ``--json`` /
``--dry-run`` never mutate, only an explicit ``--confirm`` runs the width-only
``resize-pane`` plan, structural drift fails closed toward `mozyo cockpit
reconcile`), the benign absent-cockpit / already-balanced no-ops, and every
output line are unchanged from the original command body. This module reuses
the #12135 executor via the port; it never reimplements executor semantics.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


# --- Pure rendering over a built rebalance plan. --------------------------------


def render_rebalance_preview_lines(plan: Any) -> list:
    """The `mozyo cockpit rebalance` preview lines for ``plan`` (#12135, pure).

    Byte-for-byte the lines the handler historically printed on the preview
    path (bare command or ``--dry-run``): the session/column header, one
    observed-vs-target line per column (drift-flagged when not cleanly
    resizable), then the blocked reason, the balanced no-op, or the ordered
    ``resize-pane`` plan with the ``--confirm`` hint.
    """
    import shlex as _shlex

    lines = [
        f"cockpit rebalance (preview; no tmux changes): session={plan.session} "
        f"columns={plan.column_count} total_width={plan.total_content_width}"
    ]
    for col in plan.columns:
        flag = "" if col.clean else " [drift: not a clean full-width split]"
        lines.append(
            f"  column {col.index}: current={col.current_width} -> "
            f"target={col.target_width} (delta {col.delta:+d}) "
            f"pane={col.target_pane or '-'}{flag}"
        )
    if plan.drift:
        lines.append(f"  cannot rebalance: {plan.blocked_reason}")
    elif plan.balanced:
        lines.append("  already balanced within tolerance — nothing to rebalance.")
    else:
        lines.append(
            "  rebalance plan (width only; identity untouched, splits kept):"
        )
        for cmd in plan.commands:
            lines.append(
                "    tmux " + " ".join(_shlex.quote(tok) for tok in cmd.argv)
            )
        lines.append("  run `mozyo cockpit rebalance --confirm` to apply.")
    return lines


# --- Port + live adapter over the ``commands`` seams. -------------------------


@runtime_checkable
class CockpitRebalanceOps(Protocol):
    """Port: the side effects the cockpit-rebalance use case needs.

    ``rebalance_columns`` is the ``(present, columns)`` live layout read feeding
    the plan (and the post-apply width re-read); ``require_tmux`` gates the
    mutable path; ``execute_rebalance`` runs the #12135 width-only executor;
    ``die`` is the fail-closed abort; ``emit`` is the stdout line sink.
    """

    def rebalance_columns(self, session: str) -> tuple: ...

    def require_tmux(self) -> None: ...

    def execute_rebalance(self, plan: Any) -> None: ...

    def die(self, message: str) -> None: ...

    def emit(self, text: str) -> None: ...


class LiveCockpitRebalanceOps:
    """Live :class:`CockpitRebalanceOps` over the real ``commands`` seams.

    Every method resolves its target *through the* :mod:`commands` *module at
    call time* rather than binding it at import time, so the rebalance
    characterization tests that patch ``commands._read_cockpit_window_layout``
    (which ``commands._cockpit_rebalance_columns`` reads through) /
    ``commands.require_tmux`` / ``commands.run_tmux`` keep intercepting, and
    the #12135 ``execute_cockpit_rebalance_plan`` executor + ``commands.die``
    abort are reached unchanged.
    """

    @staticmethod
    def _commands() -> Any:
        from mozyo_bridge.application import commands

        return commands

    def rebalance_columns(self, session: str) -> tuple:
        return self._commands()._cockpit_rebalance_columns(session)

    def require_tmux(self) -> None:
        return self._commands().require_tmux()

    def execute_rebalance(self, plan: Any) -> None:
        commands = self._commands()
        commands.execute_cockpit_rebalance_plan(plan, commands.run_tmux)

    def die(self, message: str) -> None:
        return self._commands().die(message)

    def emit(self, text: str) -> None:
        print(text)


# --- Use case: compose the port + pure domain into the rebalance flow. ---------


class CockpitRebalanceUseCase:
    """Preview / apply a cockpit width rebalance through the injected port.

    Composes the live column read, the pure planner
    (``build_cockpit_rebalance_plan``), the preview renderer, and the
    confirm-gated executor. Byte-for-byte equivalent to the original command
    body, including the #12135 safety contract (json / dry-run / bare previews
    never mutate; drift fails closed toward `mozyo cockpit reconcile`), the
    benign absent-cockpit / already-balanced no-ops, and the post-apply width
    report.
    """

    def __init__(self, ops: CockpitRebalanceOps) -> None:
        self._ops = ops

    def handle(
        self, session: str, *, confirm: bool, json_output: bool, dry_run: bool
    ) -> int:
        """`mozyo cockpit rebalance` — preview/confirm equal fair-share width restore (#12135).

        Reads the live cockpit ``window_layout`` tree, projects its top-level
        columns, and plans an EQUAL fair-share width rebalance. Safety
        contract: the default path and `--dry-run` / `--json` are non-mutating
        previews; only an explicit `--confirm` (and not `--dry-run` / `--json`)
        runs the `resize-pane` plan. The plan touches column width only — it
        emits no `set-option` (identity pane options stay put) and never
        `select-layout even-horizontal` (the Codex/Claude vertical splits stay
        intact). It fails closed on a structurally drifted column (a nested 2x2
        cell), deferring that repair to `mozyo cockpit reconcile` (#12136). A
        cockpit already balanced within tolerance, or with fewer than two
        columns, is a benign no-op.
        """
        import json as _json

        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
            build_cockpit_rebalance_plan,
        )

        present, columns = self._ops.rebalance_columns(session)
        plan = build_cockpit_rebalance_plan(columns, session=session)

        would_execute = bool(
            confirm
            and not dry_run
            and present
            and not plan.balanced
            and not plan.drift
            and plan.commands
        )

        if json_output:
            payload = {
                "command": "cockpit rebalance",
                # This invocation never runs tmux (json is a preview surface).
                "executes": False,
                "would_execute": would_execute,
                "confirm": confirm,
                "session": session,
                "cockpit_present": present,
                "balanced": plan.balanced,
                "drift": plan.drift,
                "plan": plan.as_dict(),
            }
            self._ops.emit(
                _json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            )
            return 0

        if not present:
            self._ops.emit(
                f"cockpit rebalance: no cockpit window for session {session!r} — "
                "nothing to rebalance."
            )
            return 0

        # Preview: bare command (no `--confirm`) or `--dry-run`. No mutation.
        if dry_run or not confirm:
            for line in render_rebalance_preview_lines(plan):
                self._ops.emit(line)
            return 0

        # Confirm-gated execution: the only path that mutates tmux.
        if plan.drift:
            self._ops.die(
                plan.blocked_reason or "cockpit rebalance blocked by structural drift."
            )
        if plan.balanced or not plan.commands:
            self._ops.emit(
                f"cockpit rebalance: session {session!r} columns already balanced "
                "within tolerance — nothing to do."
            )
            return 0

        self._ops.require_tmux()
        self._ops.emit(
            f"cockpit rebalance: restoring {plan.column_count} columns toward "
            f"fair-share width in cockpit {session!r}..."
        )
        self._ops.execute_rebalance(plan)
        _, after = self._ops.rebalance_columns(session)
        widths = [c.width for c in after]
        self._ops.emit(f"  rebalanced: column widths now {widths}.")
        return 0
