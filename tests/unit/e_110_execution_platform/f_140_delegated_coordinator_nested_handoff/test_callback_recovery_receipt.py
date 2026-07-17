"""The admission authority's lifecycle and retry-chain rules (#13910, review j#81021 F1 / F2).

Both blockers this file fixes were invisible to the first cut's tests because those tests only ever
drove the store the way the author intended it to be driven: bootstrap once, claim, replay. Neither
of the two questions that actually matter — *what happens when the store is gone?* and *what happens
when the same recovery shows up under a different journal id?* — was ever asked.
"""

import tempfile
import threading
import unittest
from pathlib import Path

from mozyo_bridge.core.state.callback_recovery_receipt import (
    CallbackRecoveryReceipt,
    CallbackRecoveryReceiptError,
    RECEIPT_ABSENT,
    RECEIPT_CLAIMED,
    SEAL_ABSENT,
    SEAL_INITIALIZING,
    SEAL_INVALID,
    SEAL_OPERATIONAL,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_recovery_key import (
    RETRY_OF_NONE,
    RecoveryAdmissionKey,
)

_ACTION = dict(
    original_dispatch_anchor="79990",
    workspace_id="ws-1",
    lane_id="lane_a",
    lane_generation="1",
    route_identity="claude-worker-1",
    receiver_identity="codex",
    action_kind="callback_sweep_recovery",
)


def key(journal="80500", **overrides):
    return RecoveryAdmissionKey(
        recovery_action_journal=journal, **{**_ACTION, **overrides}
    )


class _ReceiptBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.receipt = CallbackRecoveryReceipt(home=self.home)
        self.receipt.bootstrap()


class SealLifecycleTests(_ReceiptBase):
    """Review j#81021 F1: a store cannot tell a fresh install from a total loss by itself."""

    def test_a_total_loss_is_never_re_minted(self):
        """THE F1 regression: deleting the pair and bootstrapping re-admitted everything.

        The first cut's `bootstrap()` treated "DB and sidecar both gone" as a genuine first init,
        so a claimed key came straight back as `won=True, prior_state=absent`.
        """
        k = key()
        self.assertTrue(self.receipt.claim(k).won)
        self.assertEqual(self.receipt.seal_state(), SEAL_OPERATIONAL)

        self.receipt.path.unlink()
        self.receipt.sidecar_path.unlink()

        with self.assertRaises(CallbackRecoveryReceiptError) as ctx:
            CallbackRecoveryReceipt(home=self.home).bootstrap()
        self.assertIn("loss", str(ctx.exception).lower())
        # The refusal is what matters, not its wording: the claim path stays closed too, so the
        # already-actuated recovery cannot come back however the caller approaches the store.
        with self.assertRaises(CallbackRecoveryReceiptError):
            CallbackRecoveryReceipt(home=self.home).claim(k)
        self.assertFalse(CallbackRecoveryReceipt(home=self.home).is_bootstrapped())

    def test_bootstrap_is_idempotent_and_adopts_in_place(self):
        """A second bootstrap must not disturb the rows the first one's store already holds."""
        k = key()
        self.assertTrue(self.receipt.claim(k).won)
        CallbackRecoveryReceipt(home=self.home).bootstrap()
        self.assertEqual(CallbackRecoveryReceipt(home=self.home).peek(k), RECEIPT_CLAIMED)

    def test_seal_is_written_before_the_store(self):
        """The crash window must land on `initializing`, never on an unsealed operational store.

        A store that could grant but has no seal is undiagnosable: the loss branch has nothing to
        read. Sealing first makes the only reachable crash state one that is safe to re-mint.
        """
        home = Path(tempfile.mkdtemp())
        r = CallbackRecoveryReceipt(home=home)
        written = []
        original = r._create_fresh

        def spy(nonce):
            written.append(r.seal_state())   # what the seal says at the moment the store is minted
            return original(nonce)

        r._create_fresh = spy
        r.bootstrap()
        self.assertEqual(
            written, [SEAL_INITIALIZING],
            "the store was minted before its seal said 'initializing': a crash here would leave an "
            "operational store the loss branch can never recognize",
        )
        self.assertEqual(r.seal_state(), SEAL_OPERATIONAL)

    def test_an_initializing_seal_without_a_store_is_re_mintable(self):
        """A crashed initializer never wedges the lifecycle: it granted nothing."""
        home = Path(tempfile.mkdtemp())
        r = CallbackRecoveryReceipt(home=home)
        r._write_seal(SEAL_INITIALIZING)     # a bootstrap that died before minting
        r.bootstrap()
        self.assertEqual(r.seal_state(), SEAL_OPERATIONAL)
        self.assertTrue(r.is_bootstrapped())

    def test_an_invalid_seal_is_never_re_minted(self):
        """'Something is here and I cannot read it' is not 'nothing ever ran here'."""
        home = Path(tempfile.mkdtemp())
        r = CallbackRecoveryReceipt(home=home)
        r.seal_path.parent.mkdir(parents=True, exist_ok=True)
        r.seal_path.write_text("garbage from some other tool\n", encoding="utf-8")
        self.assertEqual(r.seal_state(), SEAL_INVALID)
        with self.assertRaises(CallbackRecoveryReceiptError):
            r.bootstrap()

    def test_a_torn_store_is_not_empty(self):
        """Unusable is not empty: the DB still holds whatever was claimed through it."""
        self.assertTrue(self.receipt.claim(key()).won)
        self.receipt.sidecar_path.unlink()      # torn: DB survives, identity does not
        self.assertTrue(self.receipt.has_store(), "the DB is still here and still holds the claim")
        self.assertFalse(self.receipt.is_bootstrapped())
        with self.assertRaises(CallbackRecoveryReceiptError):
            CallbackRecoveryReceipt(home=self.home).bootstrap()
        # The DB was NOT unlinked by the refusal: a torn store is restored, not re-minted over.
        self.assertTrue(self.receipt.path.exists())

    def test_an_unsealed_store_cannot_grant(self):
        """Readiness includes the seal: a check at the door is not a guard on the safe."""
        self.receipt.seal_path.unlink()
        self.assertEqual(self.receipt.seal_state(), SEAL_ABSENT)
        self.assertFalse(self.receipt.is_bootstrapped())
        with self.assertRaises(CallbackRecoveryReceiptError):
            self.receipt.claim(key())

    def test_concurrent_bootstrap_is_serialized(self):
        """F1b: eight bootstraps produced two winners and six raw sqlite errors.

        Raw `OperationalError` is not just untidy — it is fail-OPEN in shape: the caller never sees
        a domain error it can read as "do not actuate".
        """
        home = Path(tempfile.mkdtemp())
        errors, wins = [], []
        lock = threading.Lock()
        racers = 8
        barrier = threading.Barrier(racers)

        def run():
            barrier.wait(timeout=10)
            try:
                CallbackRecoveryReceipt(home=home).bootstrap()
                with lock:
                    wins.append(1)
            except Exception as exc:  # noqa: BLE001 - the point is WHAT leaks out
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=run) for _ in range(racers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive(), "a bootstrap thread hung on the lifecycle lock")
        self.assertEqual(errors, [], "concurrent bootstrap must not raise (least of all raw sqlite)")
        self.assertEqual(len(wins), racers)
        self.assertEqual(CallbackRecoveryReceipt(home=home).seal_state(), SEAL_OPERATIONAL)


class RetryChainTests(_ReceiptBase):
    """Review j#81021 F2: a new journal id is not authorization."""

    def test_the_same_action_at_a_new_journal_is_refused(self):
        """THE F2 regression: a copied note actuated the same recovery a second time."""
        first = key("80500")
        self.assertTrue(self.receipt.claim(first).won)

        copied = key("80900")                       # identical action, different journal, no retry_of
        self.assertEqual(copied.action_digest(), first.action_digest())
        self.assertNotEqual(copied.digest(), first.digest())

        result = self.receipt.claim(copied)
        self.assertFalse(result.won, "a duplicate publication actuated the same recovery twice")
        self.assertTrue(result.conflict)
        self.assertIn("retry_of", result.detail)

    def test_an_authorized_retry_is_admitted(self):
        """The liveness path must exist: safety that forbids every retry is a dead end."""
        first = key("80500")
        self.assertTrue(self.receipt.claim(first).won)
        retry = key("81000", retry_of=first.digest())
        self.assertTrue(self.receipt.claim(retry).won, "an explicit, correctly-linked retry")

    def test_a_retry_must_name_the_LATEST_key(self):
        """A stale linkage cannot be replayed: each retry continues the chain from its tip."""
        first = key("80500")
        self.receipt.claim(first)
        retry = key("81000", retry_of=first.digest())
        self.receipt.claim(retry)

        stale = key("81100", retry_of=first.digest())   # points at the head, not the tip
        result = self.receipt.claim(stale)
        self.assertFalse(result.won)
        self.assertTrue(result.conflict)

        chained = key("81200", retry_of=retry.digest())  # continues from the tip
        self.assertTrue(self.receipt.claim(chained).won)

    def test_a_retry_of_an_unknown_key_is_refused(self):
        """A fabricated linkage names nothing this authority admitted."""
        self.receipt.claim(key("80500"))
        forged = key("80900", retry_of="f" * 64)
        result = self.receipt.claim(forged)
        self.assertFalse(result.won)
        self.assertTrue(result.conflict)

    def test_a_retry_is_still_replay_protected(self):
        """A retry key is a key: presenting it twice is a duplicate, not a second round."""
        first = key("80500")
        self.receipt.claim(first)
        retry = key("81000", retry_of=first.digest())
        self.assertTrue(self.receipt.claim(retry).won)
        again = self.receipt.claim(retry)
        self.assertFalse(again.won)
        self.assertFalse(again.conflict, "an exact replay is a duplicate, not a conflict")

    def test_a_different_action_is_unaffected_by_the_chain_rule(self):
        """The action index must not turn unrelated recoveries into conflicts."""
        self.receipt.claim(key("80500"))
        other = key("80900", original_dispatch_anchor="88888")   # a different round entirely
        self.assertNotEqual(other.action_digest(), key("80500").action_digest())
        self.assertTrue(self.receipt.claim(other).won)

    def test_first_claim_of_an_action_needs_no_retry_of(self):
        self.assertEqual(key().retry_of, RETRY_OF_NONE)
        self.assertTrue(self.receipt.claim(key()).won)
        self.assertEqual(self.receipt.peek(key()), RECEIPT_CLAIMED)
        self.assertEqual(self.receipt.peek(key("99999")), RECEIPT_ABSENT)


if __name__ == "__main__":
    unittest.main()
