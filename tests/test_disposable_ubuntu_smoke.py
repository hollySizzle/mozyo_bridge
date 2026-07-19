"""Unit tests for scripts/disposable_ubuntu_smoke.py (Redmine #14100).

The disposable-Ubuntu smoke orchestrator is the only container-gate logic with
branching behaviour (image-mode admission, wheel provenance resolution, summary
verdict), so it is pinned here. It lives under scripts/ (not src/) because it
must run dependency-free on a fresh CI runner before the package is installed
and shells out to the Docker CLI.

These tests exercise the PURE decision surface — input validation, docker argv
assembly, container-output parsing, and summary verdict — without invoking
Docker, so they run in the normal unit suite. The real-container run is covered
by the workflow gates and the Implementation Done evidence.
"""

import importlib.util
import io
import json
import contextlib
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = ROOT / "scripts" / "disposable_ubuntu_smoke.py"

_spec = importlib.util.spec_from_file_location("disposable_ubuntu_smoke", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mod)

_DIGEST = "ubuntu@sha256:" + ("0" * 64)


def _facts(**overrides):
    base = {
        "harness_user": "smoke",
        "harness_uid": 1001,
        "harness_home": "/home/smoke",
        "observed_version_mozyo_bridge": "1.2.3",
        "observed_version_mozyo": "1.2.3",
        "package_path": "/home/smoke/venv/lib/python3.12/site-packages/mozyo_bridge",
        "preset": "redmine-governed",
        "source_mount_present": False,
    }
    base.update(overrides)
    return base


def _all_steps():
    return list(mod.EXPECTED_STEPS)


class ResolveWheelTests(unittest.TestCase):
    def test_single_match_returned(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            wheel = root / "mozyo_bridge-1.2.3-py3-none-any.whl"
            wheel.write_bytes(b"x")
            self.assertEqual(mod.resolve_wheel(root, "1.2.3"), wheel)

    def test_zero_match_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "mozyo_bridge-1.2.3-py3-none-any.whl").write_bytes(b"x")
            with self.assertRaises(mod.SmokeError):
                mod.resolve_wheel(root, "9.9.9")

    def test_version_prefix_is_exact_not_substring(self):
        # 1.2.3 must NOT match a 1.2.30 wheel: the prefix includes the trailing
        # dash so a longer version cannot be smoked under the wrong label.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "mozyo_bridge-1.2.30-py3-none-any.whl").write_bytes(b"x")
            with self.assertRaises(mod.SmokeError):
                mod.resolve_wheel(root, "1.2.3")

    def test_multiple_matches_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "mozyo_bridge-1.2.3-py3-none-any.whl").write_bytes(b"x")
            (root / "mozyo_bridge-1.2.3-py2-none-any.whl").write_bytes(b"y")
            with self.assertRaises(mod.SmokeError):
                mod.resolve_wheel(root, "1.2.3")

    def test_missing_dir_fails_closed(self):
        with self.assertRaises(mod.SmokeError):
            mod.resolve_wheel(Path("/no/such/dir/for/smoke"), "1.2.3")


class ValidateImageTests(unittest.TestCase):
    def test_blocking_requires_digest(self):
        with self.assertRaises(mod.SmokeError):
            mod.validate_image("ubuntu:24.04", "blocking")

    def test_blocking_accepts_digest(self):
        mod.validate_image(_DIGEST, "blocking")  # no raise

    def test_blocking_rejects_short_digest(self):
        with self.assertRaises(mod.SmokeError):
            mod.validate_image("ubuntu@sha256:abc", "blocking")

    def test_canary_allows_floating_tag(self):
        mod.validate_image("ubuntu:24.04", "canary")  # no raise

    def test_empty_image_fails_closed(self):
        with self.assertRaises(mod.SmokeError):
            mod.validate_image("", "canary")


class DockerCommandTests(unittest.TestCase):
    def test_mounts_artifacts_read_only_and_no_repo(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cmd = mod.build_docker_command(
                docker_bin="docker",
                image=_DIGEST,
                artifact_dir=root,
                wheel_name="mozyo_bridge-1.2.3-py3-none-any.whl",
                expected_version="1.2.3",
                preset="redmine-governed",
            )
        joined = " ".join(cmd)
        self.assertIn(f"{root.resolve()}:/artifacts:ro", joined)
        # The repo tree must never be mounted: only /artifacts appears.
        self.assertEqual(joined.count(":/artifacts:ro"), 1)
        self.assertNotIn(str(ROOT) + ":", joined)
        self.assertIn(_DIGEST, cmd)
        self.assertIn("-i", cmd)  # program arrives on stdin, nothing bind-mounted
        self.assertIn("EXPECTED_VERSION=1.2.3", cmd)
        self.assertIn("WHEEL=mozyo_bridge-1.2.3-py3-none-any.whl", cmd)
        self.assertIn("PRESET=redmine-governed", cmd)

    def test_no_secret_env_injected(self):
        with tempfile.TemporaryDirectory() as d:
            cmd = mod.build_docker_command(
                docker_bin="docker",
                image=_DIGEST,
                artifact_dir=Path(d),
                wheel_name="w.whl",
                expected_version="1.2.3",
                preset="none",
            )
        env_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-e"]
        for value in env_values:
            key = value.split("=", 1)[0]
            self.assertIn(key, {"EXPECTED_VERSION", "PRESET", "WHEEL"})


class ContainerProgramTests(unittest.TestCase):
    def test_program_covers_every_expected_surface(self):
        program = mod._container_program()
        for step in mod.EXPECTED_STEPS:
            self.assertIn(f"step {step}", program)

    def test_program_is_artifact_only_and_non_root(self):
        program = mod._container_program()
        # Installs from the mounted wheel path, never from a source tree / index.
        self.assertIn('python -m pip install --quiet "$WHEEL_PATH"', program)
        # Drops to a non-root user and refuses running the harness as root.
        self.assertIn("runuser -u smoke", program)
        self.assertIn("harness ran as root", program)
        # Read-only fingerprint; refuses a source surface.
        self.assertIn("doctor runtime --json", program)
        self.assertIn("source surface", program)


class ParseOutputTests(unittest.TestCase):
    def test_extracts_steps_and_facts(self):
        facts = _facts()
        text = "\n".join(
            [f"{mod._STEP_MARKER}{s}:ok" for s in _all_steps()]
            + [mod._FACTS_MARKER + json.dumps(facts)]
        )
        parsed = mod.parse_container_output(text)
        self.assertEqual(parsed["steps"], _all_steps())
        self.assertEqual(parsed["facts"], facts)

    def test_ignores_non_ok_and_junk_lines(self):
        text = "\n".join(
            [
                "random noise",
                f"{mod._STEP_MARKER}rules_install:fail",
                f"{mod._STEP_MARKER}rules_install:ok",
                "MOZYO_SMOKE_FACTS=not-json",
            ]
        )
        parsed = mod.parse_container_output(text)
        self.assertEqual(parsed["steps"], ["rules_install"])
        self.assertIsNone(parsed["facts"])


class BuildSummaryTests(unittest.TestCase):
    def _summary(self, **kw):
        defaults = dict(
            mode="blocking",
            image=_DIGEST,
            wheel_name="mozyo_bridge-1.2.3-py3-none-any.whl",
            wheel_sha256="deadbeef",
            expected_version="1.2.3",
            preset="redmine-governed",
            parsed={"steps": _all_steps(), "facts": _facts()},
            resolved_digest=_DIGEST,
            duration_seconds=1.2345,
            container_exit=0,
        )
        defaults.update(kw)
        return mod.build_summary(**defaults)

    def test_full_pass(self):
        summary = self._summary()
        self.assertTrue(summary["ok"])
        self.assertTrue(summary["provenance_ok"])
        self.assertTrue(summary["runtime"]["non_root"])
        self.assertEqual(summary["surfaces"]["missing"], [])
        self.assertEqual(summary["artifact"]["sha256"], "deadbeef")

    def test_missing_surface_fails(self):
        parsed = {"steps": _all_steps()[:-1], "facts": _facts()}
        summary = self._summary(parsed=parsed)
        self.assertFalse(summary["ok"])
        self.assertIn(mod.EXPECTED_STEPS[-1], summary["surfaces"]["missing"])

    def test_nonzero_exit_fails(self):
        self.assertFalse(self._summary(container_exit=3)["ok"])

    def test_root_uid_fails_provenance(self):
        summary = self._summary(parsed={"steps": _all_steps(), "facts": _facts(harness_uid=0)})
        self.assertFalse(summary["provenance_ok"])
        self.assertFalse(summary["ok"])

    def test_version_mismatch_fails_provenance(self):
        bad = _facts(observed_version_mozyo_bridge="9.9.9")
        summary = self._summary(parsed={"steps": _all_steps(), "facts": bad})
        self.assertFalse(summary["ok"])

    def test_source_mount_present_fails(self):
        bad = _facts(source_mount_present=True)
        summary = self._summary(parsed={"steps": _all_steps(), "facts": bad})
        self.assertFalse(summary["ok"])

    def test_missing_facts_fails(self):
        summary = self._summary(parsed={"steps": _all_steps(), "facts": None})
        self.assertFalse(summary["ok"])

    def test_summary_is_secret_safe_json(self):
        # The summary must serialise and must not carry credential-shaped keys.
        text = json.dumps(self._summary())
        for banned in ("token", "password", "api_key", "secret"):
            self.assertNotIn(banned, text.lower())


class MainCliTests(unittest.TestCase):
    def test_blocking_floating_tag_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                code = mod.main(
                    [
                        "--artifact-dir",
                        d,
                        "--expected-version",
                        "1.2.3",
                        "--image",
                        "ubuntu:24.04",
                    ]
                )
        self.assertEqual(code, 2)

    def test_run_smoke_success_via_fake_docker(self):
        # Drive run_smoke with a stub subprocess so the pure orchestration path
        # (validate -> resolve -> parse -> summarise) is covered without Docker.
        facts = _facts()
        fake_stdout = "\n".join(
            [f"{mod._STEP_MARKER}{s}:ok" for s in _all_steps()]
            + [mod._FACTS_MARKER + json.dumps(facts)]
        )

        class _Completed:
            returncode = 0
            stdout = fake_stdout
            stderr = ""

        real_run = mod.subprocess.run

        def fake_run(cmd, *args, **kwargs):
            # Only intercept the container run; let image-inspect fall through
            # to a harmless failure that yields no digest.
            if "run" in cmd:
                return _Completed()
            raise OSError("no docker")

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "mozyo_bridge-1.2.3-py3-none-any.whl").write_bytes(b"wheelbytes")
            args = mod._parse_args(
                [
                    "--artifact-dir",
                    str(root),
                    "--expected-version",
                    "1.2.3",
                    "--image",
                    _DIGEST,
                ]
            )
            mod.subprocess.run = fake_run
            try:
                buf = io.StringIO()
                with contextlib.redirect_stderr(buf):
                    summary = mod.run_smoke(args)
            finally:
                mod.subprocess.run = real_run

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["artifact"]["expected_version"], "1.2.3")
        self.assertEqual(summary["surfaces"]["missing"], [])


if __name__ == "__main__":
    unittest.main()
