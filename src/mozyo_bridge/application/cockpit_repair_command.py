"""Cockpit repair executor boundary: the tmux mutation plan runners (#12972).

Five executor helpers historically lived as procedural bodies in
:mod:`mozyo_bridge.application.commands`, each running an already-built cockpit
repair *plan* against tmux and translating a non-zero tmux exit into a
fail-closed abort (with best-effort rollback for the two adopt paths):

- ``execute_cockpit_adopt_plan`` — move a normal session's codex/claude pair
  into the cockpit as a column, atomically, with best-effort rollback of the
  first (codex) join and best-effort identity re-stamp (#11898).
- ``execute_peer_adopt_plan`` — bind identity options onto a role-less candidate
  pane, rolling the earlier ``set-option`` binds back on a mid-sequence failure
  so the pane is never left half-bound (#12130 / #12133).
- ``execute_cockpit_reset_plan`` — run the ``kill-session`` teardown, fail-fast
  (#11814).
- ``execute_cockpit_rebalance_plan`` — run the ``resize-pane -x`` fair-share
  restore, fail-fast (#12135).
- ``execute_cockpit_reconcile_plan`` — run the ``swap-pane`` + ``select-layout``
  per-Unit reorder, fail-fast, with a recoverable ("no pane was killed") hint on
  abort (#12136).

This module carves that into an OOP-first boundary under #12638:

- :class:`CockpitRepairOps` is the port for the two things the executors need
  from their environment — the ``run`` tmux side effect (the injected
  ``run_tmux``-style callable) and the ``die`` fail-closed abort.
  :class:`LiveCockpitRepairOps` is the live adapter: it wraps the caller's
  injected ``run`` and resolves ``die`` *through the* :mod:`commands` *module at
  call time*, so a test that patches ``mozyo_bridge.application.commands.die``
  still intercepts the abort, and this module never imports :mod:`commands` at
  module scope (no import cycle).
- :class:`CockpitRepairUseCase` composes the port and owns the confirm-gated
  safety semantics and rollback wording byte-for-byte. The thin
  ``execute_*_plan`` wrappers in :mod:`commands` build the live ops (over the
  passed-in ``run``) and run the use case, preserving the ``(plan, run)``
  signature the characterization tests call directly.

Behavior-preserving: the transactional rollback of the adopt paths, the
fail-fast abort on the destructive paths, every abort / rollback / warning
string, and the ``{"stamp_warnings": [...]}`` / ``None`` return shapes are
unchanged from the original command bodies. This module executes plans only; it
never builds them and never decides whether a mutation is safe (that stays with
the plan builders and the confirm-gated handlers).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


# --- Pure helper: the tmux result detail extraction shared by every abort. -----


def result_detail(result: Any) -> str:
    """The human-facing detail for a failed tmux step: ``stderr`` else ``stdout``.

    Both are stripped; an empty result yields ``""`` so callers can fall back to
    ``"nonzero exit"``. Pure over the result object (no tmux dependency).
    """

    return (getattr(result, "stderr", "") or "").strip() or (
        getattr(result, "stdout", "") or ""
    ).strip()


# --- Port + live adapter over the injected ``run`` and the ``commands`` seam. ---


@runtime_checkable
class CockpitRepairOps(Protocol):
    """Port: the side effects the cockpit repair use case needs.

    ``run`` is the ``run_tmux``-style callable that executes one tmux command;
    ``die`` is the fail-closed abort. The live adapter wraps the caller's
    injected ``run`` and routes ``die`` through the :mod:`commands` module so the
    monkeypatched abort tests still intercept, and so this module never imports
    :mod:`commands` at module scope (no import cycle).
    """

    def run(self, *args: Any, **kwargs: Any) -> Any: ...

    def die(self, message: str) -> None: ...


class LiveCockpitRepairOps:
    """Live :class:`CockpitRepairOps` over the injected ``run`` + ``commands.die``.

    ``run`` is bound at construction (the caller passes the module-level
    ``run_tmux`` or a fake), while ``die`` is resolved *through the*
    :mod:`commands` *module at call time* rather than at import time, so a test
    that patches ``mozyo_bridge.application.commands.die`` keeps intercepting the
    abort path.
    """

    def __init__(self, run: Any) -> None:
        self._run = run

    @staticmethod
    def _commands() -> Any:
        from mozyo_bridge.application import commands

        return commands

    def run(self, *args: Any, **kwargs: Any) -> Any:
        return self._run(*args, **kwargs)

    def die(self, message: str) -> None:
        return self._commands().die(message)


# --- Use case: run the built plans with their safety / rollback semantics. ------


class CockpitRepairUseCase:
    """Execute a cockpit repair plan through the injected port.

    Each method mirrors one of the original ``execute_*_plan`` command bodies
    byte-for-byte: the two adopt paths are transactional (best-effort rollback of
    partial mutation), the three destructive paths are fail-fast, and every abort
    aborts via the port's ``die`` rather than reporting a half-applied layout as
    success.
    """

    def __init__(self, ops: CockpitRepairOps) -> None:
        self._ops = ops

    def execute_adopt(self, plan: Any) -> dict:
        """Run a ``CockpitAdoptPlan``, atomically, with best-effort rollback (#11898).

        The two ``join_commands`` move the live codex/claude panes and are treated
        as a transaction: if the first join (codex) lands but a later join fails,
        the codex pane is **moved back** beside the still-present source claude
        pane — never ``kill-pane``'d, because it carries a live agent (this is the
        crucial difference from :func:`execute_cockpit_plan`'s
        ``cleanup_captured``, which kills freshly-*created* panes).
        ``stamp_commands`` re-apply identity after both joins succeed and are
        best-effort: a stamp failure leaves the pair adopted and is reported as a
        warning, not rolled back. Returns ``{"stamp_warnings": [...]}``.
        """

        run = self._ops.run

        def _rollback(joined_codex: bool) -> str | None:
            # Only the codex pane can be mid-adopted: it joins first, so if a later
            # step fails it is alone in the cockpit while the claude pane is still in
            # the source session. Move it back beside that source pane. Best-effort —
            # a failed rollback leaves the live codex pane in the cockpit (reported),
            # never killed.
            if not joined_codex:
                return None
            result = run(
                "join-pane", "-h", "-s", plan.source_codex_pane,
                "-t", plan.source_claude_pane, check=False,
            )
            if getattr(result, "returncode", 0) != 0:
                return (
                    f"rollback failed: codex pane {plan.source_codex_pane} could not "
                    f"be moved back to source session {plan.source_session!r} "
                    f"({result_detail(result) or 'nonzero exit'}); it is now live in "
                    f"the cockpit — move it manually, it was NOT killed."
                )
            return None

        joined_codex = False
        for cmd in plan.join_commands:
            result = run(*cmd.argv, check=False)
            if getattr(result, "returncode", 0) != 0:
                rollback_note = _rollback(joined_codex)
                message = (
                    f"cockpit adopt step failed ({cmd.purpose}): "
                    f"`tmux {' '.join(cmd.argv)}` -> {result_detail(result) or 'nonzero exit'}"
                )
                if rollback_note:
                    message += f"\n{rollback_note}"
                elif joined_codex:
                    message += (
                        f"\nrolled back: codex pane {plan.source_codex_pane} moved "
                        f"back to source session {plan.source_session!r}."
                    )
                self._ops.die(message)
            joined_codex = True

        # Both joins landed — the pair is adopted. Identity re-stamp is best-effort.
        stamp_warnings: list[str] = []
        for cmd in plan.stamp_commands:
            result = run(*cmd.argv, check=False)
            if getattr(result, "returncode", 0) != 0:
                stamp_warnings.append(
                    f"{cmd.purpose}: {result_detail(result) or 'nonzero exit'}"
                )
        return {"stamp_warnings": stamp_warnings}

    def execute_peer_adopt(self, plan: Any) -> None:
        """Run a ``PeerAdoptPlan``'s identity binds, fail-closed (Redmine #12133).

        Peer adopt only ``set-option`` (+ ``select-pane -T``) binds the role-less
        candidate pane — there is no pane move / kill / split, so the pane and any
        agent in it are untouched. The binds are treated as a small transaction:
        if a later bind fails after earlier ones landed, the earlier identity
        options are **unset** (best-effort) so the pane returns to its pre-adopt
        role-less state rather than being left half-bound (the very #12130 drift
        this repairs). Any failure raises via ``die``; a clean run returns
        ``None``.
        """

        run = self._ops.run

        # Track the identity options we successfully set so a mid-sequence failure can
        # roll them back. The title (`select-pane -T`) is cosmetic and not rolled back.
        set_options: list[str] = []
        for cmd in plan.stamp_commands:
            result = run(*cmd.argv, check=False)
            if getattr(result, "returncode", 0) != 0:
                for option in reversed(set_options):
                    run("set-option", "-p", "-u", "-t", plan.pane_id, option, check=False)
                rolled = (
                    f" rolled back {len(set_options)} identity option(s) on "
                    f"{plan.pane_id} to restore its role-less state."
                    if set_options
                    else ""
                )
                self._ops.die(
                    f"cockpit peer-adopt step failed ({cmd.purpose}): "
                    f"`tmux {' '.join(cmd.argv)}` -> {result_detail(result) or 'nonzero exit'}."
                    f"{rolled}"
                )
            if cmd.argv[:1] == ("set-option",):
                # argv is ("set-option", "-p", "-t", pane, OPTION, value).
                set_options.append(cmd.argv[4])

    def _run_fail_fast(self, plan: Any, action: str, suffix: str = "") -> None:
        """Run a plan's ``commands`` left-to-right, aborting on the first failure.

        Shared by the three non-rollback destructive paths (reset / rebalance /
        reconcile): each aborts with a ``cockpit {action} step failed`` message
        rather than reporting a half-applied layout as success. ``suffix`` is an
        optional trailing hint appended to the abort message (reconcile's
        "no pane was killed; re-run" recovery note).
        """

        for cmd in plan.commands:
            result = self._ops.run(*cmd.argv, check=False)
            if getattr(result, "returncode", 0) != 0:
                detail = result_detail(result)
                self._ops.die(
                    f"cockpit {action} step failed ({cmd.purpose}): "
                    f"`tmux {' '.join(cmd.argv)}` -> {detail or 'nonzero exit'}{suffix}"
                )

    def execute_reset(self, plan: Any) -> None:
        """Run a ``CockpitResetPlan``'s ``kill-session`` (#11814), fail-fast.

        The plan is built only after the target was graded mozyo-managed, so this
        just executes the destructive teardown and aborts (``die``) on a non-zero
        tmux exit rather than reporting a half-killed session as success.
        """

        self._run_fail_fast(plan, "reset")

    def execute_rebalance(self, plan: Any) -> None:
        """Run a ``CockpitRebalancePlan``'s ``resize-pane`` commands (#12135), fail-fast.

        The plan already targets real ``%pane`` ids (no token resolution needed)
        and touches no identity option, so this just runs each ``resize-pane -x``
        in left-to-right order and aborts (``die``) on a non-zero tmux exit rather
        than reporting a half-rebalanced layout as success.
        """

        self._run_fail_fast(plan, "rebalance")

    def execute_reconcile(self, plan: Any) -> None:
        """Run a ``CockpitReconcilePlan``'s swap + relayout commands (#12136), fail-fast.

        The plan's ``swap-pane`` commands reorder the live panes, then the single
        ``select-layout`` applies the per-Unit columns. No command kills a pane —
        ``swap-pane`` / ``select-layout`` only move / relayout live panes
        (identity rides with them). A non-zero tmux exit aborts (``die``); because
        nothing is killed, a partial reorder is recoverable by re-running
        reconcile (it re-sorts from the live order).
        """

        self._run_fail_fast(
            plan,
            "reconcile",
            suffix=(
                "\nNo pane was killed; re-run `mozyo cockpit reconcile` to continue "
                "from the current live layout."
            ),
        )
