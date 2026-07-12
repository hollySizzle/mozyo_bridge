"""Regression tests for the TestPyPI exact-SHA internal-beta gate (Redmine #13601).

The #13601 fix breaks the ``Version close -> origin/main -> TestPyPI -> ... ->
Version close`` cycle by letting the manual TestPyPI dispatch build an exact,
reviewed candidate SHA from a ``main``-fixed workflow, without first promoting
public history. These tests pin the security-relevant SHAPE of the approved
contract (j#75969 / j#75978) so a later edit that reintroduces the cycle or
weakens the OIDC boundary fails here:

  1. The workflow event ref stays ``main``; the manual path takes required
     ``source_sha`` / ``expected_version`` / ``source_ref`` / ``dispatch_nonce``
     inputs (artifact authority is the SHA, not the workflow ref).
  2. Build and publish are SEPARATE jobs; only the publish job carries
     ``id-token: write`` + ``environment: testpypi`` and it only downloads +
     publishes the artifact (no checkout / build / verify on the OIDC surface).
  3. The manual path runs fail-closed verification gates (exact SHA, source_ref
     lineage, version mirror == expected_version, successful Test CI, unused
     version), each guarded by the ``workflow_dispatch`` event.
  4. The automatic main-CI dev publish path is preserved.
  5. ``run-name`` carries the nonce and ``concurrency`` serializes per
     ``expected_version`` so dispatch<->run correlation is deterministic and
     duplicate exact-version publishes are serialized.
  6. The trusted inline version-mirror check names the SAME 2-file mirror set
     the contract declares, so it stays in lockstep.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

# This file lives at tests/regressions/, so the repo root is two levels up.
ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = ROOT / ".github" / "workflows" / "testpypi.yml"
_CONTRACT = ROOT / "vibes" / "docs" / "logics" / "release-helper-contract.md"


class TestPyPIExactShaWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _WORKFLOW.read_text(encoding="utf-8")
        cls.doc = yaml.safe_load(cls.text)
        cls.jobs = cls.doc["jobs"]
        # PyYAML parses the bare `on:` key as the boolean True.
        cls.on = cls.doc.get("on") or cls.doc.get(True)

    def test_manual_dispatch_inputs_are_exact_candidate_authority(self) -> None:
        dispatch = self.on["workflow_dispatch"]
        inputs = dispatch["inputs"]
        self.assertEqual(
            {"source_sha", "expected_version", "source_ref", "dispatch_nonce"},
            set(inputs),
        )
        for name in inputs:
            self.assertTrue(
                inputs[name].get("required"),
                msg=f"workflow_dispatch input {name} must be required",
            )

    def test_automatic_dev_path_preserved(self) -> None:
        workflow_run = self.on["workflow_run"]
        self.assertEqual(["Test"], workflow_run["workflows"])
        self.assertEqual(["main"], workflow_run["branches"])

    def test_build_and_publish_are_separate_jobs(self) -> None:
        self.assertEqual({"build", "publish"}, set(self.jobs))
        self.assertEqual("build", self.jobs["publish"]["needs"])

    def test_only_publish_job_holds_oidc_credential(self) -> None:
        build = self.jobs["build"]
        publish = self.jobs["publish"]
        # Build must NOT carry id-token: write.
        self.assertNotEqual(
            "write", (build.get("permissions") or {}).get("id-token")
        )
        # Publish carries the OIDC credential + the protected environment.
        self.assertEqual("write", publish["permissions"]["id-token"])
        self.assertEqual("testpypi", publish["environment"])

    def test_publish_job_only_downloads_and_publishes(self) -> None:
        steps = self.jobs["publish"]["steps"]
        uses = [str(step.get("uses", "")) for step in steps]
        # No checkout / build on the OIDC surface.
        self.assertFalse(
            any("checkout" in u for u in uses),
            msg="publish job must not check out source on the OIDC surface",
        )
        self.assertTrue(any("download-artifact" in u for u in uses))
        self.assertTrue(any("gh-action-pypi-publish" in u for u in uses))

    def test_manual_fail_closed_gates_present_and_guarded(self) -> None:
        steps = self.jobs["build"]["steps"]
        gates = {
            step["name"]: step
            for step in steps
            if isinstance(step, dict) and "name" in step and "Verify" in step["name"]
        }
        # The five approved fail-closed gates (j#75969 / j#75978).
        required_markers = (
            "exact source SHA",
            "source_ref resolves",
            "version mirror == expected_version",
            "successful Test CI",
            "unused on TestPyPI",
        )
        joined = " | ".join(gates)
        for marker in required_markers:
            self.assertIn(marker, joined, msg=f"missing gate for: {marker}")
        # Every gate is guarded by the manual (workflow_dispatch) event so the
        # automatic dev path is not blocked by exact-candidate requirements.
        for name, step in gates.items():
            self.assertIn(
                "workflow_dispatch",
                str(step.get("if", "")),
                msg=f"gate {name!r} must be guarded by github.event_name == workflow_dispatch",
            )

    def test_run_name_carries_nonce_and_concurrency_serializes_by_version(self) -> None:
        self.assertIn("dispatch_nonce", str(self.doc.get("run-name", "")))
        group = str(self.doc["concurrency"]["group"])
        self.assertIn("expected_version", group)

    def test_inline_version_mirror_matches_contract_mirror_set(self) -> None:
        # The trusted inline check duplicates the mirror paths on purpose (so no
        # candidate code runs the gate); assert it names the SAME files the
        # contract declares, so the duplication cannot silently drift.
        contract = _CONTRACT.read_text(encoding="utf-8")
        for mirror_file in ("pyproject.toml", "src/mozyo_bridge/__init__.py"):
            self.assertIn(mirror_file, self.text)
            self.assertIn(mirror_file, contract)


if __name__ == "__main__":
    unittest.main()
