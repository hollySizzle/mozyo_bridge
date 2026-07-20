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
  secret is hashed, copied, diffed, or restored.
- **The herdr binary is trusted-environment only.** Apply resolves it through the
  shared :func:`~...infrastructure.herdr_transport.resolve_herdr_binary` (env /
  trusted PATH), the same fail-closed resolver every herdr surface uses (#13496); a
  repo-local value can never point it at an arbitrary executable.
- **Path safety.** A config dir whose realpath escapes home (a symlink or ``..``
  traversal) is refused before any snapshot or mutation.
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
    is_credential_shaped,
    is_safe_config_dir,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pin_posture_ops import (
    verify_config,
)

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

#: How long a single ``herdr integration install`` may block before it is treated
#: as a herdr error. Kept short so an unresponsive herdr fails closed quickly.
COMMAND_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class InstallInputs:
    """Everything the installer needs, resolved by the CLI (never global state).

    ``home`` is the operator home the agent config dirs sit under (the CLI defaults
    it to ``$HOME`` but a test injects a temp dir). ``herdr_config`` is the herdr
    config whose pin posture gates the install; ``manifest_catalog_url`` is the
    observed pinned-mirror env value (or ``None``). ``env`` is the trusted
    environment used to resolve the herdr binary and passed to the runner so herdr
    resolves the agent dirs under the same ``home``. ``runner`` is injected (a fake
    in tests).
    """

    home: Path
    agents: "tuple[str, ...]"
    herdr_config: Optional[Path] = None
    manifest_catalog_url: Optional[str] = None
    env: Optional[Mapping[str, str]] = None
    runner: Optional[Runner] = None


# --- snapshot / backup / rollback IO -----------------------------------------


def _iter_files(root: Path):
    """Yield ``(relpath, abspath)`` for every non-credential regular file under ``root``.

    Credential-shaped files (by any path component) are skipped entirely so they are
    never read. Symlinked files are skipped too — the installer only tracks real hook
    files, and following a symlink out of the dir would read arbitrary content.
    """
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune credential-shaped subdirs so we never descend into them.
        dirnames[:] = [d for d in dirnames if not is_credential_shaped(d)]
        for name in filenames:
            if is_credential_shaped(name):
                continue
            abspath = Path(dirpath) / name
            if abspath.is_symlink() or not abspath.is_file():
                continue
            rel = os.path.relpath(abspath, root)
            if any(is_credential_shaped(part) for part in Path(rel).parts):
                continue
            yield rel, abspath


#: The snapshot digest for a file that could not be read. It is intentionally NOT a
#: 64-char sha256 hexdigest, so it never collides with a real content hash. Crucially,
#: two of these sentinels comparing *equal* is NOT proof the bytes match (they were
#: never read) — :func:`_has_unreadable` and :func:`_rollback_dir` treat any snapshot
#: carrying it as unverifiable (Redmine #13249 review j#83674 finding 1).
_UNREADABLE_SENTINEL = "\x00unreadable\x00"


def _snapshot_dir(root: Path) -> DirSnapshot:
    """Content manifest (relpath -> sha256) of ``root``'s non-credential files.

    An unreadable file is recorded with :data:`_UNREADABLE_SENTINEL` (never a real
    hash) so its presence is still detected, but its bytes never enter the snapshot —
    and a snapshot carrying the sentinel can never be used as restoration *proof*.
    """
    manifest: "dict[str, str]" = {}
    for rel, abspath in _iter_files(root):
        try:
            digest = hashlib.sha256(abspath.read_bytes()).hexdigest()
        except OSError:
            digest = _UNREADABLE_SENTINEL
        manifest[rel] = digest
    return DirSnapshot.of(manifest)


def _has_unreadable(snapshot: DirSnapshot) -> bool:
    """True iff ``snapshot`` carries an unreadable non-credential file.

    Such a file could not be hashed or backed up, so no snapshot equality involving
    it is a byte-level proof — an apply must refuse to start (rollback unprovable) and
    a rollback must never report itself verified.
    """
    return any(digest == _UNREADABLE_SENTINEL for _rel, digest in snapshot.entries)


def _backup_dir(root: Path) -> "dict[str, bytes]":
    """Capture the bytes of ``root``'s non-credential files for rollback."""
    backup: "dict[str, bytes]" = {}
    for rel, abspath in _iter_files(root):
        try:
            backup[rel] = abspath.read_bytes()
        except OSError:
            continue
    return backup


def _rollback_dir(root: Path, backup: "dict[str, bytes]", before: DirSnapshot) -> bool:
    """Restore ``root``'s non-credential files to their pre-apply state, and **verify** it.

    Removes any non-credential file herdr added (present now, absent in ``before``),
    then rewrites every backed-up file's original bytes (restoring changed / removed
    files). Credential files are never touched (they are absent from both the backup
    and the snapshot). Best-effort per file: a rollback IO error on one file does not
    abort the rest — but the restoration is then **re-snapshotted and compared to the
    pre-apply snapshot**, and the boolean it returns is ``True`` only when the dir's
    non-credential content is byte-identical to how it was found. A swallowed
    remove/restore error (a read-only file, a permission loss) that leaves residue
    therefore makes this return ``False`` (Redmine #13249 review j#83613 finding 1),
    so a caller can never claim ``home left as found`` on an unproven rollback.
    """
    before_paths = before.paths
    for rel, abspath in list(_iter_files(root)):
        if rel not in before_paths:
            try:
                abspath.unlink()
            except OSError:
                pass
    for rel, data in backup.items():
        target = root / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError:
            pass
    # Prove the restoration: the post-rollback snapshot must match the pre-apply one
    # AND carry no unreadable file — a sentinel that only *equals* another sentinel is
    # not byte proof, so a dir that became unreadable is reported unverified, never
    # "restored" (Redmine #13249 review j#83674 finding 1).
    after = _snapshot_dir(root)
    if _has_unreadable(after) or _has_unreadable(before):
        return False
    return after == before


# --- gating (read-only) ------------------------------------------------------


def _config_dir(home: Path, agent: str) -> Path:
    return home / AGENT_CONFIG_DIRNAME[agent]


def _gate_agent(
    agent: str,
    home: Path,
    *,
    pinned: bool,
    pin_detail: str,
    binary: Optional[str],
    binary_detail: str,
) -> AgentInstallPlan:
    """Run the read-only gates for one agent and return its plan.

    A plan promises an apply *could* run for a ready agent, so the trusted herdr
    binary must resolve for the plan to be ready — an unresolvable binary gates the
    plan closed (``herdr_unresolved``) rather than being demoted to a cosmetic
    ``detail`` while the plan still reports ``ok`` (Redmine #13249 review j#83613
    finding 2). The security / filesystem gates (pinned posture, dir present, safe
    path) are reported first so their more actionable reason surfaces; the binary
    gate is the last precondition before ``ready``.
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


def _pin_state(inputs: InstallInputs) -> "tuple[bool, Optional[str], str]":
    """Return ``(pinned, mode, detail)`` for the gate's herdr posture check."""
    if inputs.herdr_config is None:
        return (
            False,
            None,
            "no herdr config path supplied; cannot prove the posture is pinned",
        )
    result = verify_config(
        inputs.herdr_config, manifest_catalog_url=inputs.manifest_catalog_url
    )
    verdict = result.verdict
    if verdict.pinned:
        return True, verdict.mode, verdict.detail
    return False, None, f"[{verdict.reason}] {verdict.detail}"


def _herdr_argv(binary: str, agent: str) -> "tuple[str, ...]":
    return (binary, "integration", "install", agent)


def plan_install(inputs: InstallInputs) -> InstallReport:
    """Read-only plan: gate every agent, mutate nothing (byte-invariant)."""
    agents = inputs.agents
    pinned, pin_mode, pin_detail = _pin_state(inputs)
    # The trusted herdr binary is a plan precondition: an unresolvable binary gates
    # every agent closed (finding 2), so a plan never reports ok for a target no
    # apply could touch.
    binary, binary_detail = _resolve_binary(inputs)
    plans: "list[AgentInstallPlan]" = []
    for agent in agents:
        plan = _gate_agent(
            agent,
            inputs.home,
            pinned=pinned,
            pin_detail=pin_detail,
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
        )
    binary, binary_detail = _resolve_binary(inputs)
    if binary is None:
        return InstallReport(
            applied=False,
            ok=False,
            plans=plan_report.plans,
            detail=f"apply refused: herdr binary unresolved ({binary_detail})",
            pin_mode=plan_report.pin_mode,
        )
    return _run_apply_transaction(inputs, binary, plan_report.pin_mode)


def _run_apply_transaction(
    inputs: InstallInputs, binary: str, pin_mode: Optional[str]
) -> InstallReport:
    runner = inputs.runner if inputs.runner is not None else subprocess.run
    env = dict(inputs.env) if inputs.env is not None else dict(os.environ)
    # herdr resolves the agent config dirs from HOME; pin it to the resolved home so
    # a managed apply and the gate look at the same dirs.
    env["HOME"] = str(inputs.home)
    # Preflight, BEFORE any mutation: snapshot + back up every agent's dir. If any dir
    # holds an unreadable non-credential file, a rollback of it could never be
    # byte-verified, so the whole transaction is refused with zero mutation — an
    # un-provable rollback must never be started (Redmine #13249 review j#83674
    # finding 1). Snapshots are captured here (pre-mutation) and reused by the loop.
    staged: "list[tuple[str, Path, DirSnapshot, dict]]" = []
    for agent in inputs.agents:
        config_dir = _config_dir(inputs.home, agent)
        before = _snapshot_dir(config_dir)
        if _has_unreadable(before):
            return InstallReport(
                applied=False,
                ok=False,
                plans=(
                    AgentInstallPlan(
                        agent=agent,
                        config_dir=str(config_dir),
                        ready=False,
                        reason=REASON_CONFIG_DIR_UNREADABLE,
                        detail=f"config dir {config_dir} holds an unreadable "
                        f"non-credential file; a rollback could not be proven so "
                        f"nothing was mutated",
                    ),
                ),
                detail="apply refused: an un-provable rollback would be required; "
                "nothing was mutated",
                pin_mode=pin_mode,
            )
        staged.append((agent, config_dir, before, _backup_dir(config_dir)))
    applied: "list[tuple[str, Path, dict, DirSnapshot]]" = []
    outcomes: "list[AgentInstallOutcome]" = []
    for agent, config_dir, before, backup in staged:
        ok, detail = _invoke_herdr(runner, binary, agent, env)
        if not ok:
            # Roll back this agent's partial write (verified), then every prior agent.
            restored = _rollback_dir(config_dir, backup, before)
            if restored:
                failed_reason, failed_detail, rolled = REASON_HERDR_ERROR, detail, True
            else:
                failed_reason = REASON_ROLLBACK_INCOMPLETE
                failed_detail = (
                    f"herdr install failed ({detail}) AND rollback left residue in "
                    f"{config_dir}; home NOT restored"
                )
                rolled = False
            outcomes.append(
                AgentInstallOutcome(
                    agent=agent,
                    config_dir=str(config_dir),
                    ok=False,
                    reason=failed_reason,
                    detail=failed_detail,
                    rolled_back=rolled,
                )
            )
            reverted, all_restored = _rollback_applied(applied, outcomes)
            all_restored = all_restored and restored
            reverted_desc = ", ".join(reverted) if reverted else "its partial write"
            if all_restored:
                note = (
                    f"herdr install failed for {agent}; rolled back {reverted_desc} — "
                    f"home left as found"
                )
            else:
                note = (
                    f"herdr install failed for {agent}; rollback INCOMPLETE — residue "
                    f"remains, home NOT fully restored (verify the config dirs)"
                )
            return InstallReport(
                applied=True,
                ok=False,
                outcomes=tuple(outcomes),
                detail=note,
                pin_mode=pin_mode,
            )
        after = _snapshot_dir(config_dir)
        applied.append((agent, config_dir, backup, before))
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
    applied: "list[tuple[str, Path, dict, DirSnapshot]]",
    outcomes: "list[AgentInstallOutcome]",
) -> "tuple[list[str], bool]":
    """Roll back every already-applied agent **with verification**.

    Returns ``(reverted_agents, all_restored)``. Each agent's rollback is verified by
    :func:`_rollback_dir`; when a restoration cannot be proven, that agent's outcome
    is marked ``rollback_incomplete`` / ``rolled_back=False`` (never a false
    ``partial_failure`` / ``rolled_back=True``) and ``all_restored`` is ``False`` so
    the report never claims ``home left as found`` on unproven restoration (Redmine
    #13249 review j#83613 finding 1).
    """
    reverted: "list[str]" = []
    all_restored = True
    by_agent = {o.agent: i for i, o in enumerate(outcomes)}
    for agent, config_dir, backup, before in applied:
        restored = _rollback_dir(config_dir, backup, before)
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
    "InstallInputs",
    "apply_install",
    "format_report_text",
    "plan_install",
    "report_payload",
    "run_install",
)
