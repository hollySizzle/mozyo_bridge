"""Unit tests for scripts/compute_testpypi_dev_version.py.

The script is the only piece of automated-TestPyPI-dev-publish logic
(Redmine #12756) that has branching behaviour, so it is pinned here. It lives
under scripts/ (not src/) because it must run dependency-free in a fresh CI
checkout before the package is installed.
"""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = ROOT / "scripts" / "compute_testpypi_dev_version.py"

_spec = importlib.util.spec_from_file_location("compute_testpypi_dev_version", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mod)


_SAMPLE_PYPROJECT = (
    "[project]\n"
    'name = "mozyo-bridge"\n'
    'version = "0.9.2"\n'
    'description = "x"\n'
)


class ReadBaseVersionTests(unittest.TestCase):
    def test_reads_project_version(self) -> None:
        self.assertEqual("0.9.2", mod.read_base_version(_SAMPLE_PYPROJECT))

    def test_missing_version_raises(self) -> None:
        with self.assertRaises(mod.DevVersionError):
            mod.read_base_version("[project]\nname = \"x\"\n")


class BuildDevVersionTests(unittest.TestCase):
    def test_appends_dev_segment(self) -> None:
        self.assertEqual(
            "0.9.2.dev20260628090000",
            mod.build_dev_version("0.9.2", "20260628090000"),
        )

    def test_is_pep440_dev_release(self) -> None:
        # Trailing .devN release sorts BEFORE the final release, which is the
        # intended ordering for dev artifacts.
        version = mod.build_dev_version("0.9.2", "123")
        self.assertTrue(mod._DEV_VERSION_PATTERN.match(version))

    def test_non_digit_dev_number_rejected(self) -> None:
        for bad in ("abc", "12.3", "12a", "", "-1", "20260628 090000"):
            with self.subTest(bad=bad):
                with self.assertRaises(mod.DevVersionError):
                    mod.build_dev_version("0.9.2", bad)

    def test_double_dev_rejected(self) -> None:
        with self.assertRaises(mod.DevVersionError):
            mod.build_dev_version("0.9.2.dev1", "2")


class RewriteVersionTests(unittest.TestCase):
    def test_rewrites_only_version_field(self) -> None:
        out = mod.rewrite_version(_SAMPLE_PYPROJECT, "0.9.2.dev42")
        self.assertIn('version = "0.9.2.dev42"', out)
        # Name and description untouched.
        self.assertIn('name = "mozyo-bridge"', out)
        self.assertIn('description = "x"', out)
        # The original release string is gone.
        self.assertNotIn('version = "0.9.2"', out)

    def test_missing_version_field_raises(self) -> None:
        with self.assertRaises(mod.DevVersionError):
            mod.rewrite_version("[project]\nname = \"x\"\n", "1.0.0.dev1")


class MainTests(unittest.TestCase):
    def _write_pyproject(self, tmp: Path) -> Path:
        path = tmp / "pyproject.toml"
        path.write_text(_SAMPLE_PYPROJECT, encoding="utf-8")
        return path

    def test_write_rewrites_file_and_prints_version(self) -> None:
        import io
        import contextlib
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_pyproject(Path(tmp))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mod.main(
                    ["--pyproject", str(path), "--dev-number", "777", "--write"]
                )
            self.assertEqual(0, rc)
            self.assertEqual("0.9.2.dev777", buf.getvalue().strip())
            self.assertIn('version = "0.9.2.dev777"', path.read_text(encoding="utf-8"))

    def test_without_write_leaves_file_unchanged(self) -> None:
        import io
        import contextlib
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_pyproject(Path(tmp))
            original = path.read_text(encoding="utf-8")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mod.main(["--pyproject", str(path), "--dev-number", "5"])
            self.assertEqual(0, rc)
            self.assertEqual("0.9.2.dev5", buf.getvalue().strip())
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_missing_dev_number_errors(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_pyproject(Path(tmp))
            # Explicit empty dev number, no env fallback.
            rc = mod.main(["--pyproject", str(path), "--dev-number", ""])
            self.assertEqual(2, rc)


if __name__ == "__main__":
    unittest.main()
