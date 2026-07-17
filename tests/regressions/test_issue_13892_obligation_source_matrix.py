"""Redmine #13892 — the durable obligation source matrix, pinned against ROW semantics.

Review j#80594 R4-F3 rejected the first version of this file: it asserted that
`CallbackOutboxKey` has no assigned-name field — true, and irrelevant, because the **row**
carries `target_lane` / `target_receiver` and `BackendNeutralTargetResolver` rebuilds the
canonical `pane_name` from them. A test that checks the key while the row is what names the
target is vacuous: it can never fail for the reason it claims to guard.

So each entry here is pinned by the fact that actually decides it:

- **covered** — a probe shows a scratch pair's slot really can appear, and the reader really
  returns it. If a covered source stopped being read, the reader test fails.
- **structurally-inapplicable** — the *precondition* that makes it impossible is asserted, so
  the test fails if that precondition ever changes (e.g. a store gains a target column).

A scratch pair is identified ONLY by its assigned name `mzb1_<ws>_<role>_<lane>`. It has no
lane lifecycle record, hence no `lane_generation`, and no Redmine issue binding.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mozyo_bridge.core.state.dispatch_outbox_fence import DispatchOutboxFence, FenceKey
from mozyo_bridge.core.state.forward_outbox_fence import ForwardOutboxFence, ForwardRouteKey
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.scratch_pair_obligations import (  # noqa: E501
    OWED,
    UNCORRELATED,
    ObligationStoreUnreadable,
    dispatch_outbox_obligations,
    forward_generation_obligations,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

WS, LANE, ROLE = "wsabc", "dogfood13892", "claude"
NAME = encode_assigned_name(WS, ROLE, LANE)


class _Home(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.home = Path(d)
        for mod in (
            "mozyo_bridge.core.state.dispatch_outbox_fence",
            "mozyo_bridge.core.state.forward_outbox_fence",
        ):
            patcher = mock.patch(f"{mod}.mozyo_bridge_home", return_value=self.home)
            patcher.start()
            self.addCleanup(patcher.stop)


# ---------------------------------------------------------------- covered ---


class DispatchOutboxCoveredTest(_Home):
    """COVERED: `target_assigned_name` is a key column, so a scratch slot can be named."""

    def test_a_scratch_slot_really_appears_and_is_returned_as_owed(self):
        f = DispatchOutboxFence(home=self.home)
        f.bootstrap()
        f.reserve(FenceKey(WS, LANE, "13999", "42", "act1", NAME))
        found = dispatch_outbox_obligations(workspace_id=WS, assigned_names=(NAME,))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].verdict, OWED)
        self.assertEqual(found[0].target, NAME)

    def test_an_empty_anchor_is_accepted_so_the_no_issue_argument_never_applied(self):
        """The key's issue/journal accept '' — "a scratch pair has no issue" would NOT have
        made this store inapplicable. It is covered by reading, not by argument."""
        f = DispatchOutboxFence(home=self.home)
        f.bootstrap()
        f.reserve(FenceKey(WS, LANE, "", "", "act1", NAME))
        self.assertEqual(
            len(dispatch_outbox_obligations(workspace_id=WS, assigned_names=(NAME,))), 1
        )

    def test_an_unreadable_store_raises_rather_than_reporting_none_owed(self):
        f = DispatchOutboxFence(home=self.home)
        f.bootstrap()
        f.sidecar_path.write_text("deadbeef")
        with self.assertRaises(ObligationStoreUnreadable):
            dispatch_outbox_obligations(workspace_id=WS, assigned_names=(NAME,))

    def test_delivered_is_correlated_not_assumed(self):
        f = DispatchOutboxFence(home=self.home)
        f.bootstrap()
        key = FenceKey(WS, LANE, "13999", "42", "act1", NAME)
        f.reserve(key)
        f.mark_delivered(key)
        # no correlator -> unknown -> blocks (a delivery ACK is not completion)
        found = dispatch_outbox_obligations(workspace_id=WS, assigned_names=(NAME,))
        self.assertEqual([o.verdict for o in found], [UNCORRELATED])
        # positively discharged -> does not block
        found = dispatch_outbox_obligations(
            workspace_id=WS, assigned_names=(NAME,), correlate=lambda i, j: True
        )
        self.assertEqual(found, ())
        # positively not finished -> owed
        found = dispatch_outbox_obligations(
            workspace_id=WS, assigned_names=(NAME,), correlate=lambda i, j: False
        )
        self.assertEqual([o.verdict for o in found], [OWED])


class CallbackOutboxCoveredTest(unittest.TestCase):
    """COVERED — the row, not the key, names the target (review j#80594 R4-F3).

    The prior entry called this inapplicable because `CallbackOutboxKey` has no name field.
    That is true and irrelevant: `BackendNeutralTargetResolver` rebuilds the canonical
    `pane_name` as `encode_assigned_name(workspace_id, target_receiver, target_lane)` from the
    ROW's durable columns, so a callback can be owed to a scratch slot.
    """

    def test_the_row_carries_the_target_identity_the_resolver_rebuilds_from(self):
        from mozyo_bridge.core.state.callback_outbox import CallbackOutboxRow

        fields = set(CallbackOutboxRow.__dataclass_fields__)
        for f in ("target_lane", "target_receiver", "target_generation"):
            self.assertIn(f, fields, "the row is what names the target; the key is not")

    def test_the_resolver_rebuilds_a_canonical_assigned_name_from_those_columns(self):
        import inspect

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            background_service_sender as bss,
        )

        src = inspect.getsource(bss.BackendNeutralTargetResolver)
        self.assertIn("target_receiver", src)
        self.assertIn("target_lane", src)
        self.assertIn(
            "encode_assigned_name", src,
            "if this stops rebuilding an assigned name, re-derive the matrix entry",
        )

    def test_the_reader_reads_this_source(self):
        import inspect

        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            scratch_pair_obligations as spo,
        )

        self.assertIn("CallbackOutbox", inspect.getsource(spo.callback_outbox_obligations))
        self.assertIn(
            "callback_outbox_obligations", inspect.getsource(spo.all_pair_obligations)
        )


class ForwardFenceCoveredForOwedFromTest(_Home):
    """COVERED for **owed FROM** (review j#80594 R4-F3).

    The first cut excluded this because the key carries no *target* name. But Acceptance 2
    covers work dispatch **/ progress obligation**, and here the pair is the SENDER: a
    generation it opened stays active until its correlated callback returns, so closing the
    pair mid-generation strands it. `from_lane_id` / `from_role` are exactly its identity.
    """

    def test_an_active_outbound_generation_from_this_lane_is_owed(self):
        f = ForwardOutboxFence(home=self.home)
        f.bootstrap()
        f.reserve(ForwardRouteKey(WS, LANE, ROLE, "codex", "scope"))
        found = forward_generation_obligations(
            workspace_id=WS, lane_id=LANE, roles=(ROLE,)
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].verdict, OWED)
        self.assertEqual(found[0].source, "forward_outbox")

    def test_a_never_bootstrapped_store_is_a_positive_absence(self):
        self.assertEqual(
            forward_generation_obligations(workspace_id=WS, lane_id=LANE, roles=(ROLE,)), ()
        )

    def test_every_damage_shape_fails_closed(self):
        """j#80620 R5-F3 — the same fail-open the dispatch fence was corrected for at R2-F1,
        re-introduced here by writing `nonce is None and not path.exists()` again.

        `_read_sidecar_nonce() is None` also covers an EMPTY / unreadable sidecar, so a
        DB-absent + empty-sidecar-residue store read as "nothing was ever reserved here".
        """
        import sqlite3

        def bump(f):
            conn = sqlite3.connect(f.path)
            conn.execute("PRAGMA user_version = 9999")
            conn.commit()
            conn.close()

        shapes = {
            "db gone, EMPTY sidecar": lambda f: (
                f.path.unlink(), f.sidecar_path.write_text("")
            ),
            "db gone, sidecar remains": lambda f: f.path.unlink(),
            "sidecar gone, db remains": lambda f: f.sidecar_path.unlink(),
            "corrupt db": lambda f: f.path.write_bytes(b"not sqlite"),
            "nonce mismatch": lambda f: f.sidecar_path.write_text("deadbeef"),
            "unknown schema": bump,
        }
        for label, mutate in shapes.items():
            with self.subTest(shape=label):
                d = tempfile.mkdtemp()
                self.addCleanup(shutil.rmtree, d, True)
                home = Path(d)
                with mock.patch(
                    "mozyo_bridge.core.state.forward_outbox_fence.mozyo_bridge_home",
                    return_value=home,
                ):
                    f = ForwardOutboxFence(home=home)
                    f.bootstrap()
                    mutate(f)
                    with self.assertRaises(ObligationStoreUnreadable):
                        forward_generation_obligations(
                            workspace_id=WS, lane_id=LANE, roles=(ROLE,)
                        )


# ------------------------------------------- structurally inapplicable ---


class StructurallyInapplicableTest(unittest.TestCase):
    """Each asserts the PRECONDITION that makes the source impossible, so the test fails if
    that precondition ever changes — rather than asserting a fact that merely happens to be
    true today."""

    def test_publication_fence_requires_a_lane_generation_only_a_lifecycle_row_mints(self):
        from mozyo_bridge.core.state.callback_publication_fence import PublicationKey

        fields = set(PublicationKey.__dataclass_fields__)
        self.assertIn(
            "lane_generation", fields,
            "if the generation ever leaves this key, a scratch pair could be named here",
        )
        self.assertIn("issue", fields)
        self.assertNotIn("target_assigned_name", fields)

    def test_herdr_delivery_ledger_is_evidence_with_no_idempotency_key(self):
        from mozyo_bridge.core.state import herdr_delivery_ledger as led

        src = Path(led.__file__).read_text()
        self.assertNotIn("UNIQUE(", src, "an obligation store would need an idempotency key")
        self.assertIn("append_only_lossy", src)

    def test_identity_attestation_is_a_rebuildable_cache_with_no_obligation(self):
        from mozyo_bridge.core.state import herdr_identity_attestation as att

        src = Path(att.__file__).read_text()
        self.assertIn("rebuildable_cache", src)
        self.assertIn("never promotes", src)

    def test_session_inventory_is_a_rebuildable_cache(self):
        """j#80594 R4-F3(d): listed in the matrix with no test of its own."""
        from mozyo_bridge.core.state import session_inventory as inv

        src = Path(inv.__file__).read_text()
        self.assertIn("never the source of truth", src)  # a cache cannot be an obligation gate

    def test_callback_sweep_lease_requires_a_redmine_anchor_and_is_an_attempt_lease(self):
        """j#80594 R4-F3(d): listed in the matrix with no test of its own."""
        from mozyo_bridge.core.state.callback_sweep_lease import LeaseKey

        fields = set(LeaseKey.__dataclass_fields__)
        self.assertIn("issue", fields, "a scratch pair has no Redmine anchor to key on")
        self.assertIn("anchor", fields)
        self.assertNotIn("target_assigned_name", fields)


if __name__ == "__main__":
    unittest.main()
