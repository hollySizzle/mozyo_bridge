"""Participant manifest v2 — the transaction OWNS its update evidence (#14741 j#97038).

The ruling chose pinning over reconstruction, and the reason is replay: reconstructing the
generation key at consume time needs the launch-generation authority to still exist, so a
transaction could not replay its own evidence once that authority was lost. Owning the
evidence across crash and replay is exactly what C15 asks of the transaction.

`lane_id` / `provider` / `assigned_name` already carried the rest of the key, so v2 adds
only the workspace, the startup action and the typed cause.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.replacement_transaction_model import (  # noqa: E402
    PARTICIPANT_CLOSE_OWED,
    PARTICIPANTS_READABLE_VERSIONS,
    PARTICIPANTS_VERSION,
    ParticipantPin,
    ParticipantPinError,
    decode_participants,
    encode_participants,
)

ACTION = "startup-ir1-" + "a" * 64
FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "support"
    / "fixtures"
    / "parent_runtime_participants_v1_9f683818.py.txt"
)


def _pin(**kw):
    base = dict(
        lane_id="issue_14741",
        role="gateway",
        provider="codex",
        assigned_name="mzb1_wA_codex_lane",
        old_locator="wA:p1",
    )
    base.update(kw)
    return ParticipantPin(**base)


def _receipt_pin(**kw):
    return _pin(
        evidence_workspace_id="wA",
        evidence_startup_action_id=ACTION,
        evidence_cause="update_relaunch",
        **kw,
    )


class TripletContractTest(unittest.TestCase):
    def test_all_empty_is_the_legacy_and_generic_participant(self) -> None:
        pin = _pin()
        self.assertEqual(
            (pin.evidence_workspace_id, pin.evidence_startup_action_id, pin.evidence_cause),
            ("", "", ""),
        )

    def test_all_present_is_the_receipt_capable_participant(self) -> None:
        pin = _receipt_pin()
        self.assertEqual(pin.evidence_workspace_id, "wA")
        self.assertEqual(pin.evidence_startup_action_id, ACTION)
        self.assertEqual(pin.evidence_cause, "update_relaunch")

    def test_a_partial_triplet_is_refused(self) -> None:
        """A partial triplet names a generation nobody can address."""
        for label, kw in (
            ("workspace only", {"evidence_workspace_id": "wA"}),
            ("action only", {"evidence_startup_action_id": ACTION}),
            ("cause only", {"evidence_cause": "update_relaunch"}),
            (
                "missing cause",
                {"evidence_workspace_id": "wA", "evidence_startup_action_id": ACTION},
            ),
            (
                "missing action",
                {"evidence_workspace_id": "wA", "evidence_cause": "update_relaunch"},
            ),
        ):
            with self.subTest(label=label):
                with self.assertRaises(ParticipantPinError):
                    _pin(**kw)


class RoundTripAndReplayTest(unittest.TestCase):
    def test_the_envelope_this_build_writes_is_v2(self) -> None:
        self.assertEqual(PARTICIPANTS_VERSION, 2)
        raw = encode_participants([_receipt_pin()])
        self.assertEqual(json.loads(raw)["version"], 2)

    def test_the_triplet_round_trips_byte_exact(self) -> None:
        pin = _receipt_pin()
        back = decode_participants(encode_participants([pin]))[0]
        self.assertEqual(back, pin)

    def test_with_phase_preserves_the_triplet(self) -> None:
        """`with_phase` is the store's ONLY mutation; it must not drop the evidence."""
        pin = _receipt_pin()
        moved = pin.with_phase("launch_owed")
        self.assertEqual(moved.phase, "launch_owed")
        self.assertEqual(moved.evidence_workspace_id, pin.evidence_workspace_id)
        self.assertEqual(moved.evidence_startup_action_id, pin.evidence_startup_action_id)
        self.assertEqual(moved.evidence_cause, pin.evidence_cause)

    def test_a_cas_style_reencode_is_byte_stable(self) -> None:
        """Phase-only edits must round-trip deterministically, evidence included."""
        pin = _receipt_pin()
        once = encode_participants([pin.with_phase("launch_owed")])
        twice = encode_participants([decode_participants(once)[0]])
        self.assertEqual(once, twice)


class VersionCompatibilityTest(unittest.TestCase):
    def test_a_v1_manifest_reads_as_a_legacy_empty_triplet(self) -> None:
        """Read-compatibility, not a default: v1 predates receipts, so it HAS no evidence."""
        self.assertIn(1, PARTICIPANTS_READABLE_VERSIONS)
        v1 = json.dumps(
            {
                "version": 1,
                "participants": [
                    {
                        "lane_id": "issue_14741",
                        "role": "gateway",
                        "provider": "codex",
                        "assigned_name": "mzb1_wA_codex_lane",
                        "old_locator": "wA:p1",
                        "is_self": False,
                        "lane_revision": "",
                        "lane_generation": "",
                        "phase": PARTICIPANT_CLOSE_OWED,
                    }
                ],
            }
        )
        pin = decode_participants(v1)[0]
        self.assertEqual(pin.assigned_name, "mzb1_wA_codex_lane")
        self.assertEqual(
            (pin.evidence_workspace_id, pin.evidence_startup_action_id, pin.evidence_cause),
            ("", "", ""),
        )

    def test_an_unknown_or_newer_envelope_is_still_refused(self) -> None:
        for version in (0, 3, 99, True, 1.0, "2", None):
            with self.subTest(version=version):
                raw = json.dumps({"version": version, "participants": []})
                with self.assertRaises(ParticipantPinError):
                    decode_participants(raw)


class ParentRuntimeRejectsV2ManifestTest(unittest.TestCase):
    """The old-runtime fence, executed rather than described (j#97038).

    The `9f683818` model module is vendored verbatim and run against a v2 manifest. It
    refuses, because its own decode pins an exact v1. That refusal is what makes it safe for
    a v2 manifest to exist only after the #14838 offline cutover.
    """

    def _parent(self):
        name = "parent_participants_9f683818"
        spec = importlib.util.spec_from_loader(name, loader=None)
        module = importlib.util.module_from_spec(spec)
        module.__file__ = str(FIXTURE)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        exec(compile(FIXTURE.read_text(), str(FIXTURE), "exec"), module.__dict__)
        return module

    def test_the_vendored_fixture_is_the_pre_v2_runtime(self) -> None:
        """A stale fixture would make this proof vacuous."""
        text = FIXTURE.read_text()
        self.assertIn("PARTICIPANTS_VERSION = 1", text)
        self.assertNotIn("evidence_startup_action_id", text)

    def test_the_parent_runtime_reads_v1_and_refuses_v2(self) -> None:
        parent = self._parent()
        v1 = json.dumps(
            {
                "version": 1,
                "participants": [
                    {
                        "lane_id": "issue_14741",
                        "role": "gateway",
                        "provider": "codex",
                        "assigned_name": "mzb1_wA_codex_lane",
                        "old_locator": "wA:p1",
                        "is_self": False,
                        "lane_revision": "",
                        "lane_generation": "",
                        "phase": PARTICIPANT_CLOSE_OWED,
                    }
                ],
            }
        )
        # v1: the parent is perfectly happy — mixed runtime works until the cutover.
        self.assertEqual(len(parent.decode_participants(v1)), 1)

        # v2: refused outright, so a v2 manifest can only exist after the offline rollout.
        with self.assertRaises(parent.ParticipantPinError):
            parent.decode_participants(encode_participants([_receipt_pin()]))


if __name__ == "__main__":
    unittest.main()
