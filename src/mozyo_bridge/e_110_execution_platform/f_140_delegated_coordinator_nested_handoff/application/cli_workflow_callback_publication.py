"""``workflow callback-publication``: operator surface for the sweep's record fence (#13889 R9-F1).

The fence deliberately has no automatic way out: a ``reserved`` row may be an owner mid-PUT or an
owner that died mid-PUT, and nothing local can tell those apart. Rather than guess — guessing is
what duplicated records — it stalls the anchor and waits for someone to read the actual Redmine
journal. That trade is only coherent if the operator has a way to act, which is this command.

Split out of :mod:`...cli_workflow` to keep that module under the health gate; the shape follows the
sibling ``dispatch-fence`` / ``callback-lease`` surfaces.
"""

from __future__ import annotations

import argparse


def cmd_workflow_callback_publication(args: argparse.Namespace) -> int:
    """Status / bootstrap / recover / reconcile the callback-sweep publication fence (#13889).

    ``--list`` shows every anchor the fence is currently blocking. ``--reconcile`` is the operator
    disposition for one of them, taken AFTER reading the issue's journal: ``--landed <journal_id>``
    closes the anchor as published (no second record will ever be written), while ``--none-landed``
    releases the identity so a later sweep may publish it. Passing ``--none-landed`` when a record
    did land is precisely how a duplicate gets created, so confirm in Redmine first.

    ``--bootstrap`` is a safe first init (DB + sidecar both absent); ``--recover`` is deliberate
    loss recovery under a fresh nonce, which forgets every reservation and so can republish — only
    after confirming no sweep is mid-attempt.
    """
    from mozyo_bridge.core.state.callback_publication_fence import (
        CallbackPublicationFence,
        CallbackPublicationFenceError,
        PublicationKey,
    )

    fence = CallbackPublicationFence()
    try:
        if getattr(args, "pub_recover", False):
            fence.recover()
            print(f"callback publication fence recovered (fresh store) at {fence.path}")
            print("every reservation is forgotten; a stalled anchor may now publish again")
            return 0
        if getattr(args, "pub_bootstrap", False):
            fence.bootstrap()
            print(f"callback publication fence bootstrapped at {fence.path}")
            return 0
        if getattr(args, "pub_reconcile", None):
            landed = getattr(args, "pub_landed", None)
            if not landed and not getattr(args, "pub_none_landed", False):
                print("--reconcile needs either --landed <journal_id> or --none-landed")
                print("read the issue journal first: only Redmine knows whether a record landed")
                return 2
            issue, generation, anchor, outcome, workspace, lane = args.pub_reconcile
            key = PublicationKey(
                workspace_id=workspace, lane_id=lane, issue=issue,
                lane_generation=generation, dispatch_anchor=anchor, outcome=outcome,
            )
            fence.reconcile(key, published_journal=landed or None)
            verdict = f"published as journal {landed}" if landed else "released for republication"
            print(f"reconciled {issue}/{anchor}/{outcome}: {verdict}")
            return 0
        if getattr(args, "pub_list", False):
            rows = fence.pending()
            if not rows:
                print("callback publication fence: no blocked anchors")
                return 0
            print(f"callback publication fence: {len(rows)} blocked anchor(s) at {fence.path}")
            for r in rows:
                print(
                    f"  [{r['state']}] issue={r['issue']} gen={r['lane_generation']} "
                    f"anchor={r['dispatch_anchor']} outcome={r['outcome']} "
                    f"lane={r['lane_id']} workspace={r['workspace_id']}"
                    + (f" detail={r['detail']}" if r["detail"] else "")
                )
            print("each needs `--reconcile` after reading the issue journal in Redmine")
            return 0
    except CallbackPublicationFenceError as exc:
        print(f"callback publication fence error: {exc}")
        print("a store loss/replacement needs `workflow callback-publication --recover`")
        return 1
    state = "bootstrapped" if fence.is_bootstrapped() else "absent / not bootstrapped"
    print(f"callback publication fence: {state} at {fence.path}")
    return 0


def register_callback_publication_parser(workflow_sub) -> None:
    """Register ``workflow callback-publication``; kept beside its command, not in the CLI hub."""
    pub_p = workflow_sub.add_parser(
        "callback-publication",
        description=(
            "Operator surface for the callback-sweep publication fence (Redmine #13889). The fence "
            "reserves each record identity and NEVER reclaims it: a lingering reservation may be an "
            "owner mid-PUT, so a crashed owner stalls its anchor instead of risking a duplicate "
            "record. `--list` shows blocked anchors; `--reconcile` is the operator disposition for "
            "one, taken after reading the issue journal in Redmine."
        ),
        help="List / reconcile / bootstrap / recover the callback-sweep publication fence.",
    )
    pub_p.add_argument(
        "--list", dest="pub_list", action="store_true",
        help="List every anchor the fence is currently blocking (reserved / uncertain).",
    )
    pub_p.add_argument(
        "--reconcile", dest="pub_reconcile", nargs=6,
        metavar=("ISSUE", "GENERATION", "ANCHOR", "OUTCOME", "WORKSPACE", "LANE"),
        help="Dispose of one blocked anchor; needs --landed or --none-landed.",
    )
    pub_p.add_argument(
        "--landed", dest="pub_landed", metavar="JOURNAL_ID",
        help="The record DID land as this journal: close the anchor, never write a second.",
    )
    pub_p.add_argument(
        "--none-landed", dest="pub_none_landed", action="store_true",
        help="No record landed (CONFIRM in Redmine first): release the identity for republication.",
    )
    pub_p.add_argument(
        "--bootstrap", dest="pub_bootstrap", action="store_true",
        help="Initialize the fence store (safe first init; refuses on a detected loss).",
    )
    pub_p.add_argument(
        "--recover", dest="pub_recover", action="store_true",
        help="Deliberate loss recovery: fresh store under a new nonce (forgets all reservations).",
    )
    pub_p.set_defaults(func=cmd_workflow_callback_publication)
