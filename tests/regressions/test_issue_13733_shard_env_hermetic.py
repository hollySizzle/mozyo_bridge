"""The parallel runner must not leak its own import setup into the tests it runs.

Regression for Redmine #13733 / the #13735 j#78390 F1 parity break.

The runner used to hand each shard subprocess a ``PYTHONPATH`` pinned to the
mozyo_bridge package dir so the child could import the parent's runtime. But
``PYTHONPATH`` is inherited by *everything the test bodies then spawn*: a test that
builds the wheel and ``pip install``s it into a throwaway venv had the source
``src/`` (which carries ``mozyo_bridge.egg-info``) on the nested pip's path, so pip
declared the same version already installed, skipped the install, and exited 0 — no
console script, and a test that was green under ``unittest discover`` went red under
``tests parallel``. Deterministically, even at ``--jobs 1``. The serial bucket is no
remedy: serial shards go through the very same shard env.

Two seams are pinned here:

1. **runner** — the shard env is the serial env plus isolation and nothing else;
   ``PYTHONPATH`` passes through verbatim, and the child's runtime is pinned
   in-process (``python -c`` bootstrap) instead, which does not survive into
   grandchildren.
2. **test-side** — a test whose point is to exercise an *installed* artifact builds
   its nested env with :func:`tests.support.nested_python.hermetic_python_env`, so it
   does not depend on the caller's env being clean.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest
import venv as _venv
from pathlib import Path
from unittest import mock

# The parent runner discovers in-process, so each fixture tree needs its own package
# name or the second one collides with the first in this process's sys.modules.
_PKG_SEQ = itertools.count()

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.commands_test_parallel import (  # noqa: E402
    _runtime_root,
    _shard_command,
    _shard_env,
    cmd_tests_parallel,
)

from tests.support.nested_python import hermetic_python_env  # noqa: E402


class ShardEnvPythonPathTest(unittest.TestCase):
    """The shard env must not add a PYTHONPATH entry the serial run does not have."""

    def test_absent_pythonpath_stays_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            with mock.patch.dict(os.environ, env, clear=True):
                shard_env = _shard_env(ROOT, Path(tmp))
        self.assertNotIn(
            "PYTHONPATH",
            shard_env,
            msg=(
                "the shard injected a PYTHONPATH the serial run does not have; it is "
                "inherited by every nested subprocess a test spawns (#13735 F1)"
            ),
        )

    def test_inherited_pythonpath_passes_through_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inherited = os.pathsep.join(("/opt/one", "/opt/two"))
            with mock.patch.dict(os.environ, {"PYTHONPATH": inherited}):
                shard_env = _shard_env(ROOT, Path(tmp))
        self.assertEqual(
            inherited,
            shard_env.get("PYTHONPATH"),
            msg="the shard rewrote an inherited PYTHONPATH; serial does not",
        )

    def test_shard_command_pins_the_runtime_in_process(self) -> None:
        """The child still resolves the parent's runtime — via sys.path, not the env."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cmd = _shard_command(
                root, root / "spec.json", root / "result.json", _runtime_root()
            )
        self.assertEqual(sys.executable, cmd[0])
        self.assertEqual("-c", cmd[1])
        bootstrap = cmd[2]
        self.assertIn(_runtime_root(), bootstrap)
        self.assertIn("sys.path.insert", bootstrap)
        self.assertNotIn(
            "-m",
            cmd,
            msg="a `-m` launch needs PYTHONPATH to find a non-installed source runtime",
        )


# A fixture test module that records the PYTHONPATH its own process sees *and* the
# one a subprocess it spawns sees — the exact channel that broke the nested pip.
_PYTHONPATH_PROBE_MODULE = (
    "import json, os, pathlib, subprocess, sys, unittest\n"
    "class PythonPathProbe(unittest.TestCase):\n"
    "    def test_probe(self):\n"
    "        nested = subprocess.run(\n"
    "            [sys.executable, '-c',\n"
    "             'import os, sys; sys.stdout.write(os.environ.get(\"PYTHONPATH\", \"<unset>\"))'],\n"
    "            capture_output=True, text=True, check=True,\n"
    "        )\n"
    "        data = {\n"
    "            'test_body': os.environ.get('PYTHONPATH', '<unset>'),\n"
    "            'nested_subprocess': nested.stdout,\n"
    "        }\n"
    "        pathlib.Path(os.environ['PROBE_OUT']).write_text(json.dumps(data))\n"
)


def _run_parallel(root: Path) -> tuple[int, dict]:
    args = argparse.Namespace(
        repo=str(root),
        start_dir="tests",
        pattern="test*.py",
        top_level_dir=None,
        jobs=1,
        shards=None,
        durations=None,
        serial_policy=None,
        shard_timeout=None,
        failfast=False,
        format="json",
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        code = cmd_tests_parallel(args)
    out = buf.getvalue().strip()
    return code, (json.loads(out) if out else {})


class ShardChildEnvProbeTest(unittest.TestCase):
    """Live probe: what a shard's test body — and what *it* spawns — actually sees."""

    def _probe(self, tmp: Path, parent_pythonpath: str | None) -> dict:
        pkg_dir = tmp / "tests" / f"probe{next(_PKG_SEQ)}"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
        (pkg_dir / "test_pythonpath.py").write_text(
            _PYTHONPATH_PROBE_MODULE, encoding="utf-8"
        )
        out = tmp / "probe.json"

        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        if parent_pythonpath is not None:
            env["PYTHONPATH"] = parent_pythonpath
        env["PROBE_OUT"] = str(out)
        with mock.patch.dict(os.environ, env, clear=True):
            code, payload = _run_parallel(tmp)

        self.assertEqual(
            0,
            code,
            msg=(
                "the shard child failed to run — it must still resolve the parent's "
                f"runtime without PYTHONPATH: {json.dumps(payload)[:800]}"
            ),
        )
        return json.loads(out.read_text(encoding="utf-8"))

    def test_shard_test_body_and_its_children_see_no_injected_path(self) -> None:
        """With no ambient PYTHONPATH, nothing downstream of the shard gets one.

        This is the failure channel itself: before the fix the runner put ``src/``
        here, the test body's nested ``pip install`` inherited it, and pip skipped
        the install. The shard child nonetheless runs, which proves the runtime is
        pinned in-process rather than through the env.
        """
        with tempfile.TemporaryDirectory() as tmp:
            probe = self._probe(Path(tmp), None)
        self.assertEqual("<unset>", probe["test_body"])
        self.assertEqual("<unset>", probe["nested_subprocess"])

    def test_ambient_pythonpath_reaches_the_shard_unchanged(self) -> None:
        """An operator-set PYTHONPATH is passed through as-is — same as serial."""
        with tempfile.TemporaryDirectory() as tmp:
            ambient = str(ROOT / "src")
            probe = self._probe(Path(tmp), ambient)
        self.assertEqual(ambient, probe["test_body"])
        self.assertEqual(ambient, probe["nested_subprocess"])


class NestedWheelInstallTest(unittest.TestCase):
    """A nested ``pip install`` of the built wheel must really install it.

    The bug was silent: pip exits 0 while skipping the install, so only an assertion
    on the *console script* catches it. This runs the install the way the wheel-install
    tests do — with a hostile ambient ``PYTHONPATH`` pointing at the source tree — and
    proves the hermetic env still yields a populated venv.
    """

    def test_pip_install_provides_console_script_despite_source_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dist = tmp_path / "dist"
            dist.mkdir()
            build_proc = subprocess.run(
                [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            if build_proc.returncode != 0:
                self.skipTest(
                    "python -m build failed (probably missing build backend deps); "
                    f"stderr={build_proc.stderr[:500]}"
                )
            wheels = list(dist.glob("mozyo_bridge-*.whl"))
            self.assertEqual(1, len(wheels), msg=f"unexpected wheels: {wheels}")

            venv_dir = tmp_path / "venv"
            try:
                _venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
            except subprocess.CalledProcessError as exc:
                self.skipTest(f"venv with pip could not be created: {exc}")
            venv_python = venv_dir / "bin" / "python"
            venv_bin = venv_dir / "bin" / "mozyo-bridge"

            # The hostile ambient env: exactly what the runner used to inject.
            with mock.patch.dict(os.environ, {"PYTHONPATH": str(ROOT / "src")}):
                nested_env = hermetic_python_env()
            self.assertNotIn("PYTHONPATH", nested_env)

            install_proc = subprocess.run(
                [str(venv_python), "-m", "pip", "install", "-q", str(wheels[0])],
                capture_output=True,
                text=True,
                env=nested_env,
            )
            if install_proc.returncode != 0:
                self.skipTest(
                    "pip install of the built wheel failed (no network or build deps "
                    f"missing): stderr={install_proc.stderr[:500]}"
                )
            self.assertTrue(
                venv_bin.exists(),
                msg=(
                    "pip exited 0 but wrote no console script — it resolved "
                    "mozyo-bridge as already installed from an inherited PYTHONPATH "
                    "and skipped the install (#13735 F1)"
                ),
            )

            # The entry point must be the *installed* one, not a source shadow.
            run_proc = subprocess.run(
                [str(venv_bin), "--help"],
                capture_output=True,
                text=True,
                env=nested_env,
            )
            self.assertEqual(
                0,
                run_proc.returncode,
                msg=f"installed console script failed: stderr={run_proc.stderr[:500]}",
            )


if __name__ == "__main__":
    unittest.main()
