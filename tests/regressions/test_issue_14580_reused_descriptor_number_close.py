"""Regression pin for Redmine #14580: a close unwind must not close a reused
descriptor number.

Behavior-preserving move out of the 3,865-line
`tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py`
per the #14660 characterization (§5.5 移設先 module の確定) and the placement
ruling in `vibes/docs/logics/tests-placement-discovery-policy.md`
`## #14660 legacy mirror family 裁定`. Test bodies are unchanged; only the
module frame and import paths moved (Redmine #14666, T1 move-only).

Redmine #14684 (T6) then took the descriptor tracking and the failing close
from `tests/support/legacy_mirror_fault_schedule.py`. Taking the freed number
is what this pin is *about*, so it stays here as the schedule's
``before_raising`` payload rather than becoming shared behaviour.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from tests.support.legacy_mirror_fault_schedule import (  # noqa: E402
    FaultSchedule,
)
from tests.support.legacy_mirror_tree_fixture import (  # noqa: E402
    _MirrorTreeFixture,
)


class Issue14580ReusedDescriptorNumberCloseTest(_MirrorTreeFixture):
    """An unwinding close must never close a *reused* descriptor number."""

    def test_a_close_unwind_never_closes_a_reused_descriptor_number(self) -> None:
        """j#90477 R11-F1, the damaging half.

        Setting the ownership sentinel *after* the close meant a raising close
        left the number owned, and a later `finally` closed it again. Descriptor
        numbers are reused immediately, so the second close hit an unrelated
        handle — measured closing a `/dev/null` descriptor that had just been
        assigned the freed number. Failing every close cannot detect this; the
        number has to actually be reused.
        """
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        real_open, real_close = os.open, os.close
        state: dict[str, object] = {"reused": None}

        def take_the_freed_number() -> None:
            """Grab the freed number before anyone else can.

            `real_open` is the unpatched primitive, so this does not disturb the
            staging descriptor the schedule recorded.
            """
            state["reused"] = real_open(os.devnull, os.O_RDONLY)

        schedule = FaultSchedule().track_descriptors()
        schedule.raise_after_closing(
            "staging",
            RuntimeError("injected close unwind"),
            before_raising=take_the_freed_number,
        )
        schedule.raise_on("write", RuntimeError("injected write unwind"))
        with schedule:
            with self.assertRaises(RuntimeError):
                self._service(repo).sync()

        reused = state["reused"]
        self.assertIsNotNone(reused, "the close injection never fired")
        self.assertEqual(
            schedule.staging, reused, "the number was not reused; the case is not exercised"
        )
        try:
            os.fstat(int(reused))  # type: ignore[arg-type]
        except OSError as exc:
            self.fail(f"the sync closed a descriptor it did not own (errno {exc.errno})")
        real_close(int(reused))  # type: ignore[arg-type]

        self.assertEqual([], self._staging_names(repo), "the staging entry survived")

    def test_the_directory_walk_never_closes_a_reused_descriptor_number(self) -> None:
        """j#90482 R12-F1. The same defect R11-F1 fixed for the staging
        descriptor still lived in the component walk: `_close_quietly(parent)`
        unwinding meant `parent = child` was never reached, so the `finally`
        closed the freed number again — measured closing a `/dev/null` handle
        that had taken it.
        """
        repo = self._stage()
        real_open, real_close = os.open, os.close
        state: dict[str, object] = {"reused": None}

        def take_the_freed_number() -> None:
            state["reused"] = real_open(os.devnull, os.O_RDONLY)

        schedule = FaultSchedule().track_descriptors()
        schedule.raise_after_closing(
            "walk_root",
            RuntimeError("injected walk close unwind"),
            before_raising=take_the_freed_number,
        )
        with schedule:
            with self.assertRaises(RuntimeError):
                self._service(repo).audit()

        reused = state["reused"]
        self.assertIsNotNone(reused, "the walk close injection never fired")
        self.assertEqual(
            schedule.walk_root, reused, "the number was not reused; the case is not exercised"
        )
        try:
            os.fstat(int(reused))  # type: ignore[arg-type]
        except OSError as exc:
            self.fail(f"the walk closed a descriptor it did not own (errno {exc.errno})")
        real_close(int(reused))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
