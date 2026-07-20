"""High-level isolated shared-space coordinator-placement smoke harness (Redmine #14187).

Why this module exists
----------------------
Redmine #14185 (shared-space live smoke) could validate the *pure* placement
decisions and the ``fcntl.flock`` fence primitive directly, but its Acceptance 2/3
— actuating the REAL cross-process ``shared_space`` path (an actual ``coordinators``
workspace create, real coordinator pairs launched/adopted, create-count == 1 under
concurrency) and then *cleaning it up* — hit a hard blocker (#14185 Review j#83785):
the public ``mozyo-bridge herdr`` surface exposes no way to isolate a herdr instance,
observe the workspace-list/create/agent-start payloads, or close a herdr workspace
without dropping to raw Herdr (``HERDR_CONFIG_PATH`` / ``herdr server`` / manual
``herdr workspace ...``), which the acceptance forbids.

This harness is that missing high-level surface. It drives the SAME production
``shared_space`` path (:func:`herdr_session_start.prepare_session` under the real
``coordinator_shared_create_lock`` fence and the real ``_shared_coordinator_target``
resolver) through an **injected ``runner``**, so:

- unit / integration exercise it against the shared in-memory fake
  (``support.herdr_fake.FakeHerdr``) — no live herdr binary, no tmux, no SQLite;
- the real live smoke (#14185, post-review) re-drives the exact same
  :meth:`SharedSpaceSmokeHarness.run_project` / :meth:`run_concurrent` with a real
  subprocess ``runner``, in an isolated operator home, and gets a residue-proven
  teardown it never had before.

Safety posture (Redmine #14187 Acceptance 1/5/6)
------------------------------------------------
- **Isolation is proven before any actuation.** Every run happens under an
  explicitly-provided isolated operator home; :func:`prove_smoke_isolation` fails
  closed BEFORE the first herdr command unless that home is provably distinct from
  (and not nested with) the real operator home. This is also the *cleanup authority*
  gate: the harness only ever creates inside the isolated home, so it can always tear
  down exactly what it made (Acceptance 5 — "cleanup 不能時は create 前に fail-closed").
- **Cleanup is by exact identity.** Teardown closes only the exact pane handles this
  run launched (a herdr workspace auto-vanishes with its last pane — live-measured
  #13380), then :meth:`verify_residue` proves zero residue by reading the labels and
  the live inventory back.
- **Evidence is redaction-safe.** :class:`RecordingHerdrRunner` records only command
  *kinds* and the non-secret herdr/mozyo identity tokens (``coordinators`` label,
  ``mzb1_...`` names, ``wN:pM`` handles); it never records ``--env`` values, home
  paths, or full payloads. :meth:`SharedSpaceSmokeObservation.as_evidence` summarises
  counts + phase only, so nothing raw reaches a Redmine journal.
- **No new placement authority.** The harness decides nothing about placement: it
  writes the operator ``coordinator-placement.yaml`` (``mode: shared_space``) into the
  isolated home, resolves it through the real loader (the semantic facade), and passes
  the resolved mode to ``prepare_session``. The verbatim ``coordinators`` label, the
  no-implicit-promotion guard, the single-flight fence and the resolver are all the
  untouched #14139 core.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Mapping, Optional, Sequence

from mozyo_bridge.shared.paths import mozyo_bridge_home
from mozyo_bridge.core.state.workspace_registry import register_workspace
from mozyo_bridge.core.state.startup_transaction_fence import StartupTransactionFence
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_health import (  # noqa: E501
    StartupProbe,
)
from mozyo_bridge.core.state.coordinator_placement_fence import (
    coordinator_shared_create_lock,
    coordinator_shared_create_lock_path,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.coordinator_placement_loader import (  # noqa: E501
    coordinator_placement_path,
    resolve_coordinator_placement_mode,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.coordinator_placement_mode import (  # noqa: E501
    SHARED_SPACE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    _norm,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    HerdrSessionStartError,
    SHARED_COORDINATOR_WORKSPACE_LABEL,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
    HerdrLauncherIncompatibleError,
    _invoke,
    _list_rows,
    _list_workspace_labels,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
    herdr_session_start as _session,
)
from mozyo_bridge.core.state.coordinator_placement_fence import (
    CoordinatorSharedCreateLockUnavailable,
    CoordinatorSharedCreateReleaseError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    COMMAND_TIMEOUT_SECONDS,
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

#: The closed set of failure phases the harness can report.
SMOKE_FAILURE_PHASES = (
    PHASE_NONE,
    PHASE_ISOLATION,
    PHASE_LOCK_ACQUIRE,
    PHASE_LOCK_RELEASE,
    PHASE_LAUNCHER_PREFLIGHT,
    PHASE_SESSION_START,
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
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        #: ``--label`` values of every ``workspace create`` (``""`` when unlabelled).
        self.workspace_create_labels: list = []
        #: How many ``workspace list`` reads happened (the shared-path label read).
        self.workspace_list_count = 0
        #: Durable ``mzb1_...`` names passed to ``agent start`` (identity tokens).
        self.agent_start_names: list = []
        #: Exact ``wN:pM`` handles passed to ``pane close`` (teardown tape).
        self.pane_close_handles: list = []

    def __call__(self, argv, *args, **kwargs):
        rest = list(argv[1:])
        with self._lock:
            self._record(rest)
            return self._inner(argv, *args, **kwargs)

    #: ``support.herdr_fake.FakeHerdr`` and ``subprocess.run`` are both accepted as
    #: the wrapped ``runner``; expose ``.run`` too so an inner object that is *itself*
    #: a bound ``run`` method or a callable both work uniformly.
    run = __call__

    def _record(self, rest: Sequence[str]) -> None:
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

    @property
    def coordinators_create_count(self) -> int:
        """How many workspaces were created carrying the exact ``coordinators`` label."""
        return sum(
            1
            for label in self.workspace_create_labels
            if label == SHARED_COORDINATOR_WORKSPACE_LABEL
        )


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
    coordinators_create_count: int = 0  # MUST be 1 (single-flight convergence)
    duplicate_agents: int = 0  # MUST be 0 (no assigned name minted twice)
    lock_engaged: bool = False  # the single-flight fence file was created
    lock_released_clean: bool = False  # the fence is free again after the run
    residue_workspaces: int = -1  # after cleanup; MUST be 0 (unset = not verified)
    residue_agents: int = -1  # after cleanup; MUST be 0 (unset = not verified)
    cleanup_attempted: bool = False

    @property
    def converged(self) -> bool:
        """The core acceptance: exactly one ``coordinators`` space, no duplicates."""
        return self.coordinators_create_count == 1 and self.duplicate_agents == 0

    @property
    def residue_clear(self) -> bool:
        """Cleanup ran and left zero residue (Acceptance 5)."""
        return (
            self.cleanup_attempted
            and self.residue_workspaces == 0
            and self.residue_agents == 0
        )

    def as_evidence(self) -> dict:
        """A redaction-safe summary dict for a durable Redmine journal.

        Counts, bools, and closed phase tokens only — never a home path, an
        ``--env`` value, or a raw herdr payload (Redmine #14187 Acceptance 4/6).
        """
        return {
            "coordinators_create_count": self.coordinators_create_count,
            "duplicate_agents": self.duplicate_agents,
            "lock_engaged": self.lock_engaged,
            "lock_released_clean": self.lock_released_clean,
            "residue_workspaces": self.residue_workspaces,
            "residue_agents": self.residue_agents,
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


# -- isolation authority (fail-closed before actuation) ------------------------


def prove_smoke_isolation(isolated_home: Path, *, operator_home: Path) -> Path:
    """Fail closed unless ``isolated_home`` is a safe, distinct smoke home.

    The cleanup-authority gate (Redmine #14187 Acceptance 5): the harness only ever
    creates inside the isolated home, so it can always tear down exactly what it made
    — but ONLY if that home is provably NOT the real operator home and NOT nested with
    it either way (a real home *inside* the smoke home would be a cleanup target; the
    smoke home *inside* the real home would leak writes into it). Returns the resolved
    isolated home on success; raises :class:`SmokeIsolationError` (before any herdr
    command) otherwise. Never mutates anything.
    """
    isolated = Path(isolated_home).expanduser().resolve()
    operator = Path(operator_home).expanduser().resolve()
    if isolated == operator:
        raise SmokeIsolationError(
            "shared-space smoke home resolves to the real operator home; refuse to "
            "actuate the shared coordinators path against the operator's own herdr / "
            "home (Redmine #14187 Acceptance 1/5)"
        )
    if operator in isolated.parents:
        raise SmokeIsolationError(
            "shared-space smoke home is a descendant of the real operator home; a "
            "smoke write would land inside the operator's home tree — refuse "
            "(Redmine #14187 Acceptance 1/6)"
        )
    if isolated in operator.parents:
        raise SmokeIsolationError(
            "the real operator home is a descendant of the shared-space smoke home; a "
            "smoke cleanup could remove the operator's home — refuse "
            "(Redmine #14187 Acceptance 5)"
        )
    return isolated


@contextmanager
def isolated_smoke_home(
    isolated_home: Path, *, operator_home: Optional[Path] = None
) -> Iterator[Path]:
    """Establish the isolated operator home for a smoke run (fail-closed, restoring).

    Proves isolation (:func:`prove_smoke_isolation`) BEFORE touching anything, then
    points the process ``MOZYO_BRIDGE_HOME`` at the isolated home so the production
    path (``prepare_session`` → ``mozyo_bridge_home()`` → the single-flight fence /
    registry / store) resolves the isolated home — never the operator's. Writes the
    operator placement file (``mode: shared_space``) into the isolated home so the
    shared path is exercised through the real semantic facade, not a hardcoded mode.
    Restores the prior ``MOZYO_BRIDGE_HOME`` on exit. ``operator_home`` defaults to
    the currently-resolved home (captured before the override) so the distinctness
    guard is measured against the real operator home.
    """
    import os

    prior = os.environ.get("MOZYO_BRIDGE_HOME")
    # Capture the real operator home BEFORE any override so the guard measures the
    # right thing (a caller may pass it explicitly for a fully hermetic test).
    operator = Path(operator_home) if operator_home is not None else mozyo_bridge_home()
    isolated = prove_smoke_isolation(isolated_home, operator_home=operator)
    isolated.mkdir(parents=True, exist_ok=True)
    # Write the operator placement file into the isolated home and prove it round-trips
    # through the real loader (the semantic facade) to `shared_space`.
    coordinator_placement_path(isolated).write_text(
        "mode: shared_space\n", encoding="utf-8"
    )
    os.environ["MOZYO_BRIDGE_HOME"] = str(isolated)
    try:
        resolved_mode = resolve_coordinator_placement_mode(isolated)
        if resolved_mode != SHARED_SPACE:
            raise SmokeIsolationError(
                "isolated operator placement file did not resolve to shared_space "
                f"through the loader (got {resolved_mode!r}); refuse to run the "
                "shared-space smoke on an unverified facade"
            )
        yield isolated
    finally:
        if prior is None:
            os.environ.pop("MOZYO_BRIDGE_HOME", None)
        else:
            os.environ["MOZYO_BRIDGE_HOME"] = prior


# -- the harness ---------------------------------------------------------------


@dataclass
class _ProjectSpec:
    """One project's inputs for a shared-space run (a repo root + abstract key)."""

    project_key: str
    repo_root: Path


class SharedSpaceSmokeHarness:
    """Drive + observe + tear down the real ``shared_space`` path in an isolated home.

    Construct with the isolated home already established (see
    :func:`isolated_smoke_home`), an injected ``runner`` (the fake for tests, a real
    subprocess runner for the #14185 live smoke), and the trusted launch ``env`` (must
    carry ``MOZYO_HERDR_BINARY`` — the harness never resolves a binary itself). The
    ``runner`` is wrapped in a :class:`RecordingHerdrRunner` so the evidence tape is
    kept without the subject-under-test knowing.

    The harness makes NO placement decision: every project runs the production
    :func:`prepare_session` in ``shared_space`` mode for the DEFAULT (coordinator)
    lane, so the verbatim ``coordinators`` label, the single-flight fence and the
    ``_shared_coordinator_target`` resolver are all the untouched #14139 core.
    """

    def __init__(
        self,
        *,
        home: Path,
        runner,
        env: Mapping[str, str],
        timeout: float = COMMAND_TIMEOUT_SECONDS,
        providers: Sequence[str] = ("claude", "codex"),
        startup_fence_factory: "Optional[Callable[[str], StartupTransactionFence]]" = None,
        probe: "Optional[StartupProbe]" = None,
    ) -> None:
        self.home = Path(home)
        self.recorder = RecordingHerdrRunner(runner)
        self.env = dict(env)
        self.timeout = timeout
        self.providers = tuple(providers)
        # Isolate the ORTHOGONAL home-scoped startup-transaction fence (#13948, a brief
        # non-blocking per-DB-write lock) per project, so concurrent projects never
        # collide on IT — this harness targets the shared-coordinators create
        # single-flight, which still uses the shared home lock (Redmine #14139 R7
        # j#83573 established this isolation for the convergence test). Injectable so
        # the #14185 live smoke can substitute a home-scoped fence if it wants the real
        # cross-process contention on this axis too.
        self._startup_fence_factory = startup_fence_factory or (
            lambda key: StartupTransactionFence(
                path=self.home / f"smoke-startup-{key}.sqlite"
            )
        )
        # A fast, timeless startup health probe by default (no real sleep), so the
        # smoke does not wall-clock on the per-role liveness poll; #14185 live may pass
        # a real probe. Mirrors the established `_FAST_PROBE` shape.
        self._probe = probe or StartupProbe(
            polls=3, interval=0.0, sleeper=lambda _seconds: None
        )
        #: The binary the injected env pins (resolved once, shared by every call). The
        #: real code resolves it from the trusted env; the fake ignores the value.
        self._binary = _session._resolve_binary_or_die(self.env)

    # -- clean-slate preflight (cleanup authority, herdr dimension) ------------

    def preflight_clean_slate(self) -> None:
        """Fail closed (zero actuation) unless no ``coordinators`` space exists yet.

        The cleanup-authority gate in the herdr-server dimension (Redmine #14187
        Acceptance 5): the isolated home isolates the mozyo registry / fence / store,
        but the ``coordinators`` LABEL is global to the herdr server. If a REAL shared
        coordinators workspace already exists (an operator who runs ``shared_space``),
        the resolver would ADOPT it and this run would launch test coordinator pairs
        INTO the operator's live space — a side effect cleanup could not prove it owns.
        So the smoke requires a clean slate: read the labels (read-only) and refuse,
        before any create, if one is already labelled ``coordinators``. Unreadable
        labels also fail closed (never guess a clean slate). This is a no-op against the
        empty isolated fake, and the real safety fence for the #14185 live smoke.
        """
        labels = _list_workspace_labels(self._binary, self.recorder, self.timeout)
        if labels is None:
            raise SharedSpaceSmokeError(
                "could not read the herdr workspace labels; refuse to run the "
                "shared-space smoke without proving a clean slate (Redmine #14187 "
                "Acceptance 5)"
            )
        existing = sorted(
            ws for ws, label in labels.items()
            if label == SHARED_COORDINATOR_WORKSPACE_LABEL
        )
        if existing:
            raise SharedSpaceSmokeError(
                f"a workspace already carries the shared coordinators label "
                f"{SHARED_COORDINATOR_WORKSPACE_LABEL!r} ({existing!r}); refuse to "
                "actuate the shared-space smoke into a pre-existing coordinators space "
                "— it would adopt / pollute a real operator space and cleanup could not "
                "prove exact ownership (Redmine #14187 Acceptance 5). Retire the existing "
                "coordinators pairs, or run the smoke against a disposable herdr instance."
            )

    # -- one project ----------------------------------------------------------

    def run_project(self, spec: _ProjectSpec) -> ProjectSmokeObservation:
        """Run one project's ``shared_space`` coordinator launch; observe the outcome.

        Registers the project's test workspace identity in the isolated home, then
        drives the production ``prepare_session`` (``coordinator_placement_mode ==
        shared_space``, default lane). A fail-closed refusal is captured as a
        ``failed`` observation carrying the closed failure phase — never a raw message.
        """
        register_workspace(spec.repo_root, home=self.home)
        try:
            result = _session.prepare_session(
                repo_root=spec.repo_root,
                providers=list(self.providers),
                lane_id="",
                env=self.env,
                runner=self.recorder,
                timeout=self.timeout,
                coordinator_placement_mode=SHARED_SPACE,
                startup_fence=self._startup_fence_factory(spec.project_key),
                probe=self._probe,
            )
        except (HerdrSessionStartError, CoordinatorSharedCreateLockUnavailable) as exc:
            return ProjectSmokeObservation(
                project_key=spec.project_key,
                workspace_id=_resolve_project_workspace_id(spec.repo_root, self.home),
                outcome="failed",
                coordinators_workspace_id="",
                failure_phase=_classify_failure_phase(exc),
            )
        launched = tuple(s.provider for s in result.slots if s.outcome == _session.SLOT_LAUNCHED)
        adopted = tuple(s.provider for s in result.slots if s.outcome == _session.SLOT_ADOPTED)
        launched_names = tuple(
            s.assigned_name for s in result.slots if s.outcome == _session.SLOT_LAUNCHED
        )
        launched_locators = tuple(
            s.locator
            for s in result.slots
            if s.outcome == _session.SLOT_LAUNCHED and s.locator
        )
        # A fresh clean-slate launch captured a base pane when it CREATED the shared
        # workspace (`_create_workspace`); an adopt reused an existing one and captured
        # none. That per-result signal attributes create-vs-adopt without racing the
        # shared recorder tape.
        outcome = "created" if result.base_pane_id else "adopted"
        return ProjectSmokeObservation(
            project_key=spec.project_key,
            workspace_id=result.workspace_id,
            outcome=outcome,
            coordinators_workspace_id=result.herdr_workspace_id,
            launched_roles=launched,
            adopted_roles=adopted,
            launched_names=launched_names,
            launched_locators=launched_locators,
        )

    # -- concurrent projects --------------------------------------------------

    def run_concurrent(
        self, specs: Sequence[_ProjectSpec]
    ) -> "list[ProjectSmokeObservation]":
        """Run every project's shared-space launch CONCURRENTLY; return the outcomes.

        The deterministic realization of "2-process concurrent start" the isolated
        smoke needs (Redmine #14187 Acceptance 3): one thread per project, released
        together by a :class:`threading.Barrier`, all sharing the isolated home so
        they contend on the REAL ``coordinator_shared_create_lock`` (``flock`` contends
        across fds even in one process — #14139 R7 j#83573). The shared recorder is
        thread-safe, and the fence serialises the list→create critical section, so the
        first thread creates the ``coordinators`` workspace and the rest adopt it —
        create-count converges to one. #14185 re-uses :meth:`run_project` under a true
        ``multiprocessing`` driver for the live cross-process proof.
        """
        if not specs:
            return []
        barrier = threading.Barrier(len(specs))
        results: "list[Optional[ProjectSmokeObservation]]" = [None] * len(specs)

        def _worker(index: int, spec: _ProjectSpec) -> None:
            barrier.wait()
            results[index] = self.run_project(spec)

        threads = [
            threading.Thread(target=_worker, args=(index, spec), name=f"smoke-{spec.project_key}")
            for index, spec in enumerate(specs)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return [r for r in results if r is not None]

    # -- teardown by exact identity ------------------------------------------

    def cleanup(self, observations: Sequence[ProjectSmokeObservation]) -> None:
        """Close exactly the panes this run launched (herdr auto-vanishes the workspace).

        Teardown by exact identity (Redmine #14187 Acceptance 5): only the ``wN:pM``
        handles this run launched are closed — never a scanned-for pane — so a herdr
        workspace with its last pane closed auto-vanishes (live-measured #13380). A
        close that fails is left for :meth:`verify_residue` to catch rather than
        hidden; the harness never issues a generic kill.
        """
        for observation in observations:
            for locator in observation.launched_locators:
                if not locator:
                    continue
                try:
                    _invoke(
                        self._binary,
                        ["pane", "close", locator],
                        self.recorder,
                        self.timeout,
                        env=None,
                    )
                except HerdrSessionStartError:
                    # Residue verification is the proof; a close failure surfaces there.
                    continue

    def verify_residue(
        self, observations: Sequence[ProjectSmokeObservation]
    ) -> "tuple[int, int]":
        """Prove zero residue after cleanup: ``(residue_workspaces, residue_agents)``.

        Reads the live labels and the live inventory back through the same injected
        runner and counts how many of THIS run's created ``coordinators`` workspaces /
        launched ``mzb1_...`` names are still present. Both must be zero
        (Redmine #14187 Acceptance 5).
        """
        created_workspaces = {
            o.coordinators_workspace_id
            for o in observations
            if o.created_coordinators_space and o.coordinators_workspace_id
        }
        created_names = {
            name for o in observations for name in o.launched_names if name
        }
        labels = _list_workspace_labels(self._binary, self.recorder, self.timeout) or {}
        residue_workspaces = sum(1 for ws in created_workspaces if ws in labels)
        rows = _list_rows(self._binary, self.recorder, self.timeout)
        live_names = {
            _norm(row.get("name")) for row in rows if isinstance(row, Mapping)
        }
        residue_agents = sum(1 for name in created_names if name in live_names)
        return residue_workspaces, residue_agents

    # -- lock lifecycle observation (high-level) ------------------------------

    def observe_lock(self) -> "tuple[bool, bool]":
        """Observe the single-flight fence: ``(engaged, released_clean)``.

        High-level, non-actuating: the fence file existing proves the lock was
        *engaged* during the run; acquiring it non-blocking now proves it was
        *released* cleanly (a wedged holder would block). Never leaves the lock held.
        """
        lock_path = coordinator_shared_create_lock_path(self.home)
        engaged = lock_path.exists()
        released_clean = False
        if engaged:
            try:
                with coordinator_shared_create_lock(self.home):
                    released_clean = True
            except CoordinatorSharedCreateLockUnavailable:
                released_clean = False
        return engaged, released_clean

    # -- the whole smoke ------------------------------------------------------

    def smoke(
        self, specs: Sequence[_ProjectSpec]
    ) -> SharedSpaceSmokeObservation:
        """Run the whole concurrent shared-space smoke: drive → observe → clean → verify.

        The single entry the CLI / #14185 call: run every project concurrently,
        summarise the redaction-safe convergence evidence, tear down by exact
        identity, and prove zero residue. Isolation authority is the caller's
        responsibility (established via :func:`isolated_smoke_home` before construction),
        which is also the pre-create cleanup-authority gate.

        The herdr-dimension cleanup-authority gate (:meth:`preflight_clean_slate`)
        runs FIRST — before any project launches — so a pre-existing ``coordinators``
        space fails the whole smoke closed with zero actuation.
        """
        self.preflight_clean_slate()
        observations = self.run_concurrent(specs)
        duplicate_agents = _count_duplicate_agents(observations)
        coordinators_create_count = sum(
            1 for o in observations if o.created_coordinators_space
        )
        self.cleanup(observations)
        residue_workspaces, residue_agents = self.verify_residue(observations)
        lock_engaged, lock_released_clean = self.observe_lock()
        return SharedSpaceSmokeObservation(
            projects=tuple(observations),
            coordinators_create_count=coordinators_create_count,
            duplicate_agents=duplicate_agents,
            lock_engaged=lock_engaged,
            lock_released_clean=lock_released_clean,
            residue_workspaces=residue_workspaces,
            residue_agents=residue_agents,
            cleanup_attempted=True,
        )


def smoke_shared_space_preflight(
    isolated_home: Path,
    *,
    runner,
    env: Mapping[str, str],
    projects: int = 2,
    operator_home: Optional[Path] = None,
) -> dict:
    """Prove a shared-space smoke can run here safely; return a redaction-safe report.

    The read-only surface the CLI exposes and the #14185 live driver calls first: it
    establishes the isolated home (:func:`isolated_smoke_home`, which fails closed
    unless the home is provably distinct from the operator's), then runs the herdr
    clean-slate cleanup-authority gate (:meth:`SharedSpaceSmokeHarness.preflight_clean_slate`,
    a read-only ``workspace list``). It **actuates no agent** — the live coordinator
    launch is the #14185 driver's isolated-instance job. Returns counts / bools /
    closed tokens only (no home path, no env value), so the report can be summarised
    into a durable Redmine journal.
    """
    count = max(2, int(projects))
    with isolated_smoke_home(isolated_home, operator_home=operator_home) as home:
        harness = SharedSpaceSmokeHarness(home=home, runner=runner, env=env)
        harness.preflight_clean_slate()
    return {
        "isolated_home_ok": True,
        "clean_slate_ok": True,
        "mode": SHARED_SPACE,
        "projects": count,
        "coordinators_create_expected": 1,
        "actuated": False,
    }


def _count_duplicate_agents(observations: Sequence[ProjectSmokeObservation]) -> int:
    """How many durable ``mzb1_...`` names were launched more than once across projects."""
    seen: dict = {}
    for observation in observations:
        for name in observation.launched_names:
            seen[name] = seen.get(name, 0) + 1
    return sum(1 for count in seen.values() if count > 1)


def _resolve_project_workspace_id(repo_root: Path, home: Path) -> str:
    """Best-effort read-only workspace id for a project (for a failed observation)."""
    from mozyo_bridge.core.state.workspace_registry import load_workspace_by_path

    record = load_workspace_by_path(Path(repo_root), home=home)
    return _norm(record.workspace_id) if record is not None else ""


__all__ = (
    "PHASE_ISOLATION",
    "PHASE_LAUNCHER_PREFLIGHT",
    "PHASE_LOCK_ACQUIRE",
    "PHASE_LOCK_RELEASE",
    "PHASE_NONE",
    "PHASE_SESSION_START",
    "SMOKE_FAILURE_PHASES",
    "ProjectSmokeObservation",
    "RecordingHerdrRunner",
    "SharedSpaceSmokeError",
    "SharedSpaceSmokeHarness",
    "SharedSpaceSmokeObservation",
    "SmokeIsolationError",
    "isolated_smoke_home",
    "prove_smoke_isolation",
    "smoke_shared_space_preflight",
    "_ProjectSpec",
)
