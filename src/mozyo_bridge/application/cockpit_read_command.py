"""Cockpit live-read adapter boundary: the tmux column / window / geometry reads (#12971).

Three read helpers historically lived as procedural bodies in
:mod:`mozyo_bridge.application.commands`, each mixing a *side effect* (a
read-only ``tmux list-panes`` / ``list-windows`` query) with a *pure projection*
(splitting the tab-separated ``-F`` output into the dict shape the cockpit
read-model / geometry domain consumes):

- ``_read_cockpit_columns`` — a cockpit window's panes with x-axis geometry +
  workspace / lane identity + the #12658 project triple (append / reset / focus).
- ``_read_managed_cockpit_windows`` — every session window carrying a managed
  pane, each read by its stable ``window_id`` (#12330 multi-window discovery).
- ``_read_cockpit_geometry`` — every cockpit-window pane with full 2D geometry +
  identity, including role-less panes (the ``doctor-geometry`` diagnostic).

This module carves that into an OOP-first boundary under #12638:

- The module-level ``project_*`` / ``*_target`` helpers are the pure projection:
  they own the tmux ``-F`` field templates, the target-window addressing, and the
  line parsing byte-for-byte, with no tmux dependency (exercisable on a raw
  stdout string).
- :class:`CockpitReadOps` is the port for the two things the use case needs from
  its environment — the ``run_tmux`` side effect and the ``read_columns`` seam
  the managed-window discovery composes over — and :class:`LiveCockpitReadOps`
  the live adapter. The adapter resolves both *through the* :mod:`commands`
  *module at call time*, so the characterization tests that patch
  ``mozyo_bridge.application.commands.run_tmux`` (append / group-window column
  reads) and ``mozyo_bridge.application.commands._read_cockpit_columns`` (the
  group-window ``side_effect`` feed) keep intercepting unchanged, and this module
  never imports :mod:`commands` at module scope (no import cycle).
- :class:`CockpitReadUseCase` composes the port and the projection and returns
  the same list / ``None`` shapes the callers already expect. The thin
  ``_read_cockpit_columns`` / ``_read_managed_cockpit_windows`` /
  ``_read_cockpit_geometry`` wrappers in :mod:`commands` build the live ops and
  run the use case.

The #13106 tranche extends the same boundary with the residual cockpit
session-helper reads that still lived procedurally in :mod:`commands`:

- ``_rightmost_codex_anchor`` — the pure geometry pick of the visually rightmost
  codex column (#11849 tie-break), now :func:`rightmost_codex_anchor`.
- ``_cockpit_session_present`` — the tolerant ``has-session`` read (#11803),
  now :meth:`CockpitReadUseCase.session_present` over the port's
  ``session_exists``.
- ``_session_attached_clients_result`` — the ``list-clients`` read that
  distinguishes "no client" from "could not read" (#11814 review j#57928), now
  :meth:`CockpitReadUseCase.attached_clients_result`.
- ``_source_session_cleanup_note`` — the explicit never-implicit-kill
  post-adopt source-session report (#11898), now
  :meth:`CockpitReadUseCase.source_session_cleanup_note` composing the pure
  :func:`source_session_cleanup_note_text` wording.

Behavior-preserving: the read tolerance (a missing tmux binary / server or a
missing window degrades to ``None`` / ``[]`` rather than raising), the parsed
dict shapes, the attached-client known/unknown distinction, the cleanup-note
wording, and the ``cockpit list`` / ``cockpit status`` / ``cockpit
doctor-geometry`` CLI output + exit conventions are unchanged from the original
command bodies.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
    COCKPIT_WINDOW,
    GROUP_WINDOW_OPTION,
)


# --- Pure projection: field templates, target addressing, line parsing. -------

# The ``tmux list-panes -F`` field template for :func:`project_columns`: pane id,
# workspace / role / lane identity, x-axis geometry, and the #12658 project
# triple. Kept as a constant so the read and the tests share one source.
COLUMNS_FIELDS = (
    "#{pane_id}\t#{@mozyo_workspace_id}\t#{@mozyo_agent_role}"
    "\t#{@mozyo_lane_id}\t#{pane_left}\t#{pane_width}"
    "\t#{@mozyo_project_scope}\t#{@mozyo_project_path}"
    "\t#{@mozyo_project_label}"
)

# The ``list-windows -F`` template for :func:`project_managed_window_rows`:
# window id (the identifier everything keys on), display name, and the mozyo
# group marker.
WINDOWS_FIELDS = "#{window_id}\t#{window_name}\t#{" + GROUP_WINDOW_OPTION + "}"

# The ``list-panes -F`` template for :func:`project_geometry`: full 2D geometry
# plus identity, for every pane (including role-less ones).
GEOMETRY_FIELDS = (
    "#{pane_id}\t#{@mozyo_workspace_id}\t#{@mozyo_agent_role}"
    "\t#{@mozyo_lane_id}\t#{pane_left}\t#{pane_top}"
    "\t#{pane_width}\t#{pane_height}"
)


def _as_int(value: str) -> int:
    """Tolerant int parse: a missing / non-numeric geometry field reads as ``0``."""

    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def columns_target(session: str, window: str | None) -> str:
    """The ``list-panes`` target for a cockpit column read.

    ``window`` defaults to the shared ``cockpit`` window. A window id (``@N``) is
    unique across the whole tmux server, so it targets the window on its own;
    only a window *name* needs the ``session:`` qualifier.
    """

    target_window = COCKPIT_WINDOW if window is None else window
    if target_window.startswith("@"):
        return target_window
    return f"{session}:{target_window}"


def geometry_target(session: str) -> str:
    """The ``list-panes`` target for the shared cockpit window's geometry read."""

    return f"{session}:{COCKPIT_WINDOW}"


def project_columns(stdout: str) -> list[dict]:
    """Parse a cockpit column read's tab-separated ``-F`` output.

    One dict per pane carrying a ``pane_id``; missing trailing fields read as
    ``""`` / ``0`` (a pre-#11820 3-field pane, or a root pane with an empty
    project triple) without ``IndexError``.
    """

    columns: list[dict] = []
    for line in (stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0]:
            columns.append(
                {
                    "pane_id": parts[0],
                    "workspace_id": parts[1],
                    "role": parts[2],
                    "lane_id": parts[3] if len(parts) >= 4 else "",
                    "pane_left": _as_int(parts[4]) if len(parts) >= 5 else 0,
                    "pane_width": _as_int(parts[5]) if len(parts) >= 6 else 0,
                    "project_scope": parts[6] if len(parts) >= 7 else "",
                    "project_path": parts[7] if len(parts) >= 8 else "",
                    "project_label": parts[8] if len(parts) >= 9 else "",
                }
            )
    return columns


def project_managed_window_rows(stdout: str) -> list[dict]:
    """Parse the ``list-windows`` output into ``{window_id, window, group_id}`` rows.

    The window id is the identifier everything keys on; a row without one is
    skipped so a duplicate display name can never make the *name* a routing
    dependency (#12330). The per-window pane read + managed filter is applied by
    the use case (it needs the ``read_columns`` seam), not here.
    """

    rows: list[dict] = []
    for line in (stdout or "").splitlines():
        parts = line.split("\t")
        window_id = parts[0] if parts else ""
        if not window_id:
            continue
        rows.append(
            {
                "window_id": window_id,
                "window": parts[1] if len(parts) >= 2 else "",
                "group_id": parts[2] if len(parts) >= 3 else "",
            }
        )
    return rows


def project_geometry(stdout: str) -> list[dict]:
    """Parse a cockpit geometry read's tab-separated ``-F`` output.

    One dict per pane carrying a ``pane_id`` (including role-less panes); the
    row is right-padded to 8 fields so a short line reads as ``""`` / ``0``.
    """

    panes: list[dict] = []
    for line in (stdout or "").splitlines():
        parts = line.split("\t")
        if not parts or not parts[0]:
            continue
        parts = (parts + [""] * 8)[:8]
        panes.append(
            {
                "pane_id": parts[0],
                "workspace_id": parts[1],
                "role": parts[2],
                "lane_id": parts[3],
                "pane_left": _as_int(parts[4]),
                "pane_top": _as_int(parts[5]),
                "pane_width": _as_int(parts[6]),
                "pane_height": _as_int(parts[7]),
            }
        )
    return panes


def rightmost_codex_anchor(codex_columns) -> str | None:
    """The codex pane id of the visually rightmost cockpit column (#11849, pure).

    `tmux list-panes` enumeration order is NOT layout order, so anchoring an
    append on the last-listed codex pane can split a middle column and crush the
    layout. Pick by geometry instead: the largest ``pane_left``, tie-broken by
    the right edge (``pane_left + pane_width``) then ``pane_id`` so it stays
    deterministic even when geometry is missing (defaults to 0 → a stable
    pane-id ordering).
    """

    if not codex_columns:
        return None

    def _key(col):
        left = col.get("pane_left") or 0
        width = col.get("pane_width") or 0
        return (left, left + width, col.get("pane_id") or "")

    return max(codex_columns, key=_key).get("pane_id")


def project_nonempty_lines(stdout: str) -> tuple[str, ...]:
    """The stripped non-empty lines of a single-field ``-F`` read.

    Serves both the ``list-clients`` client-tty parse and the ``list-panes``
    pane-id count of the session-helper reads.
    """

    return tuple(
        line.strip() for line in (stdout or "").splitlines() if line.strip()
    )


def source_session_cleanup_note_text(
    source_session: str, present: bool, remaining: str
) -> str:
    """The post-adopt source-session cleanup wording (#11898, pure).

    Adopt never kills the source session explicitly; tmux closes a session whose
    last pane is moved away. This states which of the two outcomes happened —
    gone (closed by tmux) or still alive with ``remaining`` pane(s), left
    intact — byte-for-byte as the original ``commands`` body reported it.
    """

    if not present:
        return (
            f"source session {source_session!r} is now empty and was closed by "
            f"tmux (both agent panes moved out); not killed explicitly."
        )
    return (
        f"source session {source_session!r} still has {remaining} pane(s) and was "
        f"left intact (not killed)."
    )


# --- Port + live adapter over the ``commands`` seams. -------------------------


@runtime_checkable
class CockpitReadOps(Protocol):
    """Port: the side effects the cockpit read use case needs from its environment.

    ``run_tmux`` is the read-only tmux query; ``read_columns`` is the per-window
    column read the managed-window discovery composes over; ``session_exists``
    is the session-presence probe the session-helper reads need (#13106). The
    live adapter routes all three through the :mod:`commands` module so the
    monkeypatched characterization tests still intercept, and so this module
    never imports :mod:`commands` at module scope (no import cycle).
    """

    def run_tmux(self, *args: Any, **kwargs: Any) -> Any: ...

    def read_columns(self, session: str, window: str | None) -> list[dict] | None: ...

    def session_exists(self, session: str) -> bool: ...


class LiveCockpitReadOps:
    """Live :class:`CockpitReadOps` over the real ``commands`` seams.

    Each method resolves its target *through the* :mod:`commands` *module at call
    time* rather than binding it at import time, so the tests that patch
    ``mozyo_bridge.application.commands.run_tmux`` (append / group-window direct
    reads) and ``mozyo_bridge.application.commands._read_cockpit_columns`` (the
    group-window ``side_effect`` feed used by the managed-window discovery) keep
    intercepting the live reads.
    """

    @staticmethod
    def _commands() -> Any:
        from mozyo_bridge.application import commands

        return commands

    def run_tmux(self, *args: Any, **kwargs: Any) -> Any:
        return self._commands().run_tmux(*args, **kwargs)

    def read_columns(self, session: str, window: str | None) -> list[dict] | None:
        return self._commands()._read_cockpit_columns(session, window)

    def session_exists(self, session: str) -> bool:
        return self._commands().session_exists(session)


# --- Use case: compose the port + projection into the caller-facing shapes. ---


class CockpitReadUseCase:
    """Read cockpit panes / windows / geometry through the injected port.

    Every read is deliberately tolerant: a missing tmux binary / server, or a
    missing window, degrades to ``None`` (columns / geometry) or ``[]`` (managed
    windows) rather than raising, so ``--dry-run`` / ``--json`` stay non-mutating
    and never abort — identical to the original command bodies.
    """

    def __init__(self, ops: CockpitReadOps) -> None:
        self._ops = ops

    def read_columns(self, session: str, window: str | None = None) -> list[dict] | None:
        target = columns_target(session, window)
        try:
            result = self._ops.run_tmux(
                "list-panes", "-t", target, "-F", COLUMNS_FIELDS, check=False
            )
        except (Exception, SystemExit):
            return None
        if getattr(result, "returncode", 1) != 0:
            return None
        return project_columns(getattr(result, "stdout", "") or "")

    def read_managed_windows(self, session: str) -> list[dict]:
        try:
            result = self._ops.run_tmux(
                "list-windows", "-t", session, "-F", WINDOWS_FIELDS, check=False
            )
        except (Exception, SystemExit):
            return []
        if getattr(result, "returncode", 1) != 0:
            return []
        managed: list[dict] = []
        for row in project_managed_window_rows(getattr(result, "stdout", "") or ""):
            # Read panes by the unambiguous window id, never the (possibly
            # duplicate) name, through the ``read_columns`` seam so a patched
            # ``commands._read_cockpit_columns`` still feeds this discovery.
            columns = self._ops.read_columns(session, row["window_id"])
            if not columns:
                continue
            if any((c.get("workspace_id") or "") for c in columns):
                managed.append(
                    {
                        "window_id": row["window_id"],
                        "window": row["window"],
                        "group_id": row["group_id"],
                        "columns": columns,
                    }
                )
        return managed

    def read_geometry(self, session: str) -> list[dict] | None:
        try:
            result = self._ops.run_tmux(
                "list-panes", "-t", geometry_target(session),
                "-F", GEOMETRY_FIELDS, check=False,
            )
        except (Exception, SystemExit):
            return None
        if getattr(result, "returncode", 1) != 0:
            return None
        return project_geometry(getattr(result, "stdout", "") or "")

    def session_present(self, session: str) -> bool:
        """Tolerant `has-session` for the cockpit (#11803 review).

        Distinguishes "session absent" from "session present but cockpit window
        missing", so a real-run create never `new-session`s against (and the
        cleanup never kills) a pre-existing session. Tolerant: any tmux error
        degrades to ``False``.
        """
        try:
            return bool(self._ops.session_exists(session))
        except (Exception, SystemExit):
            return False

    def attached_clients_result(self, session: str) -> tuple[tuple[str, ...], bool]:
        """``(clients, known)`` for ``session`` — "no client" vs "could not read".

        ``known`` is ``False`` when the tmux ``list-clients`` read failed
        (exception or non-zero exit), so a caller can fail closed on an
        *unknown* client state instead of mistaking it for "no client attached".
        The destructive cockpit reset/rebuild gate needs that distinction
        (Redmine #11814 review j#57928); adopt's tolerant
        ``commands._session_attached_clients`` keeps the old "any error -> no
        client" shape over this read.
        """
        if not session:
            return (), True
        try:
            result = self._ops.run_tmux(
                "list-clients", "-t", session, "-F", "#{client_tty}", check=False
            )
        except (Exception, SystemExit):
            return (), False
        if getattr(result, "returncode", 1) != 0:
            return (), False
        return project_nonempty_lines(getattr(result, "stdout", "") or ""), True

    def source_session_cleanup_note(self, source_session: str) -> str:
        """Explicit (never-implicit-kill) report of the source session after adopt (#11898).

        Adopt moves only the two agent panes; tmux destroys a window/session
        whose last pane is moved away, so an emptied source session is cleaned
        up *by tmux*, not by an explicit ``kill-session`` from this tool
        (acceptance: cleanup must be explicit and logged, never an implicit
        kill). Reports the resulting state via the pure
        :func:`source_session_cleanup_note_text` wording. Tolerant / read-only:
        an unreadable pane count degrades to ``"?"``.
        """
        present = self.session_present(source_session)
        remaining = "?"
        if present:
            try:
                result = self._ops.run_tmux(
                    "list-panes", "-s", "-t", source_session, "-F", "#{pane_id}",
                    check=False,
                )
                if getattr(result, "returncode", 1) == 0:
                    remaining = str(
                        len(
                            project_nonempty_lines(
                                getattr(result, "stdout", "") or ""
                            )
                        )
                    )
            except (Exception, SystemExit):
                pass
        return source_session_cleanup_note_text(source_session, present, remaining)
