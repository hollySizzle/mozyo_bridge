"""`herdr smoke-shared-space` — the isolated shared-space smoke preflight (Redmine #14187).

The public, read-only front door for the high-level shared-space coordinator-placement
smoke harness (:mod:`shared_space_smoke_harness`). It answers one question through the
semantic facade — *"can the ``shared_space`` cross-process smoke run here without
touching the operator's real home or shared coordinators space?"* — and prints a
redacted, typed report.

It establishes an isolated operator home (fail-closed unless provably distinct from the
real operator home), then runs the herdr clean-slate cleanup-authority gate (a read-only
``workspace list``): a pre-existing ``coordinators`` space, unreadable labels, or an
unresolvable herdr binary all fail closed. It **actuates no agent** — the live
coordinator launch / observe / cleanup is the #14185 driver's job, which owns the
disposable herdr instance the real smoke needs (Redmine #14187 Required work 6). No
``--execute``, no send, no Enter, no credential display.
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
from mozyo_bridge.shared.errors import die


def _render_text(report: dict) -> str:
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
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(_render_text(report))
    return 0


def register_herdr_smoke_shared_space_parser(sub) -> None:
    """Bind `herdr smoke-shared-space` onto the `herdr` subparser group (#14187)."""
    parser = sub.add_parser(
        "smoke-shared-space",
        help=(
            "read-only: preflight the isolated shared_space coordinator-placement smoke "
            "(prove home isolation + herdr clean slate; #14187, no agent actuation)"
        ),
        description=(
            "Preflight the high-level isolated shared_space coordinator-placement smoke "
            "harness through the semantic facade. Establishes an isolated operator home "
            "(fail-closed unless provably distinct from the real operator home), then "
            "runs the herdr clean-slate cleanup-authority gate (a read-only `workspace "
            "list`): a pre-existing `coordinators` space, unreadable labels, or an "
            "unresolvable herdr binary all fail closed with zero actuation. It ACTUATES "
            "NO AGENT — the live cross-process coordinator launch / observe / cleanup is "
            "the Redmine #14185 driver's job (which owns the disposable herdr instance "
            "the real smoke needs). No send, no Enter, no credential display."
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
        "--json", action="store_true", help="emit the redacted report as JSON"
    )
    parser.set_defaults(func=cmd_herdr_smoke_shared_space)


__all__ = (
    "cmd_herdr_smoke_shared_space",
    "register_herdr_smoke_shared_space_parser",
)
