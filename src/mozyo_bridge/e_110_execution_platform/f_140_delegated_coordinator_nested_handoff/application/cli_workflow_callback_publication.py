"""``workflow callback-publication``: operator surface for the sweep's record fence (#13889 R9-F1).

The fence deliberately has no automatic way out: a ``reserved`` row may be an owner mid-PUT or an
owner that died mid-PUT, and nothing local can tell those apart. Rather than guess — guessing is
what duplicated records — it stalls the anchor and waits for someone to read the actual Redmine
journal. That trade is only coherent if the operator has a way to act, which is this command.

But an operator surface is not an authority override, which is what R10-F1 caught this command
being: it could delete a live ``reserved`` row on the strength of "Redmine shows zero right now",
letting the stalled owner and its replacement both publish. The fence decides what each state
permits; this command only carries the operator's intent to it, and reports the refusal.

Split out of :mod:`...cli_workflow` to keep that module under the health gate; the shape follows the
sibling ``dispatch-fence`` / ``callback-lease`` surfaces — minus their ``--recover``, which this
fence cannot safely offer (R11-F1).
"""

from __future__ import annotations

import argparse


def cmd_workflow_callback_publication(args: argparse.Namespace) -> int:
    """Status / list / reconcile / first-init the callback-sweep publication fence (#13889).

    ``--list`` shows every anchor the fence is currently blocking. ``--reconcile`` is the operator
    disposition for one of them, taken AFTER reading the issue's journal: ``--landed <journal_id>``
    closes the anchor as published (no second record will ever be written), while ``--none-landed``
    releases the identity so a later sweep may publish it.

    The fence — not this command — decides which of those the current state permits, and refuses
    the rest with a non-zero exit (R10-F1). In particular ``--none-landed`` cannot release a
    ``reserved`` row: that state means an owner may be mid-PUT, and "Redmine shows zero right now"
    is not proof it will not PUT later. An operator surface that could override that would just be
    a hand-operated version of the reclaim this fence exists to refuse.

    ``--bootstrap`` initializes the fence on first use and adopts an existing unsealed store in
    place, keeping any reservation it already holds (R13-F1). What it never does is re-mint: once
    the store is sealed as operational, "store and sidecar both gone" is a diagnosed loss rather
    than a fresh start (R12-F1). There is deliberately no ``--recover`` counterpart to the
    sibling stores': forgetting a reservation is precisely how a record gets published twice, and no
    confirmation prompt can prove that a sweep suspended between its reserve and its PUT will not
    resume (R11-F1). A lost store stays fail-closed and must be restored, not re-minted.
    """
    from mozyo_bridge.core.state.callback_publication_fence import (
        SEAL_ABSENT,
        SEAL_INVALID,
        CallbackPublicationFence,
        CallbackPublicationFenceError,
        PublicationKey,
    )

    fence = CallbackPublicationFence()
    try:
        if getattr(args, "pub_bootstrap", False):
            fence.bootstrap()
            print(f"callback publication fence ready at {fence.path} (seal: {fence.seal_state()})")
            return 0
        stray = getattr(args, "pub_landed", None) or getattr(args, "pub_none_landed", False)
        if stray and not getattr(args, "pub_reconcile", None):
            print("--landed / --none-landed only mean something with --reconcile")
            return 2
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
            try:
                fence.reconcile(key, published_journal=landed or None)
            except CallbackPublicationFenceError as exc:
                # Deliberately NOT the store-loss hint: `--recover` forgets every reservation, so
                # suggesting it to someone who merely mistyped an anchor points them straight at
                # the duplicate this whole mechanism prevents.
                print(f"reconcile refused: {exc}")
                return 1
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
        print("this fence has no reset: forgetting a reservation is how a record gets published")
        print("twice. A lost store stays fail-closed until it is restored from backup.")
        return 1
    # Compare against the states by name. `seal_state() is not None` was always true once the
    # return type stopped being Optional, so every store reported as "seal present, store missing"
    # and the adoption advice below could never print (R15-F3) -- the diagnostic surface has to
    # name the actual next safe action, because bootstrap is the operator's job.
    seal = fence.seal_state()
    if fence.is_bootstrapped():
        state = "ready (sealed operational)"
    elif seal == SEAL_INVALID:
        state = "NOT ready: the seal exists but cannot be read — restore it; do not re-create"
    elif not fence.has_store():
        # No artifact at all: the only state where --bootstrap is a first init rather than a
        # decision about existing rows.
        state = (
            "absent / never initialized — run --bootstrap"
            if seal == SEAL_ABSENT
            else f"NOT ready: sealed `{seal}` but the store is GONE — a loss; restore it"
        )
    elif fence.has_usable_store():
        state = (
            "NOT ready: store exists but is unsealed — run --bootstrap to adopt it in place"
            if seal == SEAL_ABSENT
            else f"NOT ready: seal `{seal}` — run --bootstrap to adopt in place"
        )
    else:
        # Artifacts exist but do not work together. NOT a fresh install, and not adoptable: the DB
        # may still hold rows (R16-F1/F3), so the honest report is damage, not "never initialized".
        state = (
            "NOT ready: store artifacts are inconsistent (only one of DB / sidecar, mismatched "
            "nonce, or an unreadable DB). This is damage, not a fresh install — the DB may still "
            "hold a live reservation. Restore it, or remove BOTH artifacts once Redmine confirms "
            "nothing is in flight"
        )
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
            "one, taken after reading the issue journal in Redmine. `--bootstrap` is first init "
            "only: once this fence has run, a missing store is a detected loss, and it stays "
            "fail-closed until the store is restored — there is no reset, by design."
        ),
        help="List / reconcile / first-init the callback-sweep publication fence.",
    )
    # Exactly one action, and at most one disposition: an operator who types two intents at once
    # has not decided which they mean, and this command must never pick for them (R10-F1).
    action = pub_p.add_mutually_exclusive_group()
    action.add_argument(
        "--list", dest="pub_list", action="store_true",
        help="List every anchor the fence is currently blocking (reserved / uncertain).",
    )
    action.add_argument(
        "--reconcile", dest="pub_reconcile", nargs=6,
        metavar=("ISSUE", "GENERATION", "ANCHOR", "OUTCOME", "WORKSPACE", "LANE"),
        help="Dispose of one blocked anchor; needs --landed or --none-landed.",
    )
    action.add_argument(
        "--bootstrap", dest="pub_bootstrap", action="store_true",
        help="First init, or adopt an existing unsealed store in place; refuses on a loss.",
    )
    disposition = pub_p.add_mutually_exclusive_group()
    disposition.add_argument(
        "--landed", dest="pub_landed", metavar="JOURNAL_ID",
        help="The record DID land as this journal: close the anchor, never write a second.",
    )
    disposition.add_argument(
        "--none-landed", dest="pub_none_landed", action="store_true",
        help="No record landed (CONFIRM in Redmine): release an `uncertain` identity. The fence "
             "refuses this for a `reserved` row, whose owner may be mid-PUT.",
    )
    pub_p.set_defaults(func=cmd_workflow_callback_publication)
