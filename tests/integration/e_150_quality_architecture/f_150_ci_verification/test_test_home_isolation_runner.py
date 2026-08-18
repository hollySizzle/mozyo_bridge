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

from dataclasses import replace

import itertools
import json
import re
import os
import platform
import site
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
    ambient_homes,
    read_deny_ledger,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.test_home_os_fence import (  # noqa: E402,E501
    BYTES_VICTIM_NAME,
    PROBE_BASE_EXECUTABLE,
    PROBE_CONTROL,
    PROBE_DIR_FD,
    PROBE_NO_SITE,
    SQLITE_VICTIM_NAME,
    BACKEND_BWRAP,
    BACKEND_REFUSAL_ERRNOS,
    BACKEND_SANDBOX_EXEC,
    OsFence,
    OsFenceUnavailable,
    create_boundary_fixtures,
    load_outer_context,
    resolve_os_fence,
    run_boundary_probe,
    verify_os_fence,
)
from mozyo_bridge.shared.paths import (  # noqa: E402
    HOME_FENCE_DENY_ENV,
    HOME_FENCE_ROOT_ENV,
    bind_process_home_fence,
)

_PKG_SEQ = itertools.count()

#: The user site base the interpreter resolved from the REAL home, captured before
#: any test hands a child a stand-in HOME.
_REAL_USER_BASE = site.getuserbase()



def _outer_authority() -> tuple[bool, tuple[str, ...]]:
    """Whether a nested run would inherit, and the homes it must never resolve onto.

    Both answers come from ONE reading of this process's boundary, so they cannot
    disagree, and both are derived here rather than reported by the child: the child
    cannot observe `inherited` without adding a field to the published context, and
    j#100500 forbids production changes. An outer context in THIS process is exactly
    the condition under which the nested run inherits.

    The protected roots are read from that context instead of being respelled as
    ``Path("~/.mozyo_bridge").expanduser()`` (#15229). Under
    ``mozyo-bridge tests parallel`` each shard is handed a fresh task-local ``HOME``
    (#13733 acceptance 3), so expanding ``~`` here names the *shard's* home, while
    the authority the fenced child actually inherited was captured from the
    operator's home before that shard existed. The expectation and the authority then
    came from different places and this module failed in parallel while passing
    serially -- a false failure about where the expectation was computed, not about
    isolation.
    """
    context = load_outer_context()
    if context is not None:
        # Byte-exact, and the same tuple the inherited fence denies.
        return True, tuple(str(root) for root in context.outer_protected_roots)
    # No outer boundary: this process was launched directly, so its own ambient
    # homes ARE the operator's -- taken from the production resolver rather than
    # respelled, so a test's expectation cannot drift from what the rail guards.
    return False, tuple(str(home) for home in ambient_homes())


def _assert_branch_contract(
    case: unittest.TestCase,
    *,
    observed: dict,
    synthetic_default: str,
    resolved_key: str,
) -> None:
    """The fresh and inherited branches resolve differently, and both are safe.

    j#100500. Demanding `resolved == fence_root` in both branches would contradict
    the documented resolver: a fresh nested boundary denies the synthetic default,
    so resolution must move to the new fence root; an inherited boundary keeps the
    outer authority byte-exactly and the synthetic default is then just a safe
    task-local path, which the resolver keeps rather than overriding. Asserting one
    rule for both would have forced a production change to satisfy a test.
    """
    resolved = observed[resolved_key]
    inherited, protected_roots = _outer_authority()
    if inherited:
        # The outer authority, unchanged -- the nested synthetic default is NOT
        # added to it (that is the F2 single-authority rule). Compared as the whole
        # set, not as "the default spelling appears somewhere in it": byte-exact
        # preservation is the claim, and the set is what carries it.
        case.assertNotIn(synthetic_default, observed["denied"])
        case.assertTrue(
            protected_roots, "premise lost: the outer boundary protects nothing"
        )
        case.assertEqual(
            sorted(observed["denied"]),
            sorted(protected_roots),
            "outer authority was lost",
        )
        case.assertEqual(resolved, synthetic_default)
    else:
        case.assertIn(synthetic_default, observed["denied"])
        case.assertEqual(resolved, observed["fence_root"])
        case.assertNotEqual(resolved, synthetic_default)
    # Both branches: never a protected home, nor anything under one.
    for root in protected_roots:
        case.assertFalse(
            resolved == root or resolved.startswith(root + os.sep),
            f"resolution reached a protected home {root}: {resolved}",
        )


def _require_backend(case: unittest.TestCase, exc: Exception) -> None:
    """Resolving no backend is a FAILURE on Linux, not a skip (j#100489).

    CI guarantees bwrap on Linux and the rail refuses to run a single test without
    a boundary, so a skip there converts the one platform whose hard check is
    unconditional into a silent no-op -- a green run that measured nothing. On other
    hosts a missing backend can be legitimate, so the skip stands.
    """
    if platform.system() == "Linux":
        case.fail(f"no OS write boundary resolved on this Linux host: {exc}")
    case.skipTest(f"no OS write boundary on this host: {exc}")

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
            "denied": [str(path) for path in process_home_fence().denied],
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
#
# With `os.environ` empty there is no HOME, so `expanduser()` falls through to the
# OS account lookup and reaches the REAL operator home -- which is how this path
# escaped the synthetic account HOME entirely (measured after j#100496). The fix is
# to model the passwd fallback rather than avoid it: keep the environment genuinely
# empty and patch only `pwd.getpwuid`, so the deterministic answer is the synthetic
# account. `ambient_homes()` / `guarded_homes` are NOT patched -- doing that would
# step around the production path this test exists to exercise (j#100498).
_CLEAR_ENV_MODULE = """
import json, os, pwd, sys, unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, {src!r})
from mozyo_bridge.shared.paths import mozyo_bridge_home, process_home_fence


class Cleared(unittest.TestCase):
    def test_cleared_env(self):
        real = pwd.getpwuid(os.getuid())
        synthetic = type(real)(
            (real.pw_name, real.pw_passwd, real.pw_uid, real.pw_gid,
             real.pw_gecos, {account_home!r}, real.pw_shell)
        )
        fence = process_home_fence()
        report = {{
            "fence_root": str(fence.root),
            "denied": [str(path) for path in fence.denied],
        }}
        with patch.dict(os.environ, {{}}, clear=True), patch.object(
            pwd, "getpwuid", return_value=synthetic
        ):
            report["env_empty"] = not os.environ
            report["synthetic_default"] = str(Path("~/.mozyo_bridge").expanduser())
            report["resolved"] = str(mozyo_bridge_home())
        Path({report!r}).write_text(json.dumps(report), encoding="utf-8")
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



def synthetic_account_home(guarded_home: Path) -> Path:
    """The task-local stand-in account HOME a nested test child is given.

    One definition, so a test's expectation and `_run_cli`'s env cannot disagree
    (j#100498). Production is untouched: the rail still inherits whatever HOME it
    is handed and still guards every ambient home it finds.
    """
    return guarded_home.parent / "fake-operator-account"


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
    # A task-local HOME for the nested child (j#100496). `ambient_homes()` always
    # guards the operator default `~/.mozyo_bridge` -- correct in production, but a
    # nested run started from a test would then guard the operator's REAL home, and
    # any cockpit write during the window fails the test. That coupling made the
    # suite's verdict depend on unrelated operator activity (the Mac full failure in
    # j#100495). Production's contract is unchanged: the rail still never repurposes
    # HOME, and still guards every ambient home it finds. What changes is only which
    # home a TEST's child finds.
    env["HOME"] = str(synthetic_account_home(guarded_home))
    (Path(env["HOME"]) / ".mozyo_bridge").mkdir(parents=True, exist_ok=True)
    # Repurposing HOME costs the user site-packages the interpreter found via the
    # real one -- which is exactly why the RAIL never repurposes HOME. A test may,
    # but must then supply the documented repair, or the child dies on `import
    # yaml` instead of exercising isolation.
    env["PYTHONUSERBASE"] = _REAL_USER_BASE
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
            self.assertEqual(code, 0, f"nested CLI exited {code}\n--- stdout ---\n{_out}\n--- stderr ---\n{_err}")
            observed = json.loads(report.read_text(encoding="utf-8"))

        # This child resolves with MOZYO_BRIDGE_HOME still pinned, so it is the
        # *explicit* case, not the passwd-fallback one: the pinned home is denied
        # and resolution moves to the fence root in BOTH branches. Only the
        # grandchild below, which strips that pin, follows the branch contract.
        self.assertEqual(observed["resolved"], observed["fence_root"])
        self.assertNotIn(str(fixture.guarded_home), observed["resolved"])
        self.assertNotEqual(
            observed["resolved"], str(Path("~/.mozyo_bridge").expanduser().resolve())
        )
        # HOME was inherited, not repurposed (#14757 acceptance 1). Compared
        # against the synthetic account HOME the child was GIVEN -- comparing to
        # this process's HOME only ever restated the fixture's own coupling.
        self.assertEqual(
            observed["home_env"], str(synthetic_account_home(fixture.guarded_home))
        )
        # TMPDIR points into the task root, so temp files stay there.
        task_root = Path(observed["fence_root"]).parent
        self.assertTrue(observed["tmpdir"].startswith(str(task_root)))
        # A grandchild that scrubbed MOZYO_BRIDGE_HOME but kept the fake HOME
        # resolves by the same branch contract (j#100500).
        self.assertEqual(observed["grandchild_rc"], 0)
        _assert_branch_contract(
            self,
            observed=observed,
            synthetic_default=str(
                (synthetic_account_home(fixture.guarded_home) / ".mozyo_bridge").resolve()
            ),
            resolved_key="grandchild",
        )

    def test_a_test_that_clears_the_environment_stays_inside_the_fence(self) -> None:
        """The #14477 reach, exercised through the real rail."""
        with tempfile.TemporaryDirectory() as reports:
            report = Path(reports) / "cleared.json"
            fixture = _Fixture(self, "")
            (fixture.tests / f"{fixture.module}.py").write_text(
                _CLEAR_ENV_MODULE.format(
                    src=SRC,
                    report=str(report),
                    account_home=str(synthetic_account_home(fixture.guarded_home)),
                ),
                encoding="utf-8",
            )
            code, _out, _err = _run_cli(
                ["tests", "run", "--repo", str(fixture.repo)],
                guarded_home=fixture.guarded_home,
            )
            self.assertEqual(code, 0, f"nested CLI exited {code}\n--- stdout ---\n{_out}\n--- stderr ---\n{_err}")
            observed = json.loads(report.read_text(encoding="utf-8"))
            # `.resolve()`: the deny authority holds canonical paths, and on macOS
            # /var is a symlink to /private/var.
            synthetic_default = str(
                (synthetic_account_home(fixture.guarded_home) / ".mozyo_bridge").resolve()
            )

        # The environment really was empty -- otherwise this measures nothing.
        self.assertTrue(observed["env_empty"])
        # The passwd fallback answered with the synthetic account, deterministically.
        self.assertEqual(
            str(Path(observed["synthetic_default"]).resolve()), synthetic_default
        )
        _assert_branch_contract(
            self,
            observed=observed,
            synthetic_default=synthetic_default,
            resolved_key="resolved",
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
                Path(task),
                denied_homes=(home,),
                canary=Path(task) / "canary",
                origin_backend="sandbox_exec",
                allowed_control_root=Path(task) / "control",
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
                Path(task),
                denied_homes=(home,),
                canary=Path(task) / "canary",
                origin_backend="sandbox_exec",
                allowed_control_root=Path(task) / "control",
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
                Path(task),
                denied_homes=(home,),
                canary=Path(task) / "canary",
                origin_backend="sandbox_exec",
                allowed_control_root=Path(task) / "control",
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

            def child(_layout, _env, _interpreter, _os_fence) -> int:
                # Simulate an un-hooked writer reaching the SECOND home -- the one
                # R1 denied but never snapshotted.
                (second / "leaked.txt").write_text("x", encoding="utf-8")
                return 0

            outcome = guarded_isolated_run(
                repo_root=Path(task), child=child, guarded_homes=(first, second)
            )
        self.assertEqual(len(outcome.guards), 2)
        # The suite itself passed -- the child returned 0 -- and the run still fails,
        # because the guard caught the leak. Keep both halves: they are what makes
        # "green suite" and "unchanged home" separate claims.
        self.assertTrue(outcome.suite_success)
        self.assertFalse(outcome.success, "a write to the second home was not caught")
        self.assertTrue(any(not g.unchanged for g in outcome.guards))


    def test_a_clean_run_reports_the_home_unchanged(self) -> None:
        fixture = _Fixture(self, _PASS_MODULE)
        code, out, err = _run_cli(
            ["tests", "run", "--repo", str(fixture.repo)],
            guarded_home=fixture.guarded_home,
        )
        self.assertEqual(code, 0, f"nested CLI exited {code}\n--- stdout ---\n{out}\n--- stderr ---\n{err}")
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

    def test_a_quota_failure_inside_the_suite_is_flagged_environmental(
        self,
    ) -> None:
        """Redmine #15710: a suite dying on EDQUOT stays red, but the verdict
        carries the typed environmental note so dozens of quota errors are not
        misread as regressions of the change under test."""
        fixture = _Fixture(
            self,
            "import errno, unittest\n"
            "class Quota(unittest.TestCase):\n"
            "    def test_write(self):\n"
            "        raise OSError(errno.EDQUOT, 'Disk quota exceeded')\n",
        )
        code, out, err = _run_cli(
            ["tests", "run", "--repo", str(fixture.repo), "--format", "json"],
            guarded_home=fixture.guarded_home,
        )
        self.assertEqual(code, 1, f"nested CLI exited {code}\n--- stdout ---\n{out}\n--- stderr ---\n{err}")
        payload = json.loads(out)
        self.assertFalse(payload["success"])
        self.assertFalse(payload["suite_success"])
        self.assertIsNotNone(payload["disk_pressure"])
        self.assertTrue(payload["disk_pressure"]["suspected"])
        self.assertEqual(payload["disk_pressure"]["stage"], "suite-stderr")
        self.assertIn("EDQUOT", " ".join(payload["disk_pressure"]["markers"]))
        self.assertIn(
            "environmental disk pressure suspected",
            " ".join(payload["reasons"]),
        )
        # The fixture's intentional traceback is still streamed through to
        # stderr; asserted here rather than leaked to the parent terminal.
        self.assertIn("Errno 122", err)

    def test_the_json_verdict_separates_the_two_halves(self) -> None:
        fixture = _Fixture(self, _PASS_MODULE)
        code, out, err = _run_cli(
            ["tests", "run", "--repo", str(fixture.repo), "--format", "json"],
            guarded_home=fixture.guarded_home,
        )
        self.assertEqual(code, 0, f"nested CLI exited {code}\n--- stdout ---\n{out}\n--- stderr ---\n{err}")
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
                code, out, err = _run_cli(
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
                # Carry the child's own output into the failure. A bare
                # `assertEqual(code, 0)` here cost a whole capture loop: the one
                # intermittent failure in 40 runs said only "1 != 0" and threw away
                # the only copy of why.
                self.assertEqual(
                    code,
                    0,
                    f"nested `tests {family} --format json` exited {code}\n"
                    f"--- child stdout ---\n{out}\n--- child stderr ---\n{err}",
                )
                payload = json.loads(out)  # one document, not two
                self.assertTrue(all(g["unchanged"] for g in payload["home_guards"]))
                self.assertTrue(payload["fence_root"])
                self.assertTrue(payload["success"])

    def test_the_isolation_marker_alone_does_not_buy_an_in_process_run(self) -> None:
        """Both entry points accept the marker -- and refuse to trust it (item 4).

        `--already-isolated` is a hidden flag and `process_home_fence()` is an
        in-process marker. Either can be true while nothing is enforcing: the flag
        is just an argument, and the marker only says a home resolver was rebound.
        Taking them as proof is how an unfenced run reports itself isolated, so the
        handler now requires a real OS boundary context and refuses without one.

        The flag must still *parse* -- that is what the re-exec sends -- so the
        failure here has to be the boundary check, not an unrecognised argument.
        """
        from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.commands_test_run import (  # noqa: E501
            ISOLATED_FLAG,
        )

        fixture = _Fixture(self, _PASS_MODULE)
        for family in ("profile", "parallel"):
            with self.subTest(family=family):
                # Under `tests run` this process IS inside a real boundary, so the
                # context would legitimately be present and the handler would
                # rightly proceed. Drive the case under test -- marker set, no
                # boundary -- explicitly, so the verdict does not depend on how the
                # suite was launched.
                with mock.patch(
                    "mozyo_bridge.e_150_quality_architecture.f_150_ci_verification"
                    ".application.commands_test_run.load_outer_context",
                    return_value=None,
                ):
                    code, _out, err = _run_cli(
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
                self.assertNotEqual(code, 0, "an unproven claim of isolation passed")
                self.assertIn("no OS boundary context", err)
                # Not an argparse rejection: the marker parsed, and the boundary
                # check is what stopped it.
                self.assertNotIn("unrecognized arguments", err)

    def test_a_valid_context_with_no_enforcement_is_still_refused(self) -> None:
        """A context DESCRIBES a boundary; it is not the boundary (j#100489 F1).

        The task venv carries its generated context module wherever the interpreter
        goes, so a run started *outside* the OS wrapper can present a structurally
        perfect context and pass `--already-isolated`. Checking the context's shape
        would admit it. Here the context is valid and points at real fixtures, but
        nothing is enforcing -- the probes are run for real and the writes land, so
        the handler must refuse.
        """
        from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.commands_test_run import (  # noqa: E501
            ISOLATED_FLAG,
        )
        from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.test_home_os_fence import (  # noqa: E501
            CONTEXT_VERSION,
            OuterContext,
        )

        with tempfile.TemporaryDirectory() as task:
            root = Path(task).resolve()
            canary, control = root / "canary", root / "control"
            create_boundary_fixtures(canary, control)
            unenforced = OuterContext(
                version=CONTEXT_VERSION,
                origin_backend=BACKEND_SANDBOX_EXEC,
                outer_protected_roots=(root / "home",),
                task_root=root,
                canary_root=canary,
                allowed_control_root=control,
            )
            fixture = _Fixture(self, _PASS_MODULE)
            with mock.patch(
                "mozyo_bridge.e_150_quality_architecture.f_150_ci_verification"
                ".application.test_home_os_fence.load_outer_context",
                return_value=unenforced,
            ), mock.patch(
                "mozyo_bridge.e_150_quality_architecture.f_150_ci_verification"
                ".application.commands_test_run.load_outer_context",
                return_value=unenforced,
            ):
                code, _out, err = _run_cli(
                    [
                        "tests",
                        "parallel",
                        "--repo",
                        str(fixture.repo),
                        "--start-dir",
                        "tests",
                        ISOLATED_FLAG,
                    ],
                    guarded_home=fixture.guarded_home,
                )
            self.assertNotEqual(code, 0, "an unenforced boundary context passed")
            self.assertIn("not enforcing", err)

    def test_the_marker_is_honoured_when_a_real_boundary_is_present(self) -> None:
        """Positive control: the refusal above must be about the boundary, not the flag.

        Under a genuine fence the same marker means what it claims, and the handler
        proceeds in-process instead of re-execing (nesting is what the marker
        exists to prevent).
        """
        fixture = _Fixture(self, _PASS_MODULE)
        for family in ("profile", "parallel"):
            with self.subTest(family=family):
                code, _out, err = _run_cli(
                    [
                        "tests",
                        family,
                        "--repo",
                        str(fixture.repo),
                        "--start-dir",
                        "tests",
                    ],
                    guarded_home=fixture.guarded_home,
                )
                self.assertEqual(code, 0, err)




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




class OsBoundaryRefusesEveryKnownBypassTest(unittest.TestCase):
    """The OS boundary refuses all three bypasses (#14757 Phase C item 1/2).

    **One boundary, never two.** When this runs inside `mozyo-bridge tests run` the
    outer boundary is already enforcing, so these tests reuse it — its origin
    backend and its pre-created fixtures — and launch probes as **prefix-less raw
    children**. Nesting is not an option: measured, a nested `sandbox-exec` exits 71
    with `sandbox_apply: Operation not permitted` and applies nothing, which
    satisfied "rc != 0 and bytes unchanged" and so passed while proving nothing
    (j#100436). Skipping is not an option either — that would drop coverage exactly
    where the boundary matters most. Only with no outer context does this build one
    fresh local fence.

    Each test calls the **shared** probe/parser and asserts its own exact token plus
    victim non-mutation. The broad stderr-signature list this replaces was the
    weaker check that let the vacuous passes through.
    """

    def setUp(self) -> None:
        self._task = tempfile.TemporaryDirectory()
        self.addCleanup(self._task.cleanup)
        task_root = Path(self._task.name).resolve()

        context = load_outer_context()
        if context is not None:
            # Reuse the enforcing outer boundary and ITS fixtures, prefix-less.
            self.fence = OsFence(
                backend=context.origin_backend,
                argv_prefix=(),
                denied=context.outer_protected_roots,
                verified=False,
                canary=context.canary_root,
                control_root=context.allowed_control_root,
                task_root=context.allowed_control_root.parent,
                inherited=True,
            )
            self.canary = context.canary_root
            self.control = context.allowed_control_root
            self.reused_outer = True
        else:
            # The task root is the boundary's one host-backed writable hole on
            # Linux. Putting the denied-home fixture below it asks for the same
            # subtree to be writable and denied, an invalid topology the boundary
            # now refuses. A real operator home is outside the task root, so keep
            # the fixture in a second temporary tree as its sibling-equivalent.
            self._guarded = tempfile.TemporaryDirectory()
            self.addCleanup(self._guarded.cleanup)
            self.canary = task_root / "canary"
            self.control = task_root / "control"
            home = Path(self._guarded.name).resolve() / "operator-home"
            home.mkdir()
            # Linux binds the canary read-only after opening the task root. The
            # source and its victims must therefore exist before argv resolution,
            # matching the production `isolated_env` ordering.
            create_boundary_fixtures(self.canary, self.control)
            try:
                self.fence = resolve_os_fence(
                    (home,), work_dir=task_root, canary=self.canary
                )
            except OsFenceUnavailable as exc:
                # UNCONDITIONAL failure here (j#100490 item 1). This class states
                # that skipping is not an option, but the setup still reached for
                # `skipTest` -- so the mandatory hard check could report success by
                # skipping, on every platform. A backend that will not resolve is
                # the failure this class exists to surface, not a reason to stand
                # down: the rail refuses to run a single test without a boundary,
                # so "no backend" can never be a green outcome here.
                self.fail(f"no OS write boundary resolved: {exc}")
            self.reused_outer = False

    def _victims(self) -> tuple[bytes, str]:
        store = self.canary / SQLITE_VICTIM_NAME
        conn = sqlite3.connect(f"file:{store.as_posix()}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT seen FROM victim").fetchone()[0]
        finally:
            conn.close()
        return (self.canary / BYTES_VICTIM_NAME).read_bytes(), row

    def _probe(self, probe: str, argv_head: list[str]) -> dict:
        return run_boundary_probe(
            self.fence, argv_head, probe, self.canary, self.control
        )

    def test_a_dir_fd_relative_write_is_refused(self) -> None:
        """j#100417 finding_1: the audit hook could not see dir_fd; the OS can."""
        before = self._victims()
        token = self._probe(PROBE_DIR_FD, [sys.executable])
        self.assertEqual(token["probe"], PROBE_DIR_FD)
        self.assertTrue(token["reason"].startswith("errno="), token)
        self.assertEqual(self._victims(), before, "a victim was mutated")

    def test_a_no_site_interpreter_write_is_refused(self) -> None:
        """j#100417 finding_2, cheapest form: `-S` skips site, so no .pth loads."""
        before = self._victims()
        token = self._probe(PROBE_NO_SITE, [sys.executable, "-S"])
        self.assertEqual(token["probe"], PROBE_NO_SITE)
        self.assertEqual(self._victims(), before, "the in-place row update landed")

    def test_a_base_executable_write_is_refused(self) -> None:
        """j#100417 finding_2 as reported: the venv's base interpreter has no .pth."""
        before = self._victims()
        base = getattr(sys, "_base_executable", None) or sys.executable
        token = self._probe(PROBE_BASE_EXECUTABLE, [base])
        self.assertEqual(token["probe"], PROBE_BASE_EXECUTABLE)
        self.assertEqual(self._victims(), before, "the in-place row update landed")

    def test_a_write_outside_the_denied_root_still_works(self) -> None:
        """The control. A boundary that blocks everything must not pass as correct.

        This is the test that caught the vacuous passes: when the boundary failed to
        apply, the refusal probes still "succeeded" and only this one failed.
        """
        token = self._probe(PROBE_CONTROL, [sys.executable])
        self.assertEqual(token["reason"], "wrote")

    def test_a_toothless_wrapper_cannot_satisfy_any_probe(self) -> None:
        """No boundary => the probe cannot emit a refusal token (Phase C item 3).

        Runs in BOTH contexts. The earlier version skipped under an outer boundary,
        because it aimed a prefix-less fence at the outer canary -- which that
        boundary already protects, so the fence was not actually toothless and the
        premise failed. Aiming instead at a genuinely *unprotected* directory (a
        fresh temp dir, task-local and therefore not in any deny set) makes the
        premise hold either way, so the skip is gone (j#100457).
        """
        with tempfile.TemporaryDirectory() as unprotected:
            canary = Path(unprotected) / "canary"
            control = Path(unprotected) / "control"
            create_boundary_fixtures(canary, control)
            toothless = OsFence(
                backend=self.fence.backend,
                argv_prefix=(),
                denied=(canary,),
                verified=False,
                canary=canary,
                control_root=control,
                task_root=canary.parent,
            )
            with self.assertRaises(OsFenceUnavailable) as caught:
                run_boundary_probe(
                    toothless, [sys.executable], PROBE_DIR_FD, canary, control
                )
            # It must fail because no token appeared, not for some other reason.
            self.assertIn("token", str(caught.exception))
            # And the write LANDED -- which is the point. With nothing enforcing,
            # the probe really does mutate the victim, so a fence that cannot show
            # a refusal token must never be accepted as one that refuses.
            self.assertEqual(
                (canary / BYTES_VICTIM_NAME).read_bytes(),
                b"MUTATED",
                "the unprotected probe should have written, proving the fence is "
                "toothless rather than quietly inert",
            )

    def test_a_missing_fixture_fails_closed(self) -> None:
        """Phase C item 3: absent victim must not be mistaken for a refusal."""
        with tempfile.TemporaryDirectory() as empty:
            starved = replace(
                self.fence,
                canary=Path(empty),
                control_root=Path(empty) / "control",
            )
            with self.assertRaises(OsFenceUnavailable):
                verify_os_fence(starved, Path(sys.executable))


class ProbeParserFaultTest(unittest.TestCase):
    """The probe parser rejects every malformed outcome (#14757 Phase C item 3).

    Drives `run_boundary_probe` through a **stub wrapper** that emits scripted
    stdout / stderr / exit codes, so each fault branch is exercised deterministically
    and without an OS backend. These are the shapes that must never be mistaken for
    a refusal -- the R2 tests accepted several of them, which is how they passed
    while proving nothing (j#100436).
    """

    def setUp(self) -> None:
        self._task = tempfile.TemporaryDirectory()
        self.addCleanup(self._task.cleanup)
        root = Path(self._task.name)
        self.canary = root / "canary"
        self.control = root / "control"
        create_boundary_fixtures(self.canary, self.control)

    def _fence_emitting(self, *, stdout: str = "", stderr: str = "", rc: int = 0):
        """A fence whose 'boundary' is a stub that emits exactly this outcome."""
        stub = Path(self._task.name) / f"stub_{abs(hash((stdout, stderr, rc)))}.py"
        stub.write_text(
            "import sys\n"
            f"sys.stdout.write({stdout!r})\n"
            f"sys.stderr.write({stderr!r})\n"
            f"raise SystemExit({rc})\n",
            encoding="utf-8",
        )
        return OsFence(
            backend="sandbox_exec",
            argv_prefix=(sys.executable, str(stub)),
            denied=(self.canary,),
            verified=False,
            canary=self.canary,
            control_root=self.control,
            task_root=self.canary.parent,
        )

    def _expect_refusal(self, fence, needle: str) -> None:
        with self.assertRaises(OsFenceUnavailable) as caught:
            run_boundary_probe(
                fence, [sys.executable], PROBE_DIR_FD, self.canary, self.control
            )
        self.assertIn(needle, str(caught.exception))

    def test_boundary_that_failed_to_apply_is_rejected(self) -> None:
        """exit 71 / `sandbox_apply` -- the nested-sandbox shape (j#100436)."""
        self._expect_refusal(
            self._fence_emitting(stderr="sandbox-exec: sandbox_apply: Operation not permitted", rc=71),
            "failed to APPLY",
        )

    def test_a_missing_token_is_rejected(self) -> None:
        """rc 0 with no token proves nothing."""
        self._expect_refusal(self._fence_emitting(rc=0), "exactly one token")

    def test_duplicate_tokens_are_rejected(self) -> None:
        line = 'MOZYO_FENCE_PROBE {"probe": "dir_fd", "reason": "errno=1"}\n'
        self._expect_refusal(self._fence_emitting(stdout=line * 2), "exactly one token")

    def test_a_payload_that_is_not_a_mapping_is_rejected(self) -> None:
        """A JSON list reached `.get` and raised AttributeError, not a refusal.

        An untyped crash inside the verifier is not a typed fail-closed refusal:
        callers distinguish `OsFenceUnavailable` from a bug, and only the former
        stops the run cleanly with zero tests.
        """
        self._expect_refusal(
            self._fence_emitting(stdout='MOZYO_FENCE_PROBE ["dir_fd", "errno=1"]\n'),
            "not exactly",
        )

    def test_a_payload_with_an_extra_key_is_rejected(self) -> None:
        """Extra keys mean the token came from something other than this probe."""
        self._expect_refusal(
            self._fence_emitting(
                stdout='MOZYO_FENCE_PROBE {"probe": "dir_fd", "reason": "errno=1", '
                '"ok": true}\n'
            ),
            "not exactly",
        )

    def test_free_text_as_a_reason_is_rejected(self) -> None:
        """The reason IS the evidence, so it cannot be arbitrary text.

        Before this, a stub answering `"reason": "x"` was parsed as a proven
        refusal -- the parser checked only the probe's name.
        """
        self._expect_refusal(
            self._fence_emitting(
                stdout='MOZYO_FENCE_PROBE {"probe": "dir_fd", "reason": "x"}\n'
            ),
            "is not one this probe can report",
        )

    def test_a_reason_this_probe_cannot_produce_is_rejected(self) -> None:
        """`dir_fd` refuses with an errno; it never reports SQLite's message."""
        self._expect_refusal(
            self._fence_emitting(
                stdout='MOZYO_FENCE_PROBE {"probe": "dir_fd", "reason": "sqlite"}\n'
            ),
            "is not one this probe can report",
        )

    def test_an_errno_the_backend_never_refuses_with_is_rejected(self) -> None:
        """EACCES is not Seatbelt's refusal; accepting it would widen the contract."""
        self._expect_refusal(
            self._fence_emitting(
                stdout='MOZYO_FENCE_PROBE {"probe": "dir_fd", "reason": "errno=13"}\n'
            ),
            "refuses with",
        )

    def test_a_malformed_errno_reason_is_rejected(self) -> None:
        self._expect_refusal(
            self._fence_emitting(
                stdout='MOZYO_FENCE_PROBE {"probe": "dir_fd", "reason": "errno=EPERM"}\n'
            ),
            "malformed errno",
        )

    def test_an_unknown_probe_is_refused_before_anything_is_spawned(self) -> None:
        """A typed refusal, not a KeyError from inside the measurement (F4)."""
        with self.assertRaises(OsFenceUnavailable) as caught:
            run_boundary_probe(
                self._fence_emitting(), [sys.executable], "not_a_probe",
                self.canary, self.control,
            )
        self.assertIn("unknown probe", str(caught.exception))

    def test_an_unknown_backend_is_refused_before_anything_is_spawned(self) -> None:
        fence = replace(self._fence_emitting(), backend="seccomp_maybe")
        with self.assertRaises(OsFenceUnavailable) as caught:
            run_boundary_probe(
                fence, [sys.executable], PROBE_DIR_FD, self.canary, self.control
            )
        self.assertIn("unknown backend", str(caught.exception))

    def test_a_token_with_duplicate_keys_is_rejected(self) -> None:
        """`json.loads` keeps the LAST duplicate, so a bad reason can be overwritten."""
        self._expect_refusal(
            self._fence_emitting(
                stdout='MOZYO_FENCE_PROBE {"probe": "dir_fd", "reason": "x", '
                '"reason": "errno=1"}\n'
            ),
            "duplicate key",
        )

    def test_a_non_canonical_errno_spelling_is_rejected(self) -> None:
        """`int()` accepts '+1' and '01'; the producer emits '%d' (F4)."""
        for spelling in ("errno=+1", "errno=01", "errno= 1"):
            with self.subTest(spelling=spelling):
                self._expect_refusal(
                    self._fence_emitting(
                        stdout='MOZYO_FENCE_PROBE {"probe": "dir_fd", "reason": '
                        f'"{spelling}"}}\n'
                    ),
                    "malformed errno",
                )

    def test_a_non_string_reason_is_rejected(self) -> None:
        self._expect_refusal(
            self._fence_emitting(
                stdout='MOZYO_FENCE_PROBE {"probe": "dir_fd", "reason": 1}\n'
            ),
            "non-empty string",
        )

    def test_a_token_naming_another_probe_is_rejected(self) -> None:
        """A token from a different probe must not satisfy this one."""
        self._expect_refusal(
            self._fence_emitting(
                stdout='MOZYO_FENCE_PROBE {"probe": "allowed_control", "reason": "wrote"}\n'
            ),
            "token names",
        )

    def test_an_unparseable_token_is_rejected(self) -> None:
        self._expect_refusal(
            self._fence_emitting(stdout="MOZYO_FENCE_PROBE not-json\n"), "unparseable"
        )

    def test_a_nonzero_exit_with_a_token_is_still_rejected(self) -> None:
        """The contract is rc 0 AND a token; a crash after emitting is not a pass."""
        self._expect_refusal(
            self._fence_emitting(
                stdout='MOZYO_FENCE_PROBE {"probe": "dir_fd", "reason": "errno=1"}\n', rc=9
            ),
            "expected rc 0",
        )

    def test_a_well_formed_token_is_accepted(self) -> None:
        """The positive control: the parser is not simply rejecting everything."""
        token = run_boundary_probe(
            self._fence_emitting(
                stdout='MOZYO_FENCE_PROBE {"probe": "dir_fd", "reason": "errno=1"}\n'
            ),
            [sys.executable],
            PROBE_DIR_FD,
            self.canary,
            self.control,
        )
        self.assertEqual(token, {"probe": "dir_fd", "reason": "errno=1"})


class RuntimeVerifierFaultTest(unittest.TestCase):
    """`verify_os_fence` rejects each way a boundary can look right and not be.

    Phase C3b (j#100459). The parser faults are covered by
    :class:`ProbeParserFaultTest`; these drive the **verifier**, whose job is the
    conjunction the parser cannot see: right errno for THIS backend, victims
    actually unchanged, the allowed control write actually landing, and an
    inherited fence still being re-probed rather than trusted.
    """

    def setUp(self) -> None:
        self._task = tempfile.TemporaryDirectory()
        self.addCleanup(self._task.cleanup)
        self.root = Path(self._task.name).resolve()
        self.canary = self.root / "canary"
        self.control = self.root / "control"
        self.home = self.root / "operator-home"
        self.home.mkdir()
        try:
            self.fence = resolve_os_fence(
                (self.home,), work_dir=self.root, canary=self.canary
            )
        except OsFenceUnavailable as exc:
            _require_backend(self, exc)
        create_boundary_fixtures(self.canary, self.control)

    def test_a_refusal_with_the_wrong_errno_for_this_backend_is_rejected(self) -> None:
        """An EPERM refusal must not satisfy a backend that expects EROFS.

        Real end-to-end: the macOS Seatbelt boundary refuses with EPERM, but the
        fence is labelled `bwrap`, whose contract is EROFS. The child sees an errno
        its backend does not expect, so it declines to emit a refusal token. This is
        why the errno table is per-backend rather than a shared set -- a refusal
        from the wrong mechanism is not evidence for this one.
        """
        # Mislabel as a backend that is NOT this host's real one (j#100463). On
        # Linux the real backend IS bwrap, so hardcoding bwrap would make this test
        # assert the *correct* label there and stop testing anything.
        other = (
            BACKEND_BWRAP
            if self.fence.backend == BACKEND_SANDBOX_EXEC
            else BACKEND_SANDBOX_EXEC
        )
        mislabelled = OsFence(
            backend=other,
            argv_prefix=self.fence.argv_prefix,
            denied=self.fence.denied,
            verified=False,
            canary=self.canary,
            control_root=self.control,
            task_root=self.canary.parent,
        )
        with self.assertRaises(OsFenceUnavailable):
            verify_os_fence(mislabelled, Path(sys.executable))

    def test_a_mutated_victim_is_rejected_even_when_every_token_arrives(self) -> None:
        """Tokens say 'refused'; the bytes say otherwise. The bytes win."""
        stub = self.root / "mutating_stub.py"
        stub.write_text(
            "import json, sys\n"
            f"open({str(self.canary / BYTES_VICTIM_NAME)!r}, 'wb').write(b'MUTATED')\n"
            "probe = 'dir_fd'\n"
            "for i, a in enumerate(sys.argv):\n"
            "    if a == '-c' and i + 1 < len(sys.argv):\n"
            "        src = sys.argv[i + 1]\n"
            "        for name in ('allowed_control','base_executable_sqlite','no_site_sqlite','dir_fd'):\n"
            "            if repr(name) in src:\n"
            "                probe = name\n"
            "                break\n"
            "        break\n"
            # A per-probe VALID reason. The strict parser (j#100483 item 3) now
            # rejects free text, so a stub answering 'x' would be refused at the
            # parser and never reach the verifier branch this test is about --
            # passing for the wrong reason.
            f"reasons = {{'dir_fd': 'errno={sorted(BACKEND_REFUSAL_ERRNOS[self.fence.backend])[0]}', "
            "'no_site_sqlite': 'sqlite', 'base_executable_sqlite': 'sqlite', "
            "'allowed_control': 'wrote'}\n"
            "print('MOZYO_FENCE_PROBE ' + json.dumps({'probe': probe, 'reason': reasons[probe]}))\n",
            encoding="utf-8",
        )
        forging = OsFence(
            backend=self.fence.backend,
            argv_prefix=(sys.executable, str(stub)),
            denied=(self.canary,),
            verified=False,
            canary=self.canary,
            control_root=self.control,
            task_root=self.canary.parent,
        )
        with self.assertRaises(OsFenceUnavailable) as caught:
            verify_os_fence(forging, Path(sys.executable))
        self.assertIn("mutated", str(caught.exception))

    def test_a_control_write_that_never_lands_is_rejected(self) -> None:
        """A boundary that blocks everything must not pass as a targeted one."""
        stub = self.root / "silent_stub.py"
        stub.write_text(
            "import json, sys\n"
            "probe = 'dir_fd'\n"
            "for i, a in enumerate(sys.argv):\n"
            "    if a == '-c' and i + 1 < len(sys.argv):\n"
            "        src = sys.argv[i + 1]\n"
            "        for name in ('allowed_control','base_executable_sqlite','no_site_sqlite','dir_fd'):\n"
            "            if repr(name) in src:\n"
            "                probe = name\n"
            "                break\n"
            "        break\n"
            # A per-probe VALID reason. The strict parser (j#100483 item 3) now
            # rejects free text, so a stub answering 'x' would be refused at the
            # parser and never reach the verifier branch this test is about --
            # passing for the wrong reason.
            f"reasons = {{'dir_fd': 'errno={sorted(BACKEND_REFUSAL_ERRNOS[self.fence.backend])[0]}', "
            "'no_site_sqlite': 'sqlite', 'base_executable_sqlite': 'sqlite', "
            "'allowed_control': 'wrote'}\n"
            "print('MOZYO_FENCE_PROBE ' + json.dumps({'probe': probe, 'reason': reasons[probe]}))\n",
            encoding="utf-8",
        )
        never_writes = OsFence(
            backend=self.fence.backend,
            argv_prefix=(sys.executable, str(stub)),
            denied=(self.canary,),
            verified=False,
            canary=self.canary,
            control_root=self.control,
            task_root=self.canary.parent,
        )
        with self.assertRaises(OsFenceUnavailable) as caught:
            verify_os_fence(never_writes, Path(sys.executable))
        self.assertIn("control write", str(caught.exception))

    def test_an_inherited_fence_is_re_probed_not_trusted(self) -> None:
        """Inherited must still be measured this run (j#100445 item 5).

        An inherited fence carries no wrapper, so if the surrounding boundary is not
        actually enforcing, the probes must fail rather than the fence being taken on
        trust. Here the canary is unprotected, so an inherited fence over it must be
        rejected -- and `verified` must never have been granted.
        """
        with tempfile.TemporaryDirectory() as unprotected:
            canary = Path(unprotected) / "canary"
            control = Path(unprotected) / "control"
            create_boundary_fixtures(canary, control)
            inherited = OsFence(
                backend=self.fence.backend,
                argv_prefix=(),
                denied=(canary,),
                verified=False,
                canary=canary,
                control_root=control,
                task_root=control.parent,
                inherited=True,
            )
            self.assertFalse(inherited.verified)
            with self.assertRaises(OsFenceUnavailable):
                verify_os_fence(inherited, Path(sys.executable))


class OuterContextLoaderFaultTest(unittest.TestCase):
    """`load_outer_context` fails closed on every corrupt shape (j#100483 item 3).

    Exactly one condition may answer "there is no outer boundary": our own context
    module is absent. Every other defect -- a module that exists but is broken, a
    field of the wrong type, a path that means nothing across processes -- is a
    *corrupted* boundary. Reading any of those as "absent" is the silent downgrade
    from enforcing to unfenced that this whole round exists to remove, so each one
    must raise instead.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve()
        self.task = self.dir / "task"
        (self.task / "canary").mkdir(parents=True)
        (self.task / "control").mkdir(parents=True)
        sys.path.insert(0, str(self.dir))
        self.addCleanup(sys.path.remove, str(self.dir))
        self.addCleanup(sys.modules.pop, "_mozyo_test_fence", None)

    def _write(self, body: str) -> None:
        sys.modules.pop("_mozyo_test_fence", None)
        (self.dir / "_mozyo_test_fence.py").write_text(body, encoding="utf-8")

    def _valid(self, **overrides: str) -> str:
        fields = {
            "MOZYO_FENCE_CONTEXT_VERSION": "1",
            "MOZYO_FENCE_ORIGIN_BACKEND": repr(BACKEND_SANDBOX_EXEC),
            "MOZYO_FENCE_OUTER_PROTECTED_ROOTS": repr((str(self.dir / "home"),)),
            "MOZYO_FENCE_TASK_ROOT": repr(str(self.task)),
            "MOZYO_FENCE_CANARY_ROOT": repr(str(self.task / "canary")),
            "MOZYO_FENCE_ALLOWED_CONTROL_ROOT": repr(str(self.task / "control")),
        }
        fields.update(overrides)
        return "".join(f"{k} = {v}\n" for k, v in fields.items())

    def test_a_valid_context_loads(self) -> None:
        """Positive control: without it the faults below could all pass vacuously."""
        self._write(self._valid())
        context = load_outer_context()
        self.assertIsNotNone(context)
        self.assertEqual(context.task_root, self.task)

    def test_an_absent_module_is_the_only_no_boundary_answer(self) -> None:
        """Verdict must not depend on how the suite was launched.

        Under `mozyo-bridge tests run` this process really is inside a boundary, so
        `_mozyo_test_fence` is importable from the task venv and simply dropping it
        from `sys.modules` would re-import the real one. `None` in `sys.modules`
        makes the import raise ModuleNotFoundError for *this* name specifically,
        which is the one condition that legitimately means "no boundary".
        """
        with mock.patch.dict(sys.modules, {"_mozyo_test_fence": None}):
            self.assertIsNone(load_outer_context())

    def test_a_context_whose_own_import_is_missing_is_not_read_as_absent(self) -> None:
        """The fault that motivated this: a broken module is not a missing one.

        `import _mozyo_test_fence` raising ModuleNotFoundError says nothing about
        *which* module was missing. A context that imports a helper which is not
        installed used to return None here -- so an enforcing boundary reported
        itself absent, and the caller happily built a second one (or ran bare).
        """
        self._write("import _definitely_not_installed_zz\n" + self._valid())
        with self.assertRaises(OsFenceUnavailable) as caught:
            load_outer_context()
        self.assertIn("_definitely_not_installed_zz", str(caught.exception))

    def test_a_syntactically_broken_context_is_refused(self) -> None:
        self._write("this is not python\n")
        with self.assertRaises(OsFenceUnavailable):
            load_outer_context()

    def test_a_bool_version_is_refused(self) -> None:
        """`isinstance(True, int)` is True, and `True == 1`.

        So a bool version passes both the type check and the equality check, and a
        context that never declared a real version would be accepted as version 1.
        """
        self._write(self._valid(MOZYO_FENCE_CONTEXT_VERSION="True"))
        with self.assertRaises(OsFenceUnavailable) as caught:
            load_outer_context()
        self.assertIn("bool", str(caught.exception))

    def test_an_unknown_backend_is_refused(self) -> None:
        self._write(self._valid(MOZYO_FENCE_ORIGIN_BACKEND="'seccomp_maybe'"))
        with self.assertRaises(OsFenceUnavailable) as caught:
            load_outer_context()
        self.assertIn("unknown backend", str(caught.exception))

    def test_a_relative_task_root_is_refused(self) -> None:
        """`Path('rel').resolve()` invents a root from the reader's cwd.

        The context crosses processes that do not share a working directory, so a
        relative path resolves differently in each generation -- and the boundary
        protects none of them.
        """
        self._write(self._valid(MOZYO_FENCE_TASK_ROOT="'task'"))
        with self.assertRaises(OsFenceUnavailable) as caught:
            load_outer_context()
        self.assertIn("non-absolute", str(caught.exception))

    def test_a_relative_protected_root_is_refused(self) -> None:
        self._write(
            self._valid(MOZYO_FENCE_OUTER_PROTECTED_ROOTS="('home',)")
        )
        with self.assertRaises(OsFenceUnavailable) as caught:
            load_outer_context()
        self.assertIn("non-absolute", str(caught.exception))

    def test_a_str_subclass_is_refused(self) -> None:
        """Exact types only (F3): the producer emits plain built-ins.

        A subclass can override `__eq__` / `__fspath__`, so every later check reads
        something the value only claims to be.
        """
        self._write(
            "class _S(str): pass\n"
            + self._valid(MOZYO_FENCE_ORIGIN_BACKEND=f"_S({BACKEND_SANDBOX_EXEC!r})")
        )
        with self.assertRaises(OsFenceUnavailable) as caught:
            load_outer_context()
        self.assertIn("expected exactly str", str(caught.exception))

    def test_a_non_canonical_path_is_refused(self) -> None:
        """Absolute is not enough (F3): `..` normalises away after admission."""
        sneaky = str(self.task / "canary" / ".." / "canary")
        self._write(self._valid(MOZYO_FENCE_CANARY_ROOT=repr(sneaky)))
        with self.assertRaises(OsFenceUnavailable) as caught:
            load_outer_context()
        self.assertIn("non-canonical", str(caught.exception))

    def test_a_missing_field_is_refused(self) -> None:
        body = self._valid()
        body = "\n".join(
            line for line in body.splitlines() if "CANARY_ROOT" not in line
        )
        self._write(body + "\n")
        with self.assertRaises(OsFenceUnavailable) as caught:
            load_outer_context()
        self.assertIn("MOZYO_FENCE_CANARY_ROOT", str(caught.exception))

    def test_a_fixture_outside_the_task_root_is_refused(self) -> None:
        self._write(
            self._valid(MOZYO_FENCE_CANARY_ROOT=repr(str(self.dir / "elsewhere")))
        )
        with self.assertRaises(OsFenceUnavailable) as caught:
            load_outer_context()
        self.assertIn("outside the task", str(caught.exception))


class ThreeGenerationAuthorityPropagationTest(unittest.TestCase):
    """gen-1 -> gen-2 -> gen-3 keep the FIRST boundary's authority (j#100483 item 1).

    The earlier version of this only proved a boundary survives two ``exec`` calls:
    gen-2 was a bare ``subprocess.run``, so nothing re-entered ``isolated_env`` and
    nothing re-published a context. That is the interesting step. A gen-2 that
    re-arms and republishes is free to publish *its own* roots, and then gen-3
    resolves against something no boundary protects -- it would probe an unguarded
    canary, read "no boundary", and either try to nest (impossible on macOS, exit
    71) or run unprotected.

    So here gen-2 genuinely calls ``isolated_env`` in a task-local root, and drives
    gen-3 with the prefix-less inherited fence, interpreter and env that call
    returns. gen-3 then compares **every** authority field against gen-1 exactly,
    and checks the victim/control invariants rather than a bare exit code.
    """

    def test_a_regenerated_second_generation_still_answers_to_the_first(self) -> None:
        with tempfile.TemporaryDirectory() as task:
            root = Path(task).resolve()
            expected = root / "gen1.json"
            report = root / "gen3.json"

            gen3 = root / "gen3.py"
            gen3.write_text(
                "import json, os, sys\n"
                f"sys.path.insert(0, {SRC!r})\n"
                "from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification"
                ".application.test_home_os_fence import load_outer_context\n"
                "c = load_outer_context()\n"
                "out = {'has_context': c is not None}\n"
                "if c is not None:\n"
                "    out['backend'] = c.origin_backend\n"
                "    out['protected'] = sorted(str(p) for p in c.outer_protected_roots)\n"
                "    out['task_root'] = str(c.task_root)\n"
                "    out['canary'] = str(c.canary_root)\n"
                "    out['control'] = str(c.allowed_control_root)\n"
                f"    victim = os.path.join(str(c.canary_root), {BYTES_VICTIM_NAME!r})\n"
                "    before = open(victim, 'rb').read() if os.path.exists(victim) else None\n"
                "    try:\n"
                # PREFIX-LESS: nothing wraps this. The boundary is either inherited
                # through two execs and a re-arm, or this write lands.
                "        open(os.path.join(str(c.canary_root), 'gen3-probe'), 'w').write('x')\n"
                "        out['refused'] = False\n"
                "    except OSError as exc:\n"
                "        out['refused'] = exc.errno\n"
                "    after = open(victim, 'rb').read() if os.path.exists(victim) else None\n"
                "    out['victim_intact'] = before == after\n"
                # The control write must SUCCEED. Without it, a host that refuses
                # every write for an unrelated reason reads as a passing boundary.
                "    try:\n"
                "        open(os.path.join(str(c.allowed_control_root), 'gen3-ok'), 'w').write('y')\n"
                "        out['control_writable'] = True\n"
                "    except OSError as exc:\n"
                "        out['control_writable'] = f'{type(exc).__name__}: {exc}'\n"
                f"open({str(report)!r}, 'w').write(json.dumps(out))\n",
                encoding="utf-8",
            )

            gen2 = root / "gen2.py"
            gen2.write_text(
                "import subprocess, sys\n"
                "from pathlib import Path\n"
                f"sys.path.insert(0, {SRC!r})\n"
                "from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification"
                ".application.test_home_fence import isolated_env\n"
                "from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification"
                ".application.test_home_os_fence import load_outer_context\n"
                "c = load_outer_context()\n"
                "assert c is not None, 'gen-2 lost the outer context'\n"
                # Task-local: scratch for this run, so it must not be promoted into
                # the shared deny set (classify_requested_home / j#100447 item 5).
                "task = Path(c.task_root) / 'gen2-task'\n"
                "_layout, env, interpreter, _ledger, fence = isolated_env(\n"
                "    task, denied_homes=c.outer_protected_roots\n"
                ")\n"
                "assert fence.inherited, 'gen-2 built a second boundary instead of inheriting'\n"
                "assert not fence.argv_prefix, f'gen-2 fence is not prefix-less: {fence.argv_prefix}'\n"
                f"rc = subprocess.run(fence.wrap([str(interpreter), {str(gen3)!r}]), env=env).returncode\n"
                "raise SystemExit(rc)\n",
                encoding="utf-8",
            )

            def child(layout, env, interpreter, os_fence):
                # Do NOT create a victim here: under an inherited fence this canary
                # is already denied, and the write would be refused. The fixtures
                # `create_boundary_fixtures` made before the boundary applied are
                # the victims to read back.
                expected.write_text(
                    json.dumps(
                        {
                            "backend": os_fence.backend,
                            "protected": sorted(str(p) for p in os_fence.denied),
                            "task_root": str(os_fence.task_root),
                            "canary": str(os_fence.canary),
                            "control": str(os_fence.control_root),
                        }
                    ),
                    encoding="utf-8",
                )
                return subprocess.run(
                    os_fence.wrap([str(interpreter), str(gen2)]), env=env
                ).returncode

            # No skip. A host with no boundary cannot produce acceptance evidence
            # for this rail, and a green-because-skipped run is exactly the vacuous
            # pass j#100483 item 1 refuses to accept -- so let it raise.
            # Same coupling, in-process: `guarded_isolated_run` resolves ambient
            # homes from THIS environment, so without a task-local HOME it guards
            # the operator's real home (j#100496).
            # Item 4: fix BOTH HOME and the explicit MOZYO_BRIDGE_HOME to fixture
            # paths, so neither the default-home spelling nor the explicit one can
            # reach the operator's real home. PYTHONUSERBASE keeps the already
            # resolved user packages available to the children -- a stand-in HOME
            # otherwise costs them `yaml` (measured).
            fake_home = root / "fake-operator-account"
            (fake_home / ".mozyo_bridge").mkdir(parents=True)
            fixture_home = root / "fixture-operator-home"
            fixture_home.mkdir()
            with mock.patch.dict(
                os.environ,
                {
                    "HOME": str(fake_home),
                    "MOZYO_BRIDGE_HOME": str(fixture_home),
                    "PYTHONUSERBASE": _REAL_USER_BASE,
                },
            ):
                outcome = guarded_isolated_run(repo_root=root, child=child)

            self.assertTrue(report.is_file(), "gen-3 never reported")
            seen = json.loads(report.read_text(encoding="utf-8"))
            gen1 = json.loads(expected.read_text(encoding="utf-8"))

        self.assertTrue(seen["has_context"], "gen-3 lost the boundary context")
        # Every authority field, compared exactly against gen-1 -- not just the two
        # that happened to be convenient.
        for field in ("backend", "protected", "task_root", "canary", "control"):
            self.assertEqual(
                seen[field],
                gen1[field],
                f"gen-3 {field} was regenerated by gen-2 instead of inherited from gen-1",
            )
        self.assertIn(
            seen["refused"],
            sorted(BACKEND_REFUSAL_ERRNOS[gen1["backend"]]),
            f"gen-3 was not refused (got {seen['refused']!r}); the boundary did not "
            f"survive a re-arming second generation",
        )
        self.assertTrue(seen["victim_intact"], "the refused write still mutated the victim")
        self.assertIs(
            seen["control_writable"],
            True,
            f"the control write failed ({seen['control_writable']!r}); this host refuses "
            f"writes for some reason other than the fence, so the refusal above proves nothing",
        )
        # `success`, not `suite_success` (F7): the suite passing says nothing about
        # the home guard or the refusal ledger, and this rail's verdict is the
        # conjunction of all three.
        self.assertTrue(outcome.success)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
