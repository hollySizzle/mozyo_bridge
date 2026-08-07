"""Real cross-process shared-space smoke over an owned Herdr instance (#14187)."""

from __future__ import annotations

import math
import multiprocessing
import queue
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.disposable_herdr_instance import (  # noqa: E501
    WITHHOLD_WORKERS_NOT_CONTAINED,
    WITHHOLD_WORKERS_UNVERIFIED,
    DisposableHerdrInstance,
    EndpointGateCounters,
    EndpointGateEvidence,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.shared_space_smoke_harness import (  # noqa: E501
    SharedSpaceSmokeHarness,
    _ProjectSpec,
    _count_duplicate_agents,
    isolated_smoke_home,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.shared_space_smoke_observation import (  # noqa: E501
    PHASE_WORKER_ERROR,
    ProjectSmokeObservation,
    SharedSpaceSmokeError,
    SharedSpaceSmokeObservation,
)


#: Upper bound on the per-worker wall clock a caller may ask for.  The smoke is a
#: bounded diagnostic, so "wait longer than an hour" is a misuse, not a preference.
MAX_PROCESS_TIMEOUT_SECONDS = 3600.0


def bounded_process_timeout(timeout: object) -> float:
    """Validate the per-worker bound, or refuse **before** any process exists.

    A caller must not be able to express a timeout the driver cannot honour (review
    j#91604 F2).  ``float('inf')`` used to reach :meth:`multiprocessing.Process.join`,
    where it raises ``OverflowError`` *after* every worker had already been started —
    unwinding the driver with owned processes still running and no typed error for the
    CLI to render evidence from.  ``nan`` and non-positive values are the same class of
    misuse.  Refusing here, before ``start()``, is the only place where the answer costs
    nothing.
    """
    try:
        value = float(timeout)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        # ``OverflowError`` is the huge-int case (``float(10**10000)``).  It is an
        # ``ArithmeticError``, so leaving it out let a direct caller reach a raw
        # traceback instead of a typed, renderable refusal (review j#91638 F2).
        raise SharedSpaceSmokeError(
            "smoke worker timeout must be a number of seconds"
        ) from exc
    if not math.isfinite(value) or value <= 0.0 or value > MAX_PROCESS_TIMEOUT_SECONDS:
        raise SharedSpaceSmokeError(
            "smoke worker timeout must be a finite number of seconds in "
            f"(0, {MAX_PROCESS_TIMEOUT_SECONDS:g}]"
        )
    return value


def _reap_exact_workers(started: Sequence) -> int:
    """Terminate/kill exactly the workers WE started; return how many survived.

    Only the handles this driver created are ever touched — never a name scan, never a
    generic kill.  Idempotent, so it is safe as a ``finally`` on both the normal path
    and an exception path.  The return value is evidence, not decoration: a surviving
    worker means the run may still be actuating Herdr while the parent tears the server
    and the owned root down, so the caller must fail closed on it.
    """
    for process in started:
        try:
            if not process.is_alive():
                continue
            process.terminate()
            process.join(timeout=5.0)
            if process.is_alive():
                process.kill()
                process.join(timeout=5.0)
        except (OSError, ValueError, AssertionError):
            # A handle we cannot signal is still counted below rather than assumed dead.
            continue
    survivors = 0
    for process in started:
        try:
            if process.is_alive():
                survivors += 1
        except (OSError, ValueError, AssertionError):
            survivors += 1
    return survivors


@dataclass(frozen=True)
class _ForkedRun:
    """What one bounded fork round produced, including its own cleanup verdict.

    Returned on **every** path, including the one where the round raised (review
    j#91638 F1): the reaping in the ``finally`` already established how many owned
    workers survived, and letting the exception discard that answer is exactly how the
    caller ended up tearing the endpoint down with no idea whether anything was still
    running — and with no evidence to render either.
    """

    receipts: tuple
    orphaned_workers: int
    round_failed: bool = False
    #: Exception class name only.  A type name is a closed-enough token for evidence;
    #: the message could carry a path and never enters the report.
    failure_kind: str = ""
    #: Closed tokens for receipts that were refused rather than accepted.
    receipt_anomalies: tuple = ()
    #: Exact pane locators recovered from refused receipts, kept for cleanup only.
    salvaged_locators: tuple = ()

    @property
    def workers_contained(self) -> bool:
        """Whether every owned worker is provably gone.

        The teardown fence reads this *before* releasing anything, so an indeterminate
        count is not containment: ``-1`` fails here exactly like a live survivor.
        """
        return self.orphaned_workers == 0


@dataclass(frozen=True)
class _ProcessReceipt:
    """Redaction-safe mutation receipts returned by one forked worker.

    ``endpoint_gate`` is the worker's own copy of the endpoint-gate counters.  It is
    optional at the type level precisely because its absence must stay visible: the
    parent counts a missing snapshot rather than folding in an implicit zero
    (review j#85841 F1).
    """

    index: int
    observation: ProjectSmokeObservation
    launched_locators: tuple[str, ...] = ()
    created_workspaces: tuple[tuple[str, str], ...] = ()
    agent_start_names: tuple[str, ...] = ()
    coordinators_create_count: int = 0
    endpoint_gate: "EndpointGateCounters | None" = None


def _forked_project_worker(
    index: int,
    barrier,
    output,
    harness: SharedSpaceSmokeHarness,
    spec: _ProjectSpec,
    gate_runner,
) -> None:
    """One bounded child process; always attempts one typed receipt.

    ``gate_runner`` is this child's forked copy of the endpoint-bound runner.  Its
    counters are process-local, so they are snapshotted into the receipt — that
    snapshot is the only way the parent can say anything about what THIS process
    dispatched.
    """
    try:
        barrier.wait(timeout=15.0)
        observation = harness.run_project(spec)
    except BaseException:  # noqa: BLE001 - child failure must be visible, never dropped
        observation = ProjectSmokeObservation(
            project_key=spec.project_key,
            workspace_id="",
            outcome="failed",
            coordinators_workspace_id="",
            failure_phase=PHASE_WORKER_ERROR,
        )
    try:
        output.put(
            _ProcessReceipt(
                index=index,
                observation=observation,
                launched_locators=tuple(harness.recorder.launched_locators),
                created_workspaces=tuple(harness.recorder.created_workspaces.items()),
                agent_start_names=tuple(harness.recorder.agent_start_names),
                coordinators_create_count=harness.recorder.coordinators_create_count,
                # Snapshotted even when the run failed: a worker that escaped the gate
                # or reached an operator endpoint before crashing is exactly the
                # evidence the aggregate must not lose.
                endpoint_gate=EndpointGateCounters.snapshot(gate_runner),
            )
        )
    except BaseException:
        # The parent treats a missing receipt as a typed worker failure AND as a
        # missing endpoint-gate snapshot, and still owns the exact server
        # process/state tree for bounded cleanup.
        return


def _run_forked_projects(
    *,
    harnesses: Sequence[SharedSpaceSmokeHarness],
    specs: Sequence[_ProjectSpec],
    timeout: float,
    gate_runner,
) -> _ForkedRun:
    """Release real OS processes together and collect one receipt per project.

    Every worker this function starts is reaped through its exact handle in a
    ``finally``, on the normal path and on any exception alike, and the count that
    survived even a ``kill`` is returned rather than assumed to be zero (review j#91604
    F2).  Without that, an exception anywhere after ``start()`` unwound the driver with
    owned workers still actuating Herdr while the caller went on to shut the server and
    its state tree down.
    """
    if not specs:
        return _ForkedRun(receipts=(), orphaned_workers=0)
    # Refused before a single process exists: an unusable bound must never be
    # discovered by the join that already has children waiting on it.
    bounded = bounded_process_timeout(timeout)
    try:
        context = multiprocessing.get_context("fork")
    except ValueError as exc:
        raise SharedSpaceSmokeError(
            "real shared-space smoke requires the POSIX fork multiprocessing context"
        ) from exc
    barrier = context.Barrier(len(specs))
    output = context.Queue()
    processes = [
        context.Process(
            target=_forked_project_worker,
            args=(index, barrier, output, harnesses[index], spec, gate_runner),
            name=f"mozyo-smoke-{spec.project_key}",
        )
        for index, spec in enumerate(specs)
    ]
    started: list = []
    # Owned by THIS function so a later failure cannot take the receipts that already
    # arrived with it.  Losing them meant losing the exact pane locators cleanup needs
    # and the gate counters a worker had already proven (review j#91687 F3).
    collected: dict = {}
    #: Closed tokens naming what was wrong with a rejected receipt.
    anomalies: list = []
    #: Exact pane locators from receipts we could not accept.  Cleanup still needs them
    #: even though they must not count towards the success evidence.
    locator_tape: list = []
    orphaned = 0
    round_failed = False
    failure_kind = ""
    try:
        try:
                _collect_forked_receipts(
                processes=processes,
                started=started,
                specs=specs,
                output=output,
                collected=collected,
                anomalies=anomalies,
                locator_tape=locator_tape,
                timeout=bounded,
            )
        except Exception as exc:  # noqa: BLE001 - the round's verdict must survive it
            # Partial start, a queue-collection failure, ``output.close()`` — whatever
            # it was, the caller still needs the containment answer and a renderable
            # report.  ``BaseException`` (KeyboardInterrupt/SystemExit) is deliberately
            # NOT caught; the ``finally`` still reaps before it propagates.
            round_failed = True
            failure_kind = type(exc).__name__
    finally:
        # Runs even if ``start()`` itself failed halfway through the fleet: only the
        # handles that actually started are in ``started``.
        orphaned = _reap_exact_workers(started)
    if anomalies and not round_failed:
        # A malformed receipt is a failed round, not a quietly shorter one.
        round_failed = True
        failure_kind = anomalies[0]
    return _ForkedRun(
        receipts=tuple(_fill_unreported(specs, collected)),
        orphaned_workers=orphaned,
        round_failed=round_failed,
        failure_kind=failure_kind,
        receipt_anomalies=tuple(anomalies),
        salvaged_locators=tuple(locator_tape),
    )


#: Closed vocabulary for why a receipt was refused.
RECEIPT_ANOMALY_DUPLICATE_INDEX = "receipt_duplicate_index"
RECEIPT_ANOMALY_INDEX_OUT_OF_RANGE = "receipt_index_out_of_range"
RECEIPT_ANOMALY_INDEX_NOT_INT = "receipt_index_not_int"
RECEIPT_ANOMALY_PROJECT_MISMATCH = "receipt_project_mismatch"


def _receipt_anomaly(receipt, specs: Sequence[_ProjectSpec], collected: dict) -> str:
    """Why this receipt cannot be trusted as project ``receipt.index``, or ``""``.

    The index is self-reported by the worker, so it is checked rather than believed
    (review j#91741 F3): it must be a strict integer, must name a project this round
    actually launched, must not have been claimed already, and must agree with that
    project's key.  Strictness about the *type* matters as much as the range — see the
    ``bool`` note below.
    """
    index = receipt.index
    # ``bool`` is a subclass of ``int`` and ``True == 1`` hashes as ``1``, so a
    # ``bool`` index used to pass this check and then *impersonate* project 1 in the
    # ``collected`` map — a malformed receipt read as a complete, converged round
    # (review j#91777).  A wrong type is a different fault from a wrong number, so it
    # gets its own token rather than being folded into the range answer.
    if isinstance(index, bool) or not isinstance(index, int):
        return RECEIPT_ANOMALY_INDEX_NOT_INT
    if not 0 <= index < len(specs):
        return RECEIPT_ANOMALY_INDEX_OUT_OF_RANGE
    if index in collected:
        return RECEIPT_ANOMALY_DUPLICATE_INDEX
    if receipt.observation.project_key != specs[index].project_key:
        return RECEIPT_ANOMALY_PROJECT_MISMATCH
    return ""


def _fill_unreported(specs: Sequence[_ProjectSpec], collected: "dict") -> list:
    """Everything that arrived, plus a typed ``failed`` placeholder for what did not.

    Only the indexes that never reported are replaced.  Wiping the whole set on a late
    failure threw away exact pane locators cleanup still needs, and gate counters a
    worker had already proven — a placeholder is fail-closed, but it is not free
    (review j#91687 F3).  A placeholder carries no endpoint-gate snapshot, so the
    aggregate counts it as *missing* rather than as a proven zero.
    """
    return [
        collected.get(
            index,
            _ProcessReceipt(
                index=index,
                observation=ProjectSmokeObservation(
                    project_key=spec.project_key,
                    workspace_id="",
                    outcome="failed",
                    coordinators_workspace_id="",
                    failure_phase=PHASE_WORKER_ERROR,
                ),
            ),
        )
        for index, spec in enumerate(specs)
    ]


def _collect_forked_receipts(
    *,
    processes: Sequence,
    started: list,
    specs: Sequence[_ProjectSpec],
    output,
    collected: dict,
    anomalies: list,
    locator_tape: list,
    timeout: float,
) -> None:
    """Start the fleet, join it under the bound, and publish each receipt as it lands.

    ``started`` is appended to as each process starts so the caller's ``finally`` can
    reap exactly what exists, including when this function raises partway through.
    ``collected`` is the caller's map and is written **as receipts arrive**, for the
    same reason: a failure in the tail of this function must not take the receipts that
    already made it (review j#91687 F3).
    """
    for process in processes:
        # Registered BEFORE it is started.  Between ``start()`` and an append that
        # followed it, a ``BaseException`` left a live child that no ``finally`` knew
        # about (review j#91741 F1).  A handle that never started is harmless here:
        # ``is_alive()`` is False for it, so the reap skips it without signalling.
        started.append(process)
        process.start()
    for process in processes:
        process.join(timeout=max(1.0, timeout))
        if process.is_alive():
            # Exact child handle only; never a name scan or generic kill.
            process.terminate()
            process.join(timeout=5.0)
            if process.is_alive():
                process.kill()
                process.join(timeout=5.0)
    for _ in processes:
        try:
            receipt = output.get(timeout=1.0)
        except queue.Empty:
            break
        if not isinstance(receipt, _ProcessReceipt):
            continue
        anomaly = _receipt_anomaly(receipt, specs, collected)
        if anomaly:
            # Never silently overwritten: a duplicate index used to drop the earlier
            # receipt's exact pane locator on the floor (review j#91741 F3).  The tape
            # keeps it for cleanup; the anomaly keeps it out of the success evidence.
            anomalies.append(anomaly)
            locator_tape.extend(receipt.launched_locators)
            continue
        collected[receipt.index] = receipt
    output.close()
    output.join_thread()



def run_disposable_shared_space_smoke(
    isolated_home: Path,
    *,
    env: Mapping[str, str],
    projects: int = 2,
    providers: Sequence[str] = ("claude", "codex"),
    process_timeout: float = 45.0,
    runner=None,
    popen_factory=None,
) -> dict[str, object]:
    """Own server→run two OS processes→exact cleanup→shutdown; return safe evidence.

    The function is the supported high-level actuation surface.  The operator's
    normal Herdr endpoint is never probed: every server/client call passes the
    capability gate *before* dispatch, while the lifecycle owns the only process it
    may terminate and the only state tree it may remove.  If the gate ever refuses,
    the run fails closed having made zero external requests for that call — it does
    not "notice afterwards" (blocker j#85754, design disposition j#85756).

    This is the live actuation path, so it is never the place to probe the guard: see
    the module docstring of ``disposable_herdr_instance`` for the sanctioned
    mutation-probe protocol (fake inner runner + scrubbed ambient endpoint).
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
        resolve_herdr_binary,
    )

    count = max(2, int(projects))
    # Before the binary, the server, the isolated home — before anything exists that
    # would have to be cleaned up if this turned out to be unusable (review j#91604 F2).
    bounded_timeout = bounded_process_timeout(process_timeout)
    try:
        resolution = resolve_herdr_binary(env)
    except Exception as exc:
        reason = getattr(exc, "reason", "binary_unconfigured")
        raise SharedSpaceSmokeError(
            f"could not resolve trusted Herdr binary ({reason})"
        ) from exc
    kwargs = {}
    if runner is not None:
        kwargs["runner"] = runner
    if popen_factory is not None:
        kwargs["popen_factory"] = popen_factory
    instance = DisposableHerdrInstance(
        binary=resolution.path,
        root=Path(isolated_home).expanduser().resolve() / "herdr-instance",
        base_env=env,
        **kwargs,
    )
    summary = SharedSpaceSmokeObservation(requested_projects=count)
    # One slot per project, seeded with the fail-closed value.  A worker that never
    # reports leaves its ``None`` in place, and the aggregate counts it as missing
    # rather than as a process that made zero requests (review j#85841 F1).
    worker_gate_receipts: list = [None] * count
    # Seeded to the fail-closed values: only a completed fork round may lower them.
    orphaned_workers = -1
    round_failure_kind = ""
    receipt_anomalies: tuple = ()
    # Only a completed round may claim containment.  The lifecycle policy below is the
    # authority; this mirrors it for the evidence dict.
    workers_contained = False
    try:
        with instance:
            with isolated_smoke_home(Path(isolated_home)) as capability:
                specs = []
                for index in range(count):
                    repo = capability.isolated_home / "projects" / f"p{index}"
                    repo.mkdir(parents=True, exist_ok=True)
                    specs.append(_ProjectSpec(f"p{index}", repo))
                harnesses = [
                    SharedSpaceSmokeHarness(
                        capability=capability,
                        runner=instance.runner,
                        launcher_runner=subprocess.run,
                        env=instance.child_env(),
                        providers=providers,
                    )
                    for _ in specs
                ]
                cleanup_harness = SharedSpaceSmokeHarness(
                    capability=capability,
                    runner=instance.runner,
                    launcher_runner=subprocess.run,
                    env=instance.child_env(),
                    providers=providers,
                )
                cleanup_harness.preflight_clean_slate()
                # From here a worker can exist, and an interrupt can unwind past every
                # assignment below.  Withhold the path release on the LIFECYCLE first,
                # so both the context manager's ``__exit__`` and the outer ``finally``
                # inherit it no matter how we leave (review j#91687 F1/F2).
                instance.withhold_root_release(WITHHOLD_WORKERS_UNVERIFIED)
                forked = _run_forked_projects(
                    harnesses=harnesses,
                    specs=specs,
                    timeout=bounded_timeout,
                    gate_runner=instance.runner,
                )
                receipts = forked.receipts
                orphaned_workers = forked.orphaned_workers
                # The containment fence, established BEFORE anything is torn down.
                # A survivor (or an indeterminate count) means the socket path this
                # run owns must not be handed back, because a worker still holding
                # client-call capability could address whatever binds it next.
                workers_contained = forked.workers_contained
                round_failure_kind = forked.failure_kind
                receipt_anomalies = forked.receipt_anomalies
                if workers_contained:
                    # The only positive verdict: every started worker is provably gone.
                    instance.permit_root_release()
                else:
                    instance.withhold_root_release(WITHHOLD_WORKERS_NOT_CONTAINED)
                observations = [receipt.observation for receipt in receipts]
                worker_gate_receipts = [receipt.endpoint_gate for receipt in receipts]
                if forked.salvaged_locators:
                    # Refused receipts still name real panes; cleanup must close them
                    # even though they do not count as evidence (review j#91741 F3).
                    cleanup_harness.recorder.merge_receipts(
                        launched_locators=forked.salvaged_locators,
                        created_workspaces={},
                        agent_start_names=(),
                        coordinators_create_count=0,
                    )
                for receipt in receipts:
                    cleanup_harness.recorder.merge_receipts(
                        launched_locators=receipt.launched_locators,
                        created_workspaces=dict(receipt.created_workspaces),
                        agent_start_names=receipt.agent_start_names,
                        coordinators_create_count=receipt.coordinators_create_count,
                    )
                duplicate_agents = _count_duplicate_agents(observations)
                create_count = sum(r.coordinators_create_count for r in receipts)
                cleanup_harness.cleanup(observations)
                residue_verified = True
                residue_workspaces, residue_agents = -1, -1
                try:
                    residue_workspaces, residue_agents = cleanup_harness.verify_residue(
                        observations
                    )
                except Exception:  # noqa: BLE001 - evidence stays explicitly unverified
                    residue_verified = False
                lock_engaged, lock_released = cleanup_harness.observe_lock()
                summary = SharedSpaceSmokeObservation(
                    projects=tuple(observations),
                    requested_projects=count,
                    coordinators_create_count=create_count,
                    duplicate_agents=duplicate_agents,
                    lock_engaged=lock_engaged,
                    lock_released_clean=lock_released,
                    residue_workspaces=residue_workspaces,
                    residue_agents=residue_agents,
                    residue_verified=residue_verified,
                    cleanup_attempted=True,
                )
    finally:
        # ``with instance`` already shut down, obeying the same lifecycle policy this
        # call obeys.  Idempotent, and it also covers a pre-enter/startup exception
        # without broad process discovery.
        instance.shutdown()
    # Folded only now, so the parent snapshot also covers cleanup, residue verification
    # and the shutdown ``server stop`` that ran in the ``finally`` above.
    gate = EndpointGateEvidence.aggregate(
        parent=EndpointGateCounters.snapshot(instance.runner),
        worker_receipts=worker_gate_receipts,
    )
    evidence = summary.as_evidence()
    evidence.update(instance.as_evidence(gate=gate))
    evidence["actuated"] = True
    evidence["cross_process"] = True
    #: ``-1`` = the fork round never completed, so worker residue was never established.
    #: Reported rather than folded into a bool, because "we could not tell" and "there
    #: were none" are different facts (review j#91604 F2).
    evidence["worker_processes_orphaned"] = orphaned_workers
    evidence["workers_contained"] = workers_contained
    #: Closed token: the exception class that ended the fork round, or "".
    evidence["fork_round_failure"] = round_failure_kind
    evidence["receipt_anomalies"] = list(receipt_anomalies)
    evidence["success"] = bool(
        summary.converged
        and summary.residue_clear
        and instance.stopped
        and instance.endpoint_residue == 0
        # Load-bearing Acceptance-2 negative proof, in two independent directions
        # (blocker j#85754 / disposition j#85756):
        #   - a dropped binding trips the pre-actuation gate -> escape_refusals > 0;
        #   - a dropped gate lets an operator-socket request through ->
        #     operator_endpoint_requests > 0.
        # Neither can be satisfied by a constant, and the first can no longer be
        # discovered *after* the request has already reached the operator's server.
        #
        # The proof is taken over EVERY process that held the capability, not just this
        # one: the real workspace/agent traffic happens in the forked workers, whose
        # counters never cross the fork.  ``proven_zero_external`` additionally requires
        # every worker receipt to be present and self-consistent, so a lost snapshot
        # fails the run instead of reading as a silent zero (review j#85841 F1).
        and gate.proven_zero_external
        and gate.all_calls_bound
        # A worker that outlived even the kill may still be actuating Herdr while the
        # server and the owned root are being torn down, so the run has not converged
        # and cleaned up no matter how good the rest of the evidence looks.
        and workers_contained
        and orphaned_workers == 0
        and not round_failure_kind
        # Observed after teardown, not inferred: the owned tree is actually gone.
        and not evidence.get("owned_root_present", True)
    )
    return evidence


__all__ = ("run_disposable_shared_space_smoke",)
