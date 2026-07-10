"""``onboarding.apply`` / ``onboarding.resume`` — the idempotent step runner.

Applies a human-confirmed, drift-bound plan one idempotent step at a time under
a root-scoped OS lock, persisting a credential-free receipt after every step so
a crash or partial failure can be resumed exactly where it stopped.

Boundaries enforced here (design source of truth
``vibes/docs/specs/conversational-onboarding-tool-contract.md``):

- ``apply`` refuses a plan whose fingerprint drifted from a fresh re-inspection,
  a tampered plan (``plan_id`` mismatch), or an unconfirmed plan;
- the runner installs rules *before* scaffold apply (mechanical dependency — see
  the ordering note in ``domain.receipt``);
- config is written by the typed write-once tool only — never model YAML;
- the root-scoped lock is a POSIX advisory ``flock`` released automatically on
  process crash, so a crashed apply resumes from the receipt rather than needing
  a forced break; a second concurrent runner gets ``onboarding_locked`` and
  never force-breaks the lock or merges a concurrent apply.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Mapping

from mozyo_bridge.application.repo_local_config_loader import (
    load_repo_local_config_from_path,
)
from mozyo_bridge.core.state.workspace_registry import register_workspace
from mozyo_bridge.scaffold.rules import (
    install_rules,
    resolve_rules_store,
    scaffold_status,
    write_scaffold,
)
from mozyo_bridge.shared.paths import REPO_LOCAL_CONFIG_MARKER

from ..domain.path_safety import ONBOARDING_RECEIPT_MARKER
from ..domain.plan import OnboardingPlan, compute_root_fingerprint
from ..domain.preflight import STATE_BLOCKED
from ..domain.receipt import (
    ORDERED_STEPS,
    RECEIPT_STATE_COMPLETE,
    STEP_CONFIG_WRITE_ONCE,
    STEP_FINALIZE,
    STEP_ONBOARDING_RECEIPT,
    STEP_RULES_INSTALL,
    STEP_SCAFFOLD_APPLY,
    STEP_STATUS_DONE,
    STEP_STATUS_FAILED,
    STEP_STATUS_NO_OP,
    STEP_VERIFY,
    STEP_WORKSPACE_REGISTER,
    OnboardingReceipt,
    serialize_receipt,
)
from .config_write import (
    CONFIG_WRITE_NO_OP,
    ConfigWriteError,
    write_once_config,
)
from .herdr_binary import resolve_herdr_binary
from .inspect_usecase import inspect_onboarding

try:  # POSIX advisory locking; this is a mac/linux tool.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

__all__ = ("ApplyResult", "ApplyError", "apply_plan", "resume_onboarding")

_RECEIPT_RELPATH = Path(ONBOARDING_RECEIPT_MARKER)
_LOCK_RELPATH = Path(".mozyo-bridge") / "onboarding.lock"


class ApplyError(Exception):
    """A coded refusal to apply/resume (drift, lock, tamper, not-confirmed…)."""

    def __init__(self, code: str, message: str, *, next_action: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_action = next_action

    def as_record(self) -> dict[str, object]:
        return {
            "error": self.code,
            "message": self.message,
            "next_action": self.next_action,
        }


@dataclass(frozen=True)
class ApplyResult:
    state: str  # adoption_in_progress | complete
    applied_steps: tuple[str, ...] = ()
    no_op_steps: tuple[str, ...] = ()
    failed_step: str | None = None
    failed_reason: str | None = None
    next_action: str | None = None

    def as_record(self) -> dict[str, object]:
        return {
            "state": self.state,
            "applied_steps": list(self.applied_steps),
            "no_op_steps": list(self.no_op_steps),
            "failed_step": self.failed_step,
            "failed_reason": self.failed_reason,
            "next_action": self.next_action,
        }


@dataclass
class _StepContext:
    root: Path
    scaffold_preset: str
    rules_store: str
    home: Path | None
    env: Mapping[str, str] | None


@dataclass(frozen=True)
class _StepOutcome:
    status: str  # done | no_op | failed
    reason: str | None = None


def _receipt_path(root: Path) -> Path:
    return root / _RECEIPT_RELPATH


def _write_receipt(root: Path, receipt: OnboardingReceipt) -> None:
    path = _receipt_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialize_receipt(receipt))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


@contextmanager
def _root_lock(root: Path) -> Iterator[bool]:
    """Yield ``True`` iff the root-scoped advisory lock was acquired.

    A crashed process releases the ``flock`` automatically, so resume never has
    to force-break a stale lock. On a non-POSIX platform (no ``fcntl``) the lock
    is a best-effort no-op that always acquires.
    """
    lock_path = root / _LOCK_RELPATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:  # pragma: no cover - non-POSIX
        yield True
        return
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)


# --- step executors ----------------------------------------------------------


def _step_receipt(ctx: _StepContext) -> _StepOutcome:
    # The receipt exists by the time steps run (it is written first); this step
    # is a marker that adoption_in_progress was recorded.
    return _StepOutcome(STEP_STATUS_DONE)


def _step_rules_install(ctx: _StepContext) -> _StepOutcome:
    if ctx.rules_store == "repo_local":
        store = resolve_rules_store(repo_local=ctx.root)
        written = install_rules(store=store)
    else:
        written = install_rules(home=ctx.home)
    return _StepOutcome(STEP_STATUS_DONE if written else STEP_STATUS_NO_OP)


def _step_scaffold_apply(ctx: _StepContext) -> _StepOutcome:
    status = scaffold_status(
        ctx.root, home=None if ctx.rules_store == "repo_local" else ctx.home
    )
    if status.get("clean"):
        return _StepOutcome(STEP_STATUS_NO_OP)
    if ctx.rules_store == "repo_local":
        write_scaffold(ctx.scaffold_preset, ctx.root, backup=True, repo_local=True)
    else:
        write_scaffold(ctx.scaffold_preset, ctx.root, backup=True, home=ctx.home)
    return _StepOutcome(STEP_STATUS_DONE)


def _step_config_write(ctx: _StepContext) -> _StepOutcome:
    try:
        result = write_once_config(ctx.root)
    except ConfigWriteError as exc:
        return _StepOutcome(STEP_STATUS_FAILED, reason=f"{exc.code}: {exc.message}")
    return _StepOutcome(
        STEP_STATUS_NO_OP if result.outcome == CONFIG_WRITE_NO_OP else STEP_STATUS_DONE
    )


def _step_workspace_register(ctx: _StepContext) -> _StepOutcome:
    result = register_workspace(ctx.root, home=ctx.home)
    return _StepOutcome(STEP_STATUS_DONE, reason=result.outcome)


def _step_verify(ctx: _StepContext) -> _StepOutcome:
    problems: list[str] = []
    status = scaffold_status(
        ctx.root, home=None if ctx.rules_store == "repo_local" else ctx.home
    )
    if not status.get("clean"):
        problems.append("scaffold status is not clean")
    config_path = ctx.root / REPO_LOCAL_CONFIG_MARKER
    try:
        config = load_repo_local_config_from_path(config_path)
        if not config.terminal_transport.herdr_enabled:
            problems.append("config terminal_transport backend is not herdr")
    except Exception as exc:  # noqa: BLE001 - verification records any failure
        problems.append(f"config not loadable: {exc}")
    if not (ctx.root / ".mozyo-bridge" / "workspace-anchor.json").exists():
        problems.append("workspace anchor is missing")
    herdr = resolve_herdr_binary(ctx.env)
    if herdr.state != "resolved":
        problems.append("herdr binary is not resolved")
    if problems:
        return _StepOutcome(STEP_STATUS_FAILED, reason="; ".join(problems))
    return _StepOutcome(STEP_STATUS_DONE)


def _step_finalize(ctx: _StepContext) -> _StepOutcome:
    return _StepOutcome(STEP_STATUS_DONE)


_EXECUTORS: dict[str, Callable[[_StepContext], _StepOutcome]] = {
    STEP_ONBOARDING_RECEIPT: _step_receipt,
    STEP_RULES_INSTALL: _step_rules_install,
    STEP_SCAFFOLD_APPLY: _step_scaffold_apply,
    STEP_CONFIG_WRITE_ONCE: _step_config_write,
    STEP_WORKSPACE_REGISTER: _step_workspace_register,
    STEP_VERIFY: _step_verify,
    STEP_FINALIZE: _step_finalize,
}


def _run_steps(root: Path, receipt: OnboardingReceipt, ctx: _StepContext) -> ApplyResult:
    """Run pending steps sequentially; persist the receipt after each; stop on failure."""
    applied: list[str] = []
    no_ops: list[str] = []
    for step in ORDERED_STEPS:
        if receipt.is_settled(step):
            continue
        executor = _EXECUTORS[step]
        try:
            outcome = executor(ctx)
        except SystemExit as exc:  # scaffold `die()` raises SystemExit
            outcome = _StepOutcome(STEP_STATUS_FAILED, reason=str(exc) or step)
        except Exception as exc:  # noqa: BLE001 - any step failure is recorded, not raised
            outcome = _StepOutcome(STEP_STATUS_FAILED, reason=f"{type(exc).__name__}: {exc}")

        receipt = receipt.with_step(step, outcome.status, reason=outcome.reason)
        _write_receipt(root, receipt)

        if outcome.status == STEP_STATUS_FAILED:
            return ApplyResult(
                state=receipt.state,
                applied_steps=tuple(applied),
                no_op_steps=tuple(no_ops),
                failed_step=step,
                failed_reason=outcome.reason,
                next_action=(
                    f"fix the cause, then run `mozyo-bridge onboarding resume` "
                    f"to retry the {step} step"
                ),
            )
        if outcome.status == STEP_STATUS_NO_OP:
            no_ops.append(step)
        else:
            applied.append(step)

    receipt = receipt.completed()
    _write_receipt(root, receipt)
    return ApplyResult(
        state=RECEIPT_STATE_COMPLETE,
        applied_steps=tuple(applied),
        no_op_steps=tuple(no_ops),
        next_action="adoption complete; `mozyo` now launches the herdr backend",
    )


def apply_plan(
    plan: OnboardingPlan,
    *,
    human_confirmed: bool,
    home: Path | None = None,
    sync_roots=None,
    env: Mapping[str, str] | None = None,
) -> ApplyResult:
    """Apply a confirmed, drift-bound plan under the root lock.

    Fails closed with :class:`ApplyError` on: an unconfirmed plan, a now-blocked
    root, a canonical-root mismatch, drift (fresh fingerprint != plan
    fingerprint) on a fresh apply, a different in-progress adoption, or an
    unavailable root lock.
    """
    if not human_confirmed:
        raise ApplyError(
            "plan_not_confirmed",
            "apply requires human_confirmed=true (the human must confirm the "
            "visible mutation plan)",
        )

    root = Path(plan.canonical_root)
    inspection = inspect_onboarding(root, home=home, sync_roots=sync_roots, env=env)
    if inspection.preflight.state == STATE_BLOCKED:
        raise ApplyError(
            "blocked",
            "root is now a hard block: "
            + "; ".join(inspection.preflight.hard_block_reasons),
        )
    if str(inspection.facts.canonical_root) != plan.canonical_root:
        raise ApplyError(
            "root_mismatch",
            f"plan root {plan.canonical_root} no longer resolves to the same "
            f"canonical root ({inspection.facts.canonical_root})",
        )

    existing = inspection.receipt
    if existing is not None:
        if existing.plan_id != plan.plan_id:
            raise ApplyError(
                "different_plan_in_progress",
                "a different onboarding plan is already in progress at this root; "
                "resume or clear it before applying a new plan",
            )
        # Same plan already recorded — this is effectively a resume. Do not
        # re-check drift (our own committed steps have changed the tree).
        receipt = existing
    else:
        # Fresh apply: verify no drift since the plan was built.
        fresh_fingerprint = compute_root_fingerprint(inspection.facts)
        if fresh_fingerprint != plan.root_fingerprint:
            raise ApplyError(
                "plan_drift",
                "the tree changed since the plan was built (fingerprint drift); "
                "re-run `onboarding plan` and confirm a fresh plan",
            )
        receipt = OnboardingReceipt(
            root_fingerprint=plan.root_fingerprint,
            plan_id=plan.plan_id,
            scaffold_preset=plan.scaffold_preset,
            rules_store=plan.rules_store,
        ).with_step(STEP_ONBOARDING_RECEIPT, STEP_STATUS_DONE)

    ctx = _StepContext(
        root=root,
        scaffold_preset=plan.scaffold_preset,
        rules_store=plan.rules_store,
        home=home,
        env=env,
    )
    with _root_lock(root) as acquired:
        if not acquired:
            raise ApplyError(
                "onboarding_locked",
                "another runner holds the onboarding lock for this root",
                next_action="wait for the other runner, then re-run apply/resume",
            )
        # Persist the fresh receipt before running steps so a crash right after
        # lock acquisition still leaves a resumable adoption_in_progress marker.
        if existing is None:
            _write_receipt(root, receipt)
        return _run_steps(root, receipt, ctx)


def resume_onboarding(
    root: Path | str,
    *,
    home: Path | None = None,
    sync_roots=None,
    env: Mapping[str, str] | None = None,
) -> ApplyResult:
    """Resume an in-progress adoption from its receipt (input: the current root).

    Reads the receipt for the plan parameters and remaining steps and continues
    under the root lock. Does not re-check plan-fingerprint drift — the committed
    steps have legitimately changed the tree; the receipt is the resume anchor.
    """
    inspection = inspect_onboarding(root, home=home, sync_roots=sync_roots, env=env)
    receipt = inspection.receipt
    if receipt is None:
        raise ApplyError(
            "nothing_to_resume",
            "no onboarding receipt at this root; nothing to resume",
        )
    if receipt.state == RECEIPT_STATE_COMPLETE:
        return ApplyResult(
            state=RECEIPT_STATE_COMPLETE,
            next_action="adoption already complete",
        )
    resolved_root = Path(inspection.facts.canonical_root)
    ctx = _StepContext(
        root=resolved_root,
        scaffold_preset=receipt.scaffold_preset,
        rules_store=receipt.rules_store,
        home=home,
        env=env,
    )
    with _root_lock(resolved_root) as acquired:
        if not acquired:
            raise ApplyError(
                "onboarding_locked",
                "another runner holds the onboarding lock for this root",
                next_action="wait for the other runner, then re-run resume",
            )
        return _run_steps(resolved_root, receipt, ctx)
