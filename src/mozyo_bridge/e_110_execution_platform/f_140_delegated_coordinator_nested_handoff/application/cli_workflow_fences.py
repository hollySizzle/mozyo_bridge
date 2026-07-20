"""Operator-fence subparser registration for the ``workflow`` group.

Extracted from ``cli_workflow`` to keep that composition-root module under the
module-health ceiling (Redmine #13967). These are the operator surfaces for the durable
idempotency stores: the worker-dispatch idempotency fence (#13489), the callback-sweep
attempt lease (#13889), the callback publication fence, and the herdr coordinator-forward
generation store (#13583). Each is a ``--bootstrap`` / ``--recover`` / status parser; the
command handlers stay in ``cli_workflow`` and are bound here by a function-level import so
this module never imports ``cli_workflow`` at load time (no import cycle).
"""

from __future__ import annotations


def register_fence_operator_parsers(workflow_sub) -> None:
    """Register the workflow fence/lease operator subparsers onto ``workflow_sub``."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow import (  # noqa: E501
        cmd_workflow_callback_lease,
        cmd_workflow_dispatch_fence,
        cmd_workflow_forward_fence,
        register_callback_publication_parser,
    )

    fence_p = workflow_sub.add_parser(
        "dispatch-fence",
        description=(
            "Operator surface for the increment-2 worker-dispatch idempotency fence "
            "(Redmine #13489). `--bootstrap` initializes it; `--recover` mints a fresh store "
            "after a loss (only after reconciling the lost action + issuing a new action_id "
            "upstream); no flag reports status. The reserve path never auto-creates the store."
        ),
        help="Bootstrap / recover / status the worker-dispatch idempotency fence.",
    )
    fence_p.add_argument(
        "--bootstrap", dest="fence_bootstrap", action="store_true",
        help="Initialize the fence store (safe first init; refuses on a detected loss).",
    )
    fence_p.add_argument(
        "--recover", dest="fence_recover", action="store_true",
        help="Deliberate loss recovery: mint a fresh store under a new nonce.",
    )
    fence_p.set_defaults(func=cmd_workflow_dispatch_fence)

    lease_p = workflow_sub.add_parser(
        "callback-lease",
        description=(
            "Operator surface for the callback-sweep attempt lease (Redmine #13889 / #13951). "
            "No flag reports a typed read-only status (DB/sidecar pair, nonce, live owner, "
            "recoverability, artifact fingerprint). `--bootstrap` is a safe first init. `--recover` "
            "is a DRY-RUN loss recovery; add `--apply --expect-fingerprint <token>` to actuate, "
            "which backs up the prior artifacts first and mints a fresh store under a new nonce "
            "(invalidating every outstanding grant). A live owner / an unreadable store / a "
            "concurrent mutation is zero-write, except that a concurrent mutation caught mid-backup "
            "whose backup copies cannot be removed reports a typed `rollback_incomplete` with the "
            "residue named and `zero_write=False`. `sublane callback-recovery --execute` never "
            "auto-creates or auto-recovers the store: a silent re-create would hand a second live "
            "owner the same anchor."
        ),
        help="Status / bootstrap / gated recover the callback-sweep attempt lease.",
    )
    lease_p.add_argument(
        "--bootstrap", dest="lease_bootstrap", action="store_true",
        help="Initialize the lease store (safe first init; refuses on a detected loss).",
    )
    lease_p.add_argument(
        "--recover", dest="lease_recover", action="store_true",
        help="Gated loss recovery (DRY-RUN by default; add --apply to actuate).",
    )
    lease_p.add_argument(
        "--apply", dest="lease_apply", action="store_true",
        help="With --recover: actuate the mint (needs --expect-fingerprint from a prior status).",
    )
    lease_p.add_argument(
        "--expect-fingerprint", dest="lease_expect_fingerprint", metavar="TOKEN", default="",
        help="Bind --recover --apply to the fingerprint a prior status reported; a mismatch is "
             "a concurrent mutation and zero-writes.",
    )
    lease_p.set_defaults(func=cmd_workflow_callback_lease)

    register_callback_publication_parser(workflow_sub)

    forward_fence_p = workflow_sub.add_parser(
        "forward-fence",
        description=(
            "Operator surface for the herdr coordinator-forward generation store (Redmine #13583). "
            "`--bootstrap` initializes it; `--recover` mints a fresh store after a loss (only after "
            "reconciling the lost forward); no flag reports status. The `workflow step` execution "
            "path never auto-creates the store (a loss must not resurrect a delivered forward)."
        ),
        help="Bootstrap / recover / status the herdr forward generation store.",
    )
    forward_fence_p.add_argument(
        "--bootstrap", dest="fence_bootstrap", action="store_true",
        help="Initialize the forward store (safe first init; refuses on a detected loss).",
    )
    forward_fence_p.add_argument(
        "--recover", dest="fence_recover", action="store_true",
        help="Deliberate loss recovery: mint a fresh forward store under a new nonce.",
    )
    forward_fence_p.set_defaults(func=cmd_workflow_forward_fence)


__all__ = ("register_fence_operator_parsers",)
