"""``workflow callback-admit``: the receiver's admission rail for one recovery action (#13910).

The public seam a receiver crosses **before its first state-changing effect** for a recovery it was
pointed at (design j#80984, authoritative per j#80986). It reads the durable record fresh, derives
the action's key from that record's structured marker, verifies the recovery is still warranted and
addressed here, and claims the key exactly once.

**The exit code is the contract.** ``0`` means, and only ever means, *admitted*. Every other
outcome — duplicate, superseded, conflict, unreadable — exits non-zero with its own code, so the
natural shell shape (``... && <effect>``) is fail-closed by construction. A single zero-for-success
convention would make "already actuated" and "go ahead" indistinguishable to a script, which is the
duplicate this command exists to prevent.

**What it does not do** (the j#80984 Disposition 3 / Option C boundary). It cannot stop a receiver
that never calls it: no sidecar exists, so bypass prevention is not code-enforceable here
(``vibes/docs/logics/ack-completion-receiver-state.md`` ``## Sidecar の位置づけ``). The obligation to
call it lives in the recovery record's own receiver contract. Nor does a claim mean the round
finished: admission is not completion, and this command never writes a workflow gate.

Split out of :mod:`.cli_workflow_callbacks` to keep that module under the health gate; the shape
follows the sibling ``callback-publication`` / ``dispatch-fence`` operator surfaces.
"""

from __future__ import annotations

import argparse
import json

#: Exit codes. ``0`` is admitted and nothing else, so `&&` chaining cannot actuate on a refusal.
EXIT_ADMITTED = 0
EXIT_DUPLICATE = 3
EXIT_SUPERSEDED = 4
EXIT_CONFLICT = 5
EXIT_UNREADABLE = 6


def _exit_code(outcome: str) -> int:
    """Map an admission outcome onto its dedicated exit code (fail-closed default)."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_recovery_admission import (  # noqa: E501
        ADMIT_ADMITTED,
        ADMIT_CONFLICT,
        ADMIT_DUPLICATE,
        ADMIT_SUPERSEDED,
    )

    return {
        ADMIT_ADMITTED: EXIT_ADMITTED,
        ADMIT_DUPLICATE: EXIT_DUPLICATE,
        ADMIT_SUPERSEDED: EXIT_SUPERSEDED,
        ADMIT_CONFLICT: EXIT_CONFLICT,
    }.get(outcome, EXIT_UNREADABLE)


def cmd_workflow_callback_admit(args: argparse.Namespace) -> int:
    """Admit (or refuse) one recovery action for this receiver.

    ``--bootstrap`` is the store's only creation surface and is deliberately separate from the
    admit path: the admit path never auto-creates the authority, because a store that materializes
    on demand would re-admit every recovery a deleted store had already recorded. There is no
    ``--recover`` counterpart — re-minting this store would free every claim it holds, which is the
    duplicate actuation it exists to refuse. A lost store is restored, not re-created.
    """
    from mozyo_bridge.core.state.callback_recovery_receipt import (
        CallbackRecoveryReceipt,
        CallbackRecoveryReceiptError,
    )

    receipt = CallbackRecoveryReceipt(home=None)
    if getattr(args, "bootstrap", False):
        try:
            receipt.bootstrap()
        except CallbackRecoveryReceiptError as exc:
            print(f"callback recovery receipt bootstrap refused: {exc}")
            return EXIT_UNREADABLE
        print(f"callback recovery receipt store ready: {receipt.path}")
        return EXIT_ADMITTED

    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_recovery_admission import (  # noqa: E501
        admit_recovery,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
        LiveRedmineJournalSource,
    )

    # The workspace partition is MEASURED from the canonical registry authority, never supplied:
    # this is the same single measurement `sublane_diagnostics` uses for the send fence's partition
    # (its R3-F2). Re-deriving it here would be a second implementation free to drift back into the
    # defect that docstring documents, so the one authority is reused rather than copied.
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_diagnostics import (  # noqa: E501
        attested_workspace_id,
    )

    issue = str(getattr(args, "issue", "") or "").strip()
    journal = str(getattr(args, "journal", "") or "").strip()
    route = str(getattr(args, "route", "") or "").strip()
    receiver = str(getattr(args, "receiver", "") or "").strip()
    if not (issue and journal and route and receiver):
        raise SystemExit(
            "callback-admit requires --issue, --journal, --route and --receiver: the admission key "
            "names the exact action, the exact round, and the exact receiver — an under-specified "
            "admission cannot prove which recovery it is authorizing"
        )

    workspace_id = attested_workspace_id(args)
    if not workspace_id:
        print(
            json.dumps(
                {
                    "outcome": "unreadable",
                    "may_actuate": False,
                    "detail": (
                        "no attested workspace id could be measured from the canonical registry; "
                        "the admission key is workspace-partitioned, so an unmeasured partition "
                        "would admit against a different row. Zero-actuation"
                    ),
                },
                ensure_ascii=False,
            )
        )
        return EXIT_UNREADABLE

    # A live source is required: `admit_recovery` refuses a snapshot, because a frozen read cannot
    # show a gate that landed after it was taken.
    try:
        source = LiveRedmineJournalSource.from_environment()
    except Exception as exc:  # noqa: BLE001 - unconfigured live read -> no admission
        print(
            json.dumps(
                {
                    "outcome": "unreadable",
                    "may_actuate": False,
                    "detail": (
                        f"callback-admit needs a live Redmine read boundary "
                        f"({type(exc).__name__}: {exc}); without it the recovery cannot be "
                        f"verified as still warranted"
                    ),
                },
                ensure_ascii=False,
            )
        )
        return EXIT_UNREADABLE

    outcome = admit_recovery(
        source=source,
        issue=issue,
        recovery_action_journal=journal,
        workspace_id=workspace_id,
        route_identity=route,
        receiver_identity=receiver,
        receipt=receipt,
    )
    print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2))
    return _exit_code(outcome.outcome)


def register_callback_admit(workflow_sub) -> None:
    """Register ``workflow callback-admit`` on the workflow subparser."""
    p = workflow_sub.add_parser(
        "callback-admit",
        description=(
            "Admit one callback-recovery action before performing its first state-changing effect "
            "(Redmine #13910). Reads the durable recovery record live, derives the action's "
            "versioned idempotency key from that record's structured marker (never from pane "
            "text), refuses it when the stall is no longer provable or the delivery is addressed "
            "elsewhere, and claims the key exactly once. Exit 0 means ADMITTED and nothing else: "
            "3=duplicate, 4=superseded, 5=conflict, 6=unreadable. Admission is not completion — "
            "record the round's outcome as a durable Redmine gate. `--bootstrap` initializes the "
            "store on first use; the admit path never auto-creates it, and there is deliberately "
            "no --recover (re-minting would re-admit already-actuated recoveries)."
        ),
        help="Admit a callback-recovery action once, before its first effect.",
    )
    p.add_argument("--issue", help="The Redmine issue the recovery record lives on.")
    p.add_argument(
        "--journal",
        help=(
            "The recovery record's journal id (the durable anchor the handoff pointed at). Its "
            "OWNING entry id is the key's authority; a self-reported id is never trusted."
        ),
    )
    p.add_argument(
        "--route",
        help=(
            "This receiver's assigned name. Compared against the record: a delivery addressed "
            "elsewhere is a conflict, not an admission."
        ),
    )
    p.add_argument(
        "--receiver",
        help="This receiver's semantic role (the record names the role allowed to admit).",
    )
    p.add_argument(
        "--workspace-id",
        default="",
        help=(
            "Equality assertion only. The partition is measured from the canonical registry; this "
            "can confirm what was measured but never supply it."
        ),
    )
    p.add_argument(
        "--bootstrap",
        action="store_true",
        help="Initialize the admission store (first use only; never re-mints a lost store).",
    )
    p.set_defaults(func=cmd_workflow_callback_admit)


__all__ = (
    "EXIT_ADMITTED",
    "EXIT_DUPLICATE",
    "EXIT_SUPERSEDED",
    "EXIT_CONFLICT",
    "EXIT_UNREADABLE",
    "cmd_workflow_callback_admit",
    "register_callback_admit",
)
