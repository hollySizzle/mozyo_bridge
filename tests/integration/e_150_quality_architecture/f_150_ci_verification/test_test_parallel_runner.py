"""End-to-end tests for the local parallel test runner (Redmine #13733).

These are integration tests: they build a small fixture ``tests/`` tree in a temp
dir and drive the real ``cmd_tests_parallel`` handler, which spawns actual shard
subprocesses. They pin the acceptance contract behaviorally:

- **parity** — the parallel run discovers and runs exactly the same test set as a
  serial ``python -m unittest discover`` (independently counted), with a green
  verdict.
- **fail-closed** — a failing test, a collection-time import error, a worker that
  crashes mid-run, and a shard timeout each yield a red aggregate (never a laundered
  green).
- **serial bucket** — a policy-matched module is run in its own serial shard while
  parity still holds.
- **isolation** — each shard runs under a fresh HOME / TMPDIR / MOZYO_BRIDGE_HOME
  with the live cockpit-session env pins stripped, so it cannot touch the operator's
  Herdr lane.

They are deliberately kept to a few tiny modules so the real-subprocess cost stays
small.
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
from pathlib import Path
from unittest import mock

# The parent runner discovers in-process, which imports the fixture modules into
# this test process's sys.modules. Give every fixture tree a unique package so
# module names never collide across test methods in the shared process (the real
# CLI runs once per process, so this is a test-harness concern only).
_PKG_SEQ = itertools.count()

from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.commands_test_parallel import (
    cmd_tests_parallel,
)

_PASS_MODULE = (
    "import unittest\n"
    "class {cls}(unittest.TestCase):\n"
    "    def test_a(self): self.assertEqual(1, 1)\n"
    "    def test_b(self): self.assertEqual(2, 2)\n"
)
_FAIL_MODULE = (
    "import unittest\n"
    "class Fail(unittest.TestCase):\n"
    "    def test_fail(self): self.assertEqual(1, 2)\n"
)
_IMPORT_ERROR_MODULE = (
    "import unittest\n"
    "import definitely_missing_module_zzz  # noqa: F401\n"
    "class Broken(unittest.TestCase):\n"
    "    def test_x(self): pass\n"
)
_CRASH_MODULE = (
    "import os, unittest\n"
    "class Crash(unittest.TestCase):\n"
    "    def test_crash(self): os._exit(7)\n"
)
_SLEEP_MODULE = (
    "import time, unittest\n"
    "class Slow(unittest.TestCase):\n"
    "    def test_sleep(self): time.sleep(30)\n"
)
_ENV_PROBE_MODULE = (
    "import os, json, pathlib, unittest\n"
    "class EnvProbe(unittest.TestCase):\n"
    "    def test_probe(self):\n"
    "        keys = ('HOME','TMPDIR','MOZYO_BRIDGE_HOME','TMUX',"
    "'MOZYO_WORKSPACE_ID','MOZYO_LANE_ID')\n"
    "        data = {k: os.environ.get(k) for k in keys}\n"
    "        data['cwd'] = os.getcwd()\n"
    "        pathlib.Path(pathlib.Path.cwd(), 'env_probe.json').write_text(json.dumps(data))\n"
)


def _make_tree(root: Path, modules: dict[str, str]) -> str:
    """Write a fixture tests tree under a unique top-level package; return its name.

    The package name is unique per call so neither it nor a shared parent package
    (``tests`` / ``unit``) is cached in this process's sys.modules against a stale
    fixture path across test methods. No ``tests/__init__.py`` is written, so the
    fixture's ``tests`` dir never shadows the real suite's ``tests`` package.
    """
    pkg = f"p{next(_PKG_SEQ)}"
    target = root / "tests" / pkg
    target.mkdir(parents=True, exist_ok=True)
    (target / "__init__.py").write_text("", encoding="utf-8")
    for name, body in modules.items():
        (target / f"{name}.py").write_text(body, encoding="utf-8")
    return pkg


def _namespace(root: Path, **over) -> argparse.Namespace:
    defaults = dict(
        repo=str(root),
        start_dir="tests",
        pattern="test*.py",
        top_level_dir=None,
        jobs=2,
        durations=None,
        serial_policy=None,
        shard_timeout=None,
        failfast=False,
        format="json",
    )
    defaults.update(over)
    return argparse.Namespace(**defaults)


def _run(root: Path, **over) -> tuple[int, dict]:
    buf = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        code = cmd_tests_parallel(_namespace(root, **over))
    out = buf.getvalue().strip()
    payload = json.loads(out) if out else {}
    return code, payload


def _serial_discover_count(root: Path) -> int:
    """Independently count the serial ``discover`` test set (a real subprocess)."""
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    # unittest prints "Ran N tests in ..." on the final summary line (stderr).
    for line in reversed((proc.stderr or "").splitlines()):
        if line.startswith("Ran ") and "test" in line:
            return int(line.split()[1])
    raise AssertionError(f"could not parse serial count from: {proc.stderr!r}")


class ParityTest(unittest.TestCase):
    def test_parallel_matches_serial_and_is_green(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_tree(
                root,
                {
                    "test_a": _PASS_MODULE.format(cls="A"),
                    "test_b": _PASS_MODULE.format(cls="B"),
                    "test_c": _PASS_MODULE.format(cls="C"),
                },
            )
            serial_count = _serial_discover_count(root)
            code, payload = _run(root, jobs=3)
            self.assertEqual(code, 0)
            self.assertTrue(payload["success"])
            agg = payload["aggregate"]
            # Same set: everything discovered was run, nothing extra, count matches.
            self.assertEqual(agg["total_expected_tests"], serial_count)
            self.assertEqual(agg["total_ran_tests"], serial_count)
            self.assertEqual(agg["missing_test_ids"], [])
            self.assertEqual(agg["unexpected_test_ids"], [])
            self.assertEqual(agg["counts"]["passed"], serial_count)

    def test_single_job_runs_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_tree(
                root,
                {"test_a": _PASS_MODULE.format(cls="A"), "test_b": _PASS_MODULE.format(cls="B")},
            )
            code, payload = _run(root, jobs=1)
            self.assertEqual(code, 0)
            self.assertTrue(payload["success"])
            self.assertEqual(payload["aggregate"]["total_ran_tests"], 4)


class FailClosedTest(unittest.TestCase):
    def test_failing_test_is_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_tree(
                root,
                {"test_ok": _PASS_MODULE.format(cls="Ok"), "test_fail": _FAIL_MODULE},
            )
            code, payload = _run(root, jobs=2)
            self.assertEqual(code, 1)
            self.assertFalse(payload["success"])
            self.assertTrue(payload["aggregate"]["failed_shards"])

    def test_collection_import_error_is_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_tree(
                root,
                {"test_ok": _PASS_MODULE.format(cls="Ok"), "test_bad": _IMPORT_ERROR_MODULE},
            )
            code, payload = _run(root, jobs=2)
            # Parent fails closed before sharding; no JSON payload is emitted.
            self.assertEqual(code, 1)

    def test_worker_crash_is_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_tree(
                root,
                {"test_ok": _PASS_MODULE.format(cls="Ok"), "test_crash": _CRASH_MODULE},
            )
            code, payload = _run(root, jobs=2)
            self.assertEqual(code, 1)
            self.assertFalse(payload["success"])
            statuses = {s["status"] for s in payload["shards"]}
            self.assertIn("crashed", statuses)

    def test_timeout_is_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_tree(
                root,
                {"test_ok": _PASS_MODULE.format(cls="Ok"), "test_slow": _SLEEP_MODULE},
            )
            code, payload = _run(root, jobs=2, shard_timeout=2.0)
            self.assertEqual(code, 1)
            self.assertFalse(payload["success"])
            statuses = {s["status"] for s in payload["shards"]}
            self.assertIn("timeout", statuses)


class SerialBucketTest(unittest.TestCase):
    def test_serial_module_runs_in_serial_shard_with_parity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = _make_tree(
                root,
                {
                    "test_safe": _PASS_MODULE.format(cls="Safe"),
                    "test_serialized": _PASS_MODULE.format(cls="Serialized"),
                },
            )
            serial_module = f"{prefix}.test_serialized"
            (root / "test_parallel_policy.yaml").write_text(
                f"serial_modules:\n  - {serial_module}\n", encoding="utf-8"
            )
            code, payload = _run(root, jobs=4)
            self.assertEqual(code, 0)
            self.assertTrue(payload["success"])
            serial = [s for s in payload["shards"] if s["kind"] == "serial"]
            self.assertEqual(len(serial), 1)
            self.assertEqual(serial[0]["modules"], [serial_module])
            # Parity still holds across the mixed parallel + serial plan.
            self.assertEqual(payload["aggregate"]["missing_test_ids"], [])


class IsolationTest(unittest.TestCase):
    def test_shard_env_isolates_state_and_strips_live_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_tree(root, {"test_env": _ENV_PROBE_MODULE})
            fake_env = {
                "TMUX": "/tmp/fake-tmux,1,0",
                "TMUX_PANE": "%99",
                "MOZYO_WORKSPACE_ID": "fake-workspace",
                "MOZYO_LANE_ID": "fake-lane",
                "MOZYO_BRIDGE_HOME": "/tmp/parent-mozyo-home",
            }
            with mock.patch.dict(os.environ, fake_env, clear=False):
                parent_home = os.environ.get("HOME")
                parent_mozyo = os.environ["MOZYO_BRIDGE_HOME"]
                parent_tmp = os.environ.get("TMPDIR")
                code, _ = _run(root, jobs=1)
            self.assertEqual(code, 0)
            probe = json.loads((root / "env_probe.json").read_text(encoding="utf-8"))
            # Live cockpit-session pins are stripped from the shard.
            self.assertIsNone(probe["TMUX"])
            self.assertIsNone(probe["MOZYO_WORKSPACE_ID"])
            self.assertIsNone(probe["MOZYO_LANE_ID"])
            # MOZYO_BRIDGE_HOME (shared state store) + TMPDIR are per-shard.
            self.assertTrue(probe["MOZYO_BRIDGE_HOME"])
            self.assertNotEqual(probe["MOZYO_BRIDGE_HOME"], parent_mozyo)
            self.assertTrue(probe["TMPDIR"])
            self.assertNotEqual(probe["TMPDIR"], parent_tmp)
            # HOME is inherited (not overridden) so git identity / user site-packages
            # match the serial run — this is the parity fix, not a leak.
            self.assertEqual(probe["HOME"], parent_home)
            # The shard runs with cwd pinned to the target repo root (matching the
            # serial `discover` cwd; resolve for the macOS /var -> /private/var link).
            self.assertEqual(Path(probe["cwd"]).resolve(), root.resolve())


if __name__ == "__main__":
    unittest.main()
