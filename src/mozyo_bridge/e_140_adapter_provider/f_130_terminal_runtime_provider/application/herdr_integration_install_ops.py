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
- **The verified config is the config herdr reads.** The pin posture is proven against
  a specific file, and an apply binds herdr to *that* file via
  :data:`HERDR_CONFIG_PATH_ENV`; an environment naming a different config is refused
  rather than overridden, so a pinned file can never be a decoy for an unpinned run.
- **Completeness is part of the data, not a later check.** Every read of a config dir
  returns its snapshot / backup together with the listing and read failures that
  produced it (:class:`_DirRead`, :class:`_DirBackup`), because a subtree that could
  not be enumerated disappears from every set at once and would otherwise read as
  *absent*. An apply starts only from a fully-read dir and reports success only when
  the post-apply dir can be fully read back.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass, field
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


# --- snapshot / backup / rollback IO -----------------------------------------


@dataclass(frozen=True)
class _DirScan:
    """The non-credential files under a dir **plus the proof the listing was complete**.

    ``files`` are the ``(relpath, abspath)`` pairs that were successfully enumerated;
    ``unenumerable`` names every subtree / entry whose ``scandir`` or ``lstat`` failed.
    The two travel together on purpose: an enumeration error makes a file vanish from
    *every* downstream set at once (snapshot and backup alike), so a per-file
    "unreadable" check can never see it — it reads as *absent*, and every completeness
    comparison built from those sets silently agrees (Redmine #13249 review j#91688
    finding 2). Only :attr:`complete` licenses treating the file list as exhaustive.
    """

    files: "tuple[tuple[str, Path], ...]" = ()
    unenumerable: "tuple[str, ...]" = ()

    @property
    def complete(self) -> bool:
        return not self.unenumerable


def _scan_dir(root: Path) -> _DirScan:
    """Enumerate ``root``'s non-credential regular files, recording listing failures.

    Credential-shaped files and dirs (by any path component) are pruned so they are
    never read. Symlinked files are skipped too — the installer only tracks real hook
    files, and following a symlink out of the dir would read arbitrary content.

    Every way the listing can come up short is captured rather than swallowed:
    ``os.walk``'s ``onerror`` collects a subtree whose ``scandir`` failed (without it
    ``os.walk`` drops the subtree *silently*), and the entry type is taken from an
    explicit :func:`os.lstat` instead of :meth:`Path.is_file`, which answers ``False``
    for a stat error and would make an un-stattable entry indistinguishable from a
    directory.
    """
    files: "list[tuple[str, Path]]" = []
    failed: "list[str]" = []

    def _relative(target: object) -> str:
        if not isinstance(target, (str, os.PathLike)):
            return "."
        try:
            return os.path.relpath(os.fspath(target), root)
        except (OSError, ValueError):
            return str(target)

    def _onerror(exc: OSError) -> None:
        failed.append(_relative(getattr(exc, "filename", None) or root))

    try:
        if not root.is_dir():
            return _DirScan()
    except OSError:
        return _DirScan(unenumerable=(".",))
    for dirpath, dirnames, filenames in os.walk(root, onerror=_onerror):
        # Prune credential-shaped subdirs so we never descend into them.
        dirnames[:] = [d for d in dirnames if not is_credential_shaped(d)]
        for name in filenames:
            if is_credential_shaped(name):
                continue
            abspath = Path(dirpath) / name
            rel = os.path.relpath(abspath, root)
            if any(is_credential_shaped(part) for part in Path(rel).parts):
                continue
            try:
                mode = os.lstat(abspath).st_mode
            except OSError:
                failed.append(rel)
                continue
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                continue
            files.append((rel, abspath))
    return _DirScan(files=tuple(files), unenumerable=tuple(sorted(set(failed))))


#: The snapshot digest for a file that could not be read. It is intentionally NOT a
#: 64-char sha256 hexdigest, so it never collides with a real content hash. Crucially,
#: two of these sentinels comparing *equal* is NOT proof the bytes match (they were
#: never read) — :func:`_has_unreadable` and :func:`_rollback_dir` treat any snapshot
#: carrying it as unverifiable (Redmine #13249 review j#83674 finding 1).
_UNREADABLE_SENTINEL = "\x00unreadable\x00"


@dataclass(frozen=True)
class _DirRead:
    """A dir snapshot **inseparable from the proof that the whole dir was read**.

    A bare :class:`DirSnapshot` cannot answer "did I see everything?", so every
    producer here returns this instead — the completeness verdict is part of the type
    rather than something a caller must remember to re-derive afterwards (a post-hoc
    check is exactly what the success path forgot in Redmine #13249 review j#91688
    finding 3). ``unreadable`` names files whose bytes could not be read;
    ``unenumerable`` names subtrees / entries the listing itself could not cover.
    """

    snapshot: DirSnapshot = field(default_factory=DirSnapshot)
    unreadable: "tuple[str, ...]" = ()
    unenumerable: "tuple[str, ...]" = ()

    @property
    def complete(self) -> bool:
        return not (self.unreadable or self.unenumerable)

    @property
    def gap_detail(self) -> str:
        """Bounded, operator-readable description of what could not be read."""
        parts = []
        if self.unenumerable:
            parts.append(f"un-enumerable: {', '.join(self.unenumerable[:5])}")
        if self.unreadable:
            parts.append(f"unreadable: {', '.join(self.unreadable[:5])}")
        return "; ".join(parts) or "none"


def _read_dir(root: Path) -> _DirRead:
    """Content manifest (relpath -> sha256) of ``root``'s non-credential files.

    An unreadable file is recorded with :data:`_UNREADABLE_SENTINEL` (never a real
    hash) so its presence is still detected, but its bytes never enter the snapshot —
    and a snapshot carrying the sentinel can never be used as restoration *proof*.
    The returned :class:`_DirRead` also carries the listing failures from
    :func:`_scan_dir`, so a caller cannot obtain a snapshot without its completeness.
    """
    scan = _scan_dir(root)
    manifest: "dict[str, str]" = {}
    unreadable: "list[str]" = []
    for rel, abspath in scan.files:
        try:
            digest = hashlib.sha256(abspath.read_bytes()).hexdigest()
        except OSError:
            digest = _UNREADABLE_SENTINEL
            unreadable.append(rel)
        manifest[rel] = digest
    return _DirRead(
        snapshot=DirSnapshot.of(manifest),
        unreadable=tuple(sorted(unreadable)),
        unenumerable=scan.unenumerable,
    )


def _has_unreadable(snapshot: DirSnapshot) -> bool:
    """True iff ``snapshot`` carries an unreadable non-credential file.

    Such a file could not be hashed or backed up, so no snapshot equality involving
    it is a byte-level proof — an apply must refuse to start (rollback unprovable) and
    a rollback must never report itself verified.
    """
    return any(digest == _UNREADABLE_SENTINEL for _rel, digest in snapshot.entries)


@dataclass(frozen=True)
class _DirBackup:
    """The bytes captured for a rollback, **with the proof the capture was complete**.

    ``files`` maps each successfully-read non-credential file to its bytes;
    ``unreadable`` lists files whose read failed and ``unenumerable`` the subtrees the
    listing could not cover. A backup read can fail even when the snapshot read
    succeeded (a transient error, or a file that turned unreadable between the two
    passes), and an incomplete backup means a rollback of that dir could never be
    restored — so the caller MUST refuse the apply before mutating rather than silently
    proceeding with a partial backup (Redmine #13249 review j#83737 finding 1).
    """

    files: "dict[str, bytes]" = field(default_factory=dict)
    unreadable: "tuple[str, ...]" = ()
    unenumerable: "tuple[str, ...]" = ()

    @property
    def complete(self) -> bool:
        return not (self.unreadable or self.unenumerable)

    @property
    def gap_detail(self) -> str:
        parts = []
        if self.unenumerable:
            parts.append(f"un-enumerable: {', '.join(self.unenumerable[:5])}")
        if self.unreadable:
            parts.append(f"unreadable: {', '.join(self.unreadable[:5])}")
        return "; ".join(parts) or "none"


def _backup_dir(root: Path) -> _DirBackup:
    """Capture the bytes of ``root``'s non-credential files for rollback."""
    scan = _scan_dir(root)
    files: "dict[str, bytes]" = {}
    unreadable: "list[str]" = []
    for rel, abspath in scan.files:
        try:
            files[rel] = abspath.read_bytes()
        except OSError:
            unreadable.append(rel)
    return _DirBackup(
        files=files,
        unreadable=tuple(sorted(unreadable)),
        unenumerable=scan.unenumerable,
    )


def _rollback_dir(root: Path, backup: "dict[str, bytes]", before: DirSnapshot) -> bool:
    """Restore ``root``'s non-credential files to their pre-apply state, and **verify** it.

    Removes any non-credential file herdr added (present now, absent in ``before``),
    then rewrites every backed-up file's original bytes (restoring changed / removed
    files). Credential files are never touched (they are absent from both the backup
    and the snapshot). Best-effort per file: a rollback IO error on one file does not
    abort the rest — but the restoration is then **re-read and compared to the
    pre-apply snapshot**, and the boolean it returns is ``True`` only when the dir's
    non-credential content is byte-identical to how it was found. A swallowed
    remove/restore error (a read-only file, a permission loss) that leaves residue
    therefore makes this return ``False`` (Redmine #13249 review j#83613 finding 1),
    so a caller can never claim ``home left as found`` on an unproven rollback.
    """
    before_paths = before.paths
    for rel, abspath in _scan_dir(root).files:
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
    # Prove the restoration: the post-rollback read must match the pre-apply snapshot,
    # cover the whole dir, AND carry no unreadable file — a sentinel that only *equals*
    # another sentinel is not byte proof, and a subtree that could not be listed is not
    # evidence of absence, so either gap reports the dir unverified rather than
    # "restored" (Redmine #13249 reviews j#83674 finding 1 / j#91688 finding 2).
    after = _read_dir(root)
    if not after.complete or _has_unreadable(before):
        return False
    return after.snapshot == before


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
    pinned, pin_mode, pin_detail = _pin_state(inputs)
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
        return InstallReport(
            applied=False,
            ok=False,
            plans=plan_report.plans,
            detail=f"apply refused: herdr binary unresolved ({binary_detail})",
            pin_mode=plan_report.pin_mode,
        )
    bound_config = plan_report.herdr_config_bound
    if bound_config is None:
        # Defence in depth: an ok plan always carries the bound config, so reaching
        # here would mean the posture gate and the binding disagree — refuse rather
        # than run herdr against a config whose posture is unproven (j#91688 F1).
        return InstallReport(
            applied=False,
            ok=False,
            plans=plan_report.plans,
            detail="apply refused: the verified herdr config could not be bound to "
            "the run; nothing was mutated",
            pin_mode=plan_report.pin_mode,
        )
    return _run_apply_transaction(inputs, binary, plan_report.pin_mode, bound_config)


def _run_apply_transaction(
    inputs: InstallInputs, binary: str, pin_mode: Optional[str], bound_config: str
) -> InstallReport:
    runner = inputs.runner if inputs.runner is not None else subprocess.run
    env = dict(inputs.env) if inputs.env is not None else dict(os.environ)
    # herdr resolves the agent config dirs from HOME; pin it to the resolved home so
    # a managed apply and the gate look at the same dirs.
    env["HOME"] = str(inputs.home)
    # …and pin the config too, so the file whose pin posture was verified is exactly
    # the file herdr reads. Without this the gate proves a property of one file while
    # herdr obeys another (Redmine #13249 review j#91688 finding 1).
    env[HERDR_CONFIG_PATH_ENV] = bound_config
    # Preflight, BEFORE any mutation: snapshot + back up every agent's dir. A rollback
    # of a dir is only provable when every non-credential file could be both
    # snapshotted AND backed up — and when the *listing itself* was complete, since a
    # subtree that could not be enumerated drops out of the snapshot and the backup
    # together and would otherwise satisfy every per-file check (Redmine #13249 reviews
    # j#83674 / j#83737 / j#91688 finding 2). So the whole transaction is refused with
    # zero mutation when either pass is incomplete, or when the backup does not cover
    # every snapshot path (a file readable at snapshot time can fail the separate
    # backup read). Snapshots are captured here (pre-mutation) and reused.
    staged: "list[tuple[str, Path, DirSnapshot, dict]]" = []
    for agent in inputs.agents:
        config_dir = _config_dir(inputs.home, agent)
        before_read = _read_dir(config_dir)
        backup = _backup_dir(config_dir)
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
        staged.append((agent, config_dir, before_read.snapshot, backup.files))
    applied: "list[tuple[str, Path, dict, DirSnapshot]]" = []
    outcomes: "list[AgentInstallOutcome]" = []
    for agent, config_dir, before, backup in staged:
        ok, detail = _invoke_herdr(runner, binary, agent, env)
        failure: "Optional[tuple[str, str]]" = None
        after: Optional[DirSnapshot] = None
        if not ok:
            failure = (REASON_HERDR_ERROR, detail)
        else:
            # herdr reporting success is not the same as the apply having succeeded:
            # the resulting state has to be observable. A post-apply dir that cannot be
            # fully read back yields neither an exact diff nor a final home state, so
            # it is rolled back and reported closed rather than ok (Redmine #13249
            # review j#91688 finding 3).
            after_read = _read_dir(config_dir)
            if not after_read.complete:
                failure = (
                    REASON_CONFIG_DIR_UNREADABLE,
                    f"herdr reported success but {config_dir} could not be fully read "
                    f"back ({after_read.gap_detail}); the resulting state and diff are "
                    f"unobservable",
                )
            else:
                after = after_read.snapshot
        if failure is not None:
            failed_reason, failure_detail = failure
            # Roll back this agent's write (verified), then every prior agent.
            restored = _rollback_dir(config_dir, backup, before)
            if restored:
                failed_detail, rolled = failure_detail, True
            else:
                failed_detail = (
                    f"apply failed ({failure_detail}) AND rollback left residue in "
                    f"{config_dir}; home NOT restored"
                )
                failed_reason = REASON_ROLLBACK_INCOMPLETE
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
                    f"apply failed for {agent} ({failure[0]}); rolled back "
                    f"{reverted_desc} — home left as found"
                )
            else:
                note = (
                    f"apply failed for {agent} ({failure[0]}); rollback INCOMPLETE — "
                    f"residue remains, home NOT fully restored (verify the config dirs)"
                )
            return InstallReport(
                applied=True,
                ok=False,
                outcomes=tuple(outcomes),
                detail=note,
                pin_mode=pin_mode,
                herdr_config_bound=bound_config,
            )
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
