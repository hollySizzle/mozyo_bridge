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

#: The actuating sweep found the callback-sweep lease store inconsistent (Redmine #13951). The
#: sweep zero-sends and PROJECTS this as an actionable typed blocker rather than raising an opaque
#: error the supervisor/service would swallow as a silent stop.
CALLBACK_LEASE_INCONSISTENT = "callback_lease_inconsistent"


def _callback_lease_blocker(diagnosis, args) -> dict[str, Any]:
    """Build the actionable, redaction-safe blocker for an inconsistent lease store (#13951 #3).

    ``diagnosis`` is a :class:`...callback_sweep_lease.LeaseDiagnosis` (already redaction-safe: no
    owner token, raw row, or absolute path). The result matches the ``callback-recovery`` verdict
    shape so the same text/JSON formatter and non-zero exit apply — the operator sees the typed
    state, the zero-send invariant, and the exact public recovery rail, never a stack trace.
    """
    return {
        "state": CALLBACK_LEASE_INCONSISTENT,
        "is_stall": True,
        "dispatch_delivered": bool(getattr(args, "dispatch_delivered", False)),
        "new_durable_progress": False,
        "callback": getattr(args, "callback", sublane_callback.CALLBACK_ABSENT),
        "stale_cli": bool(getattr(args, "stale_cli", False)),
        "summary": (
            f"the callback-sweep attempt lease store is inconsistent ({diagnosis.state}: "
            f"{diagnosis.reason}); the sweep zero-sent rather than run unserialized, and this is an "
            "actionable operator blocker — the supervisor/service must project it, not silent-stop"
        ),
        "recovery": [
            "inspect: `mozyo-bridge workflow callback-lease` (typed status + artifact fingerprint)",
            "dry-run: `mozyo-bridge workflow callback-lease --recover` (writes nothing)",
            "actuate: `mozyo-bridge workflow callback-lease --recover --apply --expect-fingerprint "
            "<token>` ONLY after confirming no sweep is mid-attempt (it invalidates every grant)",
        ],
        "invariants": [
            "zero-send: no callback was delivered while the lease store is inconsistent",
            f"recoverable={diagnosis.recoverable} has_live_owner={diagnosis.has_live_owner}",
        ],
        "progress_provenance": PROGRESS_DERIVED,
        "callback_lease_diagnosis": diagnosis.as_dict(),
    }


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


def attested_workspace_id(args: argparse.Namespace) -> str:
    """MEASURE the partition workspace id from the canonical registry authority (review R3-F2).

    The fence key is partitioned by workspace, so a wrong / invented id reserves a DIFFERENT row and
    the same recovery sends twice. The id must therefore be measured, and an earlier revision only
    *claimed* to measure it: it read ``read_anchor(repo_root)`` and then fell back to the
    caller-supplied ``--workspace-id`` when that returned ``None`` — which is exactly what happens
    in a **linked sublane worktree**, where anchors are untracked. So on every lane where this
    command actually runs, the "measured" authority was absent and the CLI argument minted the fence
    partition itself.

    The authority is :func:`...workspace_registry.resolve_canonical_session`, the canonical resolver
    that already handles the #13152 topology: registry row -> local anchor -> **linked worktree
    inheriting its main checkout's identity** -> derivation. A sublane worktree resolves to its
    parent workspace_id through that inheritance, which is the id the fence must partition on.

    ``--workspace-id`` is an **equality assertion only** — it can confirm what was measured but can
    never supply it. A blank / unreadable authority returns blank, and :func:`sweep_once` then
    zero-sends rather than actuate on a partition nobody measured.
    """
    from mozyo_bridge.application.commands_common import repo_root_from_args
    from mozyo_bridge.core.state.workspace_registry import resolve_canonical_session

    try:
        resolved = str(
            resolve_canonical_session(repo_root_from_args(args)).workspace_id or ""
        ).strip()
    except Exception:  # noqa: BLE001 - an unreadable authority is unattested, never assumed
        resolved = ""
    asserted = str(getattr(args, "workspace_id", "") or "").strip()
    if asserted and asserted != resolved:
        raise SystemExit(
            f"--workspace-id {asserted!r} does not match the measured workspace identity "
            f"{resolved or '<unresolved>'!r}; refusing to actuate on an unattested fence "
            f"partition (the flag asserts what was measured, it cannot supply it)"
        )
    return resolved


def _execute_sweep(args: argparse.Namespace) -> dict[str, Any]:
    """The ACTUATING sweep: fresh read -> re-read -> fence -> durable record -> one recovery.

    The production caller for :func:`...callback_sweep.sweep_once` (review R1-F1). Everything the
    acceptance turns on binds only when a real recovery runs through here, so the path is composed
    from the three authorities the reviews established:

    - a **live** durable source (R2-F1). ``--journals-json`` is read-only classification: it is a
      frozen snapshot, so its "re-read" cannot observe a gate landing after the decision and
      ``sweep_once`` refuses to mutate on it;
    - an **attested** workspace id (R2-F2), measured from the durable anchor, not defaulted;
    - a **durable recovery record written before the send** (R2-F3), which the notification then
      points at — a re-poke with no journal behind it is prohibited outright.

    Fail-closed throughout: an unconfigured live source, an unattested workspace, an unwritable
    record, an unbootstrapped fence, a lost reserve, opaque post-anchor journals, a landed gate, or
    a superseded round all zero-send.
    """
    from mozyo_bridge.core.state.callback_publication_fence import CallbackPublicationFence
    from mozyo_bridge.core.state.callback_sweep_lease import CallbackSweepLease
    from mozyo_bridge.core.state.dispatch_outbox_fence import DispatchOutboxFence
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_sweep import (
        build_recovery_recorder,
        build_recovery_sender,
        sweep_once,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_sweep_watermark import (
        SWEEP_RECOVERY_RECEIVER,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (
        LiveRedmineJournalSource,
    )
    from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_note_transport import (
        redmine_delivery_transport_from_env,
    )

    issue = str(getattr(args, "issue", "") or "").strip()
    lane = str(getattr(args, "lane", "") or "").strip()
    generation = str(getattr(args, "lane_generation", "") or "").strip()
    target = str(getattr(args, "target", "") or "").strip()
    if not (issue and lane and generation and target):
        raise SystemExit(
            "--execute requires --issue, --lane, --lane-generation and --target "
            "(the recovery is fenced on the exact dispatch round and delivered to one target)"
        )
    if getattr(args, "journals_json", None):
        raise SystemExit(
            "--execute cannot use --journals-json: a snapshot is frozen, so the pre-mutation "
            "re-read could not observe a gate landing after the decision (Redmine #13889 R2-F1). "
            "The actuating sweep reads Redmine live; --journals-json stays read-only."
        )
    # The live read boundary fails closed when the trusted credentials are unconfigured.
    try:
        source = LiveRedmineJournalSource.from_environment()
    except Exception as exc:  # noqa: BLE001 - unconfigured live read -> no actuation
        raise SystemExit(
            f"--execute needs a live Redmine read boundary ({type(exc).__name__}: {exc}); "
            f"without it the pre-mutation re-read cannot see new gates, so the sweep will not "
            f"mutate"
        ) from exc
    transport = redmine_delivery_transport_from_env()
    if transport is None:
        raise SystemExit(
            "--execute needs the Redmine note write opt-in (MOZYO_REDMINE_DELIVERY_WRITE): the "
            "stall classification must be durably recorded before the pointer send (a silent "
            "re-poke is prohibited)"
        )
    # The lease store is identity-pinned and never auto-creates (R6-F2), so the composition root
    # bootstraps it explicitly; a store LOSS then fails closed instead of minting a duplicate lease.
    # #13951 #3: a fail-closed store must PROJECT as an actionable typed blocker, not raise an opaque
    # error the supervisor/service swallows as a silent stop. Diagnose it (read-only) and return the
    # zero-send blocker naming the public recovery rail — do NOT auto-recover here (a silent
    # re-create would hand a second live owner the same anchor).
    from mozyo_bridge.core.state.callback_sweep_lease import CallbackSweepLeaseError

    lease = CallbackSweepLease(home=None)
    try:
        lease.bootstrap()
    except CallbackSweepLeaseError:
        return _callback_lease_blocker(lease.diagnose(), args)
    # The publication authority (j#80383 option (d)). Ordinary execute must NEVER bootstrap it:
    # bootstrap's both-absent branch re-mints the store, and a re-minted store forgets the
    # reservation a suspended sweep still holds -- the same store-wide reclaim that `recover()`
    # performed, just reachable from the normal path (R12-F1). So this only ever *checks*, and an
    # absent or lost store stops the sweep instead of quietly rebuilding the fence around it.
    publication_fence = CallbackPublicationFence(home=None)
    if not publication_fence.is_bootstrapped():
        raise SystemExit(
            "callback publication fence is not ready: refusing to sweep, because publishing "
            "without the fence can duplicate a recovery record. Run `mozyo-bridge workflow "
            "callback-publication --bootstrap` -- on first use it initializes the store, and on a "
            "store that predates the fence's first-init seal it adopts it in place, keeping any "
            "reservation it already holds. A store LOSS after that is fail-closed by design and "
            "needs the store restored, not re-created."
        )
    result = sweep_once(
        workspace_id=attested_workspace_id(args),
        lane_id=lane,
        issue=issue,
        lane_generation=generation,
        source=source,
        fence=DispatchOutboxFence(home=None),
        lease=lease,
        target_assigned_name=target,
        send_fn=build_recovery_sender(issue=issue, target=target),
        # R7-F1: `sweep_once` supplies the live-grant predicate, so the ownership check lands
        # after the recorder's own reads and immediately before its write — not merely before the
        # recorder is called, which left a whole Redmine round-trip unguarded.
        record_fn_factory=lambda grant_is_live: build_recovery_recorder(
            source=source, issue=issue, lane=lane, lane_generation=generation,
            post_note=transport.post_issue_note, grant_is_live=grant_is_live,
            publication_fence=publication_fence,
            workspace_id=attested_workspace_id(args),
            # #13910: the record carries the receiver-side admission key, so it must name the
            # exact route this delivery is addressed to and the role allowed to admit it. Both
            # come from the same values the send uses -- `target` is the fence's
            # `target_assigned_name`, and the receiver constant is the sender's own `--to`.
            route_identity=target,
            receiver_identity=SWEEP_RECOVERY_RECEIVER,
        ),
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
    if getattr(args, "execute", False):
        return _execute_sweep(args)
    if getattr(args, "journals_json", None):
        return _derive_from_journals(args)
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
    if result.get("sweep_complete") is False:
        # R5-F4: this field previously existed but NOTHING read it — the exit code only happened to
        # be non-zero because `is_stall` was true. A durable mutation landed and the sweep did not
        # finish, so it must be visible on the surface an operator actually reads.
        lines.append(
            f"  INCOMPLETE: a durable recovery record (j#"
            f"{result.get('recovery_record_journal') or '?'}) WAS written but the sweep did not "
            f"finish ({result.get('send_reason') or 'unknown'}) — re-run it; do not treat this as "
            f"a resolved sweep"
        )
    elif result.get("resolution_recorded") is False:
        lines.append(
            f"  INCOMPLETE: the verdict was NOT durably recorded "
            f"({result.get('record_reason') or 'unknown'}) — a stall check and its classification "
            f"are themselves durable events, so this sweep is not resolved"
        )
    elif result.get("recovery_record_journal"):
        lines.append(f"  recovery record: j#{result['recovery_record_journal']}")
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
    # An actuating sweep that did not finish, or whose resolution never landed durably, is
    # INCOMPLETE rather than resolved (reviews R3-F4 / R5-F4): exit non-zero explicitly, so a
    # caller reading only the return code can never treat it as a finished sweep. Relying on
    # `is_stall` to happen to be true here was the R5-F4 defect.
    if result.get("sweep_complete") is False or result.get("resolution_recorded") is False:
        return 1
    # A genuine stall returns non-zero so the coordinator can branch on it in
    # scripts; non-stall outcomes (complete / not-required / not-a-candidate)
    # return 0. Read-only either way.
    return 1 if result["is_stall"] else 0
