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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (
    ATTEST_CAPABILITY_MARKER,
    MOZYO_BRIDGE_LAUNCHER_ENV,
    build_attest_capability_probe_argv,
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


def preflight_attest_launcher_capability(
    launcher: str,
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
) -> None:
    """Fail closed unless ``launcher`` can run the ``herdr agent-attest`` wrapper.

    Redmine #13748. The #13637 managed launch execs every provider THROUGH
    ``<launcher> herdr agent-attest ...`` so the agent self-attests its injected
    identity env before the provider starts. :func:`resolve_attest_launcher` verifies
    the launcher is an *executable* but NOT that its CLI still carries that subcommand:
    an installed launcher can lag unreleased source (measured — installed
    ``mozyo-bridge 0.10.0`` answers the wrapper subcommand with argparse ``invalid
    choice`` / exit 2 while the source tree succeeds), so every wrapped pane exits ~0.4 s
    after start, ``sublane create`` returns a live locator, and the lane then vanishes —
    the failure this preflight closes.

    The probe (:func:`build_attest_capability_probe_argv`) runs the subcommand with
    ``--help``, which dispatches and short-circuits before any actuation (no attestation
    write, no provider exec, no pane). It is invoked HERE — before the caller's first
    ``workspace`` / ``tab`` / ``agent`` write — so an executable-but-incapable launcher
    aborts the run with zero side effect.

    A positive verdict requires BOTH an exit code of 0 AND
    :data:`ATTEST_CAPABILITY_MARKER` in the probe output (review R1). The exit code alone
    is not proof: a success-exit non-launcher (e.g. ``/usr/bin/true``) ignores the args and
    exits 0 without the subcommand, then the real launch — which runs the SAME launcher as
    the wrapper's ``argv[0]`` — would still exit before ``exec``ing the provider, the exact
    vanishing lane this closes. So an exit-0 without the marker fails closed just like a
    non-zero exit; a mechanical failure to even run the probe fails closed too. The error
    names the launcher path, the required command, and the two recovery actions
    (release/install a capable ``mozyo-bridge``, or pin an explicit absolute
    :data:`MOZYO_BRIDGE_LAUNCHER_ENV`); it is raised, never written to a durable store, so
    no personal path is persisted.

    Only an executable-but-incapable launcher reaches here. An unresolvable launcher is
    already ``""`` (wrapping disabled — the byte-invariant #13637 fallback), and the
    caller only probes when a wrapper will actually run (a launch plan under a resolved
    launcher); an adopt-only / dry-run session starts no wrapped process and is never
    probed.
    """
    probe = build_attest_capability_probe_argv(launcher)
    try:
        completed = runner(
            probe, capture_output=True, text=True, timeout=timeout, env=dict(env)
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HerdrSessionStartError(
            f"managed-launch preflight could not run launcher {launcher!r} to verify it "
            f"provides the required `herdr agent-attest` wrapper subcommand "
            f"({exc.__class__.__name__}); refusing to launch a provider whose "
            f"self-attestation wrapper may exit before the provider starts. Recovery: "
            f"install or release a mozyo-bridge whose CLI has `herdr agent-attest`, or "
            f"set {MOZYO_BRIDGE_LAUNCHER_ENV} to an absolute launcher that has it."
        ) from exc
    if completed.returncode != 0:
        raise HerdrSessionStartError(
            f"selected managed-launch launcher {launcher!r} cannot run the required "
            f"`herdr agent-attest` wrapper subcommand (probe exited "
            f"{completed.returncode}); its self-attestation wrapper would exit before the "
            f"provider starts, leaving a partial / immediately-vanishing lane. Recovery: "
            f"install or release a mozyo-bridge whose CLI has `herdr agent-attest`, or set "
            f"{MOZYO_BRIDGE_LAUNCHER_ENV} to an absolute launcher that has it."
        )
    # Exit 0 is necessary but NOT sufficient (review R1): a success-exit non-launcher
    # (e.g. `/usr/bin/true`) ignores the args and exits 0 without the subcommand, then the
    # real launch — running the SAME launcher as the wrapper's argv[0] — exits before the
    # provider. Require the marker the wrapper actually passes to appear in the probe
    # output, the positive signal that this launcher carries the `agent-attest` contract.
    output = (completed.stdout or "") + (completed.stderr or "")
    if ATTEST_CAPABILITY_MARKER not in output:
        raise HerdrSessionStartError(
            f"selected managed-launch launcher {launcher!r} exited 0 for the "
            f"`herdr agent-attest` probe but did not emit the expected wrapper contract "
            f"marker {ATTEST_CAPABILITY_MARKER!r}; a bare success exit does not prove the "
            f"subcommand (e.g. a non-mozyo executable that ignores its arguments), and its "
            f"wrapper would exit before the provider starts, leaving a partial / "
            f"immediately-vanishing lane. Recovery: install or release a mozyo-bridge whose "
            f"CLI has `herdr agent-attest`, or set {MOZYO_BRIDGE_LAUNCHER_ENV} to an "
            f"absolute launcher that has it."
        )


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
    "preflight_attest_launcher_capability",
)
