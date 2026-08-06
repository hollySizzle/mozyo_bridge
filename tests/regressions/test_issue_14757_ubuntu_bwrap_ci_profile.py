"""Ubuntu CI must load the packaged bwrap AppArmor profile (#14757 R4).

The first live Linux run reached bubblewrap but failed before every probe with
``setting up uid map: Permission denied``.  Ubuntu 24.04 intentionally restricts
unprivileged user namespaces; disabling that global restriction would make the
test green by removing a host security control.  Each suite-running job instead
loads Ubuntu's packaged, bwrap-specific profile and proves the global restriction
remains enabled before running the real boundary and its live absent-root probe.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
INSTALL_STEP = "Install the OS write boundary (bubblewrap)"
CHECK_STEP = "Check the OS write boundary refuses every known bypass"
PROFILE = "/usr/share/apparmor/extra-profiles/bwrap-userns-restrict"
LIVE_ABSENT_PROBE = (
    "tests.regressions.test_issue_14757_test_process_home_isolation."
    "AbsentDeniedRootOnLinuxTest."
    "test_live_bwrap_refuses_creation_and_leaves_the_host_root_absent"
)
EXPECTED_JOBS = {
    ".github/workflows/test.yml": ("quick", "integration", "full-matrix"),
    ".github/workflows/publish.yml": ("verify",),
    ".github/workflows/testpypi.yml": ("build",),
}


class UbuntuBwrapProfileWorkflowTest(unittest.TestCase):
    def _workflow(self, relative: str) -> dict:
        parsed = yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
        self.assertIsInstance(parsed, dict)
        self.assertIsInstance(parsed.get("jobs"), dict)
        return parsed

    def test_all_five_suite_jobs_load_the_packaged_profile_before_testing(self) -> None:
        seen: list[tuple[str, str]] = []
        required_in_order = (
            "apparmor apparmor-profiles bubblewrap",
            f"profile={PROFILE}",
            "aa-status --enabled",
            'test -r "$profile"',
            "kernel.apparmor_restrict_unprivileged_userns",
            'apparmor_parser --replace --skip-cache "$profile"',
            "bwrap (enforce)",
            "kernel.apparmor_restrict_unprivileged_userns",
            "/usr/bin/bwrap",
            "--ro-bind / / --dev /dev --proc /proc",
            "--perms 01777 --tmpfs /tmp",
        )

        for relative, expected_jobs in EXPECTED_JOBS.items():
            workflow = self._workflow(relative)
            for job_name in expected_jobs:
                job = workflow["jobs"][job_name]
                self.assertEqual(job["runs-on"], "ubuntu-24.04")
                steps = job["steps"]
                install_indexes = [
                    i for i, step in enumerate(steps)
                    if step.get("name") == INSTALL_STEP
                ]
                self.assertEqual(len(install_indexes), 1)
                index = install_indexes[0]
                install = steps[index]["run"]
                positions: list[int] = []
                start = 0
                for token in required_in_order:
                    position = install.find(token, start)
                    self.assertGreaterEqual(
                        position, 0, f"{relative}:{job_name} lacks {token!r}"
                    )
                    positions.append(position)
                    start = position + len(token)
                self.assertEqual(positions, sorted(positions))
                self.assertEqual(steps[index + 1].get("name"), CHECK_STEP)
                self.assertIn(LIVE_ABSENT_PROBE, steps[index + 1]["run"])
                seen.append((relative, job_name))

        self.assertEqual(len(seen), 5)

    def test_no_suite_job_disables_the_global_user_namespace_restriction(self) -> None:
        for relative, expected_jobs in EXPECTED_JOBS.items():
            workflow = self._workflow(relative)
            for job_name in expected_jobs:
                text = "\n".join(
                    str(step.get("run", ""))
                    for step in workflow["jobs"][job_name]["steps"]
                )
                self.assertNotIn(
                    "kernel.apparmor_restrict_unprivileged_userns=0", text
                )
                self.assertNotIn("sysctl -w", text)
                self.assertNotIn("ubuntu-22.04", text)
                self.assertNotIn("sudo /usr/bin/bwrap", text)


if __name__ == "__main__":
    unittest.main()
