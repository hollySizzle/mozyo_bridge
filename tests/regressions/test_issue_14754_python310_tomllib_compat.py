"""Regression pin for the herdr pin posture's Python 3.10 TOML import (Redmine #14754).

`herdr_pin_posture_ops` imported `tomllib` unconditionally, but `tomllib` is stdlib
only on Python 3.11+ while the package declares `requires-python = ">=3.10"`. On 3.10
the module was unimportable, and because `cli_herdr_distribution` and
`herdr_integration_install_ops` import it, so was the whole herdr distribution
surface — `mozyo-bridge herdr pin-posture --help` died with ModuleNotFoundError
before parsing an argument. Fixed by binding the parser at import time (`tomllib` on
3.11+, `tomli` on 3.10, the `instruction_doctor` precedent) in commit
`40b61a703594325db594968a1d0e45c4abf4a634`.

Every test here detects the return of that one symptom; none of them asserts the
module's public contract (the pin posture's own behaviour is characterized in
`tests/unit/e_140_adapter_provider/f_130_terminal_runtime_provider/test_herdr_distribution.py`).
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

# Runs inside a child interpreter under the Python 3.10 import surface: stdlib
# `tomllib` unimportable, `tomli` present instead.
#
# On a real <3.11 interpreter that surface is already the truth, so the probe uses
# it as-is and asserts against the genuinely installed `tomli`. On 3.11+ it has to
# be simulated — `tomllib` is blocked and a *distinct* module object named `tomli`
# is put in its place, so a run that ends up bound to `tomllib` reads as a leak,
# not a pass. The shim exists because `tomli` is not installed on 3.11+ (its
# dependency marker is `python_version < '3.11'`): the pin is on the import shape,
# not on the wheel being present on an interpreter that does not need it.
_PY310_IMPORT_SHAPE_PROBE = '''
import sys
import types
import tempfile
from pathlib import Path

try:
    _real = __import__("tomllib")
except ModuleNotFoundError:
    # Genuine Python 3.10: the import shape under test is the live one. `tomli`
    # must be importable here — it is a declared dependency on this interpreter.
    __import__("tomli")
else:
    _shim = types.ModuleType("tomli")
    _shim.loads = _real.loads
    _shim.load = _real.load
    _shim.TOMLDecodeError = _real.TOMLDecodeError
    sys.modules["tomli"] = _shim
    del sys.modules["tomllib"]

    class _NoTomllib:
        """Make `import tomllib` fail exactly as it does on Python 3.10."""

        def find_spec(self, name, path=None, target=None):
            if name == "tomllib":
                raise ModuleNotFoundError("No module named 'tomllib'", name="tomllib")
            return None

    sys.meta_path.insert(0, _NoTomllib())

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (
    herdr_pin_posture_ops as ops,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_pin_posture import (
    PIN_MODE_OFFLINE,
    REASON_UPDATE_TABLE_MALFORMED,
)

assert ops._toml.__name__ == "tomli", ops._toml.__name__
assert ops._TOMLDecodeError is ops._toml.TOMLDecodeError

with tempfile.TemporaryDirectory() as tmp:
    good = Path(tmp) / "herdr.toml"
    good.write_text(
        "[update]\\nversion_check = false\\nmanifest_check = false\\n", encoding="utf-8"
    )
    verdict = ops.verify_config(good)
    assert verdict.ok, verdict.as_payload()
    assert verdict.verdict.mode == PIN_MODE_OFFLINE, verdict.as_payload()

    # The decode-error branch must catch the *fallback* parser's exception type,
    # not a hard-coded `tomllib.TOMLDecodeError`, or this raises instead of
    # returning the fail-closed verdict.
    bad = Path(tmp) / "broken.toml"
    bad.write_text("this is = = not toml [[[", encoding="utf-8")
    verdict = ops.verify_config(bad)
    assert not verdict.ok, verdict.as_payload()
    assert verdict.verdict.reason == REASON_UPDATE_TABLE_MALFORMED, verdict.as_payload()

print("PY310_SHAPE_OK")
'''


class Python310TomllibCompatRegressionTest(unittest.TestCase):
    """The ops module must import on every Python the package says it supports."""

    def test_toml_parser_binding_is_import_resolved(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (
            herdr_pin_posture_ops as ops,
        )

        # Exact name per interpreter, not "one of the two": on 3.11+ the stdlib
        # path must still be the one taken (a fallback that swallowed 3.11+ too
        # would satisfy a membership check and hide the regression).
        expected = "tomllib" if sys.version_info >= (3, 11) else "tomli"
        self.assertEqual(expected, ops._toml.__name__)
        self.assertTrue(hasattr(ops._toml, "loads"))
        self.assertIs(ops._TOMLDecodeError, ops._toml.TOMLDecodeError)

    def test_imports_and_verifies_without_stdlib_tomllib(self) -> None:
        # Child interpreter: reshaping `sys.modules` / `sys.meta_path` in-process
        # would leave the ops module bound to a shim for the rest of the suite.
        completed = subprocess.run(
            [sys.executable, "-c", _PY310_IMPORT_SHAPE_PROBE],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
        self.assertEqual(
            0,
            completed.returncode,
            f"stdout={completed.stdout}\nstderr={completed.stderr}",
        )
        self.assertIn("PY310_SHAPE_OK", completed.stdout)


if __name__ == "__main__":
    unittest.main()
