"""Redmine #13892 R3-F3 — the durable obligation source matrix, pinned in code.

j#80526 / review j#80569 require every durable source of work owed to a scratch pair's slot to
be enumerated and fixed as **covered** or **structurally-inapplicable with an exact reason**.
A prose claim rots; these tests fail if a store's shape ever changes such that the reason stops
holding, which is the only way "structurally inapplicable" stays true rather than becoming a
silent fail-open.

A scratch pair is identified ONLY by its herdr assigned name `mzb1_<ws>_<role>_<lane>`. It has
no lane lifecycle record, hence no `lane_generation`, and no Redmine issue binding.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.callback_outbox import CallbackOutboxKey
from mozyo_bridge.core.state.forward_outbox_fence import ForwardRouteKey
from mozyo_bridge.core.state.callback_publication_fence import PublicationKey
from mozyo_bridge.core.state.dispatch_outbox_fence import FenceKey

SCRATCH_NAME = "mzb1_wsabc_claude_dogfood13892"


class ObligationSourceMatrixTest(unittest.TestCase):
    """One test per source. The docstring IS the matrix entry's reason."""

    # -- COVERED -----------------------------------------------------------

    def test_dispatch_outbox_fence_is_covered_because_it_keys_on_the_target_name(self):
        """COVERED. `target_assigned_name` is a key column, so a scratch slot CAN be named.

        This is the one store that records work owed *to* a named slot, and `session-retire`
        reads it by name (`obligations_for_targets`). Note the key's `issue`/`journal` accept
        `''` — there is NO store-level anchor guard here — so "a scratch pair has no issue"
        would NOT have made this store inapplicable. It is covered by reading, not by argument.
        """
        key = FenceKey("wsabc", "dogfood13892", "", "", "act1", SCRATCH_NAME)
        self.assertIn(SCRATCH_NAME, key.as_row())
        self.assertEqual(key.issue, "", "an empty anchor is structurally accepted here")

    # -- STRUCTURALLY INAPPLICABLE ----------------------------------------

    def test_callback_outbox_cannot_name_a_slot_and_requires_a_redmine_anchor(self):
        """INAPPLICABLE. Two independent reasons.

        1. No key column carries an assigned name: the key identifies a *gate transition on a
           journal*, not a slot.
        2. The store hard-rejects an empty `issue` / `journal` (`callback_outbox.py` enqueue),
           and a scratch pair has no Redmine anchor, so no key can be built for one.
        """
        fields = set(CallbackOutboxKey.__dataclass_fields__)
        self.assertNotIn("target_assigned_name", fields)
        self.assertFalse(
            [f for f in fields if "assigned" in f or "pane" in f],
            "no slot-naming field exists in the callback outbox key",
        )
        self.assertIn("issue", fields)
        self.assertIn("journal", fields)

    def test_forward_outbox_fence_deliberately_excludes_the_target_name(self):
        """INAPPLICABLE. The target's assigned name is deliberately NOT part of the key.

        j#76528 point 1: it is an action-time attestation, excluded so a target rename can
        never advance a generation. The key identifies the *sender's route*
        `(workspace, from_lane, from_role, to_role, project_scope)`; a scratch pair is a
        target, so there is no field that could carry its name.
        """
        fields = set(ForwardRouteKey.__dataclass_fields__)
        self.assertNotIn("target_assigned_name", fields)
        self.assertFalse([f for f in fields if "assigned" in f])
        self.assertEqual(
            fields,
            {"workspace_id", "from_lane_id", "from_role", "to_role", "project_scope"},
        )

    def test_callback_publication_fence_requires_a_lane_generation(self):
        """INAPPLICABLE. The key requires `lane_generation`, minted only by a lifecycle row.

        A `session-start` scratch pair mints no lifecycle record (#13892 acceptance 4), so it
        has no generation and no `PublicationKey` can be constructed for it. The key also
        requires `issue` + `dispatch_anchor`, which it equally lacks. And the row describes a
        *Redmine record write*, not work owed to a pane.
        """
        fields = set(PublicationKey.__dataclass_fields__)
        self.assertIn("lane_generation", fields)
        self.assertIn("issue", fields)
        self.assertNotIn("target_assigned_name", fields)

    def test_herdr_delivery_ledger_is_evidence_not_an_authority(self):
        """INAPPLICABLE. Append-only telemetry: no UNIQUE key, no state machine.

        Its recovery policy is `append_only_lossy` with "no rebuild path by design", and the
        dispatch fence's own docstring names it *evidence* that "never substitutes" for the
        authority. A store whose loss is declared harmless cannot gate "may I destroy these
        panes?" — reading it as permission would turn documented lossiness into a silent yes.
        Its `target` column holds a transport locator, not an `mzb1_...` assigned name.
        """
        from mozyo_bridge.core.state import herdr_delivery_ledger as led

        src = Path(led.__file__).read_text()
        self.assertNotIn("UNIQUE(", src, "an obligation store would need an idempotency key")
        self.assertIn("append_only_lossy", src)

    def test_identity_attestation_is_name_keyed_but_carries_no_obligation(self):
        """INAPPLICABLE. Keyed on the assigned name, but a `rebuildable_cache` projection.

        It records a startup env self-attestation verdict — nothing is owed to or by the row —
        and its own docstring forbids promoting it to a permission verdict.
        """
        from mozyo_bridge.core.state import herdr_identity_attestation as att

        src = Path(att.__file__).read_text()
        self.assertIn("rebuildable_cache", src)
        self.assertIn("never promotes", src)


if __name__ == "__main__":
    unittest.main()
