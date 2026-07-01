"""Faithful per-Project-Group tmux-window action boundary (#12330 residual, #12982).

The cross-window duplicate / append / create routing for a faithful Project
Group tmux window (``project_group_tmux_window``) historically lived as a
procedural body — ``_cockpit_group_window_action`` — in
:mod:`mozyo_bridge.application.commands`. It mixed a *side effect* (the #12330
multi-window discovery read) with a *pure decision* (cross-window duplicate
detection off the pane identity options, group-window location by the
``@mozyo_group_id`` marker, and composing the domain plan builders).

This module carves that into an OOP-first boundary under #12638, sitting on top
of the #12971 :mod:`cockpit_read_command` read boundary that already owns the
multi-window discovery:

- :func:`resolve_group_window_action` is the pure decision: given the already-read
  ``managed`` windows (and a ``rightmost_codex_anchor`` callable), it returns the
  same ``(action, plan, blocked_reason, window_name)`` tuple byte-for-byte, with no
  tmux dependency. The :data:`GROUP_ACTION_FOCUS` / :data:`GROUP_ACTION_APPEND` /
  :data:`GROUP_ACTION_CREATE` vocabulary lives here as the action's source of truth
  (:mod:`commands` re-exports the names so ``commands.GROUP_ACTION_*`` and the
  executor dispatch stay unchanged).
- :class:`CockpitGroupWindowOps` is the port for the two things the use case needs
  from its environment — the ``read_managed_windows`` discovery seam and the
  ``rightmost_codex_anchor`` geometry pick — and :class:`LiveCockpitGroupWindowOps`
  the live adapter. The adapter resolves both *through the* :mod:`commands`
  *module at call time*, so the characterization tests that patch
  ``mozyo_bridge.application.commands._read_managed_cockpit_windows`` keep
  intercepting the discovery, and this module never imports :mod:`commands` at
  module scope (no import cycle).
- :class:`CockpitGroupWindowUseCase` reads the managed windows through the port
  and hands them to the pure resolver, returning the same tuple the caller
  already expects. The thin ``_cockpit_group_window_action`` wrapper in
  :mod:`commands` builds the live ops and runs the use case.

Behavior-preserving: cross-window focus priority, group-marker (never name)
routing, the ungrouped-Unit "always its own window" rule, the different-lane
"not a duplicate" rule, the stale-window "no codex column to append beside"
block, and the returned plan / action / window-name shapes are unchanged from
the original command body.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
    build_cockpit_append_plan,
    build_group_window_create_plan,
    build_group_window_focus_plan,
    normalize_lane,
    sanitize_group_window_name,
)


# --- Group-window action vocabulary (source of truth; re-exported by commands). ---

# Faithful ``project_group_tmux_window`` action names (#12330). Distinct from the
# shared-cockpit ``create`` / ``focus`` / ``append`` so the executor never
# confuses a group-window placement (which mutates a live session without a fresh
# attach) with the single-window create (which attaches a new -CC session).
GROUP_ACTION_FOCUS = "group_focus"
GROUP_ACTION_APPEND = "group_append"
GROUP_ACTION_CREATE = "group_create"
GROUP_ACTIONS = (GROUP_ACTION_FOCUS, GROUP_ACTION_APPEND, GROUP_ACTION_CREATE)


# --- Pure decision: cross-window routing over the already-read managed windows. ---


def resolve_group_window_action(
    workspace,
    session,
    *,
    decision,
    codex_ratio,
    launch,
    managed,
    rightmost_codex_anchor: Callable[[Any], str | None],
):
    """Resolve the faithful per-Project-Group tmux-window action (#12330).

    Returns ``(action, plan, blocked_reason, window_name)`` for a workspace whose
    desired presentation faithfully executes ``project_group_tmux_window`` (the
    ``decision.executed_surface == group_tmux_window`` case). The caller has
    already confirmed the cockpit ``session`` exists with a `cockpit` home window,
    and passes ``managed`` (the #12330 multi-window discovery result) plus the
    ``rightmost_codex_anchor`` geometry pick.

    Fail-closed and identity-safe:

    - Duplicate detection is **cross-window**: if this ``workspace_id + lane_id``
      already has a Codex pane in ANY managed window (the `cockpit` home window or
      a group window), the action is :data:`GROUP_ACTION_FOCUS` of that exact pane
      — never a second placement. Identity is read off the pane options, so the
      window the pane lives in is irrelevant.
    - Otherwise the group's existing window is located by the mozyo-written
      ``@mozyo_group_id`` window marker (deterministic, never the window name).
      A non-empty match -> :data:`GROUP_ACTION_APPEND` a column beside that
      window's rightmost Codex pane (same fair-share split + identity stamping the
      shared cockpit uses). No match (or an ungrouped Unit, ``group_id`` empty) ->
      :data:`GROUP_ACTION_CREATE` a fresh group window.

    Pure: the live multi-window read is done by the caller / use case; this
    decision mutates nothing. The returned plan is executed (with rollback) only
    on a real run.
    """
    target_lane = normalize_lane(workspace.lane_id)

    # Cross-window duplicate detection (focus priority): a Codex pane carrying the
    # same workspace+lane in any window means the Unit is already laid out.
    for win in managed:
        for col in win.get("columns") or []:
            if (
                col.get("role") == "codex"
                and col.get("workspace_id") == workspace.workspace_id
                and normalize_lane(col.get("lane_id")) == target_lane
            ):
                return (
                    GROUP_ACTION_FOCUS,
                    build_group_window_focus_plan(col["pane_id"], session=session),
                    None,
                    win.get("window") or "",
                )

    group_id = decision.group_id
    window_name = sanitize_group_window_name(decision.desired_window_name)

    # Locate the group's existing window by the deterministic group marker (never
    # the window name). Only a non-empty group id can share a window; an ungrouped
    # Unit (empty group id) always gets its own fresh window.
    host = None
    if group_id:
        for win in managed:
            if (win.get("group_id") or "") == group_id:
                host = win
                break

    if host is not None:
        codex_cols = [
            c for c in (host.get("columns") or []) if c.get("role") == "codex"
        ]
        anchor = rightmost_codex_anchor(codex_cols)
        if not anchor:
            return (
                GROUP_ACTION_APPEND,
                None,
                (
                    f"Project Group window {host.get('window')!r} exists but carries "
                    "no mozyo-identified codex column to append beside; rebuild the "
                    "cockpit or remove the stale window."
                ),
                host.get("window") or window_name,
            )
        plan = build_cockpit_append_plan(
            workspace,
            anchor_pane=anchor,
            column_index=len(codex_cols),
            codex_ratio=codex_ratio,
            session=session,
            window=host.get("window") or window_name,
            launch=launch,
        )
        return (GROUP_ACTION_APPEND, plan, None, host.get("window") or window_name)

    plan = build_group_window_create_plan(
        workspace,
        group_id=group_id,
        window_name=window_name,
        codex_ratio=codex_ratio,
        session=session,
        launch=launch,
    )
    return (GROUP_ACTION_CREATE, plan, None, window_name)


# --- Port + live adapter over the ``commands`` seams. -------------------------


@runtime_checkable
class CockpitGroupWindowOps(Protocol):
    """Port: the environment the group-window action decision composes over.

    ``read_managed_windows`` is the #12330 multi-window discovery (the seam the
    characterization tests patch on the :mod:`commands` module);
    ``rightmost_codex_anchor`` is the geometry pick of a window's rightmost Codex
    column. The live adapter routes both through the :mod:`commands` module so the
    monkeypatched tests still intercept, and so this module never imports
    :mod:`commands` at module scope (no import cycle).
    """

    def read_managed_windows(self, session: str) -> Any: ...

    def rightmost_codex_anchor(self, codex_columns: Any) -> str | None: ...


class LiveCockpitGroupWindowOps:
    """Live :class:`CockpitGroupWindowOps` over the real ``commands`` seams.

    Each method resolves its target *through the* :mod:`commands` *module at call
    time* rather than binding it at import time, so the tests that patch
    ``mozyo_bridge.application.commands._read_managed_cockpit_windows`` (the
    multi-window discovery feed used by the group-window routing) keep
    intercepting the live read.
    """

    @staticmethod
    def _commands() -> Any:
        from mozyo_bridge.application import commands

        return commands

    def read_managed_windows(self, session: str) -> Any:
        return self._commands()._read_managed_cockpit_windows(session)

    def rightmost_codex_anchor(self, codex_columns: Any) -> str | None:
        return self._commands()._rightmost_codex_anchor(codex_columns)


# --- Use case: read the managed windows, then run the pure decision. ----------


class CockpitGroupWindowUseCase:
    """Resolve the group-window action through the injected port.

    Reads the #12330 managed windows via the port and hands them to the pure
    :func:`resolve_group_window_action`, returning the same
    ``(action, plan, blocked_reason, window_name)`` tuple the caller expects.
    """

    def __init__(self, ops: CockpitGroupWindowOps) -> None:
        self._ops = ops

    def resolve(self, workspace, session, *, decision, codex_ratio, launch):
        managed = self._ops.read_managed_windows(session)
        return resolve_group_window_action(
            workspace,
            session,
            decision=decision,
            codex_ratio=codex_ratio,
            launch=launch,
            managed=managed,
            rightmost_codex_anchor=self._ops.rightmost_codex_anchor,
        )
