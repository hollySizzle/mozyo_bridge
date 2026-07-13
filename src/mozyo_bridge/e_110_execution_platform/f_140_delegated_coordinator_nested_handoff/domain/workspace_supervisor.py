"""Pure workspace-supervision domain: mode selection, report fold, service contract (#13683 Phase A).

The workspace callback supervisor (application composition root in
:mod:`...application.workspace_callback_supervisor`) enumerates the workspace registry and, for
each leased workspace, supplies durable workflow events + drains the callback outbox. This module
holds the **pure** decisions and shapes that root composes, kept free of any registry / Redmine /
store / lease I/O so they are deterministically testable:

- **which issues a pass supervises** (:func:`select_supervised_issues`): the two wake modes the
  design answer (j#77065) pins —
  - ``bounded_reconciliation`` re-reads the workspace's **whole** active-lane roster, so an
    external / MCP-only Redmine update (one no mozyo command emitted a local wake for) is still
    recovered on the reconciliation interval;
  - ``local_wake`` supervises only the roster issues a mozyo-originated gate/handoff commit named
    (its local wake), so a best-effort wake does the minimum work — and a wake naming an issue
    **not** in the active roster is *ignored* (surfaced, never silently trusted), because the
    roster is the authority on what is active, not the wake.
- **the redaction-safe report shapes** (:class:`IssueSupervisionOutcome` /
  :class:`WorkspaceSupervisionOutcome` / :class:`SupervisorReport`) — counts + fixed-vocabulary
  reasons only (no pane id, credential, or path), the same public-safe posture the callback
  reports keep.
- **the service lifecycle contract** (:class:`SupervisorServiceDefinition` /
  :func:`build_service_definition`): the *declarative* definition of the supervisor daemon a host
  service manager would run, carrying **no secrets** (Redmine is the durable auth authority; a
  credential is never written into a service definition or its logs — j#77065). Phase A ships the
  command **contract**; the actual host install/restart/uninstall is Phase B (gated on #13524 /
  #13526 installed parity), expressed here by :data:`PHASE_A_SERVICE_MUTATION_REASON`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

# ---------------------------------------------------------------------------
# Supervision modes (machine-readable; literal regardless of UI language).
# ---------------------------------------------------------------------------

#: Re-read the whole active-lane roster (recovers external / MCP-only Redmine updates).
SUPERVISION_BOUNDED_RECONCILIATION = "bounded_reconciliation"
#: Supervise only the issues a mozyo-originated gate/handoff commit named (its local wake).
SUPERVISION_LOCAL_WAKE = "local_wake"

SUPERVISION_MODES = frozenset({SUPERVISION_BOUNDED_RECONCILIATION, SUPERVISION_LOCAL_WAKE})

#: Portable default bounded-reconciliation interval (seconds). Coarser than the callback wake
#: cadence — external Redmine updates are the reconciliation target, not sub-minute latency. The
#: operator tunes the concrete cadence in their runtime policy; this is only the neutral default.
DEFAULT_RECONCILIATION_INTERVAL_SECONDS = 300

#: A workspace whole-skip reason (fixed vocabulary).
SKIP_LEASE_REFUSED = "lease_held_by_other"  # a live duplicate supervisor owns this workspace
SKIP_ROSTER_UNREADABLE = "active_lane_roster_unreadable"  # the roster read failed (fail-closed)
SKIP_NO_ACTIVE_ISSUES = "no_active_issues_to_supervise"  # roster read OK but nothing to do

#: Phase A refuses to mutate the host / login service (install / restart / uninstall). The actual
#: activation is Phase B, gated on installed parity (#13524 / #13526). j#77065 boundary.
PHASE_A_SERVICE_MUTATION_REASON = "phase_a_no_host_service_mutation"


@dataclass(frozen=True)
class IssueSelection:
    """Which roster issues a pass supervises, plus local-wake hints that were ignored.

    ``supervised`` is the ordered, de-duplicated set of issues to run this pass. ``ignored_wake``
    are ``local_wake`` hints that named an issue **not** present in the active-lane roster — kept
    for the report (auditable, never silently dropped) because a wake is a hint, not the authority.
    """

    supervised: tuple[str, ...]
    ignored_wake: tuple[str, ...]


def select_supervised_issues(
    roster_issues: Iterable[str],
    *,
    mode: str,
    wake_issues: Iterable[str] = (),
) -> IssueSelection:
    """Decide which of a workspace's active-lane issues this pass supervises (pure, fail-closed).

    ``bounded_reconciliation`` returns the whole (de-duplicated, order-preserving) roster.
    ``local_wake`` returns the **intersection** of the roster and ``wake_issues`` — a wake for a
    non-active / retired / foreign issue is dropped into ``ignored_wake`` rather than trusted, so
    the roster stays the authority on what is active. An unrecognized ``mode`` falls back to
    bounded reconciliation (never silently supervises nothing).
    """
    roster = tuple(dict.fromkeys(str(i).strip() for i in roster_issues if str(i).strip()))
    if mode != SUPERVISION_LOCAL_WAKE:
        return IssueSelection(supervised=roster, ignored_wake=())
    roster_set = set(roster)
    wanted = tuple(dict.fromkeys(str(w).strip() for w in wake_issues if str(w).strip()))
    supervised = tuple(i for i in roster if i in set(wanted))
    ignored = tuple(w for w in wanted if w not in roster_set)
    return IssueSelection(supervised=supervised, ignored_wake=ignored)


# ---------------------------------------------------------------------------
# Report shapes (redaction-safe: counts + fixed-vocabulary reasons only).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssueSupervisionOutcome:
    """One supervised issue's result: durable-event supply + callback-outbox drain counts.

    ``events_supplied`` is the number of normalized workflow events appended to the runtime store
    (the glance/resume durable-state supply — j#77065 acceptance 5). ``delivered`` / ``recovered``
    / ``pending`` / ``dead_letter`` are the callback-outbox pass counts. ``error`` is a
    fixed-vocabulary token when this issue's pass failed (fail-open per issue — one issue's Redmine
    read / store error never aborts the workspace or the sweep).
    """

    issue: str
    events_supplied: int = 0
    delivered: int = 0
    recovered: int = 0
    pending: int = 0
    dead_letter: int = 0
    error: str = ""

    def as_payload(self) -> dict[str, object]:
        return {
            "issue": self.issue,
            "events_supplied": self.events_supplied,
            "delivered": self.delivered,
            "recovered": self.recovered,
            "pending": self.pending,
            "dead_letter": self.dead_letter,
            "error": self.error,
        }


@dataclass(frozen=True)
class WorkspaceSupervisionOutcome:
    """One workspace's supervision result (lease decision + per-issue outcomes)."""

    workspace_id: str
    lease_acquired: bool
    lease_reason: str
    supervised_issues: tuple[str, ...] = ()
    ignored_wake_issues: tuple[str, ...] = ()
    issues: tuple[IssueSupervisionOutcome, ...] = ()
    skipped_reason: str = ""

    @property
    def events_supplied(self) -> int:
        return sum(i.events_supplied for i in self.issues)

    @property
    def delivered(self) -> int:
        return sum(i.delivered for i in self.issues)

    def as_payload(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "lease_acquired": self.lease_acquired,
            "lease_reason": self.lease_reason,
            "supervised_issues": list(self.supervised_issues),
            "ignored_wake_issues": list(self.ignored_wake_issues),
            "skipped_reason": self.skipped_reason,
            "events_supplied": self.events_supplied,
            "delivered": self.delivered,
            "issues": [i.as_payload() for i in self.issues],
        }


@dataclass(frozen=True)
class SupervisorReport:
    """A whole run-once supervised sweep: per-workspace outcomes + roll-up counts."""

    mode: str
    holder: str
    workspaces: tuple[WorkspaceSupervisionOutcome, ...] = field(default_factory=tuple)

    @property
    def workspaces_supervised(self) -> int:
        return sum(1 for w in self.workspaces if w.lease_acquired)

    @property
    def workspaces_skipped(self) -> int:
        return sum(1 for w in self.workspaces if not w.lease_acquired)

    @property
    def events_supplied(self) -> int:
        return sum(w.events_supplied for w in self.workspaces)

    @property
    def delivered(self) -> int:
        return sum(w.delivered for w in self.workspaces)

    def as_payload(self) -> dict[str, object]:
        return {
            "action": "run-once",
            "mode": self.mode,
            "holder": self.holder,
            "workspaces_total": len(self.workspaces),
            "workspaces_supervised": self.workspaces_supervised,
            "workspaces_skipped": self.workspaces_skipped,
            "events_supplied": self.events_supplied,
            "delivered": self.delivered,
            "workspaces": [w.as_payload() for w in self.workspaces],
        }


# ---------------------------------------------------------------------------
# Service lifecycle contract (declarative; NO secrets).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupervisorServiceDefinition:
    """The declarative definition of the supervisor daemon a host service manager would run.

    Deliberately carries **no credential / secret** (Redmine is the durable auth authority; the
    daemon resolves credentials from the daemon-trusted environment / home file at run time, never
    from a service definition or its log — j#77065). ``command`` is the argv the service runs on
    each reconciliation tick; ``reconciliation_interval_seconds`` is the bounded cadence;
    ``run_at_login`` / ``keep_alive`` are the residency knobs a Phase B installer maps onto the
    concrete host service (launchd / systemd), which Phase A does not touch.
    """

    label: str
    command: tuple[str, ...]
    reconciliation_interval_seconds: int
    run_at_login: bool
    keep_alive: bool

    def as_payload(self) -> dict[str, object]:
        return {
            "label": self.label,
            "command": list(self.command),
            "reconciliation_interval_seconds": self.reconciliation_interval_seconds,
            "run_at_login": self.run_at_login,
            "keep_alive": self.keep_alive,
        }


#: The default service label (a reverse-DNS style id; not operator-private).
DEFAULT_SUPERVISOR_SERVICE_LABEL = "org.mozyo-bridge.callback-supervisor"


def build_service_definition(
    *,
    command_prefix: Sequence[str] = ("mozyo-bridge", "workflow", "supervisor"),
    reconciliation_interval_seconds: int = DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
    run_at_login: bool = True,
    keep_alive: bool = True,
    label: str = DEFAULT_SUPERVISOR_SERVICE_LABEL,
) -> SupervisorServiceDefinition:
    """Build the supervisor daemon's declarative service definition (pure, no secrets).

    The command is ``<command_prefix> --run-once`` — the service manager invokes one bounded
    supervised sweep per tick (the run-once entrypoint), so residency lives in the host manager's
    ``keep_alive`` / interval, not an unbounded in-process poll (the wait/polling doctrine keeps
    the bounded cadence in the watcher/service layer, never an LLM turn).
    """
    interval = max(1, int(reconciliation_interval_seconds))
    command = tuple(str(p) for p in command_prefix) + ("--run-once",)
    return SupervisorServiceDefinition(
        label=str(label),
        command=command,
        reconciliation_interval_seconds=interval,
        run_at_login=bool(run_at_login),
        keep_alive=bool(keep_alive),
    )


__all__ = (
    "SUPERVISION_BOUNDED_RECONCILIATION",
    "SUPERVISION_LOCAL_WAKE",
    "SUPERVISION_MODES",
    "DEFAULT_RECONCILIATION_INTERVAL_SECONDS",
    "SKIP_LEASE_REFUSED",
    "SKIP_ROSTER_UNREADABLE",
    "SKIP_NO_ACTIVE_ISSUES",
    "PHASE_A_SERVICE_MUTATION_REASON",
    "IssueSelection",
    "select_supervised_issues",
    "IssueSupervisionOutcome",
    "WorkspaceSupervisionOutcome",
    "SupervisorReport",
    "SupervisorServiceDefinition",
    "DEFAULT_SUPERVISOR_SERVICE_LABEL",
    "build_service_definition",
)
