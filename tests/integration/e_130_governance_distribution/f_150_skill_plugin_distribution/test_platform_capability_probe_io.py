"""Host capability probe I/O tests (Redmine #14651).

Behavior-preserving move out of the 3,865-line
`tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py`
per the #14660 characterization (§5.5 移設先 module の確定) and the placement
ruling in `vibes/docs/logics/tests-placement-discovery-policy.md`
`## #14660 legacy mirror family 裁定`. Test bodies are unchanged; only the
module frame and import paths moved (Redmine #14666, T1 move-only).
"""

from __future__ import annotations

import ast
import errno
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution.application import (  # noqa: E402
    legacy_mirror_sync,
    owned_descriptors,
    platform_capabilities,
)
from mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution.domain.legacy_mirror_contract import (  # noqa: E402
    PLATFORM_UNSUPPORTED,
)
from tests.support.legacy_mirror_tree_fixture import (  # noqa: E402
    _MirrorTreeFixture,
)


class PlatformCapabilityProbeIoTest(_MirrorTreeFixture):
    """The capability probe where it touches a real collaborator: the call
    surface it measures, the descriptors it must not leak, and setup failure."""

    # --- R7-F4 / #14651: the capability probe measures the call surface -------

    @staticmethod
    def _call_surface_sources() -> list[Path]:
        """Every module of the package except the prober itself.

        Naming the files here is what went wrong before: the primitives were
        split across two modules when the service crossed the module-health
        threshold, and a fence that reads only one of them goes blind to the
        other (j#90458 R8-F4). The prober is excluded because its own probe
        calls would otherwise satisfy the fence with themselves.
        """
        package = Path(legacy_mirror_sync.__file__).parent
        prober = Path(platform_capabilities.__file__)
        return sorted(path for path in package.glob("*.py") if path != prober)

    @staticmethod
    def _os_calls_taking_a_dir_fd(sources: list[Path]) -> set[str]:
        """Read the call surface out of the source instead of listing it.

        Two review rounds found the manifest listing a primitive nothing calls
        and omitting one every call goes through. A hand-written list of call
        sites in the test reproduces that failure mode one level up, so the
        oracle is the AST: any ``os.<name>(...)`` passing ``dir_fd`` /
        ``src_dir_fd`` / ``dst_dir_fd``.
        """
        found: set[str] = set()
        for path in sources:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                if not (
                    isinstance(function, ast.Attribute)
                    and isinstance(function.value, ast.Name)
                    and function.value.id == "os"
                ):
                    continue
                if any(
                    keyword.arg in ("dir_fd", "src_dir_fd", "dst_dir_fd")
                    for keyword in node.keywords
                ):
                    found.add(function.attr)
        return found

    def test_capability_manifest_is_exactly_the_primitives_the_module_calls(self) -> None:
        """Guard the manifest against the module drifting away from it —
        in both directions. `os.stat` was listed and never called; `os.lstat`
        was called and never listed (j#90450 R7-F4)."""
        sources = self._call_surface_sources()
        self.assertIn(Path(legacy_mirror_sync.__file__), sources)
        self.assertIn(Path(owned_descriptors.__file__), sources)

        called = self._os_calls_taking_a_dir_fd(sources)
        body = "\n".join(path.read_text(encoding="utf-8") for path in sources)
        if "os.scandir(" in body:
            # `scandir` takes its descriptor positionally, so the AST cannot
            # tell it from a path argument; it is named here instead.
            called.add("scandir")
        listed = {name for name, _label, _probe in platform_capabilities._REQUIRED_DIR_FD_CALLS}
        self.assertEqual(
            called,
            listed,
            "the manifest and the call surface disagree",
        )
        # The two the advertisement gets wrong, spelled out so a regression
        # names them rather than printing a set difference.
        self.assertIn("lstat", listed, "lstat(dir_fd=) is not in the manifest")
        self.assertIn("replace", listed, "replace is what the swap calls, not rename")

    def test_each_required_capability_individually_fails_closed(self) -> None:
        """A host that cannot provide one primitive must refuse, whichever
        way it says so: `NotImplementedError` is what CPython raises for an
        unavailable `dir_fd`, and a primitive that never took the keyword
        raises `TypeError`."""
        repo = self._stage()

        def unavailable(*_args: object, **_kwargs: object) -> None:
            raise NotImplementedError("dir_fd unavailable on this platform")

        def without_the_keyword(*_args: object) -> None:
            """Accepts the positional arguments and nothing else."""

        for name, label, _probe in platform_capabilities._REQUIRED_DIR_FD_CALLS:
            for host, stub in (
                ("NotImplementedError", unavailable),
                ("TypeError", without_the_keyword),
            ):
                with self.subTest(capability=label, host=host):
                    service = self._service(repo)
                    with unittest.mock.patch.object(os, name, stub):
                        missing = platform_capabilities.missing_platform_capabilities()
                        audit = service.audit()
                        code, out, _err = service.sync()
                    self.assertIn(label, missing)
                    self.assertIn(PLATFORM_UNSUPPORTED, audit.kinds())
                    self.assertTrue(audit.blocks_write)
                    self.assertEqual(1, code)
                    self.assertEqual((), out)

    def test_a_scandir_whose_failure_is_deferred_still_fails_closed(self) -> None:
        """`os.scandir` hands back an iterator before it has opened anything —
        CPython leaves the `fdopendir` to the first step. A probe that only
        constructed the iterator would read a host that cannot open a directory
        by descriptor at all as capable, so the probe steps it."""
        repo = self._stage()

        class DeferredFailure:
            def __iter__(self) -> object:
                return self

            def __next__(self) -> object:
                raise NotImplementedError("fd support unavailable")

            def close(self) -> None:
                """Constructing and closing it says nothing about the host."""

        service = self._service(repo)
        with unittest.mock.patch.object(os, "scandir", lambda _fd: DeferredFailure()):
            missing = platform_capabilities.missing_platform_capabilities()
            audit = service.audit()
        self.assertIn("scandir(fd)", missing)
        self.assertIn(PLATFORM_UNSUPPORTED, audit.kinds())
        self.assertTrue(audit.blocks_write)

    def test_the_probe_writes_nothing_and_leaks_no_descriptor(self) -> None:
        """The anchor is not a directory, so every `*at()` call is rejected
        before the relative name is resolved. That is what makes the probe
        side-effect-free; measure it rather than trust the docstring."""
        scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        origin = os.getcwd()
        os.chdir(scratch)
        self.addCleanup(os.chdir, origin)

        before = self._open_descriptors()
        for _ in range(32):
            self.assertEqual((), platform_capabilities.missing_platform_capabilities())
        self.assertEqual([], sorted(scratch.rglob("*")), "the probe left residue behind")
        self.assertEqual(before, self._open_descriptors(), "the probe leaked a descriptor")

    def test_a_probe_that_cannot_be_set_up_fails_closed(self) -> None:
        """Not being able to measure the host is not the same as the host
        being capable. It refuses for the same reason a missing primitive
        does, and says which of the two happened."""
        repo = self._stage()
        exhausted = unittest.mock.Mock(side_effect=OSError(errno.EMFILE, "too many open files"))
        with unittest.mock.patch.object(os, "pipe", exhausted):
            service = self._service(repo)
            missing = platform_capabilities.missing_platform_capabilities()
            audit = service.audit()
            code, out, _err = service.sync()
        self.assertEqual((platform_capabilities.PROBE_UNAVAILABLE,), missing)
        self.assertIn(PLATFORM_UNSUPPORTED, audit.kinds())
        self.assertTrue(audit.blocks_write)
        self.assertEqual(1, code)
        self.assertEqual((), out)

    @staticmethod
    def _open_descriptors() -> list[int]:
        live: list[int] = []
        for fd in range(1024):
            try:
                os.fstat(fd)
            except OSError:
                continue
            live.append(fd)
        return live


if __name__ == "__main__":
    unittest.main()
