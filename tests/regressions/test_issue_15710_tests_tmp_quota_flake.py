"""Regression: /tmp per-user quota flake resilience and diagnosis (Redmine #15710).

Three observed full runs died intermittently with ``OSError: [Errno 122]
Disk quota exceeded`` while writing under ``/tmp/mozyo-tests-home-*``,
producing 48 / 41 false test errors although ``df`` showed /tmp at 3%
blocks / 1% inodes — per-user tmpfs quota or transient pressure. Cleaning
leftover roots recovered every time.

Pinned here:

- the runner's own capacity refusal during temp-root setup is surfaced as
  the typed environmental note, and no suite runs (fail-closed);
- suite stderr is streamed through unchanged while being scanned for the
  errno markers;
- ``MOZYO_TESTS_TMPDIR`` relocates the task root declaratively, fails
  closed on an unusable declaration, and never leaks into the fenced
  child env (safety condition: a nested guarded run must not escape its
  parent's task root).
"""

from __future__ import annotations

import errno
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application import (
    commands_test_run,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.test_temp_root import (
    TempRootUnavailable,
    resolve_tests_temp_base,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_disk_pressure import (
    PRESSURE_NOTE,
    MarkerScanner,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_home_isolation import (
    TESTS_TEMP_BASE_ENV,
    apply_isolation,
)


class DeclaredTempBaseTest(unittest.TestCase):
    def test_unset_means_the_default_temp_root(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(resolve_tests_temp_base())
        self.assertIsNone(resolve_tests_temp_base({TESTS_TEMP_BASE_ENV: "  "}))

    def test_a_usable_declaration_is_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            resolved = resolve_tests_temp_base({TESTS_TEMP_BASE_ENV: base})
            self.assertEqual(resolved, Path(base).resolve())

    def test_a_missing_declared_base_fails_closed(self) -> None:
        """No silent fallback: /tmp is exactly what the declaration escapes."""
        with tempfile.TemporaryDirectory() as base:
            gone = str(Path(base) / "does-not-exist")
        with self.assertRaises(TempRootUnavailable) as ctx:
            resolve_tests_temp_base({TESTS_TEMP_BASE_ENV: gone})
        self.assertIn(TESTS_TEMP_BASE_ENV, str(ctx.exception))

    def test_a_file_declared_as_base_fails_closed(self) -> None:
        with tempfile.NamedTemporaryFile() as not_a_dir:
            with self.assertRaises(TempRootUnavailable):
                resolve_tests_temp_base({TESTS_TEMP_BASE_ENV: not_a_dir.name})

    def test_the_task_root_lands_under_the_declared_base(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            with patch.dict(os.environ, {TESTS_TEMP_BASE_ENV: base}):
                with commands_test_run._make_task_root() as root:
                    root_path = Path(root)
                    self.assertEqual(root_path.parent, Path(base).resolve())
                    self.assertTrue(
                        root_path.name.startswith("mozyo-tests-home-")
                    )

    def test_the_declaration_never_reaches_the_fenced_child(self) -> None:
        """A nested guarded run inside the fence must use the pinned fenced
        tmp, not escape to the operator's declared base."""
        env = apply_isolation(
            {TESTS_TEMP_BASE_ENV: "/somewhere/roomy", "KEEP": "1"}, {}
        )
        self.assertNotIn(TESTS_TEMP_BASE_ENV, env)
        self.assertEqual(env["KEEP"], "1")


class SetupPressureIsRefusedAsEnvironmentalTest(unittest.TestCase):
    def _run(self, side_effect: OSError) -> tuple[int, str]:
        stderr = io.StringIO()
        args = type(
            "Args", (), {"repo": None, "unittest_args": (), "format": "text"}
        )()
        with patch.object(
            commands_test_run.tempfile,
            "TemporaryDirectory",
            side_effect=side_effect,
        ), patch.object(
            commands_test_run.subprocess, "Popen",
            side_effect=AssertionError("the suite must not run"),
        ), patch.object(
            commands_test_run, "resolve_repo_root", return_value=Path.cwd()
        ), patch("sys.stderr", stderr):
            code = commands_test_run.cmd_tests_run(args)
        return code, stderr.getvalue()

    def test_an_edquot_during_setup_is_typed_environmental_and_runs_nothing(
        self,
    ) -> None:
        code, err = self._run(OSError(errno.EDQUOT, "Disk quota exceeded"))
        self.assertEqual(code, 1)
        self.assertIn(PRESSURE_NOTE, err)
        self.assertIn("EDQUOT", err)
        self.assertIn("temp-root-setup", err)

    def test_an_enospc_during_setup_is_typed_environmental(self) -> None:
        code, err = self._run(OSError(errno.ENOSPC, "No space left on device"))
        self.assertEqual(code, 1)
        self.assertIn(PRESSURE_NOTE, err)
        self.assertIn("ENOSPC", err)

    def test_an_unrelated_oserror_is_not_relabelled_environmental(self) -> None:
        """EACCES must propagate untouched: calling a permission failure
        "disk pressure" would hide a real fence or setup defect."""
        args = type(
            "Args", (), {"repo": None, "unittest_args": (), "format": "text"}
        )()
        with patch.object(
            commands_test_run.tempfile,
            "TemporaryDirectory",
            side_effect=OSError(errno.EACCES, "Permission denied"),
        ), patch.object(
            commands_test_run, "resolve_repo_root", return_value=Path.cwd()
        ):
            with self.assertRaises(OSError) as ctx:
                commands_test_run.cmd_tests_run(args)
        self.assertEqual(ctx.exception.errno, errno.EACCES)


class _StderrSink:
    """A stand-in for ``sys.stderr`` exposing only the ``buffer`` the
    streaming pump writes through."""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()


class SuiteStderrIsStreamedAndScannedTest(unittest.TestCase):
    def _scanned(self, program: str) -> tuple[int, MarkerScanner, bytes]:
        scanner = MarkerScanner()
        sink = _StderrSink()
        with patch("sys.stderr", sink):
            code = commands_test_run._scanned_run(
                [sys.executable, "-c", program],
                cwd=os.getcwd(),
                env=dict(os.environ),
                scanner=scanner,
            )
        return code, scanner, sink.buffer.getvalue()

    def test_quota_tracebacks_on_child_stderr_are_counted_and_passed_through(
        self,
    ) -> None:
        code, scanner, echoed = self._scanned(
            "import sys\n"
            "for _ in range(3):\n"
            "    sys.stderr.write("
            "\"OSError: [Errno 122] Disk quota exceeded: '/tmp/x'\\n\")\n"
            "sys.exit(1)\n"
        )
        self.assertEqual(code, 1)
        self.assertEqual(scanner.markers, ("EDQUOT x3",))
        # Pass-through is byte-identical: the scan must not eat the child's
        # own diagnostics.
        self.assertEqual(echoed.count(b"[Errno 122]"), 3)

    def test_a_clean_child_is_not_suspected_and_its_exit_code_is_kept(
        self,
    ) -> None:
        code, scanner, echoed = self._scanned(
            "import sys; sys.stderr.write('....\\nOK\\n'); sys.exit(0)"
        )
        self.assertEqual(code, 0)
        self.assertFalse(scanner.suspected)
        self.assertIn(b"OK", echoed)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
