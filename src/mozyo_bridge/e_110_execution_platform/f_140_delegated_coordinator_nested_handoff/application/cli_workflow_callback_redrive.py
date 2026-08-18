"""``workflow callback-redrive`` — explicit dead-letter re-enqueue (Redmine #15707 c).

A dead-lettered callback row is terminal to every automatic path — ``--run-once`` claims only
``pending``, inflight recovery never touches it, and the #13974 replay invariants forbid a
restart from resurrecting it. That is correct for a row that dead-lettered because its
generation went stale, and wrong for the measured #15707 shape: bounded
``precondition_not_idle`` retries against a coordinator mid-turn exhausted the budget and
terminalized a legitimately deliverable callback (#15700 j#107939 / #15702 j#107933).

This is the sanctioned out-of-band repair, shaped like the other gated operator surfaces
(``callback-lease --recover``): **dry-run by default** — it lists the workspace's dead-letter
backlog with each row's redrive fingerprint and writes nothing — and actuates only with
``--apply`` naming exactly ONE row (issue / journal / gate / route) plus the
``--expect-fingerprint`` a prior dry-run reported. The store's compare-and-swap
(:meth:`...core.state.callback_outbox.CallbackOutbox.requeue_dead_letter`) zero-writes on any
concurrent mutation. A redrive only returns the row to ``pending``: delivery re-runs the whole
fenced pipeline (claim -> generation fences -> admission -> one send), so this can never become
a blind retry — a stale row redriven by mistake is still a zero-send terminal there.

The exit code is the contract (the ``callback-admit`` doctrine): 0 = listed (dry-run) or
requeued (apply); 2 = invalid arguments; 4 = no such row; 5 = the row is not dead-lettered;
6 = fingerprint mismatch (a concurrent mutation; re-read and decide again).
"""

from __future__ import annotations

import argparse
import json as _json

#: Exit codes (closed contract; see module docstring).
EXIT_OK = 0
EXIT_INVALID_ARGS = 2
EXIT_ABSENT = 4
EXIT_STATE_MISMATCH = 5
EXIT_FINGERPRINT_MISMATCH = 6


def cmd_workflow_callback_redrive(args: argparse.Namespace) -> int:
    """Dry-run list of the dead-letter backlog, or the ONE gated re-enqueue (``--apply``)."""
    from mozyo_bridge.core.state.callback_outbox import (
        REDRIVE_ABSENT,
        REDRIVE_FINGERPRINT_MISMATCH,
        REDRIVE_REQUEUED,
        REDRIVE_STATE_MISMATCH,
        CallbackOutboxKey,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_callbacks import (  # noqa: E501
        _outbox_from_args,
        _require_partition_workspace_id,
    )

    as_json = bool(getattr(args, "json", False))
    # Both halves run behind the workspace attestation: the dry-run reads (and the apply
    # mutates) exactly one workspace's partition of the shared home DB. The blank legacy
    # bucket stays behind the explicit --allow-unpartitioned-callbacks surface.
    workspace_id = _require_partition_workspace_id(args)
    outbox = _outbox_from_args(args)

    def _emit(payload: dict, lines: list) -> None:
        if as_json:
            print(_json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            for line in lines:
                print(line)

    if not getattr(args, "apply", False):
        issue_filter = str(getattr(args, "issue", "") or "").strip()
        pairs = outbox.dead_letter_fingerprints(workspace_id=workspace_id)
        if issue_filter:
            pairs = tuple(p for p in pairs if p[0].issue == issue_filter)
        payload = {
            "action": "redrive_dry_run",
            "workspace_id": workspace_id,
            "dead_letter": [
                {**row.as_payload(), "redrive_fingerprint": fingerprint}
                for row, fingerprint in pairs
            ],
        }
        lines = [
            "action: redrive_dry_run (no write; add --apply --expect-fingerprint to actuate)",
            f"workspace: {workspace_id or '<unpartitioned>'}",
            f"dead_letter: {len(pairs)}",
        ]
        lines += [
            f"  #{row.issue} j#{row.journal} gate={row.normalized_gate} route={row.callback_route} "
            f"attempts={row.attempts}/{row.max_attempts} fingerprint={fingerprint} {row.detail}"
            for row, fingerprint in pairs
        ]
        _emit(payload, lines)
        return EXIT_OK

    issue = str(getattr(args, "issue", "") or "").strip()
    journal = str(getattr(args, "journal", "") or "").strip()
    gate = str(getattr(args, "gate", "") or "").strip()
    route = str(getattr(args, "route", "") or "").strip()
    source = str(getattr(args, "source", "") or "redmine").strip()
    fingerprint = str(getattr(args, "expect_fingerprint", "") or "").strip()
    if not (issue and journal and gate and route and fingerprint):
        print(
            "callback-redrive --apply names exactly ONE observed row: --issue, --journal, "
            "--gate, --route and --expect-fingerprint (from a prior dry-run) are all required."
        )
        return EXIT_INVALID_ARGS

    key = CallbackOutboxKey(
        source=source, issue=issue, journal=journal,
        normalized_gate=gate, callback_route=route, workspace_id=workspace_id,
    )
    disposition = outbox.requeue_dead_letter(key, expect_fingerprint=fingerprint)
    exit_code = {
        REDRIVE_REQUEUED: EXIT_OK,
        REDRIVE_ABSENT: EXIT_ABSENT,
        REDRIVE_STATE_MISMATCH: EXIT_STATE_MISMATCH,
        REDRIVE_FINGERPRINT_MISMATCH: EXIT_FINGERPRINT_MISMATCH,
    }.get(disposition, EXIT_INVALID_ARGS)
    _emit(
        {
            "action": "redrive_apply",
            "workspace_id": workspace_id,
            "issue": issue,
            "journal": journal,
            "normalized_gate": gate,
            "callback_route": route,
            "disposition": disposition,
        },
        [
            "action: redrive_apply",
            f"row: #{issue} j#{journal} gate={gate} route={route}",
            f"disposition: {disposition}",
        ],
    )
    return exit_code


def register_callback_redrive(sub) -> None:
    """Register ``workflow callback-redrive`` (called from the callback family registrar)."""
    p = sub.add_parser(
        "callback-redrive",
        description=(
            "Explicit dead-letter re-enqueue for the callback outbox (Redmine #15707). No "
            "--apply = DRY-RUN: list this workspace's dead-letter rows with their redrive "
            "fingerprints, write nothing. --apply returns exactly ONE observed row to pending "
            "(--issue / --journal / --gate / --route / --expect-fingerprint from a prior "
            "dry-run); any concurrent mutation, wrong state, or unknown row is a typed "
            "zero-write. A redriven row re-runs the whole fenced delivery pipeline — this "
            "re-admits, it never bypasses. Exit codes: 0 ok, 2 invalid args, 4 absent, "
            "5 not dead-lettered, 6 fingerprint mismatch."
        ),
        help="Explicit, fingerprint-gated re-enqueue of a dead-lettered callback row.",
    )
    p.add_argument(
        "--apply", action="store_true",
        help="Actuate the ONE named redrive (default is a read-only dry-run listing).",
    )
    p.add_argument("--issue", help="Row issue id (dry-run filter; required with --apply).")
    p.add_argument("--journal", help="Row source journal id (required with --apply).")
    p.add_argument("--gate", help="Row normalized gate (required with --apply).")
    p.add_argument("--route", help="Row callback route, e.g. coordinator (required with --apply).")
    p.add_argument(
        "--source", default="redmine",
        help="Row source system (default: redmine).",
    )
    p.add_argument(
        "--expect-fingerprint", dest="expect_fingerprint", metavar="TOKEN", default="",
        help="Bind --apply to the fingerprint a prior dry-run reported; a mismatch is a "
             "concurrent mutation and zero-writes.",
    )
    p.add_argument("--store-path", dest="store_path", help="Override the workflow-runtime.sqlite path (test/debug).")
    p.add_argument(
        "--allow-unpartitioned-callbacks", dest="allow_unpartitioned_callbacks", action="store_true",
        help="Explicit legacy/migration surface: operate on the un-partitioned (blank workspace) bucket.",
    )
    p.add_argument("--json", action="store_true", help="Emit the structured JSON payload.")
    p.set_defaults(func=cmd_workflow_callback_redrive)


__all__ = (
    "EXIT_ABSENT",
    "EXIT_FINGERPRINT_MISMATCH",
    "EXIT_INVALID_ARGS",
    "EXIT_OK",
    "EXIT_STATE_MISMATCH",
    "cmd_workflow_callback_redrive",
    "register_callback_redrive",
)
