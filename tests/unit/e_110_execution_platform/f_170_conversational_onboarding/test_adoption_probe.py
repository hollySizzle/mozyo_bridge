"""Mount-independent adoption classification tests (Redmine #13497 R1 / j#74919).

The bare-entry gate must recognise a *validly complete* adoption without the
fresh-adoption mount classifier, and must never treat a broken config / receipt
as adopted. Exercised against real temp fixtures.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application.adoption_probe import (
    ADOPTION_ABSENT,
    ADOPTION_BROKEN,
    ADOPTION_COMPLETE,
    ADOPTION_IN_PROGRESS,
    classify_adoption,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.receipt import (
    ORDERED_STEPS,
    STEP_STATUS_DONE,
    OnboardingReceipt,
    serialize_receipt,
)

SECRET = "trusted-onboarding-secret"
_VALID_CONFIG = "version: 1\nterminal_transport:\n  backend: herdr\n"


def _mzb(root: Path) -> Path:
    d = root / ".mozyo-bridge"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_config(root: Path, text: str = _VALID_CONFIG) -> None:
    (_mzb(root) / "config.yaml").write_text(text, encoding="utf-8")


def _write_receipt(root: Path, *, complete: bool, secret: str = SECRET) -> None:
    receipt = OnboardingReceipt(
        root_fingerprint="fp", plan_id="plan.v2.abc",
        scaffold_preset="none", rules_store="central",
    )
    if complete:
        for step in ORDERED_STEPS:
            receipt = receipt.with_step(step, STEP_STATUS_DONE)
        receipt = receipt.completed()
    else:
        receipt = receipt.with_step(ORDERED_STEPS[0], STEP_STATUS_DONE)
    (_mzb(root) / "onboarding-receipt.json").write_text(
        serialize_receipt(receipt, secret=secret), encoding="utf-8"
    )


class AdoptionProbeTest(unittest.TestCase):
    def _root(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        return Path(self._tmp.name)

    def test_absent_when_no_markers(self):
        self.assertEqual(classify_adoption(self._root(), gate_secret=SECRET).status,
                         ADOPTION_ABSENT)

    def test_readable_config_is_complete(self):
        root = self._root()
        _write_config(root)
        self.assertEqual(classify_adoption(root, gate_secret=SECRET).status,
                         ADOPTION_COMPLETE)

    def test_broken_config_is_broken_not_adopted(self):
        root = self._root()
        _write_config(root, "version: [unterminated\n")
        status = classify_adoption(root, gate_secret=SECRET)
        self.assertEqual(status.status, ADOPTION_BROKEN)
        self.assertIsNotNone(status.reason)

    def test_complete_receipt_is_complete(self):
        root = self._root()
        _write_receipt(root, complete=True)
        self.assertEqual(classify_adoption(root, gate_secret=SECRET).status,
                         ADOPTION_COMPLETE)

    def test_in_progress_receipt_is_in_progress(self):
        root = self._root()
        _write_receipt(root, complete=False)
        self.assertEqual(classify_adoption(root, gate_secret=SECRET).status,
                         ADOPTION_IN_PROGRESS)

    def test_receipt_wrong_secret_is_broken(self):
        root = self._root()
        _write_receipt(root, complete=True, secret="other-secret")
        self.assertEqual(classify_adoption(root, gate_secret=SECRET).status,
                         ADOPTION_BROKEN)

    def test_receipt_without_secret_is_broken(self):
        root = self._root()
        _write_receipt(root, complete=True)
        self.assertEqual(classify_adoption(root, gate_secret=None).status,
                         ADOPTION_BROKEN)

    def test_broken_config_wins_over_complete_receipt(self):
        # Any broken evidence fails closed even if another marker looks complete.
        root = self._root()
        _write_config(root, "version: [bad\n")
        _write_receipt(root, complete=True)
        self.assertEqual(classify_adoption(root, gate_secret=SECRET).status,
                         ADOPTION_BROKEN)


if __name__ == "__main__":
    unittest.main()
