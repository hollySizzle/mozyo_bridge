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
    _parse_workspace_list,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (
    MOZYO_BRIDGE_LAUNCHER_ENV,
    build_attest_capability_probe_argv,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (
    LauncherCapabilityObservation,
    decide_launcher_capability,
    decide_store_compatibility,
    parse_launcher_capability_output,
)
from mozyo_bridge.core.state.herdr_identity_attestation import (
    HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
    herdr_identity_attestation_path,
)
from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
    probe_store_schema,
)


class HerdrLauncherIncompatibleError(HerdrSessionStartError):
    """The probed managed-launch launcher is executable but capability-incompatible.

    Redmine #13847: a subclass of :class:`HerdrSessionStartError` so every existing
    caller that fails closed on a session-start error still does — but a caller that must
    surface a *typed* ``launcher_runtime_incompatible`` blocker (the ``sublane
    create/start`` path) can catch this specifically. Raised only for a launcher that ran
    the probe but failed the capability contract (wrong / absent ``agent-attest``
    subcommand, or an attestation-store schema that does not match the source runtime);
    a mechanical failure to run the probe stays a plain :class:`HerdrSessionStartError`.
    """

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        #: The :mod:`herdr_launcher_capability` verdict reason token (for the typed blocker).
        self.reason = reason


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
    repo_root=None,
) -> LauncherCapabilityObservation:
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

    A positive verdict requires an exit code of 0 AND — decided purely in
    :mod:`herdr_launcher_capability` from the probe output — the ``agent-attest``
    subcommand marker AND an advertised attestation-store schema that exactly matches this
    runtime's (Redmine #13847). The exit code alone is not proof: a success-exit
    non-launcher (e.g. ``/usr/bin/true``) ignores the args and exits 0 without the
    subcommand, then the real launch — which runs the SAME launcher as the wrapper's
    ``argv[0]`` — would still exit before ``exec``ing the provider, the exact vanishing
    lane this closes. The **schema** check is what #13847 adds over the #13748
    subcommand-only check: an older installed launcher carries ``agent-attest`` +
    ``--assigned-name`` (so the subcommand marker passes) but its attestation store is a
    stale schema; injected with this runtime's shared ``MOZYO_BRIDGE_HOME`` it opens the
    newer store, hits the exact-version write guard, silently drops the attestation, and
    the pair boots **live but unattested** — no public recovery, the failure #13847 closes.

    This function is the **probe adapter** only: it runs the subprocess, then delegates
    the verdict to the pure :func:`parse_launcher_capability_output` /
    :func:`decide_launcher_capability` (probe / decision separation, #13847 item 6). A
    capability-verdict failure raises the typed :class:`HerdrLauncherIncompatibleError`
    (carrying the verdict reason) so a caller can surface a typed
    ``launcher_runtime_incompatible`` blocker; a mechanical failure to run the probe stays
    a plain :class:`HerdrSessionStartError`. The error names the launcher path, the
    required command, and the two recovery actions (release/install a capable
    ``mozyo-bridge``, or pin an explicit absolute :data:`MOZYO_BRIDGE_LAUNCHER_ENV`); it is
    raised, never written to a durable store, so no personal path is persisted.

    Only an executable-but-incapable launcher reaches here. An unresolvable launcher is
    already ``""`` (wrapping disabled — the byte-invariant #13637 fallback), and the
    caller only probes when a wrapper will actually run (a launch plan under a resolved
    launcher); an adopt-only / dry-run session starts no wrapped process and is never
    probed.

    Returns the parsed :class:`LauncherCapabilityObservation` (Redmine #13882) so the
    caller can feed the same probe into :func:`preflight_attest_store_schema` without
    re-running the subprocess. Every check here remains **code vs code**; the store join
    is the separate step.
    """
    recovery = (
        f" Recovery: install or release a mozyo-bridge whose CLI has `herdr agent-attest` "
        f"at attestation schema v{HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION}, or set "
        f"{MOZYO_BRIDGE_LAUNCHER_ENV} to an absolute launcher that has it."
    )
    probe = build_attest_capability_probe_argv(launcher)
    # Redmine #14231 (root cause j#84906, disposition j#84910): the probe MUST run with the
    # same cwd the real wrapper gets. `build_agent_start_argv` passes `--cwd <repo_root>` to
    # `herdr agent start`, so the wrapper process starts inside the lane worktree — and a
    # mozyo-bridge CLI reads that directory's `.mozyo-bridge/config.yaml` at startup. A
    # launcher predating a config schema bump therefore exits non-zero THERE while exiting 0
    # in a config-less directory. Probing in the caller's cwd made that skew invisible:
    # measured on one binary, exit 0 from a config-less cwd and exit 2 from the v2 lane cwd,
    # so the probe passed and only the wrapper died — the vanishing pair of #14222 j#84620.
    # `None` keeps the caller's cwd (the pre-#14231 shape) for callers that have no repo
    # root to point at.
    try:
        completed = runner(
            probe,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(env),
            cwd=str(repo_root) if repo_root else None,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HerdrSessionStartError(
            f"managed-launch preflight could not run launcher {launcher!r} to verify it "
            f"provides the required `herdr agent-attest` wrapper subcommand "
            f"({exc.__class__.__name__}); refusing to launch a provider whose "
            f"self-attestation wrapper may exit before the provider starts." + recovery
        ) from exc
    if completed.returncode != 0:
        raise HerdrSessionStartError(
            f"selected managed-launch launcher {launcher!r} cannot run the required "
            f"`herdr agent-attest` wrapper subcommand (probe exited "
            f"{completed.returncode}); its self-attestation wrapper would exit before the "
            f"provider starts, leaving a partial / immediately-vanishing lane." + recovery
        )
    # Exit 0 is necessary but NOT sufficient: the pure decision requires the subcommand
    # marker AND an exactly-matching advertised attestation schema (#13847). A success-exit
    # non-launcher lacks the marker; an older installed launcher carries the marker but
    # advertises a stale (or no) schema — both fail closed here, before any launch.
    output = (completed.stdout or "") + (completed.stderr or "")
    observation = parse_launcher_capability_output(output)
    verdict = decide_launcher_capability(
        observation,
        required_schema_version=HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
    )
    if not verdict.ok:
        raise HerdrLauncherIncompatibleError(
            f"selected managed-launch launcher {launcher!r} exited 0 for the "
            f"`herdr agent-attest` probe but {verdict.detail}; refusing to launch a "
            f"provider whose self-attestation would be dropped, leaving a partial / "
            f"immediately-vanishing or live-but-unattested lane." + recovery,
            reason=verdict.reason,
        )
    return observation


def preflight_attest_store_schema(
    observation: LauncherCapabilityObservation,
    *,
    store_home: Path,
    replacement_launch: bool = False,
) -> None:
    """Fail closed unless the SELECTED home store's real shape can be attested into.

    Redmine #13882 — the check the #13847 capability preflight structurally cannot make.
    That one joins the launcher's *advertised* schema against this runtime's *required*
    schema; both are **code**, so two v2 runtimes agree, the probe passes, and nothing
    ever opens the store that will actually be written. The measured failure: a shared
    ``MOZYO_BRIDGE_HOME`` holding the pre-0.12 v1 shape. The launch is wrapped with
    ``--env MOZYO_BRIDGE_HOME=<store_home>``, the child's best-effort write hits the store
    guard, swallows the error (an agent boot must never be blocked by a store failure),
    and the pair boots **live but unattested** with every downstream verify failing closed.

    Store-side only, and read-only: it probes the store as it lies and **never migrates**
    (a launch that silently migrated the shared home would break every older installed
    launcher the same way — see ``herdr_identity_attestation_schema``). Called next to the
    launcher probe, i.e. before the caller's first ``workspace`` / ``tab`` / ``agent``
    write, so an incompatible store aborts with zero herdr side effect (acceptance 1).

    A v1 store with a **normal** launch is admitted: the launcher writes it v1-shaped and
    ``replacement_action_id`` is empty, so nothing is dropped and no generation is
    fabricated. A **replacement** launch is refused there, because that field cannot
    survive the v1 shape. The error is raised, never persisted, so no personal path is
    written to a durable store.
    """
    verdict = decide_store_compatibility(
        observation,
        probe_store_schema(herdr_identity_attestation_path(Path(store_home))),
        required_schema_version=HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
        replacement_launch=replacement_launch,
    )
    if not verdict.ok:
        raise HerdrLauncherIncompatibleError(
            f"managed-launch preflight refused the selected attestation store: "
            f"{verdict.detail}. No workspace / tab / agent was created.",
            reason=verdict.reason,
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


def _list_workspace_labels(
    binary: str, runner: Runner, timeout: float
) -> Optional[Mapping[str, str]]:
    """Run herdr ``workspace list`` and return ``{workspace_id: label}`` (or ``None``).

    The backend-readable label authority for the shared coordinators space
    (Redmine #14139 ``shared_space``, Design Answer j#83385 Decision 1). Called ONLY
    on the shared-space default-lane path, so ``per_project_space`` and every
    sublane launch never issue this extra command and stay byte-for-byte the
    pre-#14139 choreography. Returns ``None`` — "labels unreadable" — when the
    payload is not a recognisable ``workspace list`` shape, which the resolver
    treats as fail-closed (never a guessed shared space).
    """
    completed = _invoke(binary, ["workspace", "list"], runner, timeout, env=None)
    return _parse_workspace_list(completed.stdout)


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
    operator's focus. ``label`` names a minted workspace for the operator: for a
    **sublane host** (Redmine #13380) it is cosmetic and never a join key; for the
    **shared coordinators space** (Redmine #14139) the SAME label
    (``coordinators``) is the backend-readable adopt authority a later project
    re-reads via ``workspace list`` (:func:`_shared_coordinator_target`). Either
    way the label is set once at create and never mutated. Fails closed if the
    response is unparseable.
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
    "HerdrLauncherIncompatibleError",
    "_close_base_pane",
    "_create_tab",
    "_create_workspace",
    "_invoke",
    "_list_rows",
    "preflight_attest_launcher_capability",
)
