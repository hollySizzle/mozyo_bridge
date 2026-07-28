"""Real cross-process shared-space smoke over an owned Herdr instance (#14187)."""

from __future__ import annotations

import math
import multiprocessing
import queue
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.disposable_herdr_instance import (  # noqa: E501
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
    except (TypeError, ValueError) as exc:
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
    """What one bounded fork round produced, including its own cleanup verdict."""

    receipts: tuple
    orphaned_workers: int


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
    orphaned = 0
    try:
        receipts = _collect_forked_receipts(
            processes=processes,
            started=started,
            specs=specs,
            output=output,
            timeout=bounded,
        )
    finally:
        # Runs even if ``start()`` itself failed halfway through the fleet: only the
        # handles that actually started are in ``started``.
        orphaned = _reap_exact_workers(started)
    return _ForkedRun(receipts=tuple(receipts), orphaned_workers=orphaned)


def _collect_forked_receipts(
    *,
    processes: Sequence,
    started: list,
    specs: Sequence[_ProjectSpec],
    output,
    timeout: float,
) -> list[_ProcessReceipt]:
    """Start the fleet, join it under the bound, and collect one receipt per project.

    ``started`` is appended to as each process starts so the caller's ``finally`` can
    reap exactly what exists, including when this function raises partway through.
    """
    for process in processes:
        process.start()
        started.append(process)
    for process in processes:
        process.join(timeout=max(1.0, timeout))
        if process.is_alive():
            # Exact child handle only; never a name scan or generic kill.
            process.terminate()
            process.join(timeout=5.0)
            if process.is_alive():
                process.kill()
                process.join(timeout=5.0)
    receipts: dict[int, _ProcessReceipt] = {}
    for _ in processes:
        try:
            receipt = output.get(timeout=1.0)
        except queue.Empty:
            break
        if isinstance(receipt, _ProcessReceipt):
            receipts[receipt.index] = receipt
    output.close()
    output.join_thread()
    for index, spec in enumerate(specs):
        if index not in receipts:
            receipts[index] = _ProcessReceipt(
                index=index,
                observation=ProjectSmokeObservation(
                    project_key=spec.project_key,
                    workspace_id="",
                    outcome="failed",
                    coordinators_workspace_id="",
                    failure_phase=PHASE_WORKER_ERROR,
                ),
            )
    return [receipts[index] for index in range(len(specs))]


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
    # Seeded to the fail-closed value: only a completed fork round may lower it.
    orphaned_workers = -1
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
                        env=instance.child_env(),
                        providers=providers,
                    )
                    for _ in specs
                ]
                cleanup_harness = SharedSpaceSmokeHarness(
                    capability=capability,
                    runner=instance.runner,
                    env=instance.child_env(),
                    providers=providers,
                )
                cleanup_harness.preflight_clean_slate()
                forked = _run_forked_projects(
                    harnesses=harnesses,
                    specs=specs,
                    timeout=bounded_timeout,
                    gate_runner=instance.runner,
                )
                receipts = forked.receipts
                orphaned_workers = forked.orphaned_workers
                observations = [receipt.observation for receipt in receipts]
                worker_gate_receipts = [receipt.endpoint_gate for receipt in receipts]
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
        # ``with instance`` already shuts down.  This idempotent call covers a
        # pre-enter/startup exception without broad process discovery.
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
        and orphaned_workers == 0
    )
    return evidence


__all__ = ("run_disposable_shared_space_smoke",)
