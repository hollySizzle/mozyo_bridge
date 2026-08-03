"""``herdr offline-rollout plan`` CLI surface (Redmine #14838 Phase A)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_offline_rollout_plan import (  # noqa: E501
    run_offline_rollout_plan,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home


def _home(args: argparse.Namespace) -> Path:
    selected = getattr(args, "home", None)
    return Path(selected).expanduser().resolve() if selected else mozyo_bridge_home()


def _repo_root(args: argparse.Namespace) -> Path:
    selected = getattr(args, "repo", None)
    return Path(selected).expanduser().resolve() if selected else Path.cwd().resolve()


def cmd_herdr_offline_rollout_plan(args: argparse.Namespace) -> int:
    result = run_offline_rollout_plan(
        repo_root=_repo_root(args),
        home=_home(args),
        candidate_version=args.candidate_version,
        candidate_source_sha=args.candidate_source_sha,
        env=dict(os.environ),
    )
    payload = result.as_payload()
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    elif result.ok:
        print(
            "offline rollout plan ready: "
            f"digest={result.plan_digest} workspaces={len(result.plan['workspaces'])} "
            f"agents={len(result.plan['agents'])}"
        )
        print("No side effect was performed.")
    else:
        print(
            f"offline rollout plan refused: {result.reason} ({result.detail})",
            file=sys.stderr,
        )
    return 0 if result.ok else 1


def register_herdr_offline_rollout_parser(herdr_sub, *, add_repo_option=None) -> None:
    """Register the plan-only Phase A command; no execution parser exists yet."""
    offline = herdr_sub.add_parser(
        "offline-rollout",
        help=(
            "Plan the shared-home Herdr offline rollout across every registered workspace. "
            "Phase A is read-only: it stops, migrates, installs, publishes and relaunches "
            "nothing. Execution requires a later exact plan-digest owner approval."
        ),
    )
    sub = offline.add_subparsers(dest="offline_rollout_command", required=True)
    plan = sub.add_parser(
        "plan",
        help=(
            "Capture one drift-checked global inventory, registry, WIP, three-store and "
            "supervisor snapshot; emit its canonical stop/migrate/restore plan and digest."
        ),
    )
    plan.add_argument(
        "--candidate-version",
        required=True,
        help="Exact candidate package version intended for the later rollout.",
    )
    plan.add_argument(
        "--candidate-source-sha",
        default="",
        help=(
            "Exact 40-hex source commit of the built artifact. Omit until the artifact "
            "exists; the plan then reports exact_pin_ready=false."
        ),
    )
    plan.add_argument(
        "--home",
        default=None,
        help="Shared mozyo-bridge home (default: MOZYO_BRIDGE_HOME, else ~/.mozyo_bridge).",
    )
    if add_repo_option is not None:
        add_repo_option(plan)
    plan.add_argument("--json", action="store_true", help="Emit canonical structured JSON")
    plan.set_defaults(func=cmd_herdr_offline_rollout_plan)


__all__ = (
    "cmd_herdr_offline_rollout_plan",
    "register_herdr_offline_rollout_parser",
)
