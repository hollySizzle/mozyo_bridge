"""Characterization for the handoff typed input seam (Redmine #13729, tranche 1).

Pins :class:`HandoffNamespaceAdapter` as a *dumb, default-preserving field
capture* so the ``getattr(args, ...)`` -> ``inp.<field>`` substitution inside
``orchestrate_handoff`` stays byte-for-byte behaviour-preserving:

- every field is captured with the exact default the original body used at its
  primary read site (``force`` -> ``False``, ``forward_action_id`` -> ``""``,
  ``read_lines`` -> ``50``, ``submit_delay`` -> ``0.2``, ``landing_timeout`` ->
  ``None``, ``mode`` -> ``None``, everything else -> ``None``);
- entry policy (formerly loose keyword parameters) flows onto the value object;
- ``target_repo`` is deliberately NOT captured (it is mutated in place on the
  Namespace and re-read by the not-yet-extracted target-resolution helpers, so a
  frozen snapshot would go stale — design j#78394 Task 3 owns it);
- the value object is frozen.

These are constructor-injected fakes only — no monkeypatch — per the design's
"new unit tests use constructor-injected fake ports" rule.
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

# Every Namespace-sourced field the adapter captures, paired with the default it
# must fall back to when the attribute is absent — mirroring the original
# ``getattr(args, "<name>", <default>)`` read sites in ``orchestrate_handoff``.
FIELD_DEFAULTS = {
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
    "callback_methods": None,
    "read_contract": None,
    "forward_action_id": "",
    "target": None,
    "target_project": None,
    "no_target_activation": False,
    "restore_previous_active": False,
    "workdir": None,
    "role_profile": None,
    "profile_field": None,
    "transition_role": None,
    "workflow_contract": None,
    "read_lines": 50,
    "landing_timeout": None,
    "submit_delay": 0.2,
    "queue_enter_retry_window": None,
    "queue_enter_retry_interval": None,
}

ENTRY_POLICY_DEFAULTS = {
    "default_kind": None,
    "require_receiver_binding": False,
    "ticketless": False,
    "ticketless_consultation": False,
    "ticketless_work_intake": False,
}


class HandoffNamespaceAdapterDefaultsTest(unittest.TestCase):
    def test_bare_namespace_falls_back_to_original_getattr_defaults(self) -> None:
        # An empty Namespace is the "attribute absent" case: each field must land
        # on the exact default the original body's getattr used.
        inp = HandoffNamespaceAdapter.from_namespace(argparse.Namespace())
        for field, default in FIELD_DEFAULTS.items():
            with self.subTest(field=field):
                self.assertEqual(getattr(inp, field), default)

    def test_entry_policy_defaults_when_not_passed(self) -> None:
        inp = HandoffNamespaceAdapter.from_namespace(argparse.Namespace())
        for field, default in ENTRY_POLICY_DEFAULTS.items():
            with self.subTest(field=field):
                self.assertEqual(getattr(inp, field), default)

    def test_target_repo_is_not_a_captured_field(self) -> None:
        # target_repo stays on the Namespace (mutated in place, re-read by
        # not-yet-extracted target resolution); it must not leak onto the frozen
        # value object as a stale snapshot.
        names = {f.name for f in dataclasses.fields(HandoffCommandInput)}
        self.assertNotIn("target_repo", names)


class HandoffNamespaceAdapterCaptureTest(unittest.TestCase):
    def test_every_namespace_field_is_captured_verbatim(self) -> None:
        # A distinct sentinel per field proves the adapter reads the right
        # attribute onto the right value-object field (no crossed wires).
        ns_values = {name: f"val::{name}" for name in FIELD_DEFAULTS}
        inp = HandoffNamespaceAdapter.from_namespace(argparse.Namespace(**ns_values))
        for field, value in ns_values.items():
            with self.subTest(field=field):
                self.assertEqual(getattr(inp, field), value)

    def test_falsy_explicit_values_are_preserved_not_defaulted(self) -> None:
        # The adapter must not coerce: an explicit None / "" / 0 is stored as-is,
        # and the body (not the adapter) applies its own `or` normalization.
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


class HandoffCommandInputFrozenTest(unittest.TestCase):
    def test_value_object_is_frozen(self) -> None:
        inp = HandoffNamespaceAdapter.from_namespace(argparse.Namespace())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            inp.to = "claude"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
