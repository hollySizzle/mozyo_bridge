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
import re
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))
SRC = str(ROOT / "src")

from mozyo_bridge.application.cli import main  # noqa: E402
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.commands_test_run import (  # noqa: E402,E501
    guarded_isolated_run,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.test_home_audit_hook import (  # noqa: E402,E501
    install_audit_fence,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.test_home_fence import (  # noqa: E402,E501
    read_deny_ledger,
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


class WritesAreRefusedBeforeTheyLandTest(unittest.TestCase):
    """R2: the attempt is refused, not detected afterwards (j#100407 R1-F1).

    R1 shipped a test here that let a row land in the guarded home and asserted the
    run went red with the row still there. Review j#100407 R1-F1 rejected that
    reading of acceptance 4/7, so the semantics it pinned are gone and these pin the
    replacement: the write raises `PermissionError` inside the child and the bytes
    on disk are untouched.
    """

    def test_a_cleared_env_child_is_refused_and_the_bytes_are_unchanged(self) -> None:
        """The exact case j#100410 measured as uncovered by the discarded design.

        `env={}` strips every pin, so the refusal has to come from the interpreter
        itself. Both halves are asserted: the child fails, AND the target file still
        holds its original bytes -- "the run went red" alone is what R1 did.
        """
        with tempfile.TemporaryDirectory() as task:
            home = Path(task) / "operator-home"
            home.mkdir()
            victim = home / "coordinator-placement.yaml"
            victim.write_text("original\n", encoding="utf-8")
            before = victim.read_bytes()

            interpreter, _ledger = install_audit_fence(
                Path(task), denied_homes=(home,)
            )
            code = (
                "import sys\n"
                f"open({str(victim)!r}, 'w').write('MUTATED')\n"
            )
            proc = subprocess.run(
                [str(interpreter), "-c", code], env={}, capture_output=True, text=True
            )
            # Read inside the context: the temp tree is gone once it exits.
            after = victim.read_bytes()

        self.assertNotEqual(proc.returncode, 0, "the write was allowed")
        self.assertIn("PermissionError", proc.stderr)
        self.assertEqual(before, after, "the file was mutated")

    def test_an_in_place_sqlite_row_update_is_refused(self) -> None:
        """finding_1's hardest shape: same row count, same ids, changed contents.

        The snapshot cannot see this (that is the finding). Refusal does not need to
        see it -- it never gets to happen.
        """
        with tempfile.TemporaryDirectory() as task:
            home = Path(task) / "operator-home"
            home.mkdir()
            store = home / "registry.sqlite"
            conn = sqlite3.connect(store)
            try:
                conn.execute("CREATE TABLE workspaces (workspace_id TEXT, seen TEXT)")
                conn.execute("INSERT INTO workspaces VALUES ('ws-1','t0')")
                conn.commit()
            finally:
                conn.close()
            before = store.read_bytes()

            interpreter, _ledger = install_audit_fence(
                Path(task), denied_homes=(home,)
            )
            code = (
                "import sqlite3\n"
                f"c = sqlite3.connect({str(store)!r})\n"
                "c.execute(\"UPDATE workspaces SET seen='t1'\")\n"
                "c.commit()\n"
            )
            proc = subprocess.run(
                [str(interpreter), "-c", code], env={}, capture_output=True, text=True
            )
            after = store.read_bytes()

        self.assertNotEqual(proc.returncode, 0, "the in-place update was allowed")
        self.assertEqual(before, after, "the store was mutated")

    def test_an_attempt_is_recorded_so_the_run_fails_even_though_it_was_refused(
        self,
    ) -> None:
        """A refused attempt is still a defect; the ledger is what surfaces it."""
        with tempfile.TemporaryDirectory() as task:
            home = Path(task) / "operator-home"
            home.mkdir()
            interpreter, ledger = install_audit_fence(
                Path(task), denied_homes=(home,)
            )
            ledger.write_text("", encoding="utf-8")
            code = (
                "try:\n"
                f"    open({str(home / 'x.txt')!r}, 'w')\n"
                "except PermissionError:\n"
                "    pass\n"
            )
            subprocess.run([str(interpreter), "-c", code], env={}, capture_output=True)
            recorded = read_deny_ledger(ledger)
        self.assertFalse(recorded.clean)
        self.assertTrue(any("x.txt" in a for a in recorded.attempts))

    def test_a_missing_ledger_is_a_failure_not_an_absence(self) -> None:
        """\"No ledger\" must not read as \"nothing attempted\"."""
        with tempfile.TemporaryDirectory() as task:
            self.assertTrue(read_deny_ledger(Path(task) / "never-written.jsonl").missing)
            self.assertFalse(read_deny_ledger(Path(task) / "never-written.jsonl").clean)

    def test_every_denied_home_is_guarded_not_just_the_effective_one(self) -> None:
        """finding_2: the deny set and the guard set are now the same set."""
        with tempfile.TemporaryDirectory() as task:
            first = Path(task) / "home-a"
            second = Path(task) / "home-b"
            for home in (first, second):
                home.mkdir()

            def child(_layout, _env, _interpreter) -> int:
                # Simulate an un-hooked writer reaching the SECOND home -- the one
                # R1 denied but never snapshotted.
                (second / "leaked.txt").write_text("x", encoding="utf-8")
                return 0

            outcome = guarded_isolated_run(
                repo_root=Path(task), child=child, guarded_homes=(first, second)
            )
        self.assertEqual(len(outcome.guards), 2)
        self.assertTrue(outcome.suite_success)
        self.assertFalse(outcome.success, "a write to the second home was not caught")
        self.assertTrue(any(not g.unchanged for g in outcome.guards))


    def test_a_clean_run_reports_the_home_unchanged(self) -> None:
        fixture = _Fixture(self, _PASS_MODULE)
        code, out, err = _run_cli(
            ["tests", "run", "--repo", str(fixture.repo)],
            guarded_home=fixture.guarded_home,
        )
        self.assertEqual(code, 0)
        self.assertIn("unchanged", out)

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
        self.assertIn("unchanged", out)
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
        self.assertTrue(all(g["unchanged"] for g in payload["home_guards"]))
        self.assertTrue(payload["deny_ledger"]["clean"])


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
                self.assertTrue(all(g["unchanged"] for g in payload["home_guards"]))
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




class InterpreterBypassCatalogTest(unittest.TestCase):
    """What the refusal hook cannot reach, enumerated (j#100410 item 3).

    The hook is armed by *this* interpreter, so it rides along only where the
    fenced interpreter does. Two shapes escape it, and the point of these tests is
    that the suite cannot acquire a new one *silently*: a spawn that bypasses the
    fence must show up as a failure here and be dispositioned, rather than quietly
    widening the gap while the rail still reports isolation.

    Deliberately syntactic. These are tripwires over the corpus, not a proof that
    the listed files are safe — that judgement stays with each test's author.
    """

    #: Tests that build their own virtualenv. Their interpreters do not carry the
    #: hook. Each is a wheel-install / console-script test whose writes land in its
    #: own temp venv, not in a mozyo-bridge home; that is why they are allowed.
    #: Adding an entry is a deliberate act with a reason, not a silent drift.
    KNOWN_VENV_BUILDERS = frozenset(
        {
            "tests/integration/e_130_governance_distribution/f_120_scaffold_preset/test_scaffold.py",
            "tests/integration/e_110_execution_platform/f_130_handoff_routing/test_handoff_typed_outcome_cli_smoke.py",
            "tests/regressions/test_issue_13733_shard_env_hermetic.py",
        }
    )

    @staticmethod
    def _test_sources() -> list[Path]:
        """Every test source except this file.

        This module is excluded because it *names* the patterns it searches for, so
        it matches its own detector. Excluding the catalog is narrower than
        loosening the pattern: a real offender elsewhere still trips.
        """
        me = Path(__file__).resolve()
        return [
            path
            for path in sorted((ROOT / "tests").rglob("*.py"))
            if path.resolve() != me
        ]

    def test_no_test_spawns_a_bare_python_interpreter(self) -> None:
        """A hardcoded `python` / `python3` bypasses the fence entirely.

        With `env={}` there is no PATH either, so `subprocess` falls back to
        `os.defpath` and reaches the *system* interpreter — which has no hook and no
        pins. Every spawn must go through `sys.executable` so it inherits the fenced
        interpreter. Measured at this commit: zero occurrences.
        """
        pattern = re.compile(r"""\[\s*["']python3?["']""")
        offenders = []
        for path in self._test_sources():
            text = path.read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(
            offenders,
            [],
            "these spawn a bare interpreter, which does not carry the write-refusal "
            "hook; use sys.executable so the fenced interpreter is inherited "
            f"(Redmine #14757): {offenders}",
        )

    def test_the_set_of_venv_building_tests_is_the_known_set(self) -> None:
        """A new self-built venv is a new un-hooked interpreter; decide it openly."""
        builders = set()
        for path in self._test_sources():
            text = path.read_text(encoding="utf-8", errors="replace")
            if "EnvBuilder" in text or re.search(r'"-m",\s*"venv"', text):
                builders.add(str(path.relative_to(ROOT)))
        self.assertEqual(
            builders,
            set(self.KNOWN_VENV_BUILDERS),
            "the set of tests that build their own (un-hooked) interpreter changed. "
            "Confirm the new one cannot write a mozyo-bridge home, then add it to "
            "KNOWN_VENV_BUILDERS with that reason (Redmine #14757 / j#100410 item 3)",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
