"""Regression tests for the tiered CI gate (Redmine #13734, parent US #13732).

The reform re-routes CI by risk tier instead of running the full Python matrix
on every branch push. These tests pin the SHAPE and the ROUTING so a later edit
that reintroduces per-push full-matrix, drops a pre-publish gate, or weakens the
production/OIDC boundary fails here.

Routing is proven BEHAVIORALLY, not by string match: a minimal GitHub-Actions
`if`-expression evaluator (the operator subset the workflows actually use)
evaluates each job's condition against representative event contexts. That gives
real positive AND negative gates — e.g. an issue-branch push must route to
`quick` ONLY and must NOT reach `full-matrix`.

Covered invariants (j#77169):
  #1 the matrix was already parallel; the reform is trigger frequency ->
     issue-branch push routes to a single-Python quick lane, never the matrix.
  #2 integration push -> single-Python full + health/docs + build + smoke, once.
  #3 TestPyPI build job runs an inline clean single-Python full + install smoke
     for BOTH events before upload (closes the manual-dispatch bypass), while
     the #13601 OIDC boundary + data gates survive.
  #4 nightly keeps the 3.10-3.13 matrix; production publish mechanically runs a
     3.10-3.13 full matrix + tag<->version-mirror + fresh-install before OIDC
     publish.
  #5 concurrency cancel/serialize semantics + run-summary provenance.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
_WF = ROOT / ".github" / "workflows"
_TEST_YML = _WF / "test.yml"
_TESTPYPI_YML = _WF / "testpypi.yml"
_PUBLISH_YML = _WF / "publish.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _on(doc: dict):
    # PyYAML parses the bare `on:` key as the boolean True.
    return doc.get("on") or doc.get(True)


# --------------------------------------------------------------------------- #
# Minimal GitHub-Actions expression evaluator (the subset the workflows use):
# string literals, dotted context lookups, == != && || !, startsWith(), parens.
# --------------------------------------------------------------------------- #
class _ExprEval:
    def __init__(self, text: str, ctx: dict) -> None:
        # `if:` may omit the ${{ }} wrapper; concurrency embeds it. Strip it.
        text = text.strip()
        if text.startswith("${{") and text.endswith("}}"):
            text = text[3:-2].strip()
        self.toks = self._tokenize(text)
        self.pos = 0
        self.ctx = ctx

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        toks: list[str] = []
        i = 0
        while i < len(text):
            c = text[i]
            if c.isspace():
                i += 1
            elif c == "'":
                j = text.index("'", i + 1)
                toks.append(text[i : j + 1])
                i = j + 1
            elif text.startswith("==", i) or text.startswith("!=", i) \
                    or text.startswith("&&", i) or text.startswith("||", i):
                toks.append(text[i : i + 2])
                i += 2
            elif c in "()!,":
                toks.append(c)
                i += 1
            else:
                j = i
                while j < len(text) and (text[j].isalnum() or text[j] in "._"):
                    j += 1
                toks.append(text[i:j])
                i = j
        return toks

    def _peek(self):
        return self.toks[self.pos] if self.pos < len(self.toks) else None

    def _next(self):
        tok = self.toks[self.pos]
        self.pos += 1
        return tok

    def parse(self):
        val = self._or()
        assert self.pos == len(self.toks), f"trailing tokens: {self.toks[self.pos:]}"
        return val

    def _or(self):
        val = self._and()
        while self._peek() == "||":
            self._next()
            rhs = self._and()
            val = bool(val) or bool(rhs)
        return val

    def _and(self):
        val = self._not()
        while self._peek() == "&&":
            self._next()
            rhs = self._not()
            val = bool(val) and bool(rhs)
        return val

    def _not(self):
        if self._peek() == "!":
            self._next()
            return not bool(self._not())
        return self._cmp()

    def _cmp(self):
        left = self._primary()
        if self._peek() in ("==", "!="):
            op = self._next()
            right = self._primary()
            return (left == right) if op == "==" else (left != right)
        return left

    def _primary(self):
        tok = self._peek()
        if tok == "(":
            self._next()
            val = self._or()
            assert self._next() == ")"
            return val
        if tok == "startsWith":
            self._next()
            assert self._next() == "("
            a = self._or()
            assert self._next() == ","
            b = self._or()
            assert self._next() == ")"
            return str(a).startswith(str(b))
        self._next()
        if tok.startswith("'"):
            return tok[1:-1]
        # dotted context lookup; missing -> None
        return self.ctx.get(tok)


def _eval(expr: str, **ctx) -> bool:
    return bool(_ExprEval(expr, ctx).parse())


def _job_if(doc: dict, job: str) -> str:
    return str(doc["jobs"][job].get("if", ""))


# Representative event contexts.
def _ctx(event, ref="refs/heads/issue_1_x", lane=None):
    return {
        "github.event_name": event,
        "github.ref": ref,
        "github.workflow": "Test",
        "inputs.lane": lane,
    }


class ExprEvaluatorSelfTest(unittest.TestCase):
    """Guard the evaluator itself so a routing pass/fail is trustworthy."""

    def test_operators(self) -> None:
        self.assertTrue(_eval("github.event_name == 'push'", **_ctx("push")))
        self.assertFalse(_eval("github.event_name == 'push'", **_ctx("pull_request")))
        self.assertTrue(_eval("github.event_name != 'push'", **_ctx("schedule")))
        self.assertTrue(
            _eval("startsWith(github.ref, 'refs/heads/int_')",
                  **_ctx("push", ref="refs/heads/int_9_x"))
        )
        self.assertFalse(
            _eval("startsWith(github.ref, 'refs/heads/int_')",
                  **_ctx("push", ref="refs/heads/issue_9_x"))
        )
        self.assertTrue(_eval("a == 'x' || b == 'y'", **{"a": "x", "b": "z"}))
        self.assertFalse(_eval("a == 'x' && b == 'y'", **{"a": "x", "b": "z"}))
        self.assertTrue(_eval("!(a == 'x')", **{"a": "z"}))


class TestYmlTriggerMatrixTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = _load(_TEST_YML)
        cls.on = _on(cls.doc)
        cls.jobs = cls.doc["jobs"]

    def test_triggers_present(self) -> None:
        for trig in ("push", "pull_request", "schedule", "workflow_dispatch"):
            self.assertIn(trig, self.on)

    def test_dispatch_lane_input(self) -> None:
        lane = self.on["workflow_dispatch"]["inputs"]["lane"]
        self.assertEqual("choice", lane["type"])
        self.assertEqual(["full", "quick"], lane["options"])
        self.assertEqual("full", lane["default"])

    def test_jobs_are_the_three_tiers(self) -> None:
        self.assertEqual({"quick", "integration", "full-matrix"}, set(self.jobs))

    def test_only_full_matrix_job_uses_python_matrix(self) -> None:
        def matrix(job):
            return (self.jobs[job].get("strategy") or {}).get("matrix", {}).get(
                "python-version"
            )

        self.assertEqual(["3.10", "3.11", "3.12", "3.13"], matrix("full-matrix"))
        self.assertIsNone(matrix("quick"))
        self.assertIsNone(matrix("integration"))

    def test_concurrency_cancels_only_ephemeral_runs(self) -> None:
        conc = self.doc["concurrency"]
        self.assertIn("github.ref", str(conc["group"]))
        cancel = str(conc["cancel-in-progress"])
        # PR + issue-branch push cancel; integration / nightly / dispatch do not.
        self.assertTrue(_eval(cancel, **_ctx("pull_request")))
        self.assertTrue(_eval(cancel, **_ctx("push", ref="refs/heads/issue_5_x")))
        self.assertFalse(_eval(cancel, **_ctx("push", ref="refs/heads/main")))
        self.assertFalse(_eval(cancel, **_ctx("push", ref="refs/heads/int_5_x")))
        self.assertFalse(
            _eval(cancel, **_ctx("push", ref="refs/heads/integration_wave_x"))
        )
        self.assertFalse(_eval(cancel, **_ctx("schedule")))
        self.assertFalse(_eval(cancel, **_ctx("workflow_dispatch", lane="full")))


class TestYmlRoutingTest(unittest.TestCase):
    """Behavioral routing: which job(s) run for each event (positive+negative)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = _load(_TEST_YML)

    def _runs(self, job, event, ref="refs/heads/issue_1_x", lane=None):
        return _eval(_job_if(self.doc, job), **_ctx(event, ref=ref, lane=lane))

    def test_issue_branch_push_runs_quick_only_never_matrix(self) -> None:
        # THE core #13734 gate: an issue-branch push must NOT fire the matrix.
        self.assertTrue(self._runs("quick", "push", ref="refs/heads/issue_13734_x"))
        self.assertFalse(
            self._runs("integration", "push", ref="refs/heads/issue_13734_x")
        )
        self.assertFalse(
            self._runs("full-matrix", "push", ref="refs/heads/issue_13734_x")
        )

    def test_pull_request_runs_quick_only(self) -> None:
        self.assertTrue(self._runs("quick", "pull_request"))
        self.assertFalse(self._runs("integration", "pull_request"))
        self.assertFalse(self._runs("full-matrix", "pull_request"))

    def test_integration_push_runs_integration_only(self) -> None:
        for ref in (
            "refs/heads/main",
            "refs/heads/int_13472_session_continuity",
            "refs/heads/integration_wave_20260709",
        ):
            self.assertTrue(self._runs("integration", "push", ref=ref), msg=ref)
            self.assertFalse(self._runs("quick", "push", ref=ref), msg=ref)
            self.assertFalse(self._runs("full-matrix", "push", ref=ref), msg=ref)

    def test_schedule_runs_full_matrix_only(self) -> None:
        self.assertTrue(self._runs("full-matrix", "schedule"))
        self.assertFalse(self._runs("quick", "schedule"))
        self.assertFalse(self._runs("integration", "schedule"))

    def test_dispatch_full_runs_matrix_quick_runs_quick(self) -> None:
        self.assertTrue(self._runs("full-matrix", "workflow_dispatch", lane="full"))
        self.assertFalse(self._runs("quick", "workflow_dispatch", lane="full"))
        self.assertTrue(self._runs("quick", "workflow_dispatch", lane="quick"))
        self.assertFalse(
            self._runs("full-matrix", "workflow_dispatch", lane="quick")
        )


class IntegrationBatchStepsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.steps = _load(_TEST_YML)["jobs"]["integration"]["steps"]
        cls.names = [str(s.get("name", "")) for s in cls.steps]
        cls.blob = "\n".join(str(s.get("run", "")) for s in cls.steps)

    def test_single_python_312(self) -> None:
        setup = [s for s in self.steps if "setup-python" in str(s.get("uses", ""))]
        self.assertEqual(1, len(setup))
        self.assertEqual("3.12", setup[0]["with"]["python-version"])

    def test_full_suite_health_docs_build_and_smoke(self) -> None:
        joined = " | ".join(self.names)
        self.assertIn("Module-health gate", joined)
        self.assertIn("Docs catalog validate", joined)
        self.assertIn("full", joined.lower())
        self.assertIn("Build wheel and sdist", joined)
        self.assertIn("smoke", joined.lower())
        # Full discover + fresh-install smoke exercise both entry points.
        self.assertIn("tests profile", self.blob)
        self.assertIn("python -m build", self.blob)
        self.assertIn("mozyo-bridge --version", self.blob)
        self.assertIn("mozyo --help", self.blob)

    def test_run_summary_provenance(self) -> None:
        self.assertTrue(any("summary" in n.lower() for n in self.names))
        self.assertIn("GITHUB_STEP_SUMMARY", self.blob)


class TestPyPIPrePublishGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = _load(_TESTPYPI_YML)
        cls.build_steps = cls.doc["jobs"]["build"]["steps"]

    def _step(self, fragment):
        for s in self.build_steps:
            if fragment in str(s.get("name", "")):
                return s
        raise AssertionError(f"no build step named ~{fragment!r}")

    def test_inline_clean_full_and_smoke_run_for_both_events(self) -> None:
        # The pre-publish gate steps must NOT be guarded by a workflow_dispatch
        # `if`: they run for the automatic dev path too (closes the asymmetry).
        for frag in (
            "Run full suite (clean single-Python, pre-publish)",
            "Fresh-install smoke (built artifact, pre-publish)",
            "Module-health gate (pre-publish)",
            "Docs catalog validate (pre-publish)",
        ):
            step = self._step(frag)
            self.assertNotIn(
                "workflow_dispatch",
                str(step.get("if", "")),
                msg=f"{frag} must run for BOTH events (no dispatch-only guard)",
            )

    def test_full_suite_runs_before_upload(self) -> None:
        order = [str(s.get("name", "")) for s in self.build_steps]
        full_i = next(i for i, n in enumerate(order) if "Run full suite" in n)
        build_i = next(i for i, n in enumerate(order) if n == "Build package")
        smoke_i = next(i for i, n in enumerate(order) if "Fresh-install smoke" in n)
        upload_i = next(i for i, n in enumerate(order) if "Upload built" in n)
        self.assertLess(full_i, build_i)
        self.assertLess(build_i, smoke_i)
        self.assertLess(smoke_i, upload_i)

    def test_oidc_boundary_preserved(self) -> None:
        build = self.doc["jobs"]["build"]
        publish = self.doc["jobs"]["publish"]
        self.assertNotEqual("write", (build.get("permissions") or {}).get("id-token"))
        self.assertEqual("write", publish["permissions"]["id-token"])
        self.assertEqual("testpypi", publish["environment"])

    def test_13601_data_gates_survive(self) -> None:
        names = " | ".join(str(s.get("name", "")) for s in self.build_steps)
        for marker in (
            "exact source SHA",
            "source_ref resolves",
            "version mirror == expected_version",
            "Test workflow matches trusted main",
            "unused on TestPyPI",
        ):
            self.assertIn(marker, names, msg=f"#13601 gate lost: {marker}")


class PublishProductionGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = _load(_PUBLISH_YML)
        cls.jobs = cls.doc["jobs"]

    def test_trigger_stays_release_published(self) -> None:
        self.assertEqual({"published"}, set(_on(self.doc)["release"]["types"]))

    def test_three_jobs_gate_then_build_then_publish(self) -> None:
        self.assertEqual({"verify", "build", "publish"}, set(self.jobs))
        self.assertEqual("verify", self.jobs["build"]["needs"])
        self.assertEqual("build", self.jobs["publish"]["needs"])

    def test_verify_runs_full_supported_matrix(self) -> None:
        matrix = self.jobs["verify"]["strategy"]["matrix"]["python-version"]
        self.assertEqual(["3.10", "3.11", "3.12", "3.13"], matrix)
        names = " | ".join(str(s.get("name", "")) for s in self.jobs["verify"]["steps"])
        self.assertIn("exact release SHA", names)
        self.assertIn("tag matches version mirror", names)
        self.assertIn("full suite", names.lower())

    def test_build_does_artifact_and_fresh_install_smoke(self) -> None:
        blob = "\n".join(str(s.get("run", "")) for s in self.jobs["build"]["steps"])
        self.assertIn("python -m build", blob)
        self.assertIn("mozyo-bridge --version", blob)
        self.assertIn("mozyo --help", blob)

    def test_only_publish_job_holds_oidc(self) -> None:
        for job in ("verify", "build"):
            self.assertNotEqual(
                "write", (self.jobs[job].get("permissions") or {}).get("id-token"),
                msg=f"{job} must not hold id-token on the pre-publish surface",
            )
        publish = self.jobs["publish"]
        self.assertEqual("write", publish["permissions"]["id-token"])
        self.assertEqual("pypi", publish["environment"])

    def test_publish_job_only_downloads_and_publishes(self) -> None:
        uses = [str(s.get("uses", "")) for s in self.jobs["publish"]["steps"]]
        self.assertFalse(any("checkout" in u for u in uses))
        self.assertTrue(any("download-artifact" in u for u in uses))
        self.assertTrue(any("gh-action-pypi-publish" in u for u in uses))

    def test_concurrency_serializes_per_tag_never_cancels(self) -> None:
        conc = self.doc["concurrency"]
        self.assertIn("release.tag_name", str(conc["group"]))
        self.assertFalse(conc["cancel-in-progress"])


# Fake git for the production exact-SHA gate behavioral test.
_FAKE_GIT = """#!/bin/sh
case "$1" in
  rev-parse) printf '%s\\n' "${FAKE_HEAD:-}" ;;
  *) exit 0 ;;
esac
"""


class PublishTagVersionMirrorBehaviorTest(unittest.TestCase):
    """Execute the real tag<->version-mirror gate body against fixtures."""

    @classmethod
    def setUpClass(cls) -> None:
        steps = _load(_PUBLISH_YML)["jobs"]["verify"]["steps"]
        cls.body = next(
            s["run"] for s in steps if "tag matches version mirror" in str(s.get("name", ""))
        )

    def _run(self, tag: str, pyproject_v: str, init_v: str) -> subprocess.CompletedProcess:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "mozyo-bridge"\nversion = "{pyproject_v}"\n', encoding="utf-8"
        )
        pkg = root / "src" / "mozyo_bridge"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(f'__version__ = "{init_v}"\n', encoding="utf-8")
        script = root / "gate.sh"
        script.write_text(self.body, encoding="utf-8")
        return subprocess.run(
            ["bash", str(script)],
            cwd=root,
            env={**os.environ, "TAG_NAME": tag},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def test_matching_tag_and_mirror_passes(self) -> None:
        r = self._run("v0.11.0", "0.11.0", "0.11.0")
        self.assertEqual(0, r.returncode, msg=r.stdout)
        self.assertIn("version mirror == tag version", r.stdout)

    def test_tag_without_v_prefix_passes(self) -> None:
        r = self._run("0.11.0", "0.11.0", "0.11.0")
        self.assertEqual(0, r.returncode, msg=r.stdout)

    def test_tag_mismatch_fails_closed(self) -> None:
        r = self._run("v0.12.0", "0.11.0", "0.11.0")
        self.assertEqual(1, r.returncode, msg=r.stdout)
        self.assertIn("!= tag version", r.stdout)

    def test_mirror_disagreement_fails_closed(self) -> None:
        r = self._run("v0.11.0", "0.11.0", "0.10.0")
        self.assertEqual(1, r.returncode, msg=r.stdout)
        self.assertIn("!= tag version", r.stdout)

    def test_missing_literal_fails_closed(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
        pkg = root / "src" / "mozyo_bridge"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text('__version__ = "0.11.0"\n', encoding="utf-8")
        script = root / "gate.sh"
        script.write_text(self.body, encoding="utf-8")
        r = subprocess.run(
            ["bash", str(script)],
            cwd=root,
            env={**os.environ, "TAG_NAME": "v0.11.0"},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.assertEqual(1, r.returncode, msg=r.stdout)
        self.assertIn("no version literal found", r.stdout)


if __name__ == "__main__":
    unittest.main()
