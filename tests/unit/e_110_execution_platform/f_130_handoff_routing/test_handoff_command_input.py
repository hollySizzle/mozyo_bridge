"""Characterization for the handoff typed input seam (Redmine #13729).

Pins :class:`HandoffNamespaceAdapter` as a default-preserving field capture so the
``getattr(args, ...)`` -> ``inp.<field>`` substitution inside ``orchestrate_handoff``
stays byte-for-byte behaviour-preserving, and pins the review j#78706 corrections:

- R1: the adapter captures the **full** command input (including ``target_repo`` /
  ``target_lane`` / ``allow_direct_worker`` / ``main_lane_exception`` / the record /
  submit / persist fields), so the ``argparse.Namespace`` is confined to the adapter
  and the facade and no deep helper reads it.
- R2: ``Any`` fields are precisely typed, and the two repeatable list inputs
  (``callback_methods`` / ``profile_field``) are snapshotted into immutable tuples so
  mutating the original Namespace list cannot mutate the value object.

Constructor-injected fakes only — no monkeypatch — per the design's rule.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_command_input_adapter import (
    HandoffNamespaceAdapter,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_command_input import (
    HandoffCommandInput,
)

# Scalar Namespace-sourced fields (name -> default when the attribute is absent),
# mirroring the original ``getattr(args, "<name>", <default>)`` read sites. The two
# list fields (callback_methods / profile_field) are tested separately because the
# adapter snapshots them to tuples.
SCALAR_FIELD_DEFAULTS = {
    "to": None,
    "source": None,
    "kind": None,
    "mode": None,
    "force": False,
    "summary": None,
    "task_id": None,
    "comment_id": None,
    "anchor_url": None,
    "issue": None,
    "journal": None,
    "work_shape": None,
    "consultation_kind": None,
    "classification": None,
    "dispatch_decision": None,
    "workflow_next_owner": None,
    "callback_reason": None,
    "callback_to_role": None,
    "read_contract": None,
    "forward_action_id": "",
    "target": None,
    "target_repo": None,
    "target_lane": None,
    "target_project": None,
    "no_target_activation": False,
    "restore_previous_active": False,
    "allow_direct_worker": False,
    "main_lane_exception": None,
    "workdir": None,
    "role_profile": None,
    "transition_role": None,
    "workflow_contract": None,
    "read_lines": 50,
    "landing_timeout": None,
    "submit_delay": 0.2,
    "queue_enter_retry_window": None,
    "queue_enter_retry_interval": None,
    "record_format": None,
    "record_command": None,
    "persist_delivery": False,
    "submit_intent": None,
    "submit_delivery_id": None,
}

ENTRY_POLICY_DEFAULTS = {
    "default_kind": None,
    "require_receiver_binding": False,
    "ticketless": False,
    "ticketless_consultation": False,
    "ticketless_work_intake": False,
}

TUPLE_LIST_FIELDS = ("callback_methods", "profile_field")


class HandoffNamespaceAdapterDefaultsTest(unittest.TestCase):
    def test_bare_namespace_falls_back_to_original_getattr_defaults(self) -> None:
        inp = HandoffNamespaceAdapter.from_namespace(argparse.Namespace())
        for field, default in SCALAR_FIELD_DEFAULTS.items():
            with self.subTest(field=field):
                self.assertEqual(getattr(inp, field), default)
        for field in TUPLE_LIST_FIELDS:
            with self.subTest(field=field):
                self.assertIsNone(getattr(inp, field))

    def test_entry_policy_defaults_when_not_passed(self) -> None:
        inp = HandoffNamespaceAdapter.from_namespace(argparse.Namespace())
        for field, default in ENTRY_POLICY_DEFAULTS.items():
            with self.subTest(field=field):
                self.assertEqual(getattr(inp, field), default)

    def test_full_command_input_is_captured_r1(self) -> None:
        # Review j#78706 R1: the fields deep helpers read must be on the value
        # object, so the facade can pass typed scalars and confine the Namespace.
        names = {f.name for f in dataclasses.fields(HandoffCommandInput)}
        for required in (
            "target_repo",
            "target_lane",
            "allow_direct_worker",
            "main_lane_exception",
            "record_format",
            "record_command",
            "persist_delivery",
            "submit_intent",
            "submit_delivery_id",
        ):
            with self.subTest(field=required):
                self.assertIn(required, names)


class HandoffNamespaceAdapterCaptureTest(unittest.TestCase):
    def test_every_scalar_field_is_captured_verbatim(self) -> None:
        ns_values = {name: f"val::{name}" for name in SCALAR_FIELD_DEFAULTS}
        inp = HandoffNamespaceAdapter.from_namespace(argparse.Namespace(**ns_values))
        for field, value in ns_values.items():
            with self.subTest(field=field):
                self.assertEqual(getattr(inp, field), value)

    def test_falsy_explicit_values_are_preserved_not_defaulted(self) -> None:
        ns = argparse.Namespace(
            mode=None,
            force=None,
            forward_action_id="",
            read_lines=0,
            landing_timeout=0,
            submit_delay=0,
        )
        inp = HandoffNamespaceAdapter.from_namespace(ns)
        self.assertIsNone(inp.mode)
        self.assertIsNone(inp.force)
        self.assertEqual(inp.forward_action_id, "")
        self.assertEqual(inp.read_lines, 0)
        self.assertEqual(inp.landing_timeout, 0)
        self.assertEqual(inp.submit_delay, 0)

    def test_entry_policy_flows_through(self) -> None:
        inp = HandoffNamespaceAdapter.from_namespace(
            argparse.Namespace(),
            default_kind="reply",
            require_receiver_binding=True,
            ticketless=True,
            ticketless_consultation=True,
            ticketless_work_intake=True,
        )
        self.assertEqual(inp.default_kind, "reply")
        self.assertTrue(inp.require_receiver_binding)
        self.assertTrue(inp.ticketless)
        self.assertTrue(inp.ticketless_consultation)
        self.assertTrue(inp.ticketless_work_intake)


class HandoffCommandInputImmutabilityTest(unittest.TestCase):
    def test_value_object_is_frozen(self) -> None:
        inp = HandoffNamespaceAdapter.from_namespace(argparse.Namespace())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            inp.to = "claude"  # type: ignore[misc]

    def test_list_inputs_snapshot_to_tuples(self) -> None:
        # Review j#78706 R2: callback_methods / profile_field arrive as mutable lists.
        ns = argparse.Namespace(
            callback_methods=["chat", "journal"],
            profile_field=["parent_project=alpha"],
        )
        inp = HandoffNamespaceAdapter.from_namespace(ns)
        self.assertEqual(inp.callback_methods, ("chat", "journal"))
        self.assertEqual(inp.profile_field, ("parent_project=alpha",))
        self.assertIsInstance(inp.callback_methods, tuple)
        self.assertIsInstance(inp.profile_field, tuple)

    def test_mutating_original_list_cannot_mutate_value_object(self) -> None:
        # Review j#78706 R2: deep immutability — the tuple snapshot is independent
        # of the Namespace's list, so a later mutation cannot leak in.
        methods = ["chat"]
        fields = ["a=1"]
        ns = argparse.Namespace(callback_methods=methods, profile_field=fields)
        inp = HandoffNamespaceAdapter.from_namespace(ns)
        methods.append("journal")
        fields.append("b=2")
        self.assertEqual(inp.callback_methods, ("chat",))
        self.assertEqual(inp.profile_field, ("a=1",))


if __name__ == "__main__":
    unittest.main()
