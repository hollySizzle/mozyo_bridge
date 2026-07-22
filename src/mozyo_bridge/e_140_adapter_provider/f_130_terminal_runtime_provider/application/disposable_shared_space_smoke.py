"""Real cross-process shared-space smoke over an owned Herdr instance (#14187)."""

from __future__ import annotations

import multiprocessing
import queue
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.disposable_herdr_instance import (  # noqa: E501
    DisposableHerdrInstance,
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


@dataclass(frozen=True)
class _ProcessReceipt:
    """Redaction-safe mutation receipts returned by one forked worker."""

    index: int
    observation: ProjectSmokeObservation
    launched_locators: tuple[str, ...] = ()
    created_workspaces: tuple[tuple[str, str], ...] = ()
    agent_start_names: tuple[str, ...] = ()
    coordinators_create_count: int = 0


def _forked_project_worker(
    index: int,
    barrier,
    output,
    harness: SharedSpaceSmokeHarness,
    spec: _ProjectSpec,
) -> None:
    """One bounded child process; always attempts one typed receipt."""
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
            )
        )
    except BaseException:
        # The parent treats a missing receipt as a typed worker failure and still owns
        # the exact server process/state tree for bounded cleanup.
        return


def _run_forked_projects(
    *,
    harnesses: Sequence[SharedSpaceSmokeHarness],
    specs: Sequence[_ProjectSpec],
    timeout: float,
) -> list[_ProcessReceipt]:
    """Release real OS processes together and collect one receipt per project."""
    if not specs:
        return []
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
            args=(index, barrier, output, harnesses[index], spec),
            name=f"mozyo-smoke-{spec.project_key}",
        )
        for index, spec in enumerate(specs)
    ]
    for process in processes:
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
                receipts = _run_forked_projects(
                    harnesses=harnesses,
                    specs=specs,
                    timeout=process_timeout,
                )
                observations = [receipt.observation for receipt in receipts]
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
    evidence = summary.as_evidence()
    evidence.update(instance.as_evidence())
    evidence["actuated"] = True
    evidence["cross_process"] = True
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
        and instance.runner.all_calls_bound
        and instance.runner.escape_refusals == 0
        and instance.runner.operator_endpoint_requests == 0
    )
    return evidence


__all__ = ("run_disposable_shared_space_smoke",)
