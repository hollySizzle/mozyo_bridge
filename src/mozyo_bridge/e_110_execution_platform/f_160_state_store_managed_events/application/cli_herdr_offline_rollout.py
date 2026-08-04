"""``herdr offline-rollout`` plan/delegate/run/status surface (#14838)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_offline_rollout_plan import (  # noqa: E501
    run_offline_rollout_plan,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_offline_rollout_action import (  # noqa: E501
    delegate_offline_rollout,
    run_offline_rollout_action,
    status_offline_rollout_action,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_action import (  # noqa: E501
    OfflineRolloutActionError,
    approval_manifest,
    render_approval_note,
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
        candidate_source_ref=args.candidate_source_ref,
        candidate_workflow_run_id=args.candidate_workflow_run_id,
        candidate_wheel_sha256=args.candidate_wheel_sha256,
        candidate_sdist_sha256=args.candidate_sdist_sha256,
        legacy_recovery_pointers=tuple(args.legacy_recovery),
        env=dict(os.environ),
    )
    payload = result.as_payload()
    if result.ok and result.plan["candidate_artifact"]["exact_pin_ready"]:
        try:
            manifest = approval_manifest(result.plan, result.plan_digest)
            approval_issue = str(getattr(args, "approval_issue", "") or "")
            marker = (
                render_approval_note(manifest, approval_issue)
                if approval_issue
                else ""
            )
        except OfflineRolloutActionError as exc:
            payload["approval_ready"] = False
            payload["approval_reason"] = str(exc)
        else:
            payload["approval_manifest"] = manifest
            payload["required_approval_marker"] = marker or None
            payload["approval_ready"] = bool(marker)
            if not marker:
                payload["approval_reason"] = "approval_issue_required"
    elif result.ok:
        payload["approval_ready"] = False
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


def _fresh_plan(args: argparse.Namespace):
    return run_offline_rollout_plan(
        repo_root=_repo_root(args),
        home=_home(args),
        candidate_version=args.candidate_version,
        candidate_source_sha=args.candidate_source_sha,
        candidate_source_ref=args.candidate_source_ref,
        candidate_workflow_run_id=args.candidate_workflow_run_id,
        candidate_wheel_sha256=args.candidate_wheel_sha256,
        candidate_sdist_sha256=args.candidate_sdist_sha256,
        legacy_recovery_pointers=tuple(args.legacy_recovery),
        env=dict(os.environ),
    )


def _emit(result, *, json_output: bool) -> int:
    payload = result.as_payload()
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    elif result.ok:
        print(f"offline rollout {result.state}: action={payload.get('action_id', '')}")
    else:
        print(
            f"offline rollout {result.state}: {result.reason} ({result.detail})",
            file=sys.stderr,
        )
    return 0 if result.ok else 1


def cmd_herdr_offline_rollout_delegate(args: argparse.Namespace) -> int:
    planned = _fresh_plan(args)
    if not planned.ok:
        if args.json:
            print(
                json.dumps(
                    planned.as_payload(), ensure_ascii=False, indent=2, sort_keys=True
                )
            )
        else:
            print(
                f"offline rollout delegate refused: {planned.reason} ({planned.detail})",
                file=sys.stderr,
            )
        return 1
    if planned.plan_digest != args.plan_digest:
        payload = {
            "ok": False,
            "state": "blocked",
            "reason": "fresh_plan_digest_mismatch",
            "detail": "the live plan no longer matches the approved digest",
            "fresh_plan_digest": planned.plan_digest,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(
                "offline rollout delegate refused: fresh_plan_digest_mismatch",
                file=sys.stderr,
            )
        return 1
    result = delegate_offline_rollout(
        plan=planned.plan,
        plan_digest=planned.plan_digest,
        owner_approval=args.owner_approval,
        home=_home(args),
        repo_root=_repo_root(args),
        execute=bool(args.execute),
    )
    return _emit(result, json_output=bool(args.json))


def cmd_herdr_offline_rollout_run(args: argparse.Namespace) -> int:
    if not args.execute:
        print("offline rollout run refused: --execute is required", file=sys.stderr)
        return 1
    result = run_offline_rollout_action(action_id=args.action_id, home=_home(args))
    return _emit(result, json_output=bool(args.json))


def cmd_herdr_offline_rollout_status(args: argparse.Namespace) -> int:
    result = status_offline_rollout_action(action_id=args.action_id, home=_home(args))
    return _emit(result, json_output=bool(args.json))


def _add_candidate_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--candidate-version",
        required=True,
        help="Exact candidate package version intended for the rollout.",
    )
    parser.add_argument("--candidate-source-sha", default="")
    parser.add_argument("--candidate-source-ref", default="")
    parser.add_argument("--candidate-workflow-run-id", default="")
    parser.add_argument("--candidate-wheel-sha256", default="")
    parser.add_argument("--candidate-sdist-sha256", default="")


def _add_home(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--home",
        default=None,
        help="Shared mozyo-bridge home (default: MOZYO_BRIDGE_HOME, else ~/.mozyo_bridge).",
    )


def _add_legacy_recoveries(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--legacy-recovery",
        action="append",
        default=[],
        metavar="ISSUE:JOURNAL",
        help=(
            "Compose one already-approved #14756 legacy epoch recovery into the global "
            "window. Repeat for each exact hibernated lane; the issue must resolve to "
            "exactly one adoptable lifecycle row."
        ),
    )


def register_herdr_offline_rollout_parser(herdr_sub, *, add_repo_option=None) -> None:
    """Register the plan plus external replayable execution rail."""
    offline = herdr_sub.add_parser(
        "offline-rollout",
        help=(
            "Plan the shared-home Herdr offline rollout across every registered workspace. "
            "Plan is read-only. Delegate verifies an exact direct-owner approval and "
            "launches a consumer-external one-shot; run is reserved to that runner."
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
    _add_candidate_arguments(plan)
    _add_legacy_recoveries(plan)
    _add_home(plan)
    if add_repo_option is not None:
        add_repo_option(plan)
    plan.add_argument("--json", action="store_true", help="Emit canonical structured JSON")
    plan.add_argument(
        "--approval-issue",
        default="",
        metavar="ISSUE",
        help=(
            "Bind the emitted required_approval_marker to this exact Redmine issue. "
            "Without it the full manifest is emitted but approval_ready is false."
        ),
    )
    plan.set_defaults(func=cmd_herdr_offline_rollout_plan)

    delegate = sub.add_parser(
        "delegate",
        help=(
            "Re-capture the exact plan, verify ISSUE:JOURNAL direct-owner approval, "
            "prepare an independent runner, and launch it only with --execute."
        ),
    )
    _add_candidate_arguments(delegate)
    _add_legacy_recoveries(delegate)
    _add_home(delegate)
    if add_repo_option is not None:
        add_repo_option(delegate)
    delegate.add_argument("--plan-digest", required=True)
    delegate.add_argument("--owner-approval", required=True, metavar="ISSUE:JOURNAL")
    delegate.add_argument("--execute", action="store_true")
    delegate.add_argument("--json", action="store_true")
    delegate.set_defaults(func=cmd_herdr_offline_rollout_delegate)

    run = sub.add_parser("run", help="External one-shot only: run/resume one sealed action.")
    run.add_argument("--action-id", required=True)
    _add_home(run)
    run.add_argument("--execute", action="store_true")
    run.add_argument("--json", action="store_true")
    run.set_defaults(func=cmd_herdr_offline_rollout_run)

    status = sub.add_parser("status", help="Read one action's redacted replay status.")
    status.add_argument("--action-id", required=True)
    _add_home(status)
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_herdr_offline_rollout_status)


__all__ = (
    "cmd_herdr_offline_rollout_plan",
    "cmd_herdr_offline_rollout_delegate",
    "cmd_herdr_offline_rollout_run",
    "cmd_herdr_offline_rollout_status",
    "register_herdr_offline_rollout_parser",
)
