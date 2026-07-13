"""herdr command invocation + workspace/tab creation & root-pane reclaim.

The third cohesive sibling of :mod:`herdr_session_start`, alongside
:mod:`herdr_lane_topology` (the pure placement / parse decision core) and
:mod:`herdr_launch_argv` (the pure ``agent start`` argv assembly). Where those two are
pure, this module owns the session-start flow's **side-effecting herdr commands**: the one
fail-closed command runner every step shares (:func:`_invoke`), the ``agent list`` read
(:func:`_list_rows`), and the empty-base-pane / tab lifecycle helpers
(:func:`_create_workspace`, :func:`_create_tab`, :func:`_close_base_pane`).

Homing them here — the extraction the ``module_health.yaml`` allowlist entry for
``herdr_session_start`` pre-declared ("a further reduction could extract the empty-base-pane
/ tab reclaim helpers if this grows"), carried out when Redmine #13646 added the
config-driven ``lane_placement`` axis — keeps the session-start composition root focused on
classification / placement / reclaim *orchestration* and drops it back under the
module-health threshold.

The reclaim contract is unchanged (Redmine #13330 / #13411): a workspace / tab this run
**creates** is the only reclaim target, so its empty root pane is a *known* handle rather
than one scanned for (a user's own shell can never be mis-closed); a create that returns an
unparseable payload fails closed rather than guessing a pane; and a ``pane close`` failure
is recorded non-fatally (the agents are already live, an empty root pane is only cosmetic).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_state import (
    _extract_list_rows,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    Runner,
    _bounded_detail,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (
    HerdrSessionStartError,
    _parse_tab_created,
    _parse_workspace_created,
)


def _invoke(
    binary: str,
    tail: Sequence[str],
    runner: Runner,
    timeout: float,
    *,
    env: Optional[Mapping[str, str]],
) -> "subprocess.CompletedProcess[str]":
    """Run ``binary tail...`` fail-closed; raise on any mechanical / non-zero failure."""
    argv = [binary, *tail]
    try:
        completed = runner(
            argv, capture_output=True, text=True, timeout=timeout, env=env
        )
    except FileNotFoundError:
        raise HerdrSessionStartError(f"herdr binary not found: {binary!r}")
    except subprocess.TimeoutExpired:
        raise HerdrSessionStartError(f"herdr command timed out: {list(tail)!r}")
    except OSError as exc:
        raise HerdrSessionStartError(
            f"herdr command failed ({exc.__class__.__name__}): {list(tail)!r}"
        )
    if completed.returncode != 0:
        raise HerdrSessionStartError(
            _bounded_detail(completed.stderr)
            or f"herdr {list(tail)!r} exited {completed.returncode}"
        )
    return completed


def _list_rows(binary: str, runner: Runner, timeout: float) -> Sequence[Mapping[str, object]]:
    """Run herdr ``agent list`` and return raw rows (fail-closed)."""
    completed = _invoke(binary, ["agent", "list"], runner, timeout, env=None)
    rows = _extract_list_rows(completed.stdout)
    if rows is None:
        raise HerdrSessionStartError(
            "herdr agent list payload was not a recognised JSON array or agents object"
        )
    return rows


def _create_workspace(
    binary: str,
    repo_root: Path,
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
    label: str = "",
) -> tuple[str, str]:
    """Explicitly create a herdr workspace; return ``(workspace_id, root_pane_id)``.

    Making the workspace ourselves (rather than letting the first ``agent start``
    auto-create it) is what turns the empty base pane into a *known* handle we can
    reclaim by id — never one we scan for. ``--no-focus`` avoids stealing the
    operator's focus. ``label`` (Redmine #13380) names a minted sublane host
    workspace for the operator — cosmetic only, never a join key. Fails closed if
    the response is unparseable.
    """
    argv = ["workspace", "create", "--cwd", str(repo_root)]
    if label:
        argv.extend(["--label", label])
    argv.append("--no-focus")
    completed = _invoke(
        binary,
        argv,
        runner,
        timeout,
        env=dict(env),
    )
    parsed = _parse_workspace_created(completed.stdout)
    if parsed is None:
        raise HerdrSessionStartError(
            "herdr workspace create returned no parseable workspace id / root pane "
            "(expected result.workspace.workspace_id + result.root_pane.pane_id in a "
            "workspace_created payload); refuse to guess a pane to reclaim"
        )
    return parsed


def _create_tab(
    binary: str,
    workspace_id: str,
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
    label: str = "",
) -> tuple[str, str]:
    """Explicitly create a herdr tab in ``workspace_id``; return ``(tab_id, root_pane_id)``.

    Lane=tab subdivision (Redmine #13411): a non-default lane gets its OWN tab in
    the sublane host workspace, its gateway + worker placed as a split pair inside
    it. Minting the tab ourselves turns its empty root pane into a *known* handle
    to reclaim by id (the tab analogue of the #13330 workspace base pane), never
    one we scan for. ``--label`` (the lane label) is cosmetic and operator-readable
    only — every join decision keys on the live ``tab_id``, never the label.
    ``--no-focus`` avoids stealing the operator's focus. Fails closed if the
    response is unparseable.
    """
    argv = ["tab", "create", "--workspace", workspace_id]
    if label:
        argv.extend(["--label", label])
    argv.append("--no-focus")
    completed = _invoke(binary, argv, runner, timeout, env=dict(env))
    parsed = _parse_tab_created(completed.stdout)
    if parsed is None:
        raise HerdrSessionStartError(
            "herdr tab create returned no parseable tab id / root pane "
            "(expected result.tab.tab_id + result.root_pane.pane_id in a "
            "tab_created payload); refuse to guess a pane to reclaim"
        )
    return parsed


def _close_base_pane(
    binary: str,
    pane_id: str,
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
) -> tuple[bool, str]:
    """Reclaim a created root pane; **never hard-fail** (cosmetic residue only).

    Used for both the #13330 workspace base pane and the #13411 lane tab root
    pane. Returns ``(True, "")`` on a clean close, else ``(False, <detail>)``. A
    failed reclaim only leaves the harmless empty root pane behind — the agent
    slots are already live — so it is recorded, not raised (Redmine #13330 ruling
    j#73225).
    """
    try:
        _invoke(binary, ["pane", "close", pane_id], runner, timeout, env=dict(env))
    except HerdrSessionStartError as exc:
        return False, _bounded_detail(str(exc)) or "herdr pane close failed"
    return True, ""


__all__ = (
    "_close_base_pane",
    "_create_tab",
    "_create_workspace",
    "_invoke",
    "_list_rows",
)
