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
from pathlib import Path
from typing import Any

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_sweep import (
    read_watermark,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import sublane_callback
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_sweep_watermark import (
    classify_sweep,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    MappingRedmineJournalSource,
)
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


#: Provenance of the ``new_durable_progress`` input — the distinction Redmine #13889 turns on.
PROGRESS_DERIVED = "derived_dispatch_anchored"
PROGRESS_ASSERTED = "asserted_unanchored"


def _snapshot_source(args: argparse.Namespace) -> MappingRedmineJournalSource:
    payload = json.loads(Path(str(args.journals_json)).read_text(encoding="utf-8"))
    return MappingRedmineJournalSource(payload=payload)


def _derive_from_journals(args: argparse.Namespace) -> dict[str, Any]:
    """Derive the verdict from a durable journal snapshot, anchored on the exact dispatch marker.

    The #13889 read-only path: ``--journals-json`` supplies the ``include=journals`` snapshot and the
    verdict is derived through the SAME :func:`...callback_sweep.read_watermark` the actuating path
    uses — anchored on this lane+generation's structured IR marker, ordered by durable journal id,
    and round-aware. Classification only: no fence is reserved and nothing is sent.
    """
    source = _snapshot_source(args)
    watermark = read_watermark(
        source,
        str(getattr(args, "issue", "") or "").strip(),
        lane=str(getattr(args, "lane", "") or "").strip(),
        lane_generation=getattr(args, "lane_generation", None),
    )
    result = classify_sweep(
        watermark=watermark,
        callback=getattr(args, "callback", sublane_callback.CALLBACK_ABSENT),
        stale_cli=bool(getattr(args, "stale_cli", False)),
    )
    result["progress_provenance"] = PROGRESS_DERIVED
    return result


def _execute_sweep(args: argparse.Namespace) -> dict[str, Any]:
    """The ACTUATING sweep: derive -> re-read -> fence -> at-most-once recovery (review F1).

    The production caller :func:`...callback_sweep.sweep_once` was missing. Everything the
    acceptance turns on — the pre-mutation re-read, the first-pass zero-send, the at-most-once fence
    keyed on the dispatch anchor — only binds when a real recovery goes through this path, so this
    is where the operator's recovery now runs.

    ``--target`` is the pane the one re-notification is delivered to; the fence (a real home-scoped
    store) gates it to at most once per dispatch anchor. Fail-closed throughout: an unbootstrapped
    fence, a lost reserve, opaque post-anchor journals, a landed gate, or a superseded round all
    zero-send.
    """
    from mozyo_bridge.core.state.dispatch_outbox_fence import DispatchOutboxFence
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_sweep import (
        build_recovery_sender,
        sweep_once,
    )

    issue = str(getattr(args, "issue", "") or "").strip()
    lane = str(getattr(args, "lane", "") or "").strip()
    generation = getattr(args, "lane_generation", None)
    target = str(getattr(args, "target", "") or "").strip()
    if not (issue and lane and generation and target):
        raise SystemExit(
            "--execute requires --issue, --lane, --lane-generation and --target "
            "(the recovery is fenced on the exact dispatch round and delivered to one target)"
        )
    source = _snapshot_source(args)
    fence = DispatchOutboxFence(home=None)
    dispatch_anchor = read_watermark(
        source, issue, lane=lane, lane_generation=generation
    ).dispatch_journal
    sender = build_recovery_sender(
        issue=issue, journal=dispatch_anchor or issue, target=target
    )
    result = sweep_once(
        workspace_id=str(getattr(args, "workspace_id", "") or "").strip(),
        lane_id=lane,
        issue=issue,
        lane_generation=generation,
        source=source,
        fence=fence,
        target_assigned_name=target,
        send_fn=sender,
        callback=getattr(args, "callback", sublane_callback.CALLBACK_ABSENT),
        stale_cli=bool(getattr(args, "stale_cli", False)),
    )
    result["progress_provenance"] = PROGRESS_DERIVED
    return result


def build_callback_recovery(args: argparse.Namespace) -> dict[str, Any]:
    """Classify a delivered-but-quiet unit of work, deriving progress when a snapshot is supplied.

    Two provenances, and the output always says which (#13889):

    - ``--journals-json`` (+ ``--lane`` / ``--lane-generation``) -> **derived**: anchored on the
      exact dispatch marker and ordered by durable journal id, so a gate that landed seconds before
      the sweep is seen on the first pass;
    - ``--progress`` / no snapshot -> **asserted**: the legacy hand-set boolean. It is the input
      that produced the #13883 false stalls (an agent's earlier read is a coordinator-local cutoff),
      so the verdict is tagged unanchored rather than being silently trusted as equivalent.
    """
    if getattr(args, "journals_json", None):
        if getattr(args, "execute", False):
            return _execute_sweep(args)
        return _derive_from_journals(args)
    if getattr(args, "execute", False):
        raise SystemExit(
            "--execute requires --journals-json: an actuating sweep must derive its verdict from "
            "the durable record, never from a hand-set --progress boolean (Redmine #13889)"
        )
    result = sublane_callback.classify_callback_stall(
        dispatch_delivered=bool(getattr(args, "dispatch_delivered", False)),
        new_durable_progress=bool(getattr(args, "progress", False)),
        callback=getattr(args, "callback", sublane_callback.CALLBACK_ABSENT),
        stale_cli=bool(getattr(args, "stale_cli", False)),
    )
    result["progress_provenance"] = PROGRESS_ASSERTED
    return result


def format_callback_recovery_text(result: dict[str, Any]) -> str:
    lines = [
        f"callback stall: {result['state']} (is_stall={result['is_stall']})",
        f"  inputs: dispatch_delivered={result['dispatch_delivered']} "
        f"new_durable_progress={result['new_durable_progress']} "
        f"callback={result['callback']} stale_cli={result['stale_cli']}",
    ]
    provenance = result.get("progress_provenance", "")
    if provenance == PROGRESS_DERIVED:
        lines.append(
            f"  watermark: derived, anchored on dispatch journal "
            f"{result.get('dispatch_journal') or '-'} (ordered durable journal id)"
        )
        for entry in result.get("progress_journals", []):
            lines.append(f"    progress: j#{entry['journal']} {entry['kind']}")
    elif provenance == PROGRESS_ASSERTED:
        lines.append(
            "  watermark: ASSERTED (not dispatch-anchored) — new_durable_progress was supplied by "
            "hand, so a gate that landed after your read is invisible here. Pass --journals-json "
            "with --lane/--lane-generation to derive it from the durable record instead."
        )
    lines += [f"  summary: {result['summary']}", "  recovery:"]
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
