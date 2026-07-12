"""Regression: compute_testpypi_dev_version --write must not leave a partial
mirror on a write-time I/O failure (Redmine #13586, finding R5).

The two mirror files (pyproject.toml + src/mozyo_bridge/__init__.py) are
rewritten to the same dev version. Before the fix, the writes were applied
sequentially with no rollback: if the SECOND file could not be written (e.g.
read-only or full disk), the FIRST file was already rewritten and left the
checkout half-updated, contradicting the Start-Gate postcondition "a failure
never leaves the mirror set partially updated" (j#75722).

This pins the rollback behaviour by making the second mirror file read-only and
asserting the command returns non-zero AND both files are byte-unchanged.
"""

import importlib.util
import os
import stat
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
