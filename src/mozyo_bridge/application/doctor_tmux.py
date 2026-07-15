"""Doctor tmux pane-health section boundary (#12881).

The ``doctor_tmux_section`` collector historically mixed three responsibilities
in one free-function body: the *external read* (tmux availability via
``command -v tmux``, the ``list-panes`` connection probe + pane count, the
structured ``pane_lines`` snapshot, the ``TMUX_PANE`` environment scope, and the
project ``.claude/skills`` directory probe used by the Claude cwd warning), the
*verdict authority* that decides the section ``status`` (``missing`` /
``skipped`` / ``ok`` / ``warning``), scopes the agent-window checks to the
current tmux session, classifies each agent window
(``missing`` / ``duplicate`` / ``ok`` / ``not-agent-process`` / ``unscoped``),
raises the Claude-pane-cwd-outside-repo warning, and de-dupes the operator
``next_action`` guidance, and the *legacy section dict assembly*. This module
carves the collector slice out of the ``doctor`` body into an OOP-first boundary
(#12638 / #12870 follow-up):

- :func:`evaluate_tmux_section` is the pure domain policy that decides the whole
  section dict from a tmux read-view alone (no subprocess, no tmux server, no
  environment access, no filesystem access). It owns the
  ``missing`` / ``skipped`` early returns, the current-session scoping, the
  per-agent window verdict, the Claude cwd warning, the ``next_action`` de-dupe,
  and the final ``ok`` / ``warning`` status. It re-assembles the legacy section
  dict byte-for-byte so the ``run_doctor`` aggregation, JSON output,
  ``format_doctor_text`` rendering, and ``doctor_instruction`` consumption are
  unchanged.
- :class:`TmuxPaneHealthReads` is the port for the *external read* and
  :class:`LiveTmuxPaneHealthReads` is the live adapter over the real tmux client
  + environment + checkout. The adapter resolves the repo root from the CLI
  namespace, runs the same two ``list-panes`` calls (the bare ``#{pane_id}``
  connection/count probe and the structured ``pane_lines`` snapshot), reads
  ``TMUX_PANE``, and probes ``.claude/skills`` — and it only calls
  ``pane_lines`` once the server connection is proven, mirroring the legacy
  short-circuit (``pane_lines`` would otherwise ``die`` with no tmux server).
  The tmux-availability ``subprocess`` probe, ``run_tmux``, and ``pane_lines``
  are resolved through the ``doctor`` module *at call time* (a localized lazy
  import, mirroring the other ``doctor_*`` section adapters): ``doctor`` imports
  this module, so a module-level back-import would cycle, and the existing
  ``doctor.subprocess`` / ``doctor.run_tmux`` / ``doctor.pane_lines``-patching
  section integration tests keep working unchanged.
- :class:`TmuxSectionUseCase` composes the port and the policy.

``_cwd_is_under_repo`` moves here from ``doctor.py`` because this collector was
its sole consumer; it is a pure path predicate, so the policy stays free of the
``doctor`` module (which imports this one — a module-level back-import would
cycle).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver import (
    is_agent_process,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.default_agent_topology import (
    DEFAULT_EXPECTED_AGENTS,
)


def _cwd_is_under_repo(cwd: str, repo_root: Path) -> bool:
    if not cwd:
        return True
    try:
        Path(cwd).expanduser().resolve().relative_to(repo_root.resolve())
    except ValueError:
        return False
    return True


def evaluate_tmux_section(view: dict[str, Any]) -> dict[str, Any]:
    """Pure policy: derive the legacy ``tmux`` section dict from a read-view.

    The view is the mapping returned by the read port:

    - ``tmux_pane`` (str): the ``TMUX_PANE`` environment value (``""`` unset).
    - ``tmux_installed`` (bool): whether ``tmux`` is on ``PATH``.
    - ``tmux_server_connected`` (bool, only when installed): whether the
      ``list-panes`` probe reached a tmux server.
    - ``panes_total`` (int, only when connected): pane count from the probe.
    - ``panes`` (list, only when connected): the structured ``pane_lines``
      snapshot.
    - ``repo_root`` (str, only when connected): the resolved repo root.
    - ``project_skills_dir_exists`` (bool, only when connected): whether
      ``<repo_root>/.claude/skills`` exists (gates the Claude cwd warning).

    The branching preserves the legacy collector exactly.
    """

    info: dict[str, Any] = {
        "status": "skipped",
        "next_action": [],
        "tmux_pane": view["tmux_pane"],
    }
    if not view["tmux_installed"]:
        info["status"] = "missing"
        info["detail"] = "tmux not installed"
        info["next_action"] = ["install tmux to use mozyo-bridge pane notifications"]
        return info
    if not view["tmux_server_connected"]:
        # Not running under a tmux server. Doctor stays usable outside tmux.
        info["status"] = "skipped"
        info["detail"] = (
            "not connected to a tmux server (run `mozyo` to start the repo session)"
        )
        return info
    panes = view["panes"]
    info["panes_total"] = view["panes_total"]
    info["agent_windows"] = {}
    info["warnings"] = []
    next_actions: list[str] = []

    # Scope agent checks to the current tmux session. Cross-session panes
    # are legitimate when the operator keeps parallel project sessions open.
    pane_env = view["tmux_pane"] or ""
    current_session: str | None = None
    if pane_env:
        for pane in panes:
            if pane["id"] == pane_env:
                location = pane.get("location") or ""
                current_session = location.split(":", 1)[0] or None
                break
    info["current_session"] = current_session or ""

    bad = False
    repo_root = Path(view["repo_root"])

    session_panes = (
        [
            pane
            for pane in panes
            if (pane.get("location") or "").split(":", 1)[0] == current_session
        ]
        if current_session is not None
        else []
    )

    # Judge the EXPECTED topology, not the full registry (Redmine #13569 known-vs-
    # expected split): doctor verifies each agent the session is expected to run has a
    # healthy window; a profile-only provider that is recognizable but not part of the
    # default launch pair is never flagged missing. Built-in expected == known, so this
    # is byte-identical for the shipped providers.
    for agent in sorted(DEFAULT_EXPECTED_AGENTS):
        window_panes = [pane for pane in session_panes if pane.get("window_name") == agent]
        window_indexes = {
            (pane.get("location") or "").split(":", 1)[1].split(".", 1)[0]
            for pane in window_panes
            if ":" in (pane.get("location") or "")
        }
        if current_session is None:
            window_entry: dict[str, Any] = {"status": "unscoped"}
        elif not window_panes:
            window_entry = {"status": "missing", "session": current_session}
            bad = True
            next_actions.append(
                f"run `mozyo` from the repo, or `mozyo-bridge init {agent}` from the pane "
                f"you want to be `{agent}`"
            )
        elif len(window_indexes) > 1:
            window_entry = {
                "status": "duplicate",
                "session": current_session,
                "windows": sorted(window_indexes),
            }
            bad = True
            next_actions.append(
                f"resolve duplicate `{agent}` windows in session '{current_session}'; "
                "tmux tolerates duplicates but the resolver does not"
            )
        else:
            active = next(
                (p for p in window_panes if p.get("pane_active") == "1"),
                window_panes[0],
            )
            command = Path(active.get("command") or "").name
            window_status = "ok" if is_agent_process(command) else "not-agent-process"
            window_entry = {
                "status": window_status,
                "session": current_session,
                "window": next(iter(window_indexes), ""),
                "id": active["id"],
                "process": command,
                "cwd": active.get("cwd", ""),
            }
            if window_status != "ok":
                bad = True
                next_actions.append(
                    f"`{agent}` window in session '{current_session}' is running "
                    f"`{command or '-'}`; start the agent CLI or `mozyo-bridge init "
                    f"{agent}` on the pane that is"
                )
            if agent == "claude" and window_status == "ok":
                if view["project_skills_dir_exists"] and not _cwd_is_under_repo(
                    active.get("cwd", ""), repo_root
                ):
                    info["warnings"].append(
                        {
                            "kind": "claude_pane_cwd_outside_repo",
                            "cwd": active.get("cwd", "") or "-",
                            "repo": str(repo_root),
                        }
                    )
                    bad = True
        info["agent_windows"][agent] = window_entry

    # De-dupe while preserving order so repeated suggestions (e.g. both
    # agents missing → both producing the bare-`mozyo` hint) collapse.
    seen: set[str] = set()
    info["next_action"] = [
        action for action in next_actions if not (action in seen or seen.add(action))
    ]
    info["status"] = "ok" if not bad else "warning"
    return info


@runtime_checkable
class TmuxPaneHealthReads(Protocol):
    """Port: read the tmux pane-health view for the doctor section.

    Implementations own the external read (tmux availability, the ``list-panes``
    connection probe + pane count, the structured pane snapshot, the
    ``TMUX_PANE`` scope, the repo root resolution, and the ``.claude/skills``
    probe). The use case and policy depend only on the returned read-view
    mapping.
    """

    def describe(self) -> dict[str, Any]:
        ...


class LiveTmuxPaneHealthReads:
    """Live adapter: read the tmux pane-health view from the real environment.

    The repo root is resolved from the CLI namespace (``args.repo`` or ``.``)
    exactly as the legacy collector did. ``pane_lines`` is only called once the
    server connection is proven, mirroring the legacy short-circuit: outside a
    tmux server ``pane_lines`` would ``die`` rather than return. The
    ``subprocess`` tmux-availability probe, ``run_tmux``, and ``pane_lines`` are
    resolved through the ``doctor`` module at call time (lazy import) so the
    existing section integration tests that patch ``doctor.subprocess`` /
    ``doctor.run_tmux`` / ``doctor.pane_lines`` are unchanged, and to avoid the
    ``doctor`` <-> ``doctor_tmux`` import cycle.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self._args = args

    def describe(self) -> dict[str, Any]:
        from mozyo_bridge.application import doctor as _doctor

        view: dict[str, Any] = {
            "tmux_pane": os.environ.get("TMUX_PANE", ""),
        }
        if _doctor.subprocess.run(
            ["sh", "-c", "command -v tmux >/dev/null 2>&1"]
        ).returncode != 0:
            view["tmux_installed"] = False
            return view
        view["tmux_installed"] = True

        list_result = _doctor.run_tmux(
            "list-panes", "-a", "-F", "#{pane_id}", check=False
        )
        if list_result.returncode != 0:
            view["tmux_server_connected"] = False
            return view
        view["tmux_server_connected"] = True
        view["panes_total"] = len(list_result.stdout.splitlines())
        view["panes"] = _doctor.pane_lines()

        repo_root_raw = getattr(self._args, "repo", None) or "."
        repo_root = Path(repo_root_raw).expanduser().resolve()
        view["repo_root"] = str(repo_root)
        view["project_skills_dir_exists"] = (
            repo_root / ".claude" / "skills"
        ).exists()
        return view


class TmuxSectionUseCase:
    """Use case: read the tmux pane-health view, apply the verdict policy.

    Returns the legacy ``doctor_tmux_section`` dict shape byte-for-byte.
    """

    def __init__(self, reads: TmuxPaneHealthReads) -> None:
        self._reads = reads

    def execute(self) -> dict[str, Any]:
        view = self._reads.describe()
        return evaluate_tmux_section(view)
