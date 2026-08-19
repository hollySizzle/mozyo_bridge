"""Unit contract of the temp-root disk-pressure diagnosis (Redmine #15710).

The observed incident: a full ``tests run`` intermittently died with
``OSError: [Errno 122] Disk quota exceeded`` under ``/tmp`` while ``df``
showed 3% blocks / 1% inodes used — per-user tmpfs quota, not a defect of
the change under test. These tests pin the pure decisions: which errnos
count as pressure, how stderr markers are counted across chunk
boundaries, and that the diagnosis annotates a red run without ever
flipping it green or disclosing an absolute path by default.
"""

from __future__ import annotations

import dataclasses
import errno
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_disk_pressure import (
    PRESSURE_NOTE,
    DiskPressureDiagnosis,
    MarkerScanner,
    is_disk_pressure_errno,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_home_isolation import (
    IsolatedRunOutcome,
)

_TRACEBACK_LINE = b"OSError: [Errno 122] Disk quota exceeded: '/tmp/x'\n"


class PressureErrnoTest(unittest.TestCase):
    def test_quota_and_out_of_space_are_pressure(self) -> None:
        self.assertTrue(is_disk_pressure_errno(errno.EDQUOT))
        self.assertTrue(is_disk_pressure_errno(errno.ENOSPC))

    def test_a_permission_refusal_is_not_pressure(self) -> None:
        """EACCES/EROFS are the fence working, not the environment failing."""
        self.assertFalse(is_disk_pressure_errno(errno.EACCES))
        self.assertFalse(is_disk_pressure_errno(errno.EROFS))
        self.assertFalse(is_disk_pressure_errno(None))


class MarkerScannerTest(unittest.TestCase):
    def test_one_traceback_line_counts_once_not_once_per_pattern(self) -> None:
        """The bracket form and the strerror form share one occurrence."""
        scanner = MarkerScanner()
        scanner.feed(_TRACEBACK_LINE)
        self.assertEqual(scanner.markers, ("EDQUOT",))

    def test_occurrences_are_counted(self) -> None:
        scanner = MarkerScanner()
        scanner.feed(_TRACEBACK_LINE * 48)
        self.assertEqual(scanner.markers, ("EDQUOT x48",))

    def test_a_marker_split_across_chunks_is_still_counted(self) -> None:
        scanner = MarkerScanner()
        data = b"No space left on device\n"
        for cut in range(1, len(data)):
            split_scanner = MarkerScanner()
            split_scanner.feed(data[:cut])
            split_scanner.feed(data[cut:])
            self.assertEqual(
                split_scanner.markers, ("ENOSPC",), f"lost at cut {cut}"
            )
        scanner.feed(data)
        self.assertEqual(scanner.markers, ("ENOSPC",))

    def test_a_marker_ending_at_a_chunk_boundary_is_not_double_counted(
        self,
    ) -> None:
        scanner = MarkerScanner()
        scanner.feed(b"prefix [Errno 122]")
        scanner.feed(b" suffix with no marker")
        self.assertEqual(scanner.markers, ("EDQUOT",))

    def test_clean_output_raises_no_suspicion(self) -> None:
        scanner = MarkerScanner()
        scanner.feed(b"....\nOK (skipped=2)\n")
        self.assertFalse(scanner.suspected)
        self.assertEqual(scanner.markers, ())


class DiagnosisTest(unittest.TestCase):
    def _diagnosis(self, **overrides) -> DiskPressureDiagnosis:
        values = dict(
            stage="suite-stderr",
            markers=("EDQUOT x48",),
            temp_base="/tmp",
            used_percent=3,
            inode_percent=1,
            existing_roots=5,
        )
        values.update(overrides)
        return DiskPressureDiagnosis(**values)

    def test_the_note_leads_with_the_stable_token(self) -> None:
        reasons = self._diagnosis().reasons
        self.assertTrue(reasons[0].startswith(PRESSURE_NOTE))
        self.assertIn("EDQUOT x48", reasons[0])
        self.assertIn("blocks 3% used", reasons[1])
        self.assertIn("inodes 1% used", reasons[1])
        self.assertIn("5", reasons[1])

    def test_unmeasured_probe_values_are_said_not_faked_as_zero(self) -> None:
        reasons = self._diagnosis(
            used_percent=None, inode_percent=None, existing_roots=None
        ).reasons
        self.assertIn("unmeasured", reasons[1])
        self.assertNotIn("0%", reasons[1])

    def test_default_output_carries_no_absolute_temp_base_path(self) -> None:
        diagnosis = self._diagnosis(temp_base="/home/operator/secret-base")
        for line in diagnosis.reasons:
            self.assertNotIn("/home/operator/secret-base", line)
        payload = diagnosis.as_dict()
        self.assertNotIn("/home/operator/secret-base", payload["temp_base"])
        revealed = diagnosis.as_dict(reveal_paths=True)
        self.assertEqual(revealed["temp_base"], "/home/operator/secret-base")

    def test_the_recovery_line_never_promises_automatic_cleanup(self) -> None:
        """Leftover roots are counted and reported, never removed (#15710
        fail-closed principle: no positive proof of no live/foreign owner)."""
        reasons = self._diagnosis().reasons
        self.assertIn("not removed", reasons[1])
        self.assertIn("operator environment concern", reasons[2])


class OutcomeAnnotationTest(unittest.TestCase):
    def test_the_annotation_never_flips_a_red_run_green(self) -> None:
        outcome = dataclasses.replace(
            IsolatedRunOutcome(suite_success=False, returncode=1),
            disk_pressure=DiskPressureDiagnosis(
                stage="suite-stderr", markers=("EDQUOT",), temp_base="/tmp"
            ),
        )
        self.assertFalse(outcome.success)
        joined = "\n".join(outcome.all_reasons)
        self.assertIn(PRESSURE_NOTE, joined)
        self.assertIn("test suite failed", joined)

    def test_a_green_run_stays_green_and_the_payload_carries_the_note(
        self,
    ) -> None:
        outcome = dataclasses.replace(
            IsolatedRunOutcome(suite_success=True, returncode=0),
            disk_pressure=DiskPressureDiagnosis(
                stage="suite-stderr", markers=("ENOSPC",), temp_base="/tmp"
            ),
        )
        self.assertTrue(outcome.success)
        payload = outcome.as_dict()
        self.assertTrue(payload["disk_pressure"]["suspected"])
        self.assertEqual(payload["disk_pressure"]["markers"], ["ENOSPC"])

    def test_without_a_diagnosis_the_payload_says_none(self) -> None:
        outcome = IsolatedRunOutcome(suite_success=True, returncode=0)
        self.assertIsNone(outcome.as_dict()["disk_pressure"])
        self.assertEqual(
            outcome.all_reasons, IsolatedRunOutcome(True, returncode=0).all_reasons
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
