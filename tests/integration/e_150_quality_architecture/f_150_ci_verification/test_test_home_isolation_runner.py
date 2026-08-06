"""End-to-end tests for the isolated test-run rail (Redmine #14757).

Integration scope: a real fixture ``tests/`` tree, a real ``python -m unittest``
grandchild, and a real SQLite home standing in for the operator's. What is pinned
here is what the pure core cannot show — that the fence actually reaches the
process that runs the tests, that it reaches the process that *that* process
spawns, and that a write to the guarded home turns a green suite red.

The guarded "operator home" is always a temp directory named by
``MOZYO_BRIDGE_HOME``, so nothing here reads or writes the real one. Fixture
modules are deliberately tiny: they carry a real subprocess cost.
"""

from __future__ import annotations

import itertools
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))
SRC = str(ROOT / "src")

from mozyo_bridge.application.cli import main  # noqa: E402
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.test_home_fence import (  # noqa: E402,E501
    snapshot_home,
)
from mozyo_bridge.shared.paths import (  # noqa: E402
    HOME_FENCE_DENY_ENV,
    HOME_FENCE_ROOT_ENV,
    bind_process_home_fence,
)

_PKG_SEQ = itertools.count()

# Records what the *fenced* process resolved, so the test can assert on the
# resolution rather than on the pins that were supposed to produce it.
_PROBE_MODULE = """
import json, os, sys, unittest
from pathlib import Path

sys.path.insert(0, {src!r})
from mozyo_bridge.shared.paths import mozyo_bridge_home, process_home_fence


class Probe(unittest.TestCase):
    def test_probe(self):
        report = {{
            "fence_root": str(process_home_fence().root),
            "resolved": str(mozyo_bridge_home()),
            "home_env": os.environ.get("HOME"),
            "tmpdir": os.environ.get("TMPDIR"),
        }}
        # A grandchild with the environment scrubbed the way the test corpus
        # scrubs it: the fence must still hold there.
        import subprocess
        grandchild = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, {src!r});"
             "from mozyo_bridge.shared.paths import mozyo_bridge_home;"
             "print(mozyo_bridge_home())"],
            capture_output=True, text=True,
            env={{k: v for k, v in os.environ.items()
                 if not k.startswith("MOZYO_BRIDGE_HOME")}},
        )
        report["grandchild"] = grandchild.stdout.strip()
        report["grandchild_rc"] = grandchild.returncode
        Path({report!r}).write_text(json.dumps(report), encoding="utf-8")
"""

# Clears the environment exactly as the corpus does, then resolves.
_CLEAR_ENV_MODULE = """
import json, os, sys, unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, {src!r})
from mozyo_bridge.shared.paths import mozyo_bridge_home


class Cleared(unittest.TestCase):
    def test_cleared_env(self):
        with patch.dict(os.environ, {{}}, clear=True):
            resolved = str(mozyo_bridge_home())
        Path({report!r}).write_text(json.dumps({{"resolved": resolved}}),
                                   encoding="utf-8")
"""

# Writes into whatever home the process resolves. Under the fence that is the
# task root; unfenced it is the guarded home, and the guard must catch it.
_WRITER_MODULE = """
import os, sqlite3, unittest
from pathlib import Path


class Writer(unittest.TestCase):
    def test_write(self):
        home = Path(os.environ["MOZYO_BRIDGE_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(home / "registry.sqlite")
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS workspaces (workspace_id TEXT)")
            conn.execute("INSERT INTO workspaces VALUES ('leaked')")
            conn.execute("PRAGMA user_version = 99")
            conn.commit()
        finally:
            conn.close()
"""

_PASS_MODULE = """
import unittest


class Pass(unittest.TestCase):
    def test_ok(self):
        self.assertTrue(True)
"""


class _Fixture:
    """A throwaway repo with a ``tests/`` package holding one module."""

    def __init__(self, case: unittest.TestCase, body: str) -> None:
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "repo"
        self.tests = self.repo / "tests"
        self.tests.mkdir(parents=True)
        (self.repo / "pyproject.toml").write_text("", encoding="utf-8")
        (self.tests / "__init__.py").write_text("", encoding="utf-8")
        self.module = f"test_probe_{next(_PKG_SEQ)}"
        (self.tests / f"{self.module}.py").write_text(body, encoding="utf-8")

        self.guarded_home = Path(tmp.name) / "operator-home"
        self.guarded_home.mkdir()
        conn = sqlite3.connect(self.guarded_home / "registry.sqlite")
        try:
            conn.execute("CREATE TABLE workspaces (workspace_id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO workspaces VALUES ('operator-ws')")
            conn.execute("PRAGMA user_version = 7")
            conn.commit()
        finally:
            conn.close()



def _run_cli(argv: list[str], *, guarded_home: Path) -> tuple[int, str, str]:
    """Drive the real CLI with the guarded home named as the ambient one.

    Both standard streams are captured at the *file-descriptor* level, not with
    `redirect_stdout`: the rail spawns real subprocesses that inherit fd 1 / fd 2,
    so a Python-level redirect would leave a fixture's intentional failure output
    printed on the green suite's terminal (the output-hygiene rule in
    ``skills/mozyo-bridge-agent/references/workflow.md``). Returns
    ``(exit_code, stdout, stderr)`` so the expected negative-path output can be
    asserted instead of discarded.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in (HOME_FENCE_ROOT_ENV, HOME_FENCE_DENY_ENV)
    }
    env["MOZYO_BRIDGE_HOME"] = str(guarded_home)
    with tempfile.TemporaryDirectory() as capture:
        out_path = Path(capture) / "stdout"
        err_path = Path(capture) / "stderr"
        with open(out_path, "w+") as out_file, open(err_path, "w+") as err_file:
            saved = (os.dup(1), os.dup(2))
            try:
                sys.stdout.flush()
                sys.stderr.flush()
                os.dup2(out_file.fileno(), 1)
                os.dup2(err_file.fileno(), 2)
                with mock.patch.dict(os.environ, env, clear=True):
                    code = main(argv)
                sys.stdout.flush()
                sys.stderr.flush()
            finally:
                os.dup2(saved[0], 1)
                os.dup2(saved[1], 2)
                os.close(saved[0])
                os.close(saved[1])
        return (
            code,
            out_path.read_text(encoding="utf-8"),
            err_path.read_text(encoding="utf-8"),
        )


class FenceReachesTheProcessThatRunsTheTestsTest(unittest.TestCase):
    def test_the_test_process_and_its_grandchild_resolve_inside_the_fence(
        self,
    ) -> None:
        """The pins are inherited; the fence holds where the pins are stripped."""
        with tempfile.TemporaryDirectory() as reports:
            report = Path(reports) / "probe.json"
            fixture = _Fixture(
                self, _PROBE_MODULE.format(src=SRC, report=str(report))
            )
            code, _out, _err = _run_cli(
                [
                    "tests", "run", "--repo", str(fixture.repo),
                    "--", "discover", "-s", "tests",
                ],
                guarded_home=fixture.guarded_home,
            )
            self.assertEqual(code, 0)
            observed = json.loads(report.read_text(encoding="utf-8"))

        # The child resolved inside its own task root, not the guarded home.
        self.assertEqual(observed["resolved"], observed["fence_root"])
        self.assertNotIn(str(fixture.guarded_home), observed["resolved"])
        # HOME was inherited, not repurposed (#14757 acceptance 1).
        self.assertEqual(observed["home_env"], os.environ.get("HOME"))
        # TMPDIR points into the task root, so temp files stay there.
        task_root = Path(observed["fence_root"]).parent
        self.assertTrue(observed["tmpdir"].startswith(str(task_root)))
        # A grandchild that scrubbed MOZYO_BRIDGE_HOME still lands in the fence.
        self.assertEqual(observed["grandchild_rc"], 0)
        self.assertEqual(observed["grandchild"], observed["fence_root"])

    def test_a_test_that_clears_the_environment_stays_inside_the_fence(self) -> None:
        """The #14477 reach, exercised through the real rail."""
        with tempfile.TemporaryDirectory() as reports:
            report = Path(reports) / "cleared.json"
            fixture = _Fixture(self, "")
            (fixture.tests / f"{fixture.module}.py").write_text(
                _CLEAR_ENV_MODULE.format(src=SRC, report=str(report)),
                encoding="utf-8",
            )
            code, _out, _err = _run_cli(
                ["tests", "run", "--repo", str(fixture.repo)],
                guarded_home=fixture.guarded_home,
            )
            self.assertEqual(code, 0)
            resolved = json.loads(report.read_text(encoding="utf-8"))["resolved"]
        self.assertNotIn(str(fixture.guarded_home), resolved)
        self.assertNotEqual(
            resolved, str(Path("~/.mozyo_bridge").expanduser().resolve())
        )


class GuardTurnsAGreenSuiteRedTest(unittest.TestCase):
    def test_a_write_into_the_guarded_home_fails_the_run(self) -> None:
        """#14477's shape: passing tests, mutated shared state, reported as PASS.

        The fence is switched off with ``--no-isolate`` so the writer actually
        reaches the guarded home; the guard around it is what must refuse. Run
        directly rather than through ``tests run`` (whose own escape hatch drops
        the guard too), so the guard is the only thing under test.
        """
        from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.commands_test_run import (  # noqa: E501
            guarded_isolated_run,
        )
        import subprocess

        fixture = _Fixture(self, _WRITER_MODULE)
        before = snapshot_home(fixture.guarded_home)

        def child(_layout, env) -> int:
            # Deliberately unfenced-in-effect: point the child at the guarded
            # home, which is what an un-isolated test process would have done.
            leaky = dict(env)
            leaky["MOZYO_BRIDGE_HOME"] = str(fixture.guarded_home)
            leaky.pop(HOME_FENCE_ROOT_ENV, None)
            leaky.pop(HOME_FENCE_DENY_ENV, None)
            return subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                cwd=str(fixture.repo),
                env=leaky,
                capture_output=True,
            ).returncode

        outcome = guarded_isolated_run(
            repo_root=fixture.repo, child=child, guarded_home=fixture.guarded_home
        )
        self.assertTrue(outcome.suite_success, "the fixture suite itself should pass")
        self.assertFalse(outcome.success)
        self.assertFalse(outcome.guard.unchanged)
        self.assertTrue(
            any("operator shared home changed" in r for r in outcome.all_reasons)
        )
        # The guard reports; it never repairs. The leaked row is still there.
        after = snapshot_home(fixture.guarded_home)
        self.assertNotEqual(before.identity_digest, after.identity_digest)

    def test_a_clean_run_reports_the_home_unchanged(self) -> None:
        fixture = _Fixture(self, _PASS_MODULE)
        code, out, err = _run_cli(
            ["tests", "run", "--repo", str(fixture.repo)],
            guarded_home=fixture.guarded_home,
        )
        self.assertEqual(code, 0)
        self.assertIn("operator shared home: unchanged", out)

    def test_a_failing_suite_is_red_even_with_an_untouched_home(self) -> None:
        fixture = _Fixture(
            self,
            "import unittest\n"
            "class Fail(unittest.TestCase):\n"
            "    def test_fail(self): self.assertEqual(1, 2)\n",
        )
        code, out, err = _run_cli(
            ["tests", "run", "--repo", str(fixture.repo)],
            guarded_home=fixture.guarded_home,
        )
        self.assertEqual(code, 1)
        self.assertIn("operator shared home: unchanged", out)
        self.assertIn("result: FAIL", out)
        # The fixture's intentional failure is expected on the child's stderr;
        # asserted here rather than leaked to the parent terminal.
        self.assertIn("FAILED (failures=1)", err)

    def test_the_json_verdict_separates_the_two_halves(self) -> None:
        fixture = _Fixture(self, _PASS_MODULE)
        code, out, err = _run_cli(
            ["tests", "run", "--repo", str(fixture.repo), "--format", "json"],
            guarded_home=fixture.guarded_home,
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["success"])
        self.assertTrue(payload["suite_success"])
        self.assertTrue(payload["home_guard"]["unchanged"])


class IsolatedByDefaultThroughTheCliTest(unittest.TestCase):
    """The CLI rail must never silently lose isolation.

    ``isolate_self`` declines to re-exec when it has no recorded argv, because a
    directly-called handler must not replay the host process's ``sys.argv``. That
    fallback would silently disable isolation for the CLI too if ``cli.main``
    stopped recording the argv, so it is pinned here rather than assumed.
    """

    def test_main_records_the_argv_the_isolation_rail_replays(self) -> None:
        captured: dict = {}

        def fake_handler(args):
            captured["argv"] = getattr(args, "invoked_argv", None)
            return 0

        fixture = _Fixture(self, _PASS_MODULE)
        argv = ["tests", "run", "--repo", str(fixture.repo)]
        # Patched on the *registrar*, not on the handler module: the registrar
        # bound the name at its own import, so patching the definition site would
        # leave the already-bound reference in place.
        with mock.patch(
            "mozyo_bridge.e_150_quality_architecture.f_150_ci_verification"
            ".application.cli_test_run.cmd_tests_run",
            fake_handler,
        ):
            # The parser is built per invocation, so the patched handler is the
            # one it binds.
            main(argv)
        self.assertEqual(captured["argv"], argv)

    def test_a_reexeced_json_lane_still_emits_one_parseable_document(self) -> None:
        """The guard must not be appended as prose onto the child's JSON.

        `tests profile` / `tests parallel` render their own JSON in the fenced
        child. Printing the guard verdict after it would leave two documents on
        stdout and break the consumer the flag exists for, so the parent captures
        the child's document and merges the guard in as a key.

        The process fence is unbound for the duration. This test drives the
        *re-exec* path, and a handler that is already fenced deliberately does not
        re-exec (no nested fence) — so when the whole suite runs under
        `mozyo-bridge tests run`, the captured fence would make this path
        unreachable in-process and the assertion would depend on how the suite was
        launched. Measured: without the unbind this errored under `tests run` and
        passed under a bare `python -m unittest`. The guarded home stays the
        fixture's temp stand-in either way, via `_run_cli`'s `MOZYO_BRIDGE_HOME`.
        """
        self.addCleanup(bind_process_home_fence, bind_process_home_fence(None))
        fixture = _Fixture(self, _PASS_MODULE)
        for family in ("profile", "parallel"):
            with self.subTest(family=family):
                code, out, _err = _run_cli(
                    [
                        "tests",
                        family,
                        "--repo",
                        str(fixture.repo),
                        "--start-dir",
                        "tests",
                        "--format",
                        "json",
                    ],
                    guarded_home=fixture.guarded_home,
                )
                self.assertEqual(code, 0)
                payload = json.loads(out)  # one document, not two
                self.assertTrue(payload["home_guard"]["unchanged"])
                self.assertTrue(payload["fence_root"])
                self.assertTrue(payload["success"])

    def test_profile_and_parallel_carry_the_isolation_flags(self) -> None:
        """Both re-exec'ing entry points must accept the marker they are sent."""
        from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.commands_test_run import (  # noqa: E501
            ISOLATED_FLAG,
        )

        fixture = _Fixture(self, _PASS_MODULE)
        for family in ("profile", "parallel"):
            with self.subTest(family=family):
                code, _out, _err = _run_cli(
                    [
                        "tests",
                        family,
                        "--repo",
                        str(fixture.repo),
                        "--start-dir",
                        "tests",
                        ISOLATED_FLAG,
                    ],
                    guarded_home=fixture.guarded_home,
                )
                self.assertEqual(code, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
