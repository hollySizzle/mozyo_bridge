"""`mozyo-bridge sublane` diagnostics: startup readiness + callback recovery (#12159).

Two read-only subcommands that make sublane startup and callback-stall handling
*visible and replayable from CLI output*, without changing any handoff /
queue-enter / launch behavior:

- ``sublane readiness`` — at sublane startup, answers three questions in one
  command: will the *next* managed Claude pane come up in ``auto`` mode (and if
  not, the exact remediation, including an invalid value like ``autopilot``);
  which handoff-worthy states this lane owes the coordinator a callback for; and
  where the stall-recovery path lives. It reuses the same
  ``describe_launch_policy`` the ``doctor`` ``claude_launch_policy`` section
  uses, so the permission verdict is consistent across both surfaces.

- ``sublane callback-recovery`` — classifies a delivered-but-quiet unit of work
  into the four documented callback-stall states from durable-record facts the
  operator passes as flags, and prints the standard recovery path. This is the
  ``progress_without_callback`` (and siblings) recovery made replayable: the
  coordinator runs it with what the Redmine issue shows and gets the named state
  plus the next recoverable step, instead of re-deriving it by hand.

Both are pure over their inputs (no tmux, no network, no Redmine I/O) and never
self-authorize a close / carve-out / owner decision.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import sublane_callback
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (
    CLAUDE_PERMISSION_MODES,
    SOURCE_ENV_INVALID,
    SOURCE_ENV_OVERRIDE,
    describe_launch_policy,
)


# --- readiness ---------------------------------------------------------------


def build_readiness_report() -> dict[str, Any]:
    """Assemble the read-only sublane startup readiness report.

    ``status`` is ``ok`` when future managed Claude panes will launch in
    reproducible ``auto`` mode and the override env var is valid; ``warning``
    otherwise (invalid env value, or an explicit non-auto override / no policy).
    The callback-responsibility contract is always reported — it is a reminder,
    not a health check.
    """
    policy = describe_launch_policy()
    permission_actions: list[str] = []

    if policy["source"] == SOURCE_ENV_INVALID:
        status = "warning"
        permission_actions.append(
            f"{policy['env_var']}={policy['env_value']!r} is not a valid Claude "
            "permission mode; future managed Claude panes will hard-error at "
            "launch until it is unset or set to a valid mode (choices: "
            f"{', '.join(sorted(CLAUDE_PERMISSION_MODES))}; `auto` recommended)"
        )
    elif policy["reproducible_auto"]:
        status = "ok"
    else:
        status = "warning"
        if policy["source"] == SOURCE_ENV_OVERRIDE:
            permission_actions.append(
                f"{policy['env_var']}={policy['env_value']!r} overrides the "
                "cockpit auto policy; future managed Claude panes will launch "
                f"`--permission-mode {policy['effective_mode']}` instead of "
                "auto. Unset it to restore reproducible auto mode"
            )
        else:
            permission_actions.append(
                "future managed Claude panes will not launch in auto mode; this "
                "build has no auto launch policy configured"
            )

    callback_states = [
        {"state": name, "detail": detail}
        for name, detail in sublane_callback.COORDINATOR_CALLBACK_STATES
    ]

    return {
        "status": status,
        "permission_mode": {
            "scope": "future managed Claude panes (non-retroactive)",
            "effective_mode": policy["effective_mode"],
            "source": policy["source"],
            "reproducible_auto": policy["reproducible_auto"],
            "env_var": policy["env_var"],
            "env_present": policy["env_present"],
            "env_value": policy["env_value"],
            "next_action": permission_actions,
        },
        "callback_responsibility": {
            "note": (
                "a sublane sends a coordinator callback when it reaches any of "
                "these handoff-worthy states (the callback is a pointer; the "
                "durable Redmine journal lands first)"
            ),
            "states": callback_states,
        },
        "callback_recovery_hint": (
            "if a callback is missing, classify the stall with "
            "`mozyo-bridge sublane callback-recovery` (the four-state "
            "progress_without_callback recovery path)"
        ),
    }


def format_readiness_text(report: dict[str, Any]) -> str:
    lines: list[str] = [f"sublane readiness: {report['status']}"]

    pm = report["permission_mode"]
    lines.append(
        f"  permission_mode: effective={pm['effective_mode'] or '-'} "
        f"source={pm['source']} reproducible_auto={pm['reproducible_auto']}"
    )
    lines.append(f"    scope: {pm['scope']}")
    if pm["env_present"]:
        lines.append(f"    {pm['env_var']}={pm['env_value'] or '-'}")
    for action in pm["next_action"]:
        lines.append(f"    -> {action}")

    cr = report["callback_responsibility"]
    lines.append("  callback_responsibility:")
    lines.append(f"    note: {cr['note']}")
    for entry in cr["states"]:
        lines.append(f"    - {entry['state']}: {entry['detail']}")

    lines.append(f"  -> {report['callback_recovery_hint']}")
    return "\n".join(lines)


def cmd_sublane_readiness(args: argparse.Namespace) -> int:
    report = build_readiness_report()
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_readiness_text(report))
    return 0 if report["status"] == "ok" else 1


# --- callback-recovery -------------------------------------------------------


def build_callback_recovery(args: argparse.Namespace) -> dict[str, Any]:
    return sublane_callback.classify_callback_stall(
        dispatch_delivered=bool(getattr(args, "dispatch_delivered", False)),
        new_durable_progress=bool(getattr(args, "progress", False)),
        callback=getattr(args, "callback", sublane_callback.CALLBACK_ABSENT),
        stale_cli=bool(getattr(args, "stale_cli", False)),
    )


def format_callback_recovery_text(result: dict[str, Any]) -> str:
    lines = [
        f"callback stall: {result['state']} (is_stall={result['is_stall']})",
        f"  inputs: dispatch_delivered={result['dispatch_delivered']} "
        f"new_durable_progress={result['new_durable_progress']} "
        f"callback={result['callback']} stale_cli={result['stale_cli']}",
        f"  summary: {result['summary']}",
        "  recovery:",
    ]
    for i, step in enumerate(result["recovery"], 1):
        lines.append(f"    {i}. {step}")
    if result["invariants"]:
        lines.append("  invariants:")
        for inv in result["invariants"]:
            lines.append(f"    - {inv}")
    return "\n".join(lines)


def cmd_sublane_callback_recovery(args: argparse.Namespace) -> int:
    result = build_callback_recovery(args)
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_callback_recovery_text(result))
    # A genuine stall returns non-zero so the coordinator can branch on it in
    # scripts; non-stall outcomes (complete / not-required / not-a-candidate)
    # return 0. Read-only either way.
    return 1 if result["is_stall"] else 0
