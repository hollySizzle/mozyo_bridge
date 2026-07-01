"""OOP-first boundary for the residual status/session helpers (Redmine #12974).

This tranche carves the status/session helper tail that still lived procedurally
in ``commands.py`` — ``session_cwd_mismatch`` / ``legacy_basename_session_notice``
/ ``resolve_status_session`` — into pure decision/formatting functions plus a
read port + live adapter and three thin use cases, aligning with the existing
``commands_status.py`` / ``status_session_port.py`` boundary (#12831 / #12830 /
#12825 / #12785).

Split of concerns:

* :func:`cwd_is_under_repo` / :func:`select_offending_cwds` /
  :func:`legacy_basename_notice_text` are *pure* — no tmux read, no ``print`` —
  so the session-cwd mismatch decision and the migration-notice wording are unit
  tested with in-memory pane records instead of a ``commands.*`` monkeypatch.
* :class:`StatusSessionHelperReads` is the injectable read port (pane snapshot,
  session existence, the cwd-mismatch read, the current tmux session, and the
  canonical session name); :class:`LiveStatusSessionHelperReads` is the live
  adapter that routes each read through the ``commands`` module *at call time*.
* :class:`SessionCwdMismatchUseCase` / :class:`LegacyBasenameNoticeUseCase` /
  :class:`ResolveStatusSessionUseCase` compose the pure policy over the port.

Compatibility bridge (transitional): the live adapter reaches
``commands.pane_lines`` / ``commands.session_exists`` /
``commands.session_cwd_mismatch`` / ``commands.current_session_name`` /
``commands.repo_root_from_args`` / ``commands.resolve_canonical_session`` *at
call time* so the existing tests that patch those ``commands.*`` names — and the
``LiveLaunchOps`` adapter (#12933) that routes ``session_cwd_mismatch`` /
``legacy_basename_session_notice`` through ``commands.*`` — keep working
unchanged while the use cases gain their port seam. The thin ``commands.py``
wrappers stay as the ``commands.session_cwd_mismatch`` /
``commands.resolve_status_session`` monkeypatch surface the issue asks to
preserve. This is a read-only status/session boundary; it issues no send-keys /
paste-buffer routing (``tmux-send-safety-contract``) — the port exposes no
key-send operation.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:  # avoid importing argparse on the hot path
    import argparse


# --------------------------------------------------------------------------- #
# Pure domain (no tmux read, no stdout)
# --------------------------------------------------------------------------- #


def cwd_is_under_repo(cwd: str, repo_root: Path) -> bool:
    """Whether ``cwd`` resolves to a path under ``repo_root`` (pure).

    An empty ``cwd`` is treated as "under" (unknown-but-not-foreign), matching
    the procedural handler; a path that cannot be made relative to the repo root
    (``ValueError``) is foreign.
    """
    if not cwd:
        return True
    try:
        Path(cwd).expanduser().resolve().relative_to(repo_root.resolve())
    except ValueError:
        return False
    return True


def select_offending_cwds(panes, session: str, repo_root: Path) -> List[str]:
    """Return the cwds of ``session``'s panes when none of them are under ``repo_root``.

    Pure over a pane-record snapshot: the session is "pointing at another work
    root" only when it has at least one pane and *every* pane's cwd is outside
    ``repo_root``. Returns the offending cwds in that case; otherwise ``[]``.
    """
    same_session_panes = [
        pane
        for pane in panes
        if (pane.get("location") or "").split(":", 1)[0] == session
    ]
    if not same_session_panes:
        return []
    if any(
        cwd_is_under_repo(pane.get("cwd") or "", repo_root)
        for pane in same_session_panes
    ):
        return []
    return [pane.get("cwd") or "?" for pane in same_session_panes]


def legacy_basename_notice_text(legacy: str, derived_session: str) -> str:
    """The migration notice for a lingering legacy basename-named session (pure)."""
    return (
        f"notice: legacy session '{legacy}' (named by repo basename) exists for this repo; "
        f"bare `mozyo` now derives '{derived_session}'. Attach the old one explicitly with "
        f"`mozyo --session {legacy}` (or `tmux attach -t {legacy}`), or remove it once empty "
        f"with `tmux kill-session -t {legacy}`."
    )


# --------------------------------------------------------------------------- #
# Read port
# --------------------------------------------------------------------------- #


@runtime_checkable
class StatusSessionHelperReads(Protocol):
    """The reads the status/session helper use cases depend on (read-only)."""

    def pane_lines(self) -> list:
        """A live snapshot of the tmux pane inventory."""
        ...

    def session_exists(self, session: str) -> bool:
        """Whether the named tmux session exists."""
        ...

    def session_cwd_mismatch(self, session: str, repo_root: Path) -> List[str]:
        """Offending cwds when ``session`` points entirely outside ``repo_root``."""
        ...

    def current_session_name(self) -> Optional[str]:
        """The current tmux session name, or ``None`` when not inside tmux."""
        ...

    def repo_root_from_args(self, args: "argparse.Namespace") -> Path:
        """Resolve the repo root the command should act on."""
        ...

    def canonical_session_name(self, repo_root: Path) -> str:
        """The bare-``mozyo`` resolved canonical session name for ``repo_root``."""
        ...


# --------------------------------------------------------------------------- #
# Use cases
# --------------------------------------------------------------------------- #


class SessionCwdMismatchUseCase:
    """Decide whether a session points entirely outside the repo, over a port.

    Reads the live pane snapshot through the injected port and runs the pure
    :func:`select_offending_cwds` policy, so the decision is unit tested with a
    fake reads object (no ``commands.pane_lines`` monkeypatch).
    """

    def __init__(self, reads: StatusSessionHelperReads) -> None:
        self._reads = reads

    def resolve(self, session: str, repo_root: Path) -> List[str]:
        return select_offending_cwds(self._reads.pane_lines(), session, repo_root)


class LegacyBasenameNoticeUseCase:
    """Build the legacy-basename migration notice (advisory), over a port.

    Preserves the procedural short-circuits: nothing to migrate when the repo
    basename is empty or already equals the derived name; no notice when the
    legacy-named session does not exist; and no notice when that session belongs
    to another repo (its panes are all outside this repo, via the port's
    ``session_cwd_mismatch``). The wording itself is the pure
    :func:`legacy_basename_notice_text`.
    """

    def __init__(self, reads: StatusSessionHelperReads) -> None:
        self._reads = reads

    def resolve(self, repo_root: Path, derived_session: str) -> Optional[str]:
        legacy = repo_root.name
        if not legacy or legacy == derived_session:
            return None
        if not self._reads.session_exists(legacy):
            return None
        if self._reads.session_cwd_mismatch(legacy, repo_root):
            # The legacy-named session's panes are all outside this repo, so it
            # is a different repo's session that merely shares the basename.
            return None
        return legacy_basename_notice_text(legacy, derived_session)


class ResolveStatusSessionUseCase:
    """Pick the session ``status`` should describe, over a port.

    Order: explicit ``--session`` > current tmux session (when run inside tmux)
    > the bare-``mozyo`` resolved canonical session name. The hard-coded
    ``agents`` default is intentionally not used (Redmine #10796 / #11429;
    Asana task 1214758916882465).
    """

    def __init__(self, reads: StatusSessionHelperReads) -> None:
        self._reads = reads

    def resolve(self, args: "argparse.Namespace") -> str:
        explicit = getattr(args, "session", None)
        if explicit:
            return explicit
        current = self._reads.current_session_name()
        if current:
            return current
        repo_root = self._reads.repo_root_from_args(args)
        return self._reads.canonical_session_name(repo_root)


# --------------------------------------------------------------------------- #
# Live adapter
# --------------------------------------------------------------------------- #


class LiveStatusSessionHelperReads:
    """Live adapter for :class:`StatusSessionHelperReads`.

    Routes each read through the ``commands`` module *at call time* (see the
    module docstring's compatibility-bridge note) so the residual
    ``commands``-owned leaf reads — and the tests / ``LiveLaunchOps`` callers
    that patch or route through them — stay intact during the migration.
    """

    def pane_lines(self) -> list:
        from mozyo_bridge.application import commands as _commands

        return _commands.pane_lines()

    def session_exists(self, session: str) -> bool:
        from mozyo_bridge.application import commands as _commands

        return _commands.session_exists(session)

    def session_cwd_mismatch(self, session: str, repo_root: Path) -> List[str]:
        from mozyo_bridge.application import commands as _commands

        return _commands.session_cwd_mismatch(session, repo_root)

    def current_session_name(self) -> Optional[str]:
        from mozyo_bridge.application import commands as _commands

        return _commands.current_session_name()

    def repo_root_from_args(self, args: "argparse.Namespace") -> Path:
        from mozyo_bridge.application import commands as _commands

        return _commands.repo_root_from_args(args)

    def canonical_session_name(self, repo_root: Path) -> str:
        from mozyo_bridge.application import commands as _commands

        return _commands.resolve_canonical_session(repo_root).name
