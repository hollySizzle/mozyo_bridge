"""Unit tests for scripts/compute_testpypi_dev_version.py.

The script is the only piece of automated-TestPyPI-dev-publish logic
(Redmine #12756 / #13586) that has branching behaviour, so it is pinned here.
It lives under scripts/ (not src/) because it must run dependency-free in a
fresh CI checkout before the package is installed; it reuses the stdlib-only
canonical mirror primitives (the ``version_mirror`` module in the release
version-governance Feature package) so the
wheel METADATA and the runtime ``__version__`` are rewritten to the SAME exact
dev version.
"""

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = ROOT / "scripts" / "compute_testpypi_dev_version.py"

_spec = importlib.util.spec_from_file_location("compute_testpypi_dev_version", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mod)


# A minimal fake repo carrying the two mirror files plus the contract doc that
# declares them. Mirrors the shape used by the release-helper integration
# tests so both consumers of the mirror primitive exercise the same layout.
_CONTRACT_TEXT = (
    "# Contract\n\n"
    "release-version mirror set は以下の 2 file に固定する。\n\n"
    "- `pyproject.toml` の `[project].version`\n"
    "- `src/mozyo_bridge/__init__.py` の `__version__`\n\n"
    "Other section.\n"
)


def _build_repo(
    root: Path,
    *,
    pyproject_version: str = "0.9.2",
    module_version: str = "0.9.2",
    module_body: str | None = None,
) -> Path:
    (root / "pyproject.toml").write_text(
        "[project]\n"
        'name = "mozyo-bridge"\n'
        f'version = "{pyproject_version}"\n'
        'description = "x"\n',
        encoding="utf-8",
    )
    module_dir = root / "src" / "mozyo_bridge"
    module_dir.mkdir(parents=True)
    if module_body is None:
        module_body = f'"""pkg."""\n\n__version__ = "{module_version}"\n'
    (module_dir / "__init__.py").write_text(module_body, encoding="utf-8")
    contract_dir = root / "vibes" / "docs" / "logics"
    contract_dir.mkdir(parents=True)
    (contract_dir / "release-helper-contract.md").write_text(
        _CONTRACT_TEXT, encoding="utf-8"
    )
    return root / "pyproject.toml"


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


class ReadMirrorBaseVersionTests(unittest.TestCase):
    def test_equal_mirror_returns_base(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import (
            version_mirror,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_repo(root, pyproject_version="0.9.2", module_version="0.9.2")
            mirror = version_mirror.load_mirror_set(root)
            base, entries = mod.read_mirror_base_version(mirror)
            self.assertEqual("0.9.2", base)
            self.assertEqual(2, len(entries))

    def test_disagreeing_mirror_raises(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import (
            version_mirror,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_repo(root, pyproject_version="0.9.2", module_version="0.9.1")
            mirror = version_mirror.load_mirror_set(root)
            with self.assertRaises(mod.DevVersionError):
                mod.read_mirror_base_version(mirror)

    def test_missing_module_literal_raises(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import (
            version_mirror,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_repo(root, module_body="# version moved elsewhere\n")
            mirror = version_mirror.load_mirror_set(root)
            with self.assertRaises(mod.DevVersionError):
                mod.read_mirror_base_version(mirror)


class MainTests(unittest.TestCase):
    def _run(self, argv: list[str]) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod.main(argv)
        return rc, buf.getvalue().strip()

    def test_write_mirrors_every_file_and_prints_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pyproject = _build_repo(root, pyproject_version="0.9.2", module_version="0.9.2")
            rc, out = self._run(
                ["--pyproject", str(pyproject), "--dev-number", "777", "--write"]
            )
            self.assertEqual(0, rc)
            self.assertEqual("0.9.2.dev777", out)
            # BOTH mirror files carry the exact same dev version.
            self.assertIn(
                'version = "0.9.2.dev777"',
                pyproject.read_text(encoding="utf-8"),
            )
            self.assertIn(
                '__version__ = "0.9.2.dev777"',
                (root / "src" / "mozyo_bridge" / "__init__.py").read_text(
                    encoding="utf-8"
                ),
            )

    def test_without_write_leaves_every_file_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pyproject = _build_repo(root, pyproject_version="0.9.2", module_version="0.9.2")
            module = root / "src" / "mozyo_bridge" / "__init__.py"
            before_py = pyproject.read_text(encoding="utf-8")
            before_mod = module.read_text(encoding="utf-8")
            rc, out = self._run(["--pyproject", str(pyproject), "--dev-number", "5"])
            self.assertEqual(0, rc)
            self.assertEqual("0.9.2.dev5", out)
            self.assertEqual(before_py, pyproject.read_text(encoding="utf-8"))
            self.assertEqual(before_mod, module.read_text(encoding="utf-8"))

    def test_base_mismatch_leaves_both_files_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pyproject = _build_repo(
                root, pyproject_version="0.9.2", module_version="0.9.1"
            )
            module = root / "src" / "mozyo_bridge" / "__init__.py"
            before_py = pyproject.read_text(encoding="utf-8")
            before_mod = module.read_text(encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                rc = mod.main(
                    ["--pyproject", str(pyproject), "--dev-number", "9", "--write"]
                )
            self.assertEqual(1, rc)
            # Neither file was touched — no partial write.
            self.assertEqual(before_py, pyproject.read_text(encoding="utf-8"))
            self.assertEqual(before_mod, module.read_text(encoding="utf-8"))

    def test_missing_module_literal_leaves_pyproject_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pyproject = _build_repo(root, module_body="# version moved elsewhere\n")
            module = root / "src" / "mozyo_bridge" / "__init__.py"
            before_py = pyproject.read_text(encoding="utf-8")
            before_mod = module.read_text(encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                rc = mod.main(
                    ["--pyproject", str(pyproject), "--dev-number", "9", "--write"]
                )
            self.assertEqual(1, rc)
            self.assertEqual(before_py, pyproject.read_text(encoding="utf-8"))
            self.assertEqual(before_mod, module.read_text(encoding="utf-8"))

    def test_missing_dev_number_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pyproject = _build_repo(root)
            with contextlib.redirect_stderr(io.StringIO()):
                rc = mod.main(["--pyproject", str(pyproject), "--dev-number", ""])
            self.assertEqual(2, rc)

    def test_missing_contract_doc_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pyproject = _build_repo(root)
            (root / "vibes" / "docs" / "logics" / "release-helper-contract.md").unlink()
            with contextlib.redirect_stderr(io.StringIO()):
                rc = mod.main(["--pyproject", str(pyproject), "--dev-number", "9"])
            self.assertEqual(1, rc)


if __name__ == "__main__":
    unittest.main()
