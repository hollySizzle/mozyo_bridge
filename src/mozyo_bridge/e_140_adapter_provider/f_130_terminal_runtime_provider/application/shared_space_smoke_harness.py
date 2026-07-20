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
from dataclasses import InitVar, dataclass, field
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
    _invoke,
    _list_rows,
    _list_workspace_labels,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
    herdr_session_start as _session,
)
from mozyo_bridge.core.state.coordinator_placement_fence import (
    CoordinatorSharedCreateLockUnavailable,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    COMMAND_TIMEOUT_SECONDS,
)


from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.shared_space_smoke_observation import (  # noqa: E501
    PHASE_ISOLATION,
    PHASE_LAUNCHER_PREFLIGHT,
    PHASE_LOCK_ACQUIRE,
    PHASE_LOCK_RELEASE,
    PHASE_NONE,
    PHASE_SESSION_START,
    PHASE_WORKER_ERROR,
    SMOKE_FAILURE_PHASES,
    ProjectSmokeObservation,
    RecordingHerdrRunner,
    SharedSpaceSmokeError,
    SharedSpaceSmokeObservation,
    SmokeIsolationError,
    _classify_failure_phase,
    _flag_value,
)
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


#: Private mint token: an :class:`IsolationCapability` can only be constructed by
#: :func:`isolated_smoke_home` (which holds this token), so a caller cannot hand-build
#: a capability that names the operator home as "isolated" to bypass the guard
#: (review j#83905 F1). Module-private; never exported.
_ISOLATION_CAPABILITY_TOKEN = object()


@dataclass(frozen=True)
class IsolationCapability:
    """Proof that an isolated smoke home was established distinct from the operator home.

    Minted ONLY by :func:`isolated_smoke_home`, AFTER :func:`prove_smoke_isolation` has
    proven the isolated home is distinct from (and un-nested with) the operator home the
    mint captured from the **source of truth** — the effective ``mozyo_bridge_home()``
    BEFORE the ``MOZYO_BRIDGE_HOME`` override — NOT from any caller argument (review
    j#83935 F1). It carries that operator home so the harness can RE-prove isolation at
    actuation time rather than trust a bare value.

    Two constraints back the guarantee (nothing more is claimed): it is **immutable**
    (a frozen dataclass — its fields cannot be reassigned) and its construction requires
    the module-private mint token (a hand-built capability is refused). Because
    :func:`isolated_smoke_home` never accepts a caller-supplied proof target, there is no
    supported public path for a normal caller to name the operator home as "isolated" —
    the earlier ``operator_home`` override that allowed exactly that (review j#83935 F1)
    is removed. The harness requires an ``IsolationCapability`` to construct, so the
    "never used the context manager" misuse cannot even build a harness — the public
    use-case is the safety authority, not caller discipline.
    """

    isolated_home: Path
    operator_home: Path
    _mint_token: InitVar[object] = None

    def __post_init__(self, _mint_token: object) -> None:
        if _mint_token is not _ISOLATION_CAPABILITY_TOKEN:
            raise SmokeIsolationError(
                "IsolationCapability must be minted by isolated_smoke_home(); refuse a "
                "hand-built capability (Redmine #14187 Acceptance 1/5; review j#83905 F1)"
            )


@contextmanager
def isolated_smoke_home(isolated_home: Path) -> Iterator[IsolationCapability]:
    """Establish the isolated operator home for a smoke run (fail-closed, restoring).

    Proves isolation (:func:`prove_smoke_isolation`) BEFORE touching anything, then
    points the process ``MOZYO_BRIDGE_HOME`` at the isolated home so the production
    path (``prepare_session`` → ``mozyo_bridge_home()`` → the single-flight fence /
    registry / store) resolves the isolated home — never the operator's. Writes the
    operator placement file (``mode: shared_space``) into the isolated home so the
    shared path is exercised through the real semantic facade, not a hardcoded mode.
    Restores the prior ``MOZYO_BRIDGE_HOME`` on exit.

    The operator home the isolation is proven against is ALWAYS the effective
    ``mozyo_bridge_home()`` captured BEFORE the override — the source of truth — never a
    caller argument (review j#83935 F1: a public ``operator_home`` override let a caller
    name a fake distinct home and mint the REAL operator home as "isolated"). A hermetic
    test controls the operator home the only legitimate way: by setting the ambient
    ``MOZYO_BRIDGE_HOME`` to its temp fixture before calling this — that IS the authority,
    not a substitution of it.

    Yields an :class:`IsolationCapability` — the verified, immutable, mint-token-guarded
    proof the harness requires to construct.
    """
    import os

    prior = os.environ.get("MOZYO_BRIDGE_HOME")
    # The operator home is the effective home BEFORE the override — the source of truth,
    # not a caller argument (review j#83935 F1). A test sets ambient MOZYO_BRIDGE_HOME to
    # its fixture to control this; production reads the operator's real home.
    operator = mozyo_bridge_home()
    isolated = prove_smoke_isolation(isolated_home, operator_home=operator)
    capability = IsolationCapability(
        isolated, operator, _mint_token=_ISOLATION_CAPABILITY_TOKEN
    )
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
        yield capability
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

    Construct with the :class:`IsolationCapability` :func:`isolated_smoke_home` minted
    (the verified isolation proof — review j#83905 F1), an injected ``runner`` (the fake
    for tests, a real subprocess runner for the #14185 live smoke), and the trusted
    launch ``env`` (must carry ``MOZYO_HERDR_BINARY`` — the harness never resolves a
    binary itself). The ``runner`` is wrapped in a :class:`RecordingHerdrRunner` that
    keeps the actuation-receipt tape (what was created / launched / closed) so cleanup
    and residue verification never depend on a crashed project's observation.

    The harness makes NO placement decision: every project runs the production
    :func:`prepare_session` in ``shared_space`` mode for the DEFAULT (coordinator)
    lane, so the verbatim ``coordinators`` label, the single-flight fence and the
    ``_shared_coordinator_target`` resolver are all the untouched #14139 core.
    """

    def __init__(
        self,
        *,
        capability: IsolationCapability,
        runner,
        env: Mapping[str, str],
        timeout: float = COMMAND_TIMEOUT_SECONDS,
        providers: Sequence[str] = ("claude", "codex"),
        startup_fence_factory: "Optional[Callable[[str], StartupTransactionFence]]" = None,
        probe: "Optional[StartupProbe]" = None,
    ) -> None:
        # The verified isolation proof (review j#83905 F1): the harness cannot be
        # constructed without an `IsolationCapability` that `isolated_smoke_home` minted
        # after proving distinctness from the REAL operator home. `_assert_isolation_bound`
        # re-proves it at actuation time, so neither a split-brain (ambient != isolated)
        # nor an operator-home-as-both (value agreement without isolation) can reach a
        # herdr write. A caller that hand-builds a capability is refused at mint.
        if not isinstance(capability, IsolationCapability):
            raise SmokeIsolationError(
                "SharedSpaceSmokeHarness requires an IsolationCapability from "
                "isolated_smoke_home(); refuse to actuate without a verified isolation "
                "proof (Redmine #14187 Acceptance 1/5; review j#83905 F1)"
            )
        self._capability = capability
        self.home = capability.isolated_home
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

    # -- isolation binding (fail-closed before any actuation) -----------------

    def _assert_isolation_bound(self) -> None:
        """Fail closed unless the run is bound to a proven-isolated home (two checks).

        Runs at EVERY mutating entry, BEFORE the first herdr command, so any isolation
        break fails closed with zero workspace / agent create and zero operator-home
        write. The public use-case is the safety authority, never caller discipline.
        Two independent checks (the messages name no path — redaction):

        1. **Distinctness re-proof (review j#83905 F1).** Re-run
           :func:`prove_smoke_isolation` on the capability's ``(isolated_home,
           operator_home)`` — the operator home the isolation was minted against. This
           is what value-agreement (the R2 check alone) missed: a home that IS the
           operator home never mints a capability (the mint runs the same proof), and
           re-proving here refuses a stale one. Proving isolation ≠ proving two values
           are equal.
        2. **Ambient binding (R1 review j#83870 F1).** The production path
           (``prepare_session`` / the single-flight fence / the internal
           ``register_workspace(repo_root)``) resolves the home from the ambient
           ``MOZYO_BRIDGE_HOME`` (``mozyo_bridge_home()``), so it must equal the proven
           isolated home; otherwise a write would land in whatever the ambient home is.
        """
        # 1. The capability's isolated home is genuinely distinct from the operator home.
        prove_smoke_isolation(
            self._capability.isolated_home, operator_home=self._capability.operator_home
        )
        # 2. The ambient home the production path reads IS that isolated home.
        if mozyo_bridge_home() != self.home:
            raise SmokeIsolationError(
                "the ambient MOZYO_BRIDGE_HOME does not resolve to the harness's isolated "
                "home; the production shared-space path (prepare_session / the single-"
                "flight fence / the workspace registry) reads the ambient home, so a "
                "mismatch would actuate into the operator home. Run inside "
                "`isolated_smoke_home(...)` so both resolve to the same isolated home. "
                "Refuse to actuate (Redmine #14187 Acceptance 1/5/6; review j#83870 F1)."
            )

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
        self._assert_isolation_bound()
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
        # The split-brain guard, BEFORE any registry / herdr write (review j#83870 F1):
        # the production path reads the ambient home, so refuse unless it is the
        # isolated one — zero operator-home write on a mismatch.
        self._assert_isolation_bound()
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
            try:
                results[index] = self.run_project(spec)
            except BaseException as exc:  # noqa: BLE001 - never drop a project silently
                # A worker that crashes with an UNCLASSIFIED exception must NOT vanish
                # from the results (review j#83870 F2): a dropped project let the
                # aggregate claim `converged` / `residue_clear` over the survivors
                # alone — a false green — and its partial actuation would escape
                # cleanup. Record a typed `failed` observation instead, so the count
                # of completed projects always equals the count requested and the
                # aggregate can fail closed on it.
                phase = (
                    _classify_failure_phase(exc)
                    if isinstance(
                        exc,
                        (HerdrSessionStartError, CoordinatorSharedCreateLockUnavailable),
                    )
                    else PHASE_WORKER_ERROR
                )
                results[index] = ProjectSmokeObservation(
                    project_key=spec.project_key,
                    workspace_id="",
                    outcome="failed",
                    coordinators_workspace_id="",
                    failure_phase=phase,
                )

        threads = [
            threading.Thread(target=_worker, args=(index, spec), name=f"smoke-{spec.project_key}")
            for index, spec in enumerate(specs)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # Every index is now populated (a crash produced a typed `failed` result, not a
        # None), so a `None` here would be a real internal invariant break.
        return [r for r in results if r is not None]

    # -- teardown by exact identity ------------------------------------------

    def cleanup(self, observations: Sequence[ProjectSmokeObservation] = ()) -> None:
        """Close exactly the panes this run launched (herdr auto-vanishes the workspace).

        Teardown by exact identity (Redmine #14187 Acceptance 5), driven by the
        **actuation-receipt tape** (review j#83905 F2): the recorder captured the
        ``wN:pM`` locator of every SUCCESSFUL ``agent start``, so this is the authoritative
        set of panes actually launched — a superset of the per-project observations. A
        worker that crashed AFTER its ``agent start`` succeeded lost its observation, but
        its pane is on the tape, so it is still closed here (never a scanned-for pane, so
        a user's own shell can never be mis-closed; the harness never issues a generic
        kill). A herdr workspace with its last pane closed auto-vanishes (live-measured
        #13380). A close that fails is left for :meth:`verify_residue` to catch, not
        hidden. ``observations`` is accepted for API stability but the tape is the source.
        """
        for locator in list(self.recorder.launched_locators):
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
        self, observations: Sequence[ProjectSmokeObservation] = ()
    ) -> "tuple[int, int]":
        """Prove zero residue after cleanup: ``(residue_workspaces, residue_agents)``.

        Reads the live labels and the live inventory back through the same injected
        runner and counts how many of THIS run's created ``coordinators`` workspaces /
        launched ``mzb1_...`` names are still present. Both must be zero
        (Redmine #14187 Acceptance 5).

        The created-set is the **actuation-receipt tape** (review j#83905 F2), NOT the
        per-project observations: the recorder captured every SUCCESSFUL ``workspace
        create`` (id + label) and ``agent start`` (name), so a crashed project's leaked
        workspace / agents — absent from its blanked observation — are still counted here.

        Fails closed on an UNREADABLE inventory (review j#83870 F3): an unreadable /
        unrecognised ``workspace list`` (``_list_workspace_labels`` -> ``None``) must
        NOT be treated as an empty inventory — that would turn an unknown into a
        residue-0 success and let a labelled husk hide. It raises
        :class:`SharedSpaceSmokeError`, exactly the fail-closed posture
        :meth:`preflight_clean_slate` already takes on the same ``None`` (the contract
        was asymmetric before). ``_list_rows`` already raises on an unreadable ``agent
        list``.
        """
        created_workspaces = set(self.recorder.created_coordinators_workspaces)
        created_names = {name for name in self.recorder.agent_start_names if name}
        labels = _list_workspace_labels(self._binary, self.recorder, self.timeout)
        if labels is None:
            raise SharedSpaceSmokeError(
                "residue verification could not read the herdr workspace labels "
                "(unreadable / unrecognised `workspace list`); refuse to claim residue-0 "
                "on an unreadable inventory — a labelled husk could be hiding "
                "(Redmine #14187 Acceptance 5; review j#83870 F3)"
            )
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
        identity, and prove zero residue.

        Two cleanup-authority gates run FIRST, before any project launches, so the
        public use-case is itself the safety authority (not the caller's discipline —
        review j#83870 F1): :meth:`preflight_clean_slate` calls
        :meth:`_assert_isolation_bound` (the ambient home must be the isolated one) and
        then refuses a pre-existing ``coordinators`` space. Either failure aborts the
        whole smoke with zero actuation.

        The aggregate never claims a false green: :attr:`converged` requires every
        requested project to have completed (F2), and :attr:`residue_clear` requires
        the residue to have been READ BACK, not merely assumed on an unreadable
        inventory (F3 — :meth:`verify_residue` fails closed there, caught here so the
        observation records ``residue_verified = False`` instead of crashing).
        """
        self.preflight_clean_slate()
        observations = self.run_concurrent(specs)
        duplicate_agents = _count_duplicate_agents(observations)
        coordinators_create_count = sum(
            1 for o in observations if o.created_coordinators_space
        )
        self.cleanup(observations)
        residue_verified = True
        residue_workspaces, residue_agents = -1, -1
        try:
            residue_workspaces, residue_agents = self.verify_residue(observations)
        except (SharedSpaceSmokeError, HerdrSessionStartError):
            # Unreadable inventory (F3): keep the failure as an observation
            # (`residue_verified=False` → `residue_clear` stays False) rather than
            # crashing the whole summary — the honest "could not prove residue-0".
            residue_verified = False
        lock_engaged, lock_released_clean = self.observe_lock()
        return SharedSpaceSmokeObservation(
            projects=tuple(observations),
            requested_projects=len(specs),
            coordinators_create_count=coordinators_create_count,
            duplicate_agents=duplicate_agents,
            lock_engaged=lock_engaged,
            lock_released_clean=lock_released_clean,
            residue_workspaces=residue_workspaces,
            residue_agents=residue_agents,
            residue_verified=residue_verified,
            cleanup_attempted=True,
        )


def smoke_shared_space_preflight(
    isolated_home: Path,
    *,
    runner,
    env: Mapping[str, str],
    projects: int = 2,
) -> dict:
    """Prove a shared-space smoke can run here safely; return a redaction-safe report.

    The read-only surface the CLI exposes and the #14185 live driver calls first: it
    establishes the isolated home (:func:`isolated_smoke_home`, which fails closed
    unless the home is provably distinct from the operator's — the operator home is the
    ambient ``mozyo_bridge_home()``, never a caller argument, review j#83935 F1), then
    runs the herdr clean-slate cleanup-authority gate
    (:meth:`SharedSpaceSmokeHarness.preflight_clean_slate`, a read-only ``workspace
    list``). It **actuates no agent** — the live coordinator launch is the #14185
    driver's isolated-instance job. Returns counts / bools / closed tokens only (no home
    path, no env value), so the report can be summarised into a durable Redmine journal.
    """
    count = max(2, int(projects))
    with isolated_smoke_home(isolated_home) as capability:
        harness = SharedSpaceSmokeHarness(capability=capability, runner=runner, env=env)
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
    "PHASE_WORKER_ERROR",
    "SMOKE_FAILURE_PHASES",
    "IsolationCapability",
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
