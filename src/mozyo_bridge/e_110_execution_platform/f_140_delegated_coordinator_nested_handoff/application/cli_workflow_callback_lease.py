"""``workflow callback-lease``: operator surface for the callback-sweep attempt lease (#13889).

Split out of :mod:`...cli_workflow` to keep that module under the health gate; the command is the
lease's counterpart to the sibling ``dispatch-fence`` / ``forward-fence`` surfaces.
"""

from __future__ import annotations

import argparse


def cmd_workflow_callback_lease(args: argparse.Namespace) -> int:
    """Operator surface for the callback-sweep attempt lease (Redmine #13889 R8-F3).

    The sweep's ``--execute`` path never auto-creates or auto-recovers this store: a silent
    re-create hands a second live owner the same anchor while the first is still working. So a
    store loss leaves the command fail-closed, and this is the sanctioned way out — the same
    contract the sibling ``dispatch-fence`` / ``forward-fence`` surfaces provide.

    ``--bootstrap`` is a safe first init (both DB + sidecar absent). ``--recover`` is a deliberate
    loss recovery: a fresh store under a new nonce, which INVALIDATES every outstanding grant —
    invoke ONLY after confirming no sweep is mid-attempt. With no flag, reports status.
    """
    from mozyo_bridge.core.state.callback_sweep_lease import (
        CallbackSweepLease,
        CallbackSweepLeaseError,
    )

    lease = CallbackSweepLease()
    try:
        if getattr(args, "lease_recover", False):
            lease.recover()
            print(f"callback sweep lease recovered (fresh store) at {lease.path}")
            print("every outstanding grant is now invalid; confirm no sweep was mid-attempt")
            return 0
        if getattr(args, "lease_bootstrap", False):
            lease.bootstrap()
            print(f"callback sweep lease bootstrapped at {lease.path}")
            return 0
    except CallbackSweepLeaseError as exc:
        print(f"callback sweep lease error: {exc}")
        print("a store loss/replacement needs `workflow callback-lease --recover`")
        return 1
    state = "bootstrapped" if lease.is_bootstrapped() else "absent / not bootstrapped"
    print(f"callback sweep lease: {state} at {lease.path}")
    return 0
