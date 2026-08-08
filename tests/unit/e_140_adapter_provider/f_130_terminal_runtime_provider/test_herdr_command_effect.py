"""Herdr 0.8 nested pane-command effect envelope regressions (#14608)."""

from __future__ import annotations

import json
import unittest

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_command_effect import (
    EFFECT_CHANGED,
    EFFECT_UNCHANGED,
    EFFECT_UNKNOWN,
    parse_changed_effect,
)


class ChangedEffectTests(unittest.TestCase):
    def _parse(self, payload: object) -> str:
        return parse_changed_effect(
            json.dumps(payload), result_type="pane_swap", envelope="swap"
        )

    def test_exact_nested_true_and_false_are_typed(self) -> None:
        for changed, expected in (
            (True, EFFECT_CHANGED),
            (False, EFFECT_UNCHANGED),
        ):
            with self.subTest(changed=changed):
                self.assertEqual(
                    self._parse(
                        {
                            "result": {
                                "type": "pane_swap",
                                "swap": {"changed": changed},
                            }
                        }
                    ),
                    expected,
                )

    def test_schema_drift_and_non_boolean_effect_are_unknown(self) -> None:
        payloads = (
            {"result": {"type": "pane_swap", "changed": True}},
            {"result": {"type": "pane_swap", "swap": {}}},
            {"result": {"type": "pane_resize", "swap": {"changed": True}}},
            {"result": {"type": "pane_swap", "swap": {"changed": 1}}},
            [],
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual(self._parse(payload), EFFECT_UNKNOWN)

    def test_invalid_json_is_unknown(self) -> None:
        self.assertEqual(
            parse_changed_effect(
                "not-json", result_type="pane_swap", envelope="swap"
            ),
            EFFECT_UNKNOWN,
        )


if __name__ == "__main__":
    unittest.main()
