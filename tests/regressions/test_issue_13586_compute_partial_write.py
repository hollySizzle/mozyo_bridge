"""Regression: compute_testpypi_dev_version --write must not leave a partial
mirror on a write-time I/O failure (Redmine #13586, findings R5 and R6).

The two mirror files (pyproject.toml + src/mozyo_bridge/__init__.py) are
rewritten to the same dev version. Two failure modes are pinned:

- R5: the SECOND file cannot be written at all (open fails, e.g. read-only), so
  the FIRST must be rolled back. Reproduced by making the file read-only.
- R6: the SECOND file's write fails AFTER truncating / partially writing it
  (e.g. a full disk), so the current file itself — not just files written
  before it — must be rolled back. Reproduced by patching write_text to write
  partial bytes and then raise.

Both assert the command returns non-zero AND both files are byte-identical to
their pre-write contents, honouring the Start-Gate postcondition "a failure
never leaves the mirror set partially updated" (j#75722).
"""

import importlib.util
import os
import pathlib
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "scripts" / "compute_testpypi_dev_version.py"

_spec = importlib.util.spec_from_file_location("compute_testpypi_dev_version", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mod)

_CONTRACT_TEXT = (
    "# Contract\n\n"
    "release-version mirror set は以下の 2 file に固定する。\n\n"
    "- `pyproject.toml` の `[project].version`\n"
    "- `src/mozyo_bridge/__init__.py` の `__version__`\n\n"
)


def _build_repo(root: Path, version: str = "0.10.0") -> Path:
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "mozyo-bridge"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    module_dir = root / "src" / "mozyo_bridge"
    module_dir.mkdir(parents=True)
    (module_dir / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    contract_dir = root / "vibes" / "docs" / "logics"
    contract_dir.mkdir(parents=True)
    (contract_dir / "release-helper-contract.md").write_text(
        _CONTRACT_TEXT, encoding="utf-8"
    )
    return root / "pyproject.toml"


@unittest.skipIf(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    "read-only enforcement does not hold for root",
)
class ComputePartialWriteRegressionTest(unittest.TestCase):
    def test_second_mirror_write_failure_rolls_back_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pyproject = _build_repo(root)
            module = root / "src" / "mozyo_bridge" / "__init__.py"
            before_py = pyproject.read_text(encoding="utf-8")
            before_mod = module.read_text(encoding="utf-8")

            # Make the second mirror file (written after pyproject) read-only so
            # its write fails mid-way through the write phase.
            module.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            try:
                rc = mod.main(
                    ["--pyproject", str(pyproject), "--dev-number", "777", "--write"]
                )
            finally:
                module.chmod(
                    stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
                )

            self.assertEqual(1, rc)
            # The first mirror file must have been rolled back: no partial write.
            self.assertEqual(
                before_py,
                pyproject.read_text(encoding="utf-8"),
                "pyproject.toml was left rewritten after a mid-write failure",
            )
            self.assertEqual(before_mod, module.read_text(encoding="utf-8"))

    def test_second_mirror_partial_write_then_raise_rolls_back(self) -> None:
        # R6: the write of the second mirror file truncates / partially writes
        # it and THEN raises (e.g. a full disk). The current file — not just the
        # files written before it — must be restored. This case is not covered
        # by the read-only reproduction, where the write fails at open before
        # any truncation.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pyproject = _build_repo(root)
            module = root / "src" / "mozyo_bridge" / "__init__.py"
            before_py = pyproject.read_bytes()
            before_mod = module.read_bytes()

            real_write_text = pathlib.Path.write_text

            def flaky_write_text(self, data, *args, **kwargs):  # noqa: ANN001
                if self.name == "__init__.py":
                    # Truncate + partially write, then fail — the classic
                    # full-disk shape that leaves the file corrupted on disk.
                    self.write_bytes(b'__version__ = "0.10')
                    raise OSError("simulated disk full")
                return real_write_text(self, data, *args, **kwargs)

            with mock.patch.object(pathlib.Path, "write_text", flaky_write_text):
                rc = mod.main(
                    ["--pyproject", str(pyproject), "--dev-number", "777", "--write"]
                )

            self.assertEqual(1, rc)
            # Both files must be byte-identical to their originals: the current
            # (partially-written) file was rolled back, not left truncated.
            self.assertEqual(
                before_mod,
                module.read_bytes(),
                "__init__.py was left partially written after a mid-write failure",
            )
            self.assertEqual(before_py, pyproject.read_bytes())


if __name__ == "__main__":
    unittest.main()
