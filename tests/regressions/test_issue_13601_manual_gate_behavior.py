"""Behavioral regressions for the testpypi.yml manual fail-closed gates (Redmine #13601 j#76006).

The three review findings (F1/F2/F3) are logic bugs in the workflow's inline
shell / python gate bodies. These tests EXTRACT the actual `run:` body of each
gate step from ``.github/workflows/testpypi.yml`` and EXECUTE it against
controlled fakes, so they exercise the real gate code (no drift from a mirrored
copy) and prove fail-closed behavior:

  - F1: candidate ``.github/workflows/test.yml`` differing from trusted main
        fails closed (fake ``git``).
  - F2: source_ref that is a glob / resolves to zero or many origin refs fails
        closed; exactly one matching tip passes (fake ``git``).
  - F3: a TestPyPI payload without a valid ``releases`` object, malformed JSON,
        or an unreachable endpoint fails closed; a real dict decides used/unused
        (``MOZYO_TESTPYPI_JSON_URL`` pointed at a local fixture).

Only ``git`` is faked; awk / grep / printf / python3 are the real tools the gate
uses, so the shell/parsing logic stays honest.
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
_WORKFLOW = ROOT / ".github" / "workflows" / "testpypi.yml"

_DOC = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
_BUILD_STEPS = _DOC["jobs"]["build"]["steps"]


def _gate_body(name_fragment: str) -> str:
    for step in _BUILD_STEPS:
        if isinstance(step, dict) and name_fragment in str(step.get("name", "")):
            return step["run"]
    raise AssertionError(f"no build step whose name contains {name_fragment!r}")


# Fake git: only the subcommands the gates invoke, driven by env vars so each
# test controls what `ls-remote` / `show` return without a real remote.
_FAKE_GIT = r"""#!/bin/sh
case "$1" in
  ls-remote) printf '%b' "${FAKE_LSREMOTE:-}" ;;
  fetch) exit 0 ;;
  rev-parse) printf '%s\n' "${FAKE_HEAD:-}" ;;
  show)
    case "$2" in
      FETCH_HEAD:*) printf '%s' "${FAKE_TRUSTED:-}" ;;
      HEAD:*) printf '%s' "${FAKE_CANDIDATE:-}" ;;
      *) exit 0 ;;
    esac ;;
  *) exit 0 ;;
esac
"""


class _GateRunner(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bindir = Path(self._tmp.name) / "bin"
        self.bindir.mkdir()
        git = self.bindir / "git"
        git.write_text(_FAKE_GIT, encoding="utf-8")
        git.chmod(git.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def run_gate(self, body: str, env: dict) -> subprocess.CompletedProcess:
        script = Path(self._tmp.name) / "gate.sh"
        script.write_text(body, encoding="utf-8")
        run_env = {
            **os.environ,
            "PATH": f"{self.bindir}{os.pathsep}{os.environ['PATH']}",
            **env,
        }
        return subprocess.run(
            ["bash", str(script)],
            env=run_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )


class F1TestDefinitionEqualityTest(_GateRunner):
    BODY = _gate_body("Test workflow matches trusted main")

    def test_identical_test_yml_passes(self) -> None:
        result = self.run_gate(
            self.BODY,
            {"FAKE_TRUSTED": "name: Test\njobs: {}\n", "FAKE_CANDIDATE": "name: Test\njobs: {}\n"},
        )
        self.assertEqual(0, result.returncode, msg=result.stdout)
        self.assertIn("== trusted origin/main", result.stdout)

    def test_weakened_candidate_test_yml_fails_closed(self) -> None:
        result = self.run_gate(
            self.BODY,
            {
                "FAKE_TRUSTED": "name: Test\njobs: {real: gate}\n",
                "FAKE_CANDIDATE": "name: Test\njobs: {weakened: true}\n",
            },
        )
        self.assertEqual(1, result.returncode, msg=result.stdout)
        self.assertIn("differs from trusted origin/main", result.stdout)


class F2SourceRefResolutionTest(_GateRunner):
    BODY = _gate_body("source_ref resolves")
    SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"

    def _run(self, source_ref: str, lsremote: str) -> subprocess.CompletedProcess:
        return self.run_gate(
            self.BODY,
            {"SOURCE_SHA": self.SHA, "SOURCE_REF": source_ref, "FAKE_LSREMOTE": lsremote},
        )

    def test_exactly_one_matching_tip_passes(self) -> None:
        result = self._run(
            "int_13472_session_continuity",
            f"{self.SHA}\trefs/heads/int_13472_session_continuity\n",
        )
        self.assertEqual(0, result.returncode, msg=result.stdout)
        self.assertIn("exactly one origin ref tip == source_sha", result.stdout)

    def test_glob_pattern_rejected_before_lookup(self) -> None:
        # A refspec/glob must be rejected on input, never resolved by first line.
        result = self._run("refs/heads/*", f"{self.SHA}\trefs/heads/a\n{self.SHA}\trefs/heads/b\n")
        self.assertEqual(1, result.returncode, msg=result.stdout)
        self.assertIn("no globs/refspec metacharacters", result.stdout)

    def test_zero_matches_fails_closed(self) -> None:
        result = self._run("nope", "")
        self.assertEqual(1, result.returncode, msg=result.stdout)
        self.assertIn("resolved to 0 origin refs", result.stdout)

    def test_multiple_matches_fails_closed(self) -> None:
        result = self._run(
            "ambiguous",
            f"{self.SHA}\trefs/heads/ambiguous\n{self.SHA}\trefs/tags/ambiguous\n",
        )
        self.assertEqual(1, result.returncode, msg=result.stdout)
        self.assertIn("require exactly one", result.stdout)

    def test_single_match_wrong_sha_fails_closed(self) -> None:
        result = self._run("branch", "0000000000000000000000000000000000000000\trefs/heads/branch\n")
        self.assertEqual(1, result.returncode, msg=result.stdout)
        self.assertIn("!= source_sha", result.stdout)

    def test_annotated_tag_peel_line_counts_once(self) -> None:
        # Annotated tag: ls-remote emits the tag object line + a `^{}` peel line.
        # The peel line must be dropped so the tag counts as exactly one ref.
        result = self._run(
            "v1",
            f"{self.SHA}\trefs/tags/v1\n"
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\trefs/tags/v1^{}\n",
        )
        self.assertEqual(0, result.returncode, msg=result.stdout)


class F3UniquenessSchemaTest(_GateRunner):
    BODY = _gate_body("unused on TestPyPI")

    def _fixture_url(self, content: str) -> str:
        path = Path(self._tmp.name) / "testpypi.json"
        path.write_text(content, encoding="utf-8")
        return path.as_uri()

    def _run(self, url: str, expected: str = "0.10.0") -> subprocess.CompletedProcess:
        return self.run_gate(
            self.BODY,
            {"EXPECTED_VERSION": expected, "MOZYO_TESTPYPI_JSON_URL": url},
        )

    def test_unused_version_passes(self) -> None:
        url = self._fixture_url('{"releases": {"0.9.0": []}}')
        result = self._run(url)
        self.assertEqual(0, result.returncode, msg=result.stdout)
        self.assertIn("is unused on TestPyPI", result.stdout)

    def test_used_version_fails(self) -> None:
        url = self._fixture_url('{"releases": {"0.10.0": [], "0.9.0": []}}')
        result = self._run(url)
        self.assertEqual(1, result.returncode, msg=result.stdout)
        self.assertIn("already published", result.stdout)

    def test_missing_releases_key_fails_closed(self) -> None:
        url = self._fixture_url('{"info": {"name": "mozyo-bridge"}}')
        result = self._run(url)
        self.assertEqual(1, result.returncode, msg=result.stdout)
        self.assertIn("cannot prove version unused", result.stdout)

    def test_non_dict_releases_fails_closed(self) -> None:
        url = self._fixture_url('{"releases": ["0.9.0", "0.10.0"]}')
        result = self._run(url)
        self.assertEqual(1, result.returncode, msg=result.stdout)
        self.assertIn("cannot prove version unused", result.stdout)

    def test_malformed_json_fails_closed(self) -> None:
        url = self._fixture_url("not json at all {")
        result = self._run(url)
        self.assertEqual(1, result.returncode, msg=result.stdout)
        self.assertIn("not valid JSON", result.stdout)

    def test_unreachable_endpoint_fails_closed(self) -> None:
        missing = (Path(self._tmp.name) / "does-not-exist.json").as_uri()
        result = self._run(missing)
        self.assertEqual(1, result.returncode, msg=result.stdout)
        self.assertIn("unreachable", result.stdout)


if __name__ == "__main__":
    unittest.main()
