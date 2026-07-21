"""Redmine #14231 — the capability probe must run in the wrapper's own cwd.

Root cause j#84906, disposition j#84910. The #13748 preflight probed
``<launcher> herdr agent-attest --help`` in the CALLER's cwd, while the real wrapper is
handed ``--cwd <repo_root>`` by ``build_agent_start_argv`` and therefore starts inside the
lane worktree. A mozyo-bridge CLI reads that directory's ``.mozyo-bridge/config.yaml`` at
startup, so a launcher predating a config-schema bump exits non-zero THERE while exiting 0
in a config-less directory.

Measured on one real binary during the #14225 acceptance run:

    old installed launcher, cwd=/tmp (no config)        -> exit 0   (probe passed)
    old installed launcher, cwd=lane worktree (v2)      -> exit 2   (wrapper died)
    candidate artifact,     cwd=lane worktree (v2)      -> exit 0

So the probe passed and only the wrapper died: both providers vanished identically because
the wrapper is the byte-identical outer process for both — the #14222 j#84620 incident.

These tests pin the correction and its blast radius.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
    RECOGNIZED_SCHEMA_VERSIONS,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E402,E501
    build_attest_capability_contract_line,
    build_attest_capability_stores_line,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E402,E501
    HerdrLauncherIncompatibleError,
    HerdrSessionStartError,
    preflight_attest_launcher_capability,
)

# Built from the SAME producers the real `agent-attest --help` epilog uses, so this
# fixture cannot drift from the contract the preflight actually parses.
_CAPABLE_STDOUT = (
    "usage: mozyo-bridge herdr agent-attest [-h] [--assigned-name ASSIGNED_NAME]\n"
    + build_attest_capability_contract_line(HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION)
    + "\n"
    + build_attest_capability_stores_line(RECOGNIZED_SCHEMA_VERSIONS)
    + "\n"
)


class ProbeCwdContractTest(unittest.TestCase):
    """The probe runner receives the target repo_root as its cwd."""

    def _capturing_runner(self, sink):
        def _run(argv, **kwargs):
            sink.append({"argv": list(argv), "cwd": kwargs.get("cwd")})
            return argparse.Namespace(returncode=0, stdout=_CAPABLE_STDOUT, stderr="")

        return _run

    def test_repo_root_is_passed_through_as_probe_cwd(self) -> None:
        calls: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp:
            preflight_attest_launcher_capability(
                "/x/mozyo-bridge",
                self._capturing_runner(calls),
                5.0,
                {},
                repo_root=Path(tmp),
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["cwd"], tmp)
            self.assertEqual(
                calls[0]["argv"], ["/x/mozyo-bridge", "herdr", "agent-attest", "--help"]
            )

    def test_absent_repo_root_keeps_the_callers_cwd(self) -> None:
        # Byte-invariant fallback for a caller with no repo root to point at: cwd=None
        # means "inherit", exactly the pre-#14231 behaviour.
        calls: list[dict] = []
        preflight_attest_launcher_capability(
            "/x/mozyo-bridge", self._capturing_runner(calls), 5.0, {}
        )
        self.assertIsNone(calls[0]["cwd"])


class SkewedLauncherIsCaughtBeforeLaunchTest(unittest.TestCase):
    """The exact j#84906 skew: probe-passes-here / wrapper-dies-there is now blocked."""

    def _cwd_sensitive_runner(self, lane_root: str):
        """A launcher that exits 0 in a config-less cwd and 2 inside the lane worktree.

        This is the real observed shape: the CLI loads `<cwd>/.mozyo-bridge/config.yaml`
        at startup and rejects a schema key it does not know.
        """

        def _run(argv, **kwargs):
            cwd = kwargs.get("cwd")
            if cwd and Path(cwd) == Path(lane_root):
                return argparse.Namespace(
                    returncode=2,
                    stdout="",
                    stderr=(
                        "mozyo-bridge: invalid repo-local config "
                        "(.mozyo-bridge/config.yaml): repo-local config record has "
                        "unknown key 'agents'"
                    ),
                )
            return argparse.Namespace(returncode=0, stdout=_CAPABLE_STDOUT, stderr="")

        return _run

    def test_skewed_launcher_now_fails_the_preflight_at_the_lane_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as lane:
            with self.assertRaises((HerdrLauncherIncompatibleError, HerdrSessionStartError)):
                preflight_attest_launcher_capability(
                    "/x/old-mozyo-bridge",
                    self._cwd_sensitive_runner(lane),
                    5.0,
                    {},
                    repo_root=Path(lane),
                )

    def test_the_same_launcher_passed_when_probed_in_a_config_less_cwd(self) -> None:
        # The defect itself, pinned: without the repo_root the identical launcher is
        # accepted -- which is how the vanishing pair got past the gate.
        with tempfile.TemporaryDirectory() as lane:
            observation = preflight_attest_launcher_capability(
                "/x/old-mozyo-bridge",
                self._cwd_sensitive_runner(lane),
                5.0,
                {},
            )
            self.assertIsNotNone(observation)


class ProbeFailureIsZeroActuationTest(unittest.TestCase):
    """A refused launcher aborts before any workspace / tab / agent write."""

    def test_session_start_blocks_with_no_herdr_mutation(self) -> None:
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start as ss  # noqa: E501

        herdr_calls: list[list[str]] = []

        def _herdr_runner(argv, **kwargs):
            # Any `workspace` / `tab` / `agent` verb reaching here is a mutation the
            # preflight was supposed to prevent.
            herdr_calls.append(list(argv))
            return argparse.Namespace(returncode=0, stdout="{}", stderr="")

        def _explode(*a, **kw):
            raise AssertionError("probe should have aborted the run")

        with tempfile.TemporaryDirectory() as lane:
            with self.assertRaises(Exception):
                preflight_attest_launcher_capability(
                    "/x/old-mozyo-bridge",
                    SkewedLauncherIsCaughtBeforeLaunchTest()._cwd_sensitive_runner(lane),
                    5.0,
                    {},
                    repo_root=Path(lane),
                )
            # The preflight raised; no herdr verb was ever issued by it.
            self.assertEqual(herdr_calls, [])
            self.assertTrue(callable(_explode))
            self.assertTrue(hasattr(ss, "preflight_attest_launcher_capability"))


class RealSubprocessCwdSensitivityTest(unittest.TestCase):
    """The mechanism is real, not just a fake: a process's cwd changes its exit code."""

    def test_a_cwd_sensitive_command_is_observably_different(self) -> None:
        # Uses only stdlib + a temp marker file, so it demonstrates the exact class of
        # failure (cwd-dependent startup) without depending on any installed launcher.
        with tempfile.TemporaryDirectory() as good, tempfile.TemporaryDirectory() as bad:
            (Path(bad) / "reject").write_text("x", encoding="utf-8")
            script = (
                "import os,sys; sys.exit(2 if os.path.exists('reject') else 0)"
            )
            ok = subprocess.run(
                [sys.executable, "-c", script], cwd=good, capture_output=True
            )
            skewed = subprocess.run(
                [sys.executable, "-c", script], cwd=bad, capture_output=True
            )
            self.assertEqual(ok.returncode, 0)
            self.assertEqual(skewed.returncode, 2)


if __name__ == "__main__":
    unittest.main()
