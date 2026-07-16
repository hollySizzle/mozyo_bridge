"""Regression pins for Redmine #13847 — hibernated exact-pair recovery (items 3/4/5).

The public ``sublane recover-pair`` surface recovers the exact gateway + worker pair of a
hibernated lane whose fresh launch booted partially (unattested / stale). This file pins the
pure per-slot recovery decision (items 3/4): only a slot positively presenting the
hibernated pair's own stale / unattested bad generation is closed + relaunched; every other
disposition — a productive provider / tool-child, a pending composer, a foreign slot, an
ambiguous / unreadable identity, or a NEWER generation — is preserved (zero-close, worktree
bytes kept). The orchestration (owner-approved ``--execute``, bounded close/relaunch,
post-hibernate re-attest, resume CAS, exactly-once redispatch) is layered on top of this
decision; its tests are added as that surface lands.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_pair_recovery import (  # noqa: E501
    SLOT_HEALTHY,
    SLOT_PRESERVE_AMBIGUOUS,
    SLOT_PRESERVE_FOREIGN,
    SLOT_PRESERVE_NEWER,
    SLOT_PRESERVE_PENDING,
    SLOT_PRESERVE_PRODUCTIVE,
    SLOT_PRESERVE_UNREADABLE,
    SLOT_RECOVER,
    SlotRecoveryObservation,
    decide_slot_recovery,
    hibernated_pair_recovery_action_id,
    slot_recovers,
)


def _obs(**kw):
    base = dict(
        identity_resolved=True,
        belongs_to_pair=True,
        generation_not_newer=True,
        not_productive=True,
        no_pending_composer=True,
        worktree_readable=True,
        is_bad_generation=True,
        already_healthy=False,
    )
    base.update(kw)
    return SlotRecoveryObservation(**base)


class SlotRecoveryDecision(unittest.TestCase):
    def test_bad_generation_slot_is_the_only_actuating_disposition(self):
        self.assertEqual(decide_slot_recovery(_obs()), SLOT_RECOVER)
        self.assertTrue(slot_recovers(SLOT_RECOVER))

    def test_each_preserve_class_zero_closes(self):
        cases = {
            "identity_resolved": SLOT_PRESERVE_AMBIGUOUS,
            "belongs_to_pair": SLOT_PRESERVE_FOREIGN,
            "generation_not_newer": SLOT_PRESERVE_NEWER,
            "not_productive": SLOT_PRESERVE_PRODUCTIVE,
            "no_pending_composer": SLOT_PRESERVE_PENDING,
            "worktree_readable": SLOT_PRESERVE_UNREADABLE,
        }
        for field, expected in cases.items():
            with self.subTest(field=field):
                self.assertEqual(decide_slot_recovery(_obs(**{field: False})), expected)
                self.assertFalse(slot_recovers(expected))

    def test_already_healthy_slot_needs_no_action(self):
        self.assertEqual(decide_slot_recovery(_obs(already_healthy=True)), SLOT_HEALTHY)
        self.assertFalse(slot_recovers(SLOT_HEALTHY))

    def test_indeterminate_slot_preserves_never_closes_on_absent_signal(self):
        # No positive bad-generation signal, but every preserve gate cleared: fail closed
        # to preserve — never close a slot on the ABSENCE of a residue signal.
        self.assertEqual(
            decide_slot_recovery(_obs(is_bad_generation=False, already_healthy=False)),
            SLOT_PRESERVE_AMBIGUOUS,
        )

    def test_guard_bite_productive_beats_bad_generation(self):
        # Adversarial: a live productive slot that ALSO looks like a bad generation must be
        # preserved — the productive gate precedes the actuating check, so in-flight work is
        # never destroyed.
        v = decide_slot_recovery(_obs(not_productive=False, is_bad_generation=True))
        self.assertEqual(v, SLOT_PRESERVE_PRODUCTIVE)
        self.assertFalse(slot_recovers(v))

    def test_guard_bite_newer_generation_beats_bad_generation(self):
        # A newer generation that also carries a bad-gen signal must be preserved (a fresher
        # generation superseded the approval), never closed.
        v = decide_slot_recovery(_obs(generation_not_newer=False, is_bad_generation=True))
        self.assertEqual(v, SLOT_PRESERVE_NEWER)

    def test_default_observation_preserves(self):
        # A fully-default (all-unsafe) observation must preserve, never actuate.
        self.assertFalse(slot_recovers(decide_slot_recovery(SlotRecoveryObservation())))

    def test_absent_slot_is_relaunch_recoverable(self):
        # R1-F1: a vanished pair slot (0 live panes — e.g. closed in a prior partial run) is
        # SLOT_RECOVER (relaunch), NOT preserve_ambiguous, so a partial close/relaunch replays.
        v = decide_slot_recovery(
            SlotRecoveryObservation(slot_absent=True, generation_not_newer=True)
        )
        self.assertEqual(v, SLOT_RECOVER)
        self.assertTrue(slot_recovers(v))

    def test_absent_slot_on_superseded_lane_preserves(self):
        # An absent slot whose lane generation was superseded must NOT be relaunched.
        v = decide_slot_recovery(
            SlotRecoveryObservation(slot_absent=True, generation_not_newer=False)
        )
        self.assertEqual(v, SLOT_PRESERVE_NEWER)
        self.assertFalse(slot_recovers(v))

    def test_guard_bite_absent_vs_ambiguous(self):
        # Adversarial: absent (0 panes) recovers; ambiguous (>1 panes: not absent, not resolved)
        # preserves. The two must never be conflated (conflating them would either strand a
        # replay or relaunch onto a duplicate).
        self.assertTrue(slot_recovers(decide_slot_recovery(
            SlotRecoveryObservation(slot_absent=True, generation_not_newer=True))))
        self.assertFalse(slot_recovers(decide_slot_recovery(
            SlotRecoveryObservation(slot_absent=False, identity_resolved=False, generation_not_newer=True))))


class RecoveryActionId(unittest.TestCase):
    def test_pins_exact_hibernated_generation(self):
        aid = hibernated_pair_recovery_action_id(
            issue="13847", lane_id="issue_13847_x", revision="3", generation="2"
        )
        self.assertEqual(aid, "recover-pair:13847:issue_13847_x:3:2")

    def test_different_revision_or_generation_is_a_different_key(self):
        a = hibernated_pair_recovery_action_id(issue="1", lane_id="l", revision="1", generation="1")
        b = hibernated_pair_recovery_action_id(issue="1", lane_id="l", revision="2", generation="1")
        c = hibernated_pair_recovery_action_id(issue="1", lane_id="l", revision="1", generation="2")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)

    def test_under_specified_target_raises_never_ambiguous(self):
        for missing in ("issue", "lane_id", "revision", "generation"):
            kw = dict(issue="1", lane_id="l", revision="1", generation="1")
            kw[missing] = ""
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                hibernated_pair_recovery_action_id(**kw)


if __name__ == "__main__":
    unittest.main()
