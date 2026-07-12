"""Read-only recovery-plan surface for the callback runtime (Redmine #13520 j#75276 / review F4).

Assembles an :class:`...domain.recovery_reconciler.AuthorityObservation` from the readily-available
authorities and returns the pure :func:`build_recovery_plan` result. Deliberately **read-only**: it
probes git dirty/worktree state (a read-only ``git`` invocation), the callback outbox backlog, the
workspace anchor, and the launch-time sender env, and NEVER mutates anything. The live Herdr slot
liveness probe is best-effort — an injected ``slot_probe`` (the #13490 live harness / a test) supplies
composite-liveness :class:`RuntimeSlot` rows; without it the plan simply omits stale-slot steps
(safe: a slot it cannot observe is never blindly relaunched).
"""

from __future__ import annotations

import subprocess  # noqa: S404 - read-only `git status` probe, fixed argv, no shell
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_reconciler import (
    AuthorityObservation,
    RecoveryPlan,
    RuntimeSlot,
    build_recovery_plan,
)

#: Injected probe for the live Herdr runtime slots (composite liveness). ``None`` -> no slot rows.
SlotProbe = Callable[[], Sequence[RuntimeSlot]]

#: The launch-time sender identity env vars re-attested during recovery (never a persistent authority).
_SENDER_ENV_VARS = ("MOZYO_WORKSPACE_ID", "MOZYO_AGENT_ROLE")


def _git_worktree_state(repo_root: str, runner) -> "tuple[bool, bool]":
    """Return ``(worktree_present, dirty)`` from a read-only ``git status`` (fail-safe)."""
    try:
        rc, out = runner(["git", "-C", str(repo_root), "status", "--porcelain"])
    except Exception:  # noqa: BLE001 - a probe failure is fail-safe: treat as absent worktree
        return False, False
    if rc != 0:
        return False, False
    return True, bool((out or "").strip())


def _default_git_runner(argv: list) -> "tuple[int, str]":
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell; read-only git status
        argv, capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout


def build_observation(
    *,
    workspace_id_expected: str,
    workspace_id_registry: str,
    redmine_anchor_readable: bool,
    repo_root: str,
    outbox_present: bool,
    outbox_pending: int,
    outbox_uncertain: int,
    outbox_workspace_id: str = "",
    env: Optional[Mapping[str, str]] = None,
    slot_probe: Optional[SlotProbe] = None,
    git_runner: Optional[Callable[[list], "tuple[int, str]"]] = None,
) -> AuthorityObservation:
    """Assemble the authority observation from the probed signals (read-only, fail-safe)."""
    runner = git_runner if git_runner is not None else _default_git_runner
    worktree_present, dirty = _git_worktree_state(repo_root, runner)
    env_map = env if env is not None else {}
    sender_env_present = all((env_map.get(v) or "").strip() for v in _SENDER_ENV_VARS)
    slots = tuple(slot_probe() if slot_probe is not None else ())
    return AuthorityObservation(
        workspace_id_expected=workspace_id_expected,
        workspace_id_registry=workspace_id_registry,
        redmine_anchor_readable=redmine_anchor_readable,
        git_worktree_present=worktree_present,
        git_dirty=dirty,
        outbox_present=outbox_present,
        outbox_pending=int(outbox_pending),
        outbox_uncertain=int(outbox_uncertain),
        outbox_workspace_id=outbox_workspace_id,
        runtime_slots=slots,
        sender_env_present=sender_env_present,
    )


def recovery_plan_from_observation(obs: AuthorityObservation) -> RecoveryPlan:
    """Return the pure read-only recovery plan for an observation (thin pass-through)."""
    return build_recovery_plan(obs)


__all__ = (
    "SlotProbe",
    "build_observation",
    "recovery_plan_from_observation",
)
