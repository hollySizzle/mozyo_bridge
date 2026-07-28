"""`herdr smoke-shared-space` — the isolated shared-space smoke preflight (Redmine #14187).

The public, read-only front door for the high-level shared-space coordinator-placement
smoke harness (:mod:`shared_space_smoke_harness`). It answers one question through the
semantic facade — *"can the ``shared_space`` cross-process smoke run here without
touching the operator's real home or shared coordinators space?"* — and prints a
redacted, typed report.

Without ``--execute`` it preserves the original read-only preflight.  With
``--execute`` it owns a disposable endpoint-bound Herdr server, releases two real OS
processes into the production shared-space start path, closes the exact launched pane
locators, proves residue zero, and shuts the server down.  No raw server/socket command
is exposed to the operator and the report contains counts/bools/closed tokens only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    HerdrSessionStartError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.shared_space_smoke_harness import (  # noqa: E501
    SharedSpaceSmokeError,
    SmokeIsolationError,
    smoke_shared_space_preflight,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.disposable_shared_space_smoke import (  # noqa: E501
    MAX_PROCESS_TIMEOUT_SECONDS,
    bounded_process_timeout,
    run_disposable_shared_space_smoke,
)
from mozyo_bridge.shared.errors import die


def _process_timeout(raw: str) -> float:
    """Parse ``--process-timeout``, refusing a bound the driver cannot honour.

    ``argparse``'s bare ``float`` happily accepted ``inf`` / ``nan`` / ``0`` / negatives,
    which only failed much later inside ``Process.join`` — after every worker had been
    started (review j#91604 F2).  The domain check belongs at the entry point, where
    refusing costs nothing; :func:`bounded_process_timeout` is the same authority the
    driver applies to direct callers.
    """
    try:
        return bounded_process_timeout(raw)
    except Exception as exc:  # noqa: BLE001 - re-typed for argparse's own error path
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _failure_phases(report: dict) -> str:
    """The closed failure-phase tokens the run produced, as a replayable summary.

    ``none`` when nothing failed.  Phases are the whole point of the evidence on the
    failing branch (issue Acceptance 4), so text mode names them rather than telling
    the operator to go and read a JSON document that was never printed.
    """
    phases = [
        str(project.get("failure_phase", ""))
        for project in report.get("projects", [])
        if str(project.get("failure_phase", "")) not in ("", "none")
    ]
    return ",".join(sorted(set(phases))) if phases else "none"


def _render_text(report: dict) -> str:
    if report.get("actuated"):
        return (
            "herdr smoke-shared-space: "
            f"success={report['success']} cross_process={report['cross_process']} "
            f"create_count={report['coordinators_create_count']} "
            f"duplicates={report['duplicate_agents']} "
            f"completed_projects={report['completed_projects']}"
            f"/{report['requested_projects']} "
            f"failure_phases={_failure_phases(report)} "
            f"residue_clear={report['residue_clear']} "
            f"orphaned_workers={report['worker_processes_orphaned']} "
            f"server_stopped={report['server_stopped']} "
            f"operator_endpoint_requests={report['operator_endpoint_requests']} "
            f"endpoint_escape_refusals={report['endpoint_escape_refusals']} "
            f"endpoint_gate_processes={report['endpoint_gate_processes']} "
            "endpoint_gate_receipts_complete="
            f"{report['endpoint_gate_receipts_complete']} "
            "endpoint_gate_proven_zero_external="
            f"{report['endpoint_gate_proven_zero_external']}"
        )
    return (
        "herdr smoke-shared-space preflight: "
        f"isolated_home_ok={report['isolated_home_ok']} "
        f"clean_slate_ok={report['clean_slate_ok']} mode={report['mode']} "
        f"projects={report['projects']} "
        f"coordinators_create_expected={report['coordinators_create_expected']} "
        f"actuated={report['actuated']}\n"
        "  ready: the isolated home is distinct and the herdr instance has no "
        "pre-existing coordinators space; the live shared-space smoke (Redmine "
        "#14185) may run here."
    )


def cmd_herdr_smoke_shared_space(args: argparse.Namespace) -> int:
    """CLI entry: prove the shared-space smoke can run here; print a redacted report."""
    isolated_home = (getattr(args, "isolated_home", "") or "").strip()
    if not isolated_home:
        die(
            "herdr smoke-shared-space failed: --isolated-home PATH is required (a fresh "
            "directory distinct from the operator home; the smoke never touches the real "
            "operator home)."
        )
        raise AssertionError("unreachable")
    projects = getattr(args, "projects", None) or 2
    try:
        if getattr(args, "execute", False):
            report = run_disposable_shared_space_smoke(
                Path(isolated_home),
                env=os.environ,
                projects=projects,
                # Passed through unconverted: the driver's own
                # ``bounded_process_timeout`` is the single domain authority.  A bare
                # ``float()`` here was a second, weaker check in front of it, and it
                # raised untyped for a huge int (review j#91638 F2).
                process_timeout=getattr(args, "process_timeout", 45.0),
            )
        else:
            report = smoke_shared_space_preflight(
                Path(isolated_home),
                runner=subprocess.run,
                env=os.environ,
                projects=projects,
            )
    except (SmokeIsolationError, SharedSpaceSmokeError, HerdrSessionStartError) as exc:
        # Literal, typed, fail-closed: isolation not provable, a coordinators space
        # already exists / labels unreadable, or the herdr binary is unresolvable.
        die(f"herdr smoke-shared-space failed: {exc}")
        raise AssertionError("unreachable")
    # Evidence FIRST, verdict second (review j#85841 F3).  A non-converged run is
    # exactly when the failure phase, the residue counts and the endpoint-gate
    # negative proof matter, so the report is rendered on both branches and the
    # failure is signalled by the exit code — never by withholding the evidence.
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(_render_text(report))
    if getattr(args, "execute", False) and not report.get("success", False):
        die(
            "herdr smoke-shared-space failed: disposable cross-process smoke did not "
            "converge and clean up; the redacted evidence above names the phase"
        )
        raise AssertionError("unreachable")
    return 0


def register_herdr_smoke_shared_space_parser(sub) -> None:
    """Bind `herdr smoke-shared-space` onto the `herdr` subparser group (#14187)."""
    parser = sub.add_parser(
        "smoke-shared-space",
        help=(
            "preflight or execute shared_space smoke on an owned disposable Herdr instance"
        ),
        description=(
            "Preflight the high-level isolated shared_space coordinator-placement smoke "
            "harness through the semantic facade. Establishes an isolated operator home "
            "(fail-closed unless provably distinct from the real operator home), then "
            "runs the clean-slate gate. `--execute` additionally owns an endpoint-bound "
            "disposable Herdr server, starts two OS processes through the production "
            "shared-space path, performs exact-locator cleanup and residue verification, "
            "then shuts the server down. The operator's normal server is never contacted."
        ),
    )
    parser.add_argument(
        "--isolated-home",
        required=True,
        help=(
            "a fresh directory to use as the isolated operator home — must be distinct "
            "from (and not nested with) the real operator home"
        ),
    )
    parser.add_argument(
        "--projects",
        type=int,
        default=2,
        help="how many concurrent projects the live smoke would run (default: 2)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help=(
            "run the real cross-process smoke on an owned disposable Herdr server "
            "(default is the compatibility read-only preflight)"
        ),
    )
    parser.add_argument(
        "--process-timeout",
        type=_process_timeout,
        default=45.0,
        help=(
            "bounded seconds allowed for each smoke worker process (default: 45; "
            f"must be finite and within (0, {MAX_PROCESS_TIMEOUT_SECONDS:g}])"
        ),
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the redacted report as JSON"
    )
    parser.set_defaults(func=cmd_herdr_smoke_shared_space)


__all__ = (
    "cmd_herdr_smoke_shared_space",
    "register_herdr_smoke_shared_space_parser",
)
