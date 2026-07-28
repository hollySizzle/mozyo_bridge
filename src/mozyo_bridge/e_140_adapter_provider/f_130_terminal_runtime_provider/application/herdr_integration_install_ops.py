"""Opt-in herdr integration-hook installer — orchestration ops (Redmine #13249).

The pure model (:mod:`...domain.herdr_integration_install`) owns the agent
vocabulary, the fail-closed reason set, and the snapshot / diff / path-safety data
model. This ops layer is the IO edge that turns a plan into a *transaction* around
herdr's own ``integration install``:

- **plan** (default, read-only) — resolve each agent's config dir under the operator
  home, run every gate (agent known, dir present + safe, herdr posture pinned), and
  report what an apply *would* do. It makes **zero mutation** — the invariant the CLI
  proves with a before/after byte compare.
- **apply** (explicit ``--apply`` opt-in) — only when *every* agent gate passes: for
  each agent, snapshot the dir, back up its (non-credential) files, invoke
  ``herdr integration install <agent>`` through the injected runner, snapshot again,
  and diff. If any agent fails, the whole transaction rolls back — every
  already-installed agent is restored to its pre-snapshot — so a partial multi-agent
  failure leaves home byte-for-byte as it was found (issue #13249: "atomic/rollback",
  "部分失敗は成功扱いしない").

Boundaries kept enforced here:

- **herdr owns the hook; mozyo only brackets it.** The runner runs herdr's real
  ``integration install``; mozyo never authors hook bytes. The runner is injected so
  tests drive a fake herdr and never spawn a live one.
- **Credentials are never read.** Snapshots and backups skip credential-shaped files
  (:func:`~...domain.herdr_integration_install.is_credential_shaped`), so no operator
  secret is hashed, copied, diffed, or restored. The filesystem half of the transaction
  — reading, backing up and restoring a config dir, and the target-identity guard on
  every write — lives in :mod:`...herdr_integration_install_dir_io`.
- **The herdr binary is trusted-environment only.** Apply resolves it through the
  shared :func:`~...infrastructure.herdr_transport.resolve_herdr_binary` (env /
  trusted PATH), the same fail-closed resolver every herdr surface uses (#13496); a
  repo-local value can never point it at an arbitrary executable.
- **Path safety, re-asked at every action.** A config dir whose realpath escapes home
  (a symlink or ``..`` traversal) is refused before any snapshot or mutation — and the
  same question is re-asked before each herdr invocation and before any rollback write,
  because the gate's answer describes the moment it looked, while every mutation
  happens later. Identity, not just containment: the dir must still be the one this
  operation was staged against.
- **The verified config is the config herdr reads — content and all.** The pin posture
  is proven against a specific file, and an apply binds herdr to *that* file via
  :data:`HERDR_CONFIG_PATH_ENV`; an environment naming a different config is refused
  rather than overridden, so a pinned file can never be a decoy for an unpinned run.
  Because a pin is a claim about *content*, the file's digest and identity are captured
  when it verifies and re-asserted immediately before and after each invocation. What
  those two checks can prove is bounded and worth stating exactly: they detect drift
  that is **still present** when one of them runs, and a drift caught after the run is
  rolled back rather than left installed. They cannot see a config swapped to unpinned
  and restored *within* the invocation — herdr would read the unpinned bytes and the
  hook would remain. Closing that would require herdr to read a config this process
  holds open, which is not something this installer can impose; the runbook assigns it
  to operator write-authority over the config file instead.
- **Completeness is part of the data, not a later check.** Every read of a config dir
  returns its snapshot / backup together with the listing and read failures that
  produced it (:class:`~...herdr_integration_install_dir_io.DirRead`,
  :class:`~...herdr_integration_install_dir_io.DirBackup`), because a subtree that could
  not be enumerated disappears from every set at once and would otherwise read as
  *absent*. An apply starts only from a fully-read dir and reports success only when
  the post-apply dir can be fully read back.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_integration_install import (
    AGENT_CONFIG_DIRNAME,
    REASON_CONFIG_DIR_MISSING,
    REASON_CONFIG_DIR_UNREADABLE,
    REASON_CONFIG_PIN_MISMATCH,
    REASON_HERDR_ERROR,
    REASON_HERDR_UNRESOLVED,
    REASON_PARTIAL_FAILURE,
    REASON_ROLLBACK_INCOMPLETE,
    REASON_UNPINNED_REMOTE,
    REASON_UNSAFE_CONFIG_PATH,
    AgentInstallOutcome,
    AgentInstallPlan,
    DirSnapshot,
    InstallReport,
    SnapshotDiff,
    diff_snapshots,
    is_safe_config_dir,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_integration_install_dir_io import (
    DirBackup,
    DirIdentity,
    DirRead,
    backup_dir,
    config_dir_drift,
    observe_config_dir,
    read_dir,
    rollback_dir,
    with_identity_bracket,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pin_posture_ops import (
    verify_config,
)

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

#: How long a single ``herdr integration install`` may block before it is treated
#: as a herdr error. Kept short so an unresponsive herdr fails closed quickly.
COMMAND_TIMEOUT_SECONDS = 30

#: The environment variable herdr reads its config from. PoC #13175 measured that
#: ``HERDR_CONFIG_PATH`` overrides herdr's XDG config resolution
#: (``vibes/docs/logics/herdr-poc-13175-experiment-log.md``), which is exactly why the
#: pin gate cannot stop at "some file is pinned": an apply binds herdr to the *verified*
#: config through this variable, so the file whose posture was proven is the file herdr
#: actually reads (Redmine #13249 review j#91688 finding 1).
HERDR_CONFIG_PATH_ENV = "HERDR_CONFIG_PATH"


@dataclass(frozen=True)
class InstallInputs:
    """Everything the installer needs, resolved by the CLI (never global state).

    ``home`` is the operator home the agent config dirs sit under (the CLI defaults
    it to ``$HOME`` but a test injects a temp dir). ``herdr_config`` is the herdr
    config whose pin posture gates the install **and** which an apply binds herdr to
    (:data:`HERDR_CONFIG_PATH_ENV`), so the verified file is the effective one;
    ``manifest_catalog_url`` is the observed pinned-mirror env value (or ``None``).
    ``env`` is the trusted environment used to resolve the herdr binary and passed to
    the runner so herdr resolves the agent dirs under the same ``home``. ``runner`` is
    injected (a fake in tests).
    """

    home: Path
    agents: "tuple[str, ...]"
    herdr_config: Optional[Path] = None
    manifest_catalog_url: Optional[str] = None
    env: Optional[Mapping[str, str]] = None
    runner: Optional[Runner] = None




# --- gating (read-only) ------------------------------------------------------


def _config_dir(home: Path, agent: str) -> Path:
    return home / AGENT_CONFIG_DIRNAME[agent]


def _gate_agent(
    agent: str,
    home: Path,
    *,
    pinned: bool,
    pin_detail: str,
    bind_conflict: Optional[str],
    binary: Optional[str],
    binary_detail: str,
) -> AgentInstallPlan:
    """Run the read-only gates for one agent and return its plan.

    A plan promises an apply *could* run for a ready agent, so the trusted herdr
    binary must resolve for the plan to be ready — an unresolvable binary gates the
    plan closed (``herdr_unresolved``) rather than being demoted to a cosmetic
    ``detail`` while the plan still reports ``ok`` (Redmine #13249 review j#83613
    finding 2). The security / filesystem gates (pinned posture, config identity, dir
    present, safe path) are reported first so their more actionable reason surfaces;
    the binary gate is the last precondition before ``ready``.

    The posture gate is two questions, not one: *is a config pinned* and *is that the
    config herdr will read*. ``bind_conflict`` carries the second — an environment that
    names a different config makes the verified pin unrelated to the run, so the plan
    is gated ``config_pin_mismatch`` (Redmine #13249 review j#91688 finding 1).
    """
    config_dir = _config_dir(home, agent)
    display = str(config_dir)
    if not pinned:
        return AgentInstallPlan(
            agent=agent,
            config_dir=display,
            ready=False,
            reason=REASON_UNPINNED_REMOTE,
            detail=f"herdr posture not pinned: {pin_detail}",
        )
    if bind_conflict:
        return AgentInstallPlan(
            agent=agent,
            config_dir=display,
            ready=False,
            reason=REASON_CONFIG_PIN_MISMATCH,
            detail=bind_conflict,
        )
    if not config_dir.exists():
        return AgentInstallPlan(
            agent=agent,
            config_dir=display,
            ready=False,
            reason=REASON_CONFIG_DIR_MISSING,
            detail=f"config dir {display} does not exist; create it first (herdr "
            f"refuses to install a hook into a missing dir)",
        )
    home_real = os.path.realpath(home)
    config_real = os.path.realpath(config_dir)
    if not os.path.isdir(config_real) or not is_safe_config_dir(
        resolved=config_real, home_resolved=home_real
    ):
        return AgentInstallPlan(
            agent=agent,
            config_dir=display,
            ready=False,
            reason=REASON_UNSAFE_CONFIG_PATH,
            detail=f"config path {display} resolves outside home or is not a "
            f"directory (symlink / traversal); refusing to touch it",
        )
    if binary is None:
        return AgentInstallPlan(
            agent=agent,
            config_dir=display,
            ready=False,
            reason=REASON_HERDR_UNRESOLVED,
            detail=f"herdr binary unresolved from the trusted environment: "
            f"{binary_detail}",
        )
    return AgentInstallPlan(
        agent=agent,
        config_dir=display,
        ready=True,
        herdr_argv=_herdr_argv(binary, agent),
    )


def _resolve_binary(inputs: InstallInputs) -> "tuple[Optional[str], str]":
    """Resolve the trusted herdr binary, or ``(None, detail)`` on failure."""
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
        resolve_herdr_binary,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
        TerminalTransportError,
    )

    env = inputs.env if inputs.env is not None else os.environ
    try:
        resolution = resolve_herdr_binary(env)
    except TerminalTransportError as exc:
        return None, str(exc)
    return resolution.path, ""


@dataclass(frozen=True)
class _ConfigPin:
    """What was actually proven about the herdr config, in a re-checkable form.

    A pin is a claim about *content* — "these ``[update]`` switches are off" — so
    carrying only the config's path forward is not enough to keep the claim true: the
    same path can hold different bytes a moment later. This record therefore keeps the
    content digest and the filesystem identity (``st_dev`` / ``st_ino``) observed when
    the posture verified, so the claim can be re-asserted against the file as it stands
    at each action time (Redmine #13249 review j#91762 finding 2).
    """

    path: str
    mode: Optional[str]
    digest: str
    identity: "tuple[int, int]"


def _observe_config(
    config_path: Path, manifest_catalog_url: Optional[str]
) -> "tuple[Optional[_ConfigPin], str]":
    """Verify the posture and capture the pinned file's content + identity, together.

    Returns ``(pin, detail)``; ``pin`` is ``None`` when the config is not pinned or
    could not be read, and ``detail`` explains it either way.
    """
    result = verify_config(config_path, manifest_catalog_url=manifest_catalog_url)
    verdict = result.verdict
    if not verdict.pinned:
        return None, f"[{verdict.reason}] {verdict.detail}"
    try:
        raw = config_path.read_bytes()
        st = os.stat(config_path)
    except OSError as exc:
        return None, (
            f"herdr config at {config_path} verified pinned but could not be pinned to "
            f"an identity ({exc.__class__.__name__}); refusing to proceed"
        )
    return (
        _ConfigPin(
            path=os.path.realpath(config_path),
            mode=verdict.mode,
            digest=hashlib.sha256(raw).hexdigest(),
            identity=(st.st_dev, st.st_ino),
        ),
        verdict.detail,
    )


def _config_pin_drift(
    inputs: InstallInputs, pin: _ConfigPin, *, when: str
) -> "Optional[str]":
    """Re-assert ``pin`` against the config as it stands now; return a detail or ``None``.

    Both halves are re-checked: the posture must still verify as pinned, **and** the
    file must still be the same bytes and the same inode. Either alone is insufficient
    — a swap to a different pinned file would pass the posture check, and a content
    edit that keeps the posture pinned is not what was approved.

    Scope of what this can prove, stated precisely because the difference matters: a
    check sees the config *as it stands when the check runs*. Calling it before and
    after the invocation therefore catches drift that persists past either point, and
    the post-run call is what turns a late-detected swap into a rollback instead of an
    installed hook. A swap made and undone entirely between the two calls leaves no
    trace for either to find; that residual is documented rather than implied to be
    covered (Redmine #13249 review j#91805 finding 3).
    """
    if inputs.herdr_config is None:
        return f"the herdr config is no longer known {when}"
    current, detail = _observe_config(inputs.herdr_config, inputs.manifest_catalog_url)
    if current is None:
        return f"herdr config no longer verifies as pinned {when}: {detail}"
    if current.path != pin.path:
        return (
            f"herdr config path resolved to {current.path} {when} but the posture was "
            f"verified against {pin.path}"
        )
    if current.identity != pin.identity:
        return (
            f"herdr config at {pin.path} was replaced {when} (different file identity); "
            f"the bytes herdr reads are not the bytes whose posture was verified"
        )
    if current.digest != pin.digest:
        return (
            f"herdr config at {pin.path} changed content {when}; the bytes herdr reads "
            f"are not the bytes whose posture was verified"
        )
    return None


def _pin_state(inputs: InstallInputs) -> "tuple[Optional[_ConfigPin], str]":
    """Return ``(pin, detail)`` for the gate's herdr posture check."""
    if inputs.herdr_config is None:
        return (
            None,
            "no herdr config path supplied; cannot prove the posture is pinned",
        )
    return _observe_config(inputs.herdr_config, inputs.manifest_catalog_url)


def _config_binding(inputs: InstallInputs) -> "tuple[Optional[str], Optional[str]]":
    """Return ``(bound_config, conflict_detail)`` for the config herdr will read.

    ``bound_config`` is the resolved (realpath) config whose posture :func:`_pin_state`
    verified — the value an apply pins into :data:`HERDR_CONFIG_PATH_ENV` so herdr reads
    *that* file and not whatever it would have resolved on its own.

    ``conflict_detail`` is set when the caller's environment already names a different
    config. Silently overriding it would be the wrong kind of fix: two authorities then
    disagree about which config governs the run and the installer just picks one. The
    fail-closed answer is to refuse, which is also what makes the decoy visible — an
    unrelated pinned file offered as ``--herdr-config`` while the environment points
    herdr at an unpinned one (Redmine #13249 review j#91688 finding 1).
    """
    if inputs.herdr_config is None:
        return None, None  # the posture gate already refuses a missing config
    bound = os.path.realpath(inputs.herdr_config)
    env = inputs.env if inputs.env is not None else os.environ
    declared = env.get(HERDR_CONFIG_PATH_ENV)
    if declared and os.path.realpath(declared) != bound:
        return bound, (
            f"the environment sets {HERDR_CONFIG_PATH_ENV}={declared} but the pin "
            f"posture was verified against {bound}; herdr would read a config whose "
            f"posture was never proven, so the install is refused (supply the same "
            f"config to both, or unset {HERDR_CONFIG_PATH_ENV})"
        )
    return bound, None


def _herdr_argv(binary: str, agent: str) -> "tuple[str, ...]":
    return (binary, "integration", "install", agent)


def plan_install(inputs: InstallInputs) -> InstallReport:
    """Read-only plan: gate every agent, mutate nothing (byte-invariant)."""
    agents = inputs.agents
    pin, pin_detail = _pin_state(inputs)
    pinned = pin is not None
    pin_mode = pin.mode if pin is not None else None
    # The verified config must also be the config herdr reads — a pin proven on a file
    # herdr never opens is not a pin (j#91688 finding 1).
    bound_config, bind_conflict = _config_binding(inputs)
    # The trusted herdr binary is a plan precondition: an unresolvable binary gates
    # every agent closed (j#83613 finding 2), so a plan never reports ok for a target
    # no apply could touch.
    binary, binary_detail = _resolve_binary(inputs)
    plans: "list[AgentInstallPlan]" = []
    for agent in agents:
        plan = _gate_agent(
            agent,
            inputs.home,
            pinned=pinned,
            pin_detail=pin_detail,
            bind_conflict=bind_conflict,
            binary=binary,
            binary_detail=binary_detail,
        )
        plans.append(plan)
    ok = bool(plans) and all(p.ready for p in plans)
    detail = "" if binary else f"herdr binary unresolved: {binary_detail}"
    return InstallReport(
        applied=False,
        ok=ok,
        plans=tuple(plans),
        detail=detail,
        pin_mode=pin_mode if pinned else None,
        herdr_config_bound=bound_config if pinned and not bind_conflict else None,
    )


def apply_install(inputs: InstallInputs) -> InstallReport:
    """Explicit apply: install the hook for every agent, or roll the whole set back.

    Fail-closed order: build the read-only plan first; if any agent is gated, mutate
    nothing and return the plan-shaped report (the CLI exits non-zero). Only when
    every agent is ready does the transaction run. A herdr failure on any agent rolls
    back every agent already applied and returns a ``partial_failure`` report.
    """
    plan_report = plan_install(inputs)
    if not plan_report.ok:
        # A gate blocked at least one agent — refuse the whole apply, mutate nothing.
        return InstallReport(
            applied=False,
            ok=False,
            plans=plan_report.plans,
            detail="apply refused: at least one agent is gated (see plan); nothing "
            "was mutated",
            pin_mode=plan_report.pin_mode,
            herdr_config_bound=plan_report.herdr_config_bound,
        )
    binary, binary_detail = _resolve_binary(inputs)
    if binary is None:
        # The plan proved the binary resolved, so losing it here is drift — and drift
        # has to arrive as a closed reason, not as prose attached to plans that still
        # say `ready` (j#91805 finding 2B).
        return InstallReport(
            applied=False,
            ok=False,
            plans=_gated_plans(
                inputs,
                REASON_HERDR_UNRESOLVED,
                f"the trusted herdr binary no longer resolves ({binary_detail}); "
                f"nothing was mutated",
            ),
            detail=f"apply refused: herdr binary unresolved ({binary_detail}); nothing "
            f"was mutated",
            pin_mode=plan_report.pin_mode,
            herdr_config_bound=plan_report.herdr_config_bound,
        )
    bound_config = plan_report.herdr_config_bound
    # Re-observe the posture here, at the start of the transaction, and keep the
    # content digest + file identity it verified. This capture — not the plan's — is
    # what every later drift check compares against (j#91762 finding 2).
    pin, pin_detail = _pin_state(inputs)
    if bound_config is None or pin is None or pin.path != bound_config:
        # An ok plan always carries a bound, pinned config, so reaching here means the
        # config changed between the plan and now — refuse rather than run herdr
        # against a config whose posture is not currently proven.
        return InstallReport(
            applied=False,
            ok=False,
            plans=_gated_plans(
                inputs,
                REASON_CONFIG_PIN_MISMATCH,
                f"the verified herdr config could not be bound to the run "
                f"({pin_detail}); nothing was mutated",
            ),
            detail=f"apply refused: the verified herdr config could not be bound to "
            f"the run ({pin_detail}); nothing was mutated",
            pin_mode=plan_report.pin_mode,
        )
    return _run_apply_transaction(inputs, binary, pin)


def _gated_plans(
    inputs: InstallInputs, reason: str, detail: str
) -> "tuple[AgentInstallPlan, ...]":
    """Project a whole-transaction refusal onto every agent as a closed reason.

    A refusal that only carries prose leaves consumers with plans still marked
    ``ready`` and no reason anywhere in the structured payload — the closed vocabulary
    exists precisely so "why did this stop" is machine-readable (Redmine #13249 review
    j#91805 finding 2).
    """
    return tuple(
        AgentInstallPlan(
            agent=agent,
            config_dir=str(_config_dir(inputs.home, agent)),
            ready=False,
            reason=reason,
            detail=detail,
        )
        for agent in inputs.agents
    )


def _run_apply_transaction(
    inputs: InstallInputs, binary: str, pin: _ConfigPin
) -> InstallReport:
    runner = inputs.runner if inputs.runner is not None else subprocess.run
    env = dict(inputs.env) if inputs.env is not None else dict(os.environ)
    # herdr resolves the agent config dirs from HOME; pin it to the resolved home so
    # a managed apply and the gate look at the same dirs.
    env["HOME"] = str(inputs.home)
    # …and pin the config too, so the file whose pin posture was verified is exactly
    # the file herdr reads. Without this the gate proves a property of one file while
    # herdr obeys another (Redmine #13249 review j#91688 finding 1).
    env[HERDR_CONFIG_PATH_ENV] = pin.path
    pin_mode = pin.mode
    bound_config = pin.path
    # Preflight, BEFORE any mutation: snapshot + back up every agent's dir. A rollback
    # of a dir is only provable when every non-credential file could be both
    # snapshotted AND backed up — and when the *listing itself* was complete, since a
    # subtree that could not be enumerated drops out of the snapshot and the backup
    # together and would otherwise satisfy every per-file check (Redmine #13249 reviews
    # j#83674 / j#83737 / j#91688 finding 2). So the whole transaction is refused with
    # zero mutation when either pass is incomplete, or when the backup does not cover
    # every snapshot path (a file readable at snapshot time can fail the separate
    # backup read). Snapshots are captured here (pre-mutation) and reused.
    staged: "list[tuple[str, Path, DirIdentity, DirSnapshot, dict]]" = []
    for agent in inputs.agents:
        config_dir = _config_dir(inputs.home, agent)
        # Re-validate the target NOW and stage *which object* it is: the plan gate's
        # answer is about the past, and everything below reads, writes, or hands the
        # dir to herdr (j#91762 F1 / j#91805 F1).
        staged_identity, drift = observe_config_dir(config_dir, inputs.home)
        if drift is not None:
            drift_reason, drift_detail = drift
            return InstallReport(
                applied=False,
                ok=False,
                plans=(
                    AgentInstallPlan(
                        agent=agent,
                        config_dir=str(config_dir),
                        ready=False,
                        reason=drift_reason,
                        detail=drift_detail,
                    ),
                ),
                detail="apply refused: a target config dir changed after the plan "
                "gate; nothing was mutated",
                pin_mode=pin_mode,
                herdr_config_bound=bound_config,
            )
        # The snapshot and the backup have to describe ONE object, so the pair is
        # enclosed in the identity bracket rather than merely followed by a check
        # (j#91805 finding 1 / j#91840).
        reads, read_drift = with_identity_bracket(
            config_dir,
            inputs.home,
            staged_identity,
            lambda: (read_dir(config_dir), backup_dir(config_dir)),
        )
        if read_drift is None:
            before_read, backup = reads
        else:
            drift_reason, drift_detail = read_drift
            return InstallReport(
                applied=False,
                ok=False,
                plans=(
                    AgentInstallPlan(
                        agent=agent,
                        config_dir=str(config_dir),
                        ready=False,
                        reason=drift_reason,
                        detail=f"the config dir changed while it was being snapshotted "
                        f"and backed up, so neither describes a single object: "
                        f"{drift_detail}",
                    ),
                ),
                detail="apply refused: a target config dir changed while it was being "
                "read; nothing was mutated",
                pin_mode=pin_mode,
                herdr_config_bound=bound_config,
            )
        missing_from_backup = before_read.snapshot.paths - set(backup.files)
        if not before_read.complete or not backup.complete or missing_from_backup:
            gaps = f"snapshot [{before_read.gap_detail}], backup [{backup.gap_detail}]"
            if missing_from_backup:
                gaps += f", not backed up: {', '.join(sorted(missing_from_backup)[:5])}"
            return InstallReport(
                applied=False,
                ok=False,
                plans=(
                    AgentInstallPlan(
                        agent=agent,
                        config_dir=str(config_dir),
                        ready=False,
                        reason=REASON_CONFIG_DIR_UNREADABLE,
                        detail=f"config dir {config_dir} could not be fully "
                        f"snapshotted and backed up ({gaps}); a rollback could not be "
                        f"proven so nothing was mutated",
                    ),
                ),
                detail="apply refused: an un-provable rollback would be required; "
                "nothing was mutated",
                pin_mode=pin_mode,
                herdr_config_bound=bound_config,
            )
        staged.append(
            (agent, config_dir, staged_identity, before_read.snapshot, backup.files)
        )
    applied: "list[tuple[str, Path, DirIdentity, dict, DirSnapshot]]" = []
    outcomes: "list[AgentInstallOutcome]" = []
    for agent, config_dir, staged_identity, before, backup in staged:
        failure: "Optional[tuple[str, str]]" = None
        after: Optional[DirSnapshot] = None
        # Nothing has been written for this agent yet, so a drift caught here must not
        # trigger a rollback write into whatever the path now points at.
        mutated = False
        drift = config_dir_drift(config_dir, inputs.home, expected=staged_identity)
        pin_drift = _config_pin_drift(inputs, pin, when="before invoking herdr")
        if drift is not None:
            failure = (drift[0], f"refused before invoking herdr: {drift[1]}")
        elif pin_drift is not None:
            failure = (REASON_CONFIG_PIN_MISMATCH, pin_drift)
        else:
            ok, detail = _invoke_herdr(runner, binary, agent, env)
            mutated = True  # herdr may have written whatever its exit code says
            # Re-assert the config pin after the run as well: the check before the
            # invocation cannot be atomic with herdr's own read, so a swap inside that
            # window is caught here and rolled back instead of left installed.
            after_pin_drift = _config_pin_drift(inputs, pin, when="while herdr ran")
            # The dir's identity after the run is asserted by the bracket around the
            # post-apply read below (success path) and by `rollback_dir`'s own guard
            # (failure path). A separate check here would duplicate both and could not
            # be made to fail on its own — the bracket is the single mechanism.
            if not ok:
                failure = (REASON_HERDR_ERROR, detail)
            elif after_pin_drift is not None:
                failure = (
                    REASON_CONFIG_PIN_MISMATCH,
                    f"herdr reported success but {after_pin_drift}",
                )
            else:
                # herdr reporting success is not the same as the apply having
                # succeeded: the resulting state has to be observable. A post-apply dir
                # that cannot be fully read back yields neither an exact diff nor a
                # final home state, so it is rolled back and reported closed rather
                # than ok (Redmine #13249 review j#91688 finding 3).
                # …and the read that produces that state is itself bracketed: a diff
                # computed from a directory that replaced the staged one describes
                # someone else's contents, and would be reported as the change herdr
                # made (Redmine #13249 review j#91840).
                after_read, read_drift = with_identity_bracket(
                    config_dir,
                    inputs.home,
                    staged_identity,
                    lambda: read_dir(config_dir),
                )
                if read_drift is not None:
                    failure = (
                        read_drift[0],
                        f"herdr reported success but the post-apply state could not be "
                        f"attributed to the staged config dir: {read_drift[1]}",
                    )
                elif not after_read.complete:
                    failure = (
                        REASON_CONFIG_DIR_UNREADABLE,
                        f"herdr reported success but {config_dir} could not be fully "
                        f"read back ({after_read.gap_detail}); the resulting state and "
                        f"diff are unobservable",
                    )
                else:
                    after = after_read.snapshot
        if failure is not None:
            failed_reason, failure_detail = failure
            # Roll back this agent's write (verified), then every prior agent. When
            # nothing was written there is nothing to restore — and attempting one
            # would be the write this guard exists to prevent.
            restored = (
                rollback_dir(
                    config_dir,
                    backup,
                    before,
                    home=inputs.home,
                    expected=staged_identity,
                )
                if mutated
                else True
            )
            if not restored:
                failed_detail = (
                    f"apply failed ({failure_detail}) AND rollback left residue in "
                    f"{config_dir}; home NOT restored"
                )
                failed_reason = REASON_ROLLBACK_INCOMPLETE
            elif mutated:
                failed_detail = failure_detail
            else:
                failed_detail = f"{failure_detail} (nothing was mutated for this agent)"
            outcomes.append(
                AgentInstallOutcome(
                    agent=agent,
                    config_dir=str(config_dir),
                    ok=False,
                    reason=failed_reason,
                    detail=failed_detail,
                    # `rolled_back` says this agent's mutation was reverted. When the
                    # refusal landed before anything was written there is no mutation
                    # to revert, and claiming one would misreport what happened
                    # (Redmine #13249 review j#91805 finding 2).
                    rolled_back=mutated and restored,
                )
            )
            reverted, all_restored = _rollback_applied(
                applied, outcomes, home=inputs.home
            )
            all_restored = all_restored and restored
            reverted_desc = ", ".join(
                ([f"{agent}'s partial write"] if mutated else []) + list(reverted)
            )
            if not all_restored:
                note = (
                    f"apply failed for {agent} ({failure[0]}); rollback INCOMPLETE — "
                    f"residue remains, home NOT fully restored (verify the config dirs)"
                )
            elif reverted_desc:
                note = (
                    f"apply failed for {agent} ({failure[0]}); rolled back "
                    f"{reverted_desc} — home left as found"
                )
            else:
                note = (
                    f"apply refused at {agent} ({failure[0]}); nothing was mutated — "
                    f"home left as found"
                )
            return InstallReport(
                # `applied` reports whether the transaction actually reached a
                # mutation. A drift caught before the first herdr invocation leaves
                # home untouched, and saying otherwise would overstate what happened.
                applied=mutated or bool(applied),
                ok=False,
                outcomes=tuple(outcomes),
                detail=note,
                pin_mode=pin_mode,
                herdr_config_bound=bound_config,
            )
        applied.append((agent, config_dir, staged_identity, backup, before))
        outcomes.append(
            AgentInstallOutcome(
                agent=agent,
                config_dir=str(config_dir),
                ok=True,
                diff=diff_snapshots(before, after),
            )
        )
    return InstallReport(
        applied=True,
        ok=True,
        outcomes=tuple(outcomes),
        detail="hook installed for every requested agent",
        pin_mode=pin_mode,
        herdr_config_bound=bound_config,
    )


def _invoke_herdr(
    runner: Runner, binary: str, agent: str, env: "dict[str, str]"
) -> "tuple[bool, str]":
    """Run ``herdr integration install <agent>``; return ``(ok, detail)`` fail-closed."""
    argv = list(_herdr_argv(binary, agent))
    try:
        completed = runner(
            argv,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            env=env,
        )
    except FileNotFoundError:
        return False, f"herdr binary not found: {binary!r}"
    except subprocess.TimeoutExpired:
        return False, "herdr integration install timed out"
    except OSError as exc:
        return False, f"herdr integration install failed ({exc.__class__.__name__})"
    if completed.returncode != 0:
        return False, _bounded(completed.stderr) or f"herdr exit {completed.returncode}"
    return True, ""


def _rollback_applied(
    applied: "list[tuple[str, Path, DirIdentity, dict, DirSnapshot]]",
    outcomes: "list[AgentInstallOutcome]",
    *,
    home: Path,
) -> "tuple[list[str], bool]":
    """Roll back every already-applied agent **with verification**.

    Returns ``(reverted_agents, all_restored)``. Each agent's rollback is verified by
    :func:`rollback_dir`; when a restoration cannot be proven, that agent's outcome
    is marked ``rollback_incomplete`` / ``rolled_back=False`` (never a false
    ``partial_failure`` / ``rolled_back=True``) and ``all_restored`` is ``False`` so
    the report never claims ``home left as found`` on unproven restoration (Redmine
    #13249 review j#83613 finding 1). A dir whose identity drifted since it was staged
    is likewise unrestorable — :func:`rollback_dir` refuses to write into it.
    """
    reverted: "list[str]" = []
    all_restored = True
    by_agent = {o.agent: i for i, o in enumerate(outcomes)}
    for agent, config_dir, staged_identity, backup, before in applied:
        restored = rollback_dir(
            config_dir, backup, before, home=home, expected=staged_identity
        )
        reverted.append(agent)
        all_restored = all_restored and restored
        idx = by_agent.get(agent)
        if idx is not None:
            prev = outcomes[idx]
            if restored:
                reason = REASON_PARTIAL_FAILURE
                detail = "rolled back because another agent failed the transaction"
            else:
                reason = REASON_ROLLBACK_INCOMPLETE
                detail = (
                    f"rollback left residue in {config_dir}; home NOT restored for "
                    f"this agent"
                )
            outcomes[idx] = AgentInstallOutcome(
                agent=prev.agent,
                config_dir=prev.config_dir,
                ok=False,
                reason=reason,
                detail=detail,
                diff=prev.diff,
                rolled_back=restored,
            )
    return reverted, all_restored


def _bounded(text: object, *, limit: int = 200) -> str:
    if not isinstance(text, str):
        return ""
    collapsed = " ".join(text.split())
    return collapsed[:limit] + "…" if len(collapsed) > limit else collapsed


# --- formatting --------------------------------------------------------------


def report_payload(report: InstallReport) -> dict:
    """JSON-serialisable view of an install report."""

    def diff_payload(diff: Optional[SnapshotDiff]) -> Optional[dict]:
        if diff is None:
            return None
        return {
            "added": list(diff.added),
            "removed": list(diff.removed),
            "changed": list(diff.changed),
        }

    return {
        "applied": report.applied,
        "ok": report.ok,
        "pin_mode": report.pin_mode,
        "herdr_config_bound": report.herdr_config_bound,
        "detail": report.detail,
        "plans": [
            {
                "agent": p.agent,
                "config_dir": p.config_dir,
                "ready": p.ready,
                "reason": p.reason,
                "detail": p.detail,
                "herdr_argv": list(p.herdr_argv),
            }
            for p in report.plans
        ],
        "outcomes": [
            {
                "agent": o.agent,
                "config_dir": o.config_dir,
                "ok": o.ok,
                "reason": o.reason,
                "detail": o.detail,
                "rolled_back": o.rolled_back,
                "diff": diff_payload(o.diff),
            }
            for o in report.outcomes
        ],
    }


def format_report_text(report: InstallReport) -> str:
    """Human-readable install report (plan or apply)."""
    head = "APPLY" if report.applied else "PLAN"
    status = "ok" if report.ok else "blocked"
    lines = [f"herdr integration-install {head}: {status}"]
    if report.pin_mode:
        lines.append(f"  herdr posture: pinned ({report.pin_mode})")
    if report.herdr_config_bound:
        lines.append(
            f"  herdr config: {report.herdr_config_bound} "
            f"(bound via {HERDR_CONFIG_PATH_ENV})"
        )
    if report.detail:
        lines.append(f"  {report.detail}")
    for p in report.plans:
        if p.ready:
            lines.append(f"  [ready] {p.agent} -> {p.config_dir}")
            lines.append(f"          would run: {' '.join(p.herdr_argv)}")
        else:
            lines.append(f"  [gated:{p.reason}] {p.agent} -> {p.config_dir}")
            if p.detail:
                lines.append(f"          {p.detail}")
    for o in report.outcomes:
        tag = "ok" if o.ok else (o.reason or "failed")
        suffix = " (rolled back)" if o.rolled_back else ""
        lines.append(f"  [{tag}] {o.agent} -> {o.config_dir}{suffix}")
        if o.diff is not None and not o.diff.is_empty:
            lines.append(
                f"          diff: +{list(o.diff.added)} ~{list(o.diff.changed)} "
                f"-{list(o.diff.removed)}"
            )
        if o.detail:
            lines.append(f"          {o.detail}")
    return "\n".join(lines)


def run_install(inputs: InstallInputs, *, apply: bool) -> InstallReport:
    """Single entry point the CLI calls: plan (read-only) or apply (opt-in)."""
    if apply:
        return apply_install(inputs)
    return plan_install(inputs)


__all__ = (
    "COMMAND_TIMEOUT_SECONDS",
    "HERDR_CONFIG_PATH_ENV",
    "InstallInputs",
    "apply_install",
    "format_report_text",
    "plan_install",
    "report_payload",
    "run_install",
)
