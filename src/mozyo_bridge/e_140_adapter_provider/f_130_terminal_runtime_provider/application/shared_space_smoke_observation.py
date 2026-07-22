"""Redaction-safe observation / recording layer for the shared-space smoke harness (#14187).

The pure evidence + recording layer of :mod:`shared_space_smoke_harness` (the cohesive
split that keeps the orchestrator + isolation module under its module-health baseline,
mirroring the ``herdr_lane_topology`` / ``herdr_pane_lifecycle`` sibling split): the
fail-closed error types, the closed failure-phase vocabulary, the
:class:`RecordingHerdrRunner` actuation-receipt adapter, and the redaction-safe
:class:`ProjectSmokeObservation` / :class:`SharedSpaceSmokeObservation` value objects.

No orchestration, no ``prepare_session``, no ambient home I/O — the harness imports and
composes these. Every value here is a count / bool / closed token / non-secret herdr /
mozyo identity token (``coordinators`` label, ``mzb1_...`` name, ``wN:pM`` handle), so a
summary can reach a Redmine journal without leaking a home path or a credential-shaped
literal (Redmine #14187 Acceptance 4/6).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    _norm,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    SHARED_COORDINATOR_WORKSPACE_LABEL,
    _parse_started_agent,
    _parse_workspace_created,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
    HerdrLauncherIncompatibleError,
)
from mozyo_bridge.core.state.coordinator_placement_fence import (
    CoordinatorSharedCreateLockUnavailable,
    CoordinatorSharedCreateReleaseError,
)


class SharedSpaceSmokeError(RuntimeError):
    """A shared-space smoke harness step cannot proceed (fail-closed)."""


class SmokeIsolationError(SharedSpaceSmokeError):
    """Isolation / cleanup authority could not be established (pre-actuation).

    Raised BEFORE any herdr command runs when the requested smoke home is not
    provably distinct from the real operator home, so the harness never creates
    something it could not later tear down exactly (Redmine #14187 Acceptance 5 —
    "cleanup 不能時は create 前に fail-closed").
    """


# -- failure-phase vocabulary (closed; redaction-safe evidence) ----------------
#: The phase a project run failed in, or :data:`PHASE_NONE`. A closed enum so the
#: durable evidence names *where* a run stopped without ever carrying a raw message.
PHASE_NONE = "none"
PHASE_ISOLATION = "isolation"  # pre-create: cleanup authority not established
PHASE_LOCK_ACQUIRE = "lock_acquire"  # single-flight fence could not be acquired (zero create)
PHASE_LOCK_RELEASE = "lock_release_after_create"  # fence release failed AFTER create/adopt
PHASE_LAUNCHER_PREFLIGHT = "launcher_preflight"  # managed-launch launcher incompatible
PHASE_SESSION_START = "session_start"  # any other fail-closed session-start refusal
PHASE_WORKER_ERROR = "worker_error"  # an UNCLASSIFIED exception crashed a concurrent worker

#: The closed set of failure phases the harness can report.
SMOKE_FAILURE_PHASES = (
    PHASE_NONE,
    PHASE_ISOLATION,
    PHASE_LOCK_ACQUIRE,
    PHASE_LOCK_RELEASE,
    PHASE_LAUNCHER_PREFLIGHT,
    PHASE_SESSION_START,
    PHASE_WORKER_ERROR,
)


def _classify_failure_phase(exc: BaseException) -> str:
    """Map a session-start fail-closed exception to a redaction-safe phase token.

    The subtype order matters: :class:`CoordinatorSharedCreateReleaseError` is a
    subclass of :class:`CoordinatorSharedCreateLockUnavailable`, and the release
    phase is *materially different* from the acquire phase (a release failure runs
    AFTER the shared ``workspace create`` — R8 review j#83633 F1), so it is checked
    first. ``prepare_session`` wraps both fence errors in a
    :class:`HerdrSessionStartError` (phase-accurate message), so the raw fence types
    are matched via the chained ``__cause__`` when present, then the message-free
    fallback keeps the enum closed.
    """
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, CoordinatorSharedCreateReleaseError):
        return PHASE_LOCK_RELEASE
    if isinstance(cause, CoordinatorSharedCreateLockUnavailable):
        return PHASE_LOCK_ACQUIRE
    if isinstance(exc, CoordinatorSharedCreateReleaseError):
        return PHASE_LOCK_RELEASE
    if isinstance(exc, CoordinatorSharedCreateLockUnavailable):
        return PHASE_LOCK_ACQUIRE
    if isinstance(exc, HerdrLauncherIncompatibleError):
        return PHASE_LAUNCHER_PREFLIGHT
    return PHASE_SESSION_START


# -- redaction-safe command observation ----------------------------------------


class RecordingHerdrRunner:
    """Wrap an injected ``runner``; record redaction-safe herdr command observations.

    Injected where the production path takes a ``runner`` (the
    :data:`~...infrastructure.herdr_transport.Runner` port). Every call is forwarded
    verbatim to the wrapped runner (so the real code drives the real state machine /
    fake), while a redaction-safe *tape* is kept for the evidence summary:

    - ``workspace create`` — the ``--label`` only (``coordinators`` is a fixed,
      non-secret vocabulary token, never a path);
    - ``workspace list`` — a bare count (the label read the shared path performs);
    - ``agent start`` — the durable ``mzb1_...`` NAME positional only (a mozyo
      identity token, not a secret);
    - ``pane close`` — the exact ``wN:pM`` handle.

    It never records ``--env`` values, ``--cwd`` paths, or any full payload, so the
    tape can be summarised into a Redmine journal without leaking a home path or a
    credential-shaped literal (Redmine #14187 Acceptance 4/6). Thread-safe: the
    concurrent driver shares one instance across threads, so a lock guards both the
    forward call and the tape append (the ``flock`` fence already serialises the
    list→create critical section; this lock only keeps the *tape* consistent).

    **Actuation-receipt authority (review j#83905 F2).** The tape also records the
    *results* of successful mutations — the ``pane_id`` a ``workspace create`` /
    ``agent start`` actually landed, and the ``(workspace_id -> label)`` a create
    minted — parsed from the forwarded response. This is what makes cleanup and residue
    verification independent of the per-project observations: a worker that crashes
    AFTER its ``agent start`` succeeds loses its observation, but the pane it launched
    is already on this tape, so :meth:`SharedSpaceSmokeHarness.cleanup` still closes it
    and :meth:`verify_residue` still counts it. Only a ``returncode == 0`` response with
    a parseable id is recorded (a failed / mislocated launch left no live pane).
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        #: ``--label`` values of every ``workspace create`` request (``""`` unlabelled).
        self.workspace_create_labels: list = []
        #: How many ``workspace list`` reads happened (the shared-path label read).
        self.workspace_list_count = 0
        #: Durable ``mzb1_...`` names passed to ``agent start`` (identity tokens).
        self.agent_start_names: list = []
        #: Exact ``wN:pM`` handles passed to ``pane close``.
        self.pane_close_handles: list = []
        #: RECEIPT — ``wN:pM`` pane locators every SUCCESSFUL ``agent start`` landed
        #: (parsed from the response), the authoritative teardown set.
        self.launched_locators: list = []
        #: RECEIPT — ``{workspace_id: label}`` every SUCCESSFUL ``workspace create``
        #: minted (id from the response, label from the request).
        self.created_workspaces: dict = {}

    def __call__(self, argv, *args, **kwargs):
        rest = list(argv[1:])
        with self._lock:
            self._record_request(rest)
            result = self._inner(argv, *args, **kwargs)
            self._record_receipt(rest, result)
            return result

    #: ``support.herdr_fake.FakeHerdr`` and ``subprocess.run`` are both accepted as
    #: the wrapped ``runner``; expose ``.run`` too so an inner object that is *itself*
    #: a bound ``run`` method or a callable both work uniformly.
    run = __call__

    def _record_request(self, rest: Sequence[str]) -> None:
        head = list(rest[:2])
        if head == ["workspace", "create"]:
            self.workspace_create_labels.append(_flag_value(rest, "--label"))
        elif head == ["workspace", "list"]:
            self.workspace_list_count += 1
        elif head == ["agent", "start"]:
            # argv is ["agent", "start", NAME, ...]; NAME is the durable identity.
            name = rest[2] if len(rest) > 2 and not str(rest[2]).startswith("--") else ""
            self.agent_start_names.append(_norm(name))
        elif head == ["pane", "close"]:
            self.pane_close_handles.append(rest[2] if len(rest) > 2 else "")

    def _record_receipt(self, rest: Sequence[str], result: object) -> None:
        # Only a successful, parseable response is a real actuation receipt.
        if getattr(result, "returncode", 1) != 0:
            return
        stdout = getattr(result, "stdout", "")
        head = list(rest[:2])
        if head == ["agent", "start"]:
            parsed = _parse_started_agent(stdout)
            if parsed is not None and parsed[0]:
                self.launched_locators.append(parsed[0])
        elif head == ["workspace", "create"]:
            parsed = _parse_workspace_created(stdout)
            if parsed is not None:
                workspace_id, _root_pane = parsed
                self.created_workspaces[workspace_id] = _flag_value(rest, "--label")

    def merge_receipts(
        self,
        *,
        launched_locators: Sequence[str],
        created_workspaces: dict[str, str],
        agent_start_names: Sequence[str],
        coordinators_create_count: int,
    ) -> None:
        """Merge redacted receipts returned by an owned forked smoke worker.

        Only the exact identity tokens the recorder already owns are accepted.  This
        is the parent-side recovery seam for the true cross-process driver: cleanup
        remains receipt-driven even though each child had its own address space.
        """
        with self._lock:
            self.launched_locators.extend(
                locator for locator in launched_locators if _norm(locator)
            )
            self.created_workspaces.update(
                {
                    _norm(workspace): _norm(label)
                    for workspace, label in created_workspaces.items()
                    if _norm(workspace)
                }
            )
            self.agent_start_names.extend(
                _norm(name) for name in agent_start_names if _norm(name)
            )
            # Request-count evidence is independent of parseable create receipts.
            self.workspace_create_labels.extend(
                [SHARED_COORDINATOR_WORKSPACE_LABEL]
                * max(0, int(coordinators_create_count))
            )

    @property
    def coordinators_create_count(self) -> int:
        """How many workspaces were created carrying the exact ``coordinators`` label."""
        return sum(
            1
            for label in self.workspace_create_labels
            if label == SHARED_COORDINATOR_WORKSPACE_LABEL
        )

    @property
    def created_coordinators_workspaces(self) -> "list[str]":
        """The receipt ``workspace_id``s created carrying the exact ``coordinators`` label."""
        return [
            ws
            for ws, label in self.created_workspaces.items()
            if label == SHARED_COORDINATOR_WORKSPACE_LABEL
        ]


def _flag_value(rest: Sequence[str], flag: str) -> str:
    """The token following ``flag`` in ``rest`` (``""`` if absent / trailing)."""
    tokens = list(rest)
    try:
        index = tokens.index(flag)
    except ValueError:
        return ""
    return tokens[index + 1] if index + 1 < len(tokens) else ""


# -- per-project + aggregate observations (redaction-safe) ---------------------


@dataclass(frozen=True)
class ProjectSmokeObservation:
    """One project's shared-space run outcome (redaction-safe value).

    ``project_key`` is an abstract label (``"p1"`` …), never a real path. Every id
    is a herdr / mozyo identity token, not a secret.
    """

    project_key: str
    workspace_id: str  # the mozyo workspace segment (this project's identity)
    outcome: str  # "created" | "adopted" | "failed"
    coordinators_workspace_id: str  # the shared herdr workspace (``wN``), ``""`` on fail
    launched_roles: tuple = ()
    adopted_roles: tuple = ()
    launched_names: tuple = ()  # durable ``mzb1_...`` names this project launched
    launched_locators: tuple = ()  # exact ``wN:pM`` handles this project launched
    failure_phase: str = PHASE_NONE

    @property
    def created_coordinators_space(self) -> bool:
        """Whether THIS project created the shared ``coordinators`` workspace."""
        return self.outcome == "created"


@dataclass(frozen=True)
class SharedSpaceSmokeObservation:
    """The aggregate, residue-proven evidence of a shared-space smoke run.

    Every field is a count / bool / closed token, so :meth:`as_evidence` can be
    summarised straight into a Redmine journal with no path or payload leak.
    """

    projects: tuple = ()
    requested_projects: int = 0  # how many projects the smoke was asked to run
    coordinators_create_count: int = 0  # MUST be 1 (single-flight convergence)
    duplicate_agents: int = 0  # MUST be 0 (no assigned name minted twice)
    lock_engaged: bool = False  # the single-flight fence file was created
    lock_released_clean: bool = False  # the fence is free again after the run
    residue_workspaces: int = -1  # after cleanup; MUST be 0 (unset/failed = not verified)
    residue_agents: int = -1  # after cleanup; MUST be 0 (unset/failed = not verified)
    residue_verified: bool = False  # cleanup residue was read back successfully
    cleanup_attempted: bool = False

    @property
    def all_projects_completed(self) -> bool:
        """Every requested project produced an observation and none failed (F2).

        A crashed / dropped project must never let the aggregate claim success over
        the survivors alone (review j#83870 F2): both the count must match AND no
        observation may carry a ``failed`` outcome.
        """
        return (
            len(self.projects) == self.requested_projects
            and self.requested_projects > 0
            and all(p.outcome != "failed" for p in self.projects)
        )

    @property
    def converged(self) -> bool:
        """The core acceptance: exactly one ``coordinators`` space, no duplicates.

        Now gated on completeness (F2): a false green from a dropped project — where
        the survivors happen to show create-count 1 / duplicate 0 — is no longer
        ``converged``, because a missing or failed project fails
        :attr:`all_projects_completed`.
        """
        return (
            self.all_projects_completed
            and self.coordinators_create_count == 1
            and self.duplicate_agents == 0
        )

    @property
    def residue_clear(self) -> bool:
        """Cleanup ran, every project completed, residue was READ BACK, and it was zero.

        Gated on three things (Acceptance 5):

        - :attr:`residue_verified` (F3) — an unreadable inventory can no longer
          masquerade as residue-0; an unverified residue is never clear;
        - :attr:`all_projects_completed` (review j#83905 F2) — a crashed / failed
          project's actuation-identity coverage may be incomplete, so even a receipt-
          driven residue-0 read is not claimed clean while any project failed (the
          honest fallback that complements the receipt-tape cleanup);
        - both residue counts zero.
        """
        return (
            self.cleanup_attempted
            and self.all_projects_completed
            and self.residue_verified
            and self.residue_workspaces == 0
            and self.residue_agents == 0
        )

    def as_evidence(self) -> dict:
        """A redaction-safe summary dict for a durable Redmine journal.

        Counts, bools, and closed phase tokens only — never a home path, an
        ``--env`` value, or a raw herdr payload (Redmine #14187 Acceptance 4/6).
        """
        return {
            "requested_projects": self.requested_projects,
            "completed_projects": len(self.projects),
            "all_projects_completed": self.all_projects_completed,
            "coordinators_create_count": self.coordinators_create_count,
            "duplicate_agents": self.duplicate_agents,
            "lock_engaged": self.lock_engaged,
            "lock_released_clean": self.lock_released_clean,
            "residue_workspaces": self.residue_workspaces,
            "residue_agents": self.residue_agents,
            "residue_verified": self.residue_verified,
            "cleanup_attempted": self.cleanup_attempted,
            "converged": self.converged,
            "residue_clear": self.residue_clear,
            "projects": [
                {
                    "project_key": p.project_key,
                    "outcome": p.outcome,
                    "launched_roles": list(p.launched_roles),
                    "adopted_roles": list(p.adopted_roles),
                    "failure_phase": p.failure_phase,
                }
                for p in self.projects
            ],
        }


__all__ = (
    "PHASE_ISOLATION",
    "PHASE_LAUNCHER_PREFLIGHT",
    "PHASE_LOCK_ACQUIRE",
    "PHASE_LOCK_RELEASE",
    "PHASE_NONE",
    "PHASE_SESSION_START",
    "PHASE_WORKER_ERROR",
    "SMOKE_FAILURE_PHASES",
    "ProjectSmokeObservation",
    "RecordingHerdrRunner",
    "SharedSpaceSmokeError",
    "SharedSpaceSmokeObservation",
    "SmokeIsolationError",
    "_classify_failure_phase",
    "_flag_value",
)
