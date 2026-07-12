"""Managed callback-watcher composition root (Redmine #13520 review R2-F2).

The re-audit found the callback watcher had no *managed lifecycle*: the real Herdr wait only ran
when an operator hand-passed ``--watch --wake-target`` on one CLI invocation, with no composition
root deciding the source issue, stable wake target, workspace, attested sender identity, or restart
owner. This module is that composition root. It:

- **resolves** the watcher configuration fail-closed (:func:`resolve_watcher_config`): a managed
  watcher REQUIRES a source issue (what to re-read), a workspace id (whose outbox / route it owns),
  and a stable wake target (the Herdr event to block on) — a missing input stops rather than
  silently degrading into an ad-hoc poll. The launch-time sender identity is recorded as
  ``sender_attested`` so an un-attested watcher is visible (its sends fail-closed downstream).
- **owns the bounded run loop** (:func:`run_managed_watch`): it drives one production pass per wake
  through the shared :func:`...callback_runtime.watch` loop, which re-reads the exact Redmine
  journal on every wake outcome (event / timeout / error) and survives a raising pass — so the
  managed watcher is the restart owner within its bounded budget. The live daemon/supervisor process
  that calls this repeatedly under a Herdr session is the #13490 live surface; this root makes the
  composition explicit and testable without one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_runtime import (
    watch,
)


class WatcherConfigError(ValueError):
    """A managed watcher configuration is missing a required composition input (fail-closed)."""


@dataclass(frozen=True)
class WatcherConfig:
    """The resolved composition of a managed callback watcher (all inputs decided up front).

    ``source_issue`` — the Redmine issue the watcher re-reads for handoff-worthy gate markers.
    ``workspace_id`` — the workspace whose callback outbox / route this watcher owns (its sends and
    claims are pinned here, not to ambient cwd/env — R2-F5). ``wake_target`` — the stable Herdr
    assigned-name the watcher blocks on via ``wait agent-status`` (a hint; Redmine stays authority).
    ``sender_attested`` — whether the launch-time sender identity was attested; an un-attested
    watcher is recorded so its downstream sends fail-closed rather than routing on ambient env.
    """

    source_issue: str
    workspace_id: str
    wake_target: str
    sender_attested: bool
    max_passes: int = 1
    wake_status: str = "working"
    wake_timeout_ms: int = 50_000

    def as_payload(self) -> dict:
        return {
            "source_issue": self.source_issue,
            "workspace_id": self.workspace_id,
            "wake_target": self.wake_target,
            "sender_attested": self.sender_attested,
            "max_passes": self.max_passes,
            "wake_status": self.wake_status,
            "wake_timeout_ms": self.wake_timeout_ms,
        }


def resolve_watcher_config(
    *,
    source_issue: str,
    workspace_id: str,
    wake_target: str,
    sender_attested: bool,
    max_passes: int = 1,
    wake_status: str = "working",
    wake_timeout_ms: int = 50_000,
) -> WatcherConfig:
    """Resolve + validate the managed watcher composition, fail-closed on any missing input.

    A managed watcher cannot run without knowing WHAT to re-read (``source_issue``), WHOSE outbox it
    owns (``workspace_id``), and WHICH Herdr event to block on (``wake_target``). A blank required
    input raises :class:`WatcherConfigError` (no silent degrade into an unbounded ad-hoc poll).
    ``sender_attested`` is recorded, not required: an un-attested watcher is allowed to observe /
    plan but its downstream sends fail-closed (they never route on ambient cwd/env).
    """
    src = str(source_issue or "").strip()
    ws = str(workspace_id or "").strip()
    tgt = str(wake_target or "").strip()
    missing = [
        name
        for name, value in (("source_issue", src), ("workspace_id", ws), ("wake_target", tgt))
        if not value
    ]
    if missing:
        raise WatcherConfigError(
            f"managed callback watcher requires {missing} — a watcher with no {missing} would "
            "degrade into an ad-hoc poll with no route authority (refuse to run)"
        )
    passes = int(max_passes)
    if passes < 1:
        raise WatcherConfigError(f"max_passes must be >= 1, got {max_passes!r}")
    return WatcherConfig(
        source_issue=src,
        workspace_id=ws,
        wake_target=tgt,
        sender_attested=bool(sender_attested),
        max_passes=passes,
        wake_status=str(wake_status or "working").strip() or "working",
        wake_timeout_ms=int(wake_timeout_ms),
    )


def run_managed_watch(
    config: WatcherConfig,
    *,
    run_pass: Callable[[], dict],
    wait_fn: Callable[[], object],
    watch_fn: Optional[Callable] = None,
) -> list:
    """Own the bounded managed run loop: one production pass per wake, restart-resilient.

    Drives ``config.max_passes`` bounded passes through the shared watch loop, which re-reads the
    exact Redmine journal on EVERY wake outcome (event / timeout / error) and records a raising pass
    as ``{"error": ...}`` rather than crashing — so this root is the restart owner within its
    bounded budget. ``run_pass`` performs one discover -> ingest -> deliver-once -> sweep pass;
    ``wait_fn`` is the (fail-safe) Herdr event wait. Returns the per-pass records.
    """
    loop = watch_fn if watch_fn is not None else watch
    return loop(wait_fn, run_pass, max_passes=config.max_passes)


__all__ = (
    "WatcherConfigError",
    "WatcherConfig",
    "resolve_watcher_config",
    "run_managed_watch",
)
