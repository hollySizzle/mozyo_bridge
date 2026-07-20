"""``workflow callback-lease``: operator surface for the callback-sweep attempt lease (#13889).

Split out of :mod:`...cli_workflow` to keep that module under the health gate; the command is the
lease's counterpart to the sibling ``dispatch-fence`` / ``forward-fence`` surfaces.

Redmine #13951 turned the blunt ``--recover`` (an unconditional re-mint) into a read-only typed
status plus a **backup-first, identity-bound, dry-run-default** recovery. Status classifies the
DB/sidecar pair into one typed state and prints the artifact fingerprint; ``--recover`` alone is a
dry-run that reports what an apply would do; ``--recover --apply --expect-fingerprint <token>``
actuates only when the store is a recoverable clean loss and the quoted fingerprint still matches.
A live owner / an unreadable store / a concurrent mutation is zero-write / zero-send.
"""

from __future__ import annotations

import argparse


def _print_diagnosis(diag) -> None:
    """Print the redaction-safe typed status (no owner token, no raw row; path is a basename)."""
    print(f"callback sweep lease: {diag.state}")
    print(f"  reason: {diag.reason}")
    print(
        f"  db_present={diag.db_present} sidecar_present={diag.sidecar_present} "
        f"readable={diag.readable} nonce_matches={diag.nonce_matches}"
    )
    print(
        f"  live_lease_count={diag.live_lease_count} has_live_owner={diag.has_live_owner} "
        f"recoverable={diag.recoverable}"
    )
    print(f"  fingerprint: {diag.fingerprint}")
    print(f"  -> {diag.recovery_action}")


def _print_recovery(outcome) -> None:
    print(f"callback sweep lease recovery: {outcome.status} (zero_write={outcome.zero_write})")
    print(f"  reason: {outcome.reason}")
    if outcome.backups:
        print(f"  backups: {', '.join(outcome.backups)}")
    if outcome.residue:
        # A rollback could not remove its own backup copies: name the residue so the operator can
        # clear it. This is NOT zero-write, and hiding it would misreport the store state (R2 #13951).
        print(f"  RESIDUE (remove by hand): {', '.join(outcome.residue)}")
    _print_diagnosis(outcome.diagnosis)


def cmd_workflow_callback_lease(args: argparse.Namespace) -> int:
    """Operator surface for the callback-sweep attempt lease (Redmine #13889 R8-F3 / #13951).

    The sweep's ``--execute`` path never auto-creates or auto-recovers this store: a silent
    re-create hands a second live owner the same anchor while the first is still working. So a
    store loss leaves the command fail-closed, and this is the sanctioned way out.

    - no flag  -> typed read-only **status** (the DB/sidecar pair, nonce agreement, live owner,
      recoverability, and the artifact fingerprint). Exit 0 only when healthy.
    - ``--bootstrap`` -> safe first init (both DB + sidecar absent; refuses on a detected loss).
    - ``--recover`` -> **dry-run** loss recovery: reports what an apply would do, writes nothing.
    - ``--recover --apply --expect-fingerprint <token>`` -> actuate. Backs the prior artifacts up
      first, then mints a fresh store under a new nonce — INVALIDATING every outstanding grant. A
      live owner / an unreadable store / a fingerprint that no longer matches is zero-write.
    """
    from mozyo_bridge.core.state.callback_sweep_lease import (
        RECOVERY_APPLIED,
        RECOVERY_PLANNED,
        CallbackSweepLease,
        CallbackSweepLeaseError,
    )

    lease = CallbackSweepLease()
    try:
        if getattr(args, "lease_recover", False):
            apply = bool(getattr(args, "lease_apply", False))
            expect = str(getattr(args, "lease_expect_fingerprint", "") or "").strip()
            if apply and not expect:
                print("callback sweep lease: --recover --apply requires --expect-fingerprint")
                print("run `workflow callback-lease --recover` first and pass its fingerprint")
                return 2
            outcome = lease.recover_guarded(expected_fingerprint=expect, apply=apply)
            _print_recovery(outcome)
            if outcome.status == RECOVERY_APPLIED:
                print("every outstanding grant is now invalid; confirm no sweep was mid-attempt")
                return 0
            # A dry-run that found a recoverable loss is a healthy exit (there is a way forward);
            # any refusal (live owner / unreadable / concurrent / healthy / absent) is non-zero.
            return 0 if outcome.status == RECOVERY_PLANNED else 1
        if getattr(args, "lease_bootstrap", False):
            lease.bootstrap()
            print(f"callback sweep lease bootstrapped at {lease.path}")
            return 0
    except CallbackSweepLeaseError as exc:
        print(f"callback sweep lease error: {exc}")
        print("a store loss/replacement needs `workflow callback-lease --recover` (dry-run first)")
        return 1
    diag = lease.diagnose()
    _print_diagnosis(diag)
    from mozyo_bridge.core.state.callback_sweep_lease import LEASE_HEALTHY

    return 0 if diag.state == LEASE_HEALTHY else 1
