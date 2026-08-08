"""Direct Herdr 0.8 response tests for pair split ratio actuation (#14608)."""

from __future__ import annotations

import json
import subprocess
import unittest

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pair_split_ratio import (
    RESIZE_CHANGED,
    RESIZE_REFUSED,
    RESIZE_UNCHANGED,
    RESIZE_UNKNOWN,
    _resize,
)


class ResizeEffectTests(unittest.TestCase):
    @staticmethod
    def _runner(payload: object = None, *, returncode: int = 0):
        def run(argv, **_kwargs):
            return subprocess.CompletedProcess(
                argv,
                returncode,
                stdout=json.dumps(payload) if payload is not None else "",
                stderr="refused" if returncode else "",
            )

        return run

    def _resize(self, runner) -> str:
        return _resize(
            "pane-token",
            "down",
            0.2,
            binary="herdr",
            runner=runner,
            timeout=1.0,
            env=None,
        )

    def test_nested_changed_true_and_false_are_preserved(self) -> None:
        for changed, expected in (
            (True, RESIZE_CHANGED),
            (False, RESIZE_UNCHANGED),
        ):
            with self.subTest(changed=changed):
                payload = {
                    "result": {
                        "type": "pane_resize",
                        "resize": {"changed": changed},
                    }
                }
                self.assertEqual(self._resize(self._runner(payload)), expected)

    def test_malformed_success_is_unknown(self) -> None:
        self.assertEqual(
            self._resize(self._runner({"result": {"type": "ok"}})),
            RESIZE_UNKNOWN,
        )

    def test_nonzero_command_is_refused(self) -> None:
        self.assertEqual(
            self._resize(self._runner(returncode=1)),
            RESIZE_REFUSED,
        )


if __name__ == "__main__":
    unittest.main()
