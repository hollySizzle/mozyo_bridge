"""Regression pin for Redmine #14580: a close unwind must not close a reused
descriptor number.

Behavior-preserving move out of the 3,865-line
`tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py`
per the #14660 characterization (§5.5 移設先 module の確定) and the placement
ruling in `vibes/docs/logics/tests-placement-discovery-policy.md`
`## #14660 legacy mirror family 裁定`. Test bodies are unchanged; only the
module frame and import paths moved (Redmine #14666, T1 move-only).
"""

from __future__ import annotations

import os
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution.application import (  # noqa: E402
    legacy_mirror_sync,
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
        state: dict[str, object] = {"fd": None, "reused": None, "fired": False}

        def tracking_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
            fd = real_open(path, flags, *args, **kwargs)
            if flags & os.O_CREAT:
                state["fd"] = fd
            return fd

        def reusing_close(fd: int) -> None:
            if fd == state["fd"] and not state["fired"]:
                state["fired"] = True
                real_close(fd)
                # Grab the freed number before anyone else can.
                state["reused"] = real_open(os.devnull, os.O_RDONLY)
                raise RuntimeError("injected close unwind")
            real_close(fd)

        def unwinding_write(fd: int, data) -> int:  # type: ignore[no-untyped-def]
            raise RuntimeError("injected write unwind")

        with unittest.mock.patch.object(legacy_mirror_sync.os, "open", tracking_open):
            with unittest.mock.patch.object(
                legacy_mirror_sync.os, "close", reusing_close
            ):
                with unittest.mock.patch.object(
                    legacy_mirror_sync.os, "write", unwinding_write
                ):
                    with self.assertRaises(RuntimeError):
                        self._service(repo).sync()

        reused = state["reused"]
        self.assertIsNotNone(reused, "the close injection never fired")
        self.assertEqual(
            state["fd"], reused, "the number was not reused; the case is not exercised"
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
        state: dict[str, object] = {"root": None, "reused": None, "fired": False}

        def tracking_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
            fd = real_open(path, flags, *args, **kwargs)
            if state["root"] is None and "dir_fd" not in kwargs:
                state["root"] = fd
            return fd

        def reusing_close(fd: int) -> None:
            if fd == state["root"] and not state["fired"]:
                state["fired"] = True
                real_close(fd)
                state["reused"] = real_open(os.devnull, os.O_RDONLY)
                raise RuntimeError("injected walk close unwind")
            real_close(fd)

        with unittest.mock.patch.object(legacy_mirror_sync.os, "open", tracking_open):
            with unittest.mock.patch.object(
                legacy_mirror_sync.os, "close", reusing_close
            ):
                with self.assertRaises(RuntimeError):
                    self._service(repo).audit()

        reused = state["reused"]
        self.assertIsNotNone(reused, "the walk close injection never fired")
        self.assertEqual(
            state["root"], reused, "the number was not reused; the case is not exercised"
        )
        try:
            os.fstat(int(reused))  # type: ignore[arg-type]
        except OSError as exc:
            self.fail(f"the walk closed a descriptor it did not own (errno {exc.errno})")
        real_close(int(reused))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
