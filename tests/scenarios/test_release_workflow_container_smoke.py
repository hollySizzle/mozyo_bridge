"""Wiring regression for the disposable-Ubuntu container smoke in the release
workflows (Redmine #14100 review j#82881 finding 2).

The container smoke is only a release gate if it is actually invoked in the
right place in the right jobs. These tests parse the workflow YAML and pin:

- both TestPyPI and production ``build`` jobs invoke
  ``scripts/disposable_ubuntu_smoke.py`` exactly once,
- the invocation sits after ``Build package`` and before ``Upload built
  distributions`` (order build -> smoke -> upload),
- it runs in blocking mode against a DIGEST-pinned image (accepted by the
  script's own blocking-mode admission), never ``--mode canary``,
- the ``build`` job carries no ``id-token: write`` and only ``publish`` does,
  with ``publish`` gated on ``needs: build`` (the OIDC boundary),
- the quick lane ``test.yml`` does NOT wire the container smoke.

Without these, deleting or reordering the smoke step, dropping the digest pin,
or moving OIDC into the build job would pass the rest of the suite silently.
"""

import importlib.util
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
_SCRIPT_REF = "scripts/disposable_ubuntu_smoke.py"

_spec = importlib.util.spec_from_file_location(
    "disposable_ubuntu_smoke", ROOT / "scripts" / "disposable_ubuntu_smoke.py"
)
smoke = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(smoke)


def _load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _steps(workflow, job):
    return [s for s in workflow["jobs"][job]["steps"] if isinstance(s, dict)]


def _smoke_step_index(steps):
    hits = [
        i
        for i, s in enumerate(steps)
        if _SCRIPT_REF in (s.get("run") or "")
    ]
    return hits


def _step_index_by_name_contains(steps, needle):
    return [i for i, s in enumerate(steps) if needle in (s.get("name") or "")]


class ReleaseWorkflowSmokeWiringTests(unittest.TestCase):
    def _assert_build_job_wires_smoke(self, workflow, job, image_env="env"):
        steps = _steps(workflow, job)
        hits = _smoke_step_index(steps)
        self.assertEqual(len(hits), 1, f"{job}: expected exactly one container-smoke step")
        smoke_idx = hits[0]

        build_idx = _step_index_by_name_contains(steps, "Build package")
        upload_idx = _step_index_by_name_contains(steps, "Upload built distributions")
        self.assertTrue(build_idx, f"{job}: no 'Build package' step")
        self.assertTrue(upload_idx, f"{job}: no 'Upload built distributions' step")
        # Order: build -> smoke -> upload.
        self.assertLess(build_idx[0], smoke_idx, f"{job}: smoke must run after build")
        self.assertLess(smoke_idx, upload_idx[0], f"{job}: smoke must run before upload")

        step = steps[smoke_idx]
        env = step.get("env") or {}
        image = env.get("DISPOSABLE_UBUNTU_IMAGE")
        self.assertIsNotNone(image, f"{job}: smoke step missing DISPOSABLE_UBUNTU_IMAGE")
        # The pinned image must satisfy the script's OWN blocking-mode admission
        # (digest-pinned). A floating tag would raise here.
        smoke.validate_image(image, "blocking")

        run = step.get("run") or ""
        self.assertIn("--image", run)
        self.assertNotIn("--mode canary", run, f"{job}: release gate must be blocking, not canary")

    def _assert_oidc_boundary(self, workflow):
        build_perms = (workflow["jobs"]["build"].get("permissions") or {})
        self.assertNotIn(
            "id-token",
            build_perms,
            "build job must not carry id-token (OIDC lives only in publish)",
        )
        publish = workflow["jobs"]["publish"]
        publish_perms = publish.get("permissions") or {}
        self.assertEqual(
            publish_perms.get("id-token"),
            "write",
            "publish job must carry id-token: write",
        )
        needs = publish.get("needs")
        needs = [needs] if isinstance(needs, str) else (needs or [])
        self.assertIn("build", needs, "publish must depend on build")

    def test_testpypi_wires_blocking_smoke(self):
        wf = _load("testpypi.yml")
        self._assert_build_job_wires_smoke(wf, "build")
        self._assert_oidc_boundary(wf)

    def test_publish_wires_blocking_smoke(self):
        wf = _load("publish.yml")
        self._assert_build_job_wires_smoke(wf, "build")
        self._assert_oidc_boundary(wf)

    def test_quick_lane_does_not_wire_smoke(self):
        text = (WORKFLOWS / "test.yml").read_text(encoding="utf-8")
        self.assertNotIn(_SCRIPT_REF, text, "quick lane test.yml must not wire the container smoke")
        self.assertNotIn("DISPOSABLE_UBUNTU_IMAGE", text)

    def test_pinned_digest_is_consistent_across_release_workflows(self):
        # Both release workflows must pin the SAME digest (kept in lockstep with
        # the doc); a drift between them would smoke two different images.
        images = set()
        for name in ("testpypi.yml", "publish.yml"):
            wf = _load(name)
            for step in _steps(wf, "build"):
                if _SCRIPT_REF in (step.get("run") or ""):
                    images.add((step.get("env") or {}).get("DISPOSABLE_UBUNTU_IMAGE"))
        self.assertEqual(len(images), 1, f"release workflows pin differing images: {images}")
        digest = next(iter(images))
        self.assertRegex(digest, r"@sha256:[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
