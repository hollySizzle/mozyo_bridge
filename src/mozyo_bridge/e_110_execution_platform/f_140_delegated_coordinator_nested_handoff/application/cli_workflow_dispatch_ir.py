"""CLI surface for `workflow dispatch-ir` — the canonical Implementation Request writer (#13758 R6-F4).

`mozyo-bridge workflow dispatch-ir` is the production entry point that makes the reconciler's exact
dispatch anchor durable: it writes the Implementation Request journal with the machine dispatch
marker embedded (Design Answer j#79507 Q2), reads back the marker's OWNING journal id, and — only on
a single resolved anchor — SENDS the gated `handoff send ... --journal <anchor>` to the worker
agent. The write -> readback -> handoff sequence and its fail-closed cases live in the pure
:func:`...application.reconcile_dispatch_writer.dispatch_implementation_request`; this is the thin CLI
that owns argv, stdout, and the live composition.

Modes:

- default (dry-run) — compose the marker-bearing note and print it for preview; **no** Redmine write,
  **no** handoff send. The operator sees the exact journal body (marker included) ``--execute`` would post.
- ``--execute`` — performs SIDE EFFECTS: it writes the note as a Redmine journal (opt-in-gated live
  transport), reads the journal back, resolves the anchor, and then **sends a live handoff to the
  worker agent** (the gated `handoff send`). Only a positively-delivered handoff is reported success;
  a missing/invalid route, a write / readback failure, an unresolved / ambiguous anchor, or a
  non-delivered handoff fails closed (exit 2, an explicit redacted reason on stderr) — the durable
  dispatch intent still persists for a later readback-recovery.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_dispatch_writer import (
    DISPATCH_ROLE_PROFILE,
    DISPATCH_SENDABLE,
    DispatchRoute,
    build_live_handoff_send,
    build_live_ir_dispatch,
    build_live_vocabulary,
    dispatch_implementation_request,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    render_dispatch_note,
)


def _dispatch_body(args: argparse.Namespace) -> str:
    """Resolve the IR prose body from ``--body`` or ``--body-file`` (a file read fails closed)."""
    body = getattr(args, "body", None)
    if body is not None:
        return str(body)
    path_text = getattr(args, "body_file", None)
    if path_text:
        try:
            return Path(path_text).read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"mozyo-bridge workflow dispatch-ir: cannot read --body-file: {exc}")
    return ""


def _route_from_args(args: argparse.Namespace, lane: str) -> DispatchRoute:
    """Build the handoff route identity from args (validated against the vocabulary in the use case)."""
    return DispatchRoute(
        to=str(getattr(args, "to", None) or "claude"),
        target=str(getattr(args, "target", "") or ""),
        target_repo=str(getattr(args, "target_repo", "") or ""),
        lane=lane,
        gateway_callback_target=str(getattr(args, "gateway_callback_target", "") or ""),
        role_profile=str(getattr(args, "role_profile", None) or DISPATCH_ROLE_PROFILE),
        source=str(getattr(args, "source", None) or "redmine"),
    )


def cmd_workflow_dispatch_ir(args: argparse.Namespace) -> int:
    """Write the canonical marker-bearing IR journal, resolve its anchor, and EXECUTE the handoff.

    Exit codes: 0 on a dry-run preview or a delivered ``--execute`` dispatch (fresh or idempotently
    recovered); 2 on a fail-closed ``--execute`` — missing route identity, write / readback failure,
    an unresolved / ambiguous anchor, or a handoff that did not deliver (never a guessed anchor,
    never a silent no-op).
    """
    issue = str(getattr(args, "issue", "") or "").strip()
    lane = str(getattr(args, "lane", "") or "").strip()
    generation = getattr(args, "generation", None)
    body = _dispatch_body(args)

    if not getattr(args, "execute", False):
        note = render_dispatch_note(body, lane=lane, lane_generation=generation)
        print("dispatch-ir (dry-run: no Redmine write, no handoff send). Marker-bearing IR journal body:")
        print(note)
        print(
            "\nRun with --execute to write this journal, resolve the anchor, and SEND the live handoff."
        )
        return 0

    route = _route_from_args(args, lane)
    vocab = build_live_vocabulary()
    post_note, read_entries = build_live_ir_dispatch()
    handoff_send = build_live_handoff_send(issue=issue, route=route)

    result = dispatch_implementation_request(
        issue=issue,
        lane=lane,
        lane_generation=generation,
        body=body,
        route=route,
        vocab=vocab,
        post_note=post_note,
        read_entries=read_entries,
        handoff_send=handoff_send,
    )
    if result.status not in DISPATCH_SENDABLE or not result.sendable:
        print(
            f"mozyo-bridge workflow dispatch-ir: dispatch not delivered ({result.status}): "
            f"{result.detail}",
            file=sys.stderr,
        )
        return 2
    print(f"status: {result.status}")
    print(f"dispatch_journal: {result.dispatch_journal}")
    print(f"handoff_delivered: {result.handoff_delivered}")
    return 0


def register_dispatch_ir(workflow_sub) -> None:
    """Register ``workflow dispatch-ir`` onto the ``workflow`` subparser (Redmine #13758 R6-F4)."""
    p = workflow_sub.add_parser(
        "dispatch-ir",
        description=(
            "Canonical Implementation Request writer (Redmine #13758). Writes the IR journal with "
            "the durable dispatch marker embedded, reads back the marker's owning journal id, and — "
            "with --execute — SENDS the gated `handoff send --journal <anchor>` live to the worker "
            "agent, so the reconciler can correlate the Herdr turn edge against the exact dispatch "
            "anchor. Default is a dry-run preview (no Redmine write, no handoff send). --execute "
            "performs SIDE EFFECTS: the Redmine journal write AND the live agent handoff. A missing/"
            "invalid route, a write / readback failure, an unresolved / ambiguous anchor, or a "
            "non-delivered handoff all fail closed (exit 2)."
        ),
        help=(
            "Write the canonical marker-bearing Implementation Request journal and SEND its anchored "
            "live handoff to the worker (dry-run preview by default; --execute performs the side effects)."
        ),
    )
    p.add_argument("--issue", required=True, help="The Redmine issue id the IR journal is written on.")
    p.add_argument("--lane", required=True, help="The lane id the dispatch marker names.")
    p.add_argument(
        "--generation", required=True,
        help="The lane generation the dispatch marker names (distinguishes same-lane re-dispatch).",
    )
    body = p.add_mutually_exclusive_group()
    body.add_argument("--body", help="The IR prose body (the marker is appended to it).")
    body.add_argument("--body-file", dest="body_file", help="Read the IR prose body from a file.")
    p.add_argument(
        "--target",
        help="The worker target for the handoff (REQUIRED for --execute; a missing value fails "
             "closed as input_invalid before any write).",
    )
    p.add_argument(
        "--target-repo", dest="target_repo",
        help="The --target-repo identity gate value (REQUIRED for --execute; a missing value fails "
             "closed as input_invalid before any write).",
    )
    p.add_argument(
        "--gateway-callback-target", dest="gateway_callback_target",
        help="The same-lane gateway callback target the implementation_worker role profile requires "
             "(REQUIRED for --execute; an unresolved placeholder fails closed before any write).",
    )
    p.add_argument(
        "--role-profile", dest="role_profile", default=DISPATCH_ROLE_PROFILE,
        help=f"The handoff role profile (--execute; default {DISPATCH_ROLE_PROFILE}).",
    )
    p.add_argument("--source", default="redmine", help="The handoff --source (--execute; default redmine).")
    p.add_argument("--to", default="claude", help="The handoff --to receiver (--execute; default claude).")
    p.add_argument(
        "--execute", action="store_true",
        help="SIDE EFFECTS: write the IR journal, resolve the anchor, and SEND the live agent handoff "
             "(default: dry-run preview, no write, no send).",
    )
    p.set_defaults(func=cmd_workflow_dispatch_ir)


__all__ = ("cmd_workflow_dispatch_ir", "register_dispatch_ir")
