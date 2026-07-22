"""Real endpoint-bound Herdr smoke with harmless provider stubs (#14187).

Beyond convergence, this file carries the operator-invariance proof the incident
(blocker j#85754) lacked: a **stand-in operator server** is started first, its socket
is planted as the ambient ``HERDR_SOCKET_PATH`` the smoke inherits, and after the
smoke completes the stand-in must still be alive and still answering.  A real
operator's Herdr is never involved — the stand-in is itself a disposable instance the
test owns and tears down (design disposition j#85756: assert operator request
count 0, operator process/state unchanged, owned-child-only cleanup).

This test starts real Herdr servers and real (harmless stub) provider processes, so
it is **opt-in**: it stays skipped unless ``MOZYO_SMOKE_LIVE_HERDR=1`` is set.  Live
actuation must be a deliberate, approved act — never a side effect of
``unittest discover`` on a machine that also runs the operator's Herdr.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

LIVE_OPT_IN_ENV = "MOZYO_SMOKE_LIVE_HERDR"

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.disposable_herdr_instance import (  # noqa: E402,E501
    DisposableHerdrInstance,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.disposable_shared_space_smoke import (  # noqa: E402,E501
    run_disposable_shared_space_smoke,
)


@unittest.skipUnless(
    os.environ.get(LIVE_OPT_IN_ENV) == "1",
    f"live Herdr actuation is opt-in; set {LIVE_OPT_IN_ENV}=1 to run it",
)
@unittest.skipUnless(shutil.which("herdr"), "herdr binary is not installed")
class DisposableSharedSpaceLiveIntegrationTests(unittest.TestCase):
    def test_two_processes_converge_and_the_ambient_server_is_untouched(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            base = Path(tmp)
            bindir = base / "bin"
            bindir.mkdir()
            for provider in ("claude", "codex"):
                script = bindir / provider
                script.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
                script.chmod(script.stat().st_mode | stat.S_IEXEC)
            herdr = shutil.which("herdr")
            assert herdr is not None
            path = os.pathsep.join(
                [str(bindir), "/usr/local/bin", "/usr/bin", "/bin"]
            )

            # A server that plays the operator's role for this test: the smoke will
            # inherit ITS socket as the ambient endpoint, and it must survive.
            stand_in = DisposableHerdrInstance(
                binary=herdr,
                root=base / "stand-in-operator",
                base_env={"HOME": str(base / "stand-in-home"), "PATH": path},
                ambient_env={},
            )
            with stand_in:
                self.assertTrue(stand_in.process_alive)

                env = dict(os.environ)
                env.update(
                    {
                        "MOZYO_HERDR_BINARY": herdr,
                        # Disable the attestation wrapper for the harmless provider
                        # stubs; production/built-artifact E2E exercises the real one.
                        "MOZYO_BRIDGE_LAUNCHER": str(base / "absent-launcher"),
                        "PATH": path,
                        # The ambient endpoint the smoke inherits. Every request the
                        # smoke makes must be redirected away from it, and the gate
                        # must refuse (never dispatch) anything that is not.
                        "HERDR_SOCKET_PATH": str(stand_in.binding.socket_path),
                    }
                )
                report = run_disposable_shared_space_smoke(
                    base / "smoke-home", env=env, projects=2, process_timeout=20.0
                )
                self.assertTrue(report["success"], report)
                self.assertTrue(report["cross_process"])
                self.assertEqual(report["coordinators_create_count"], 1)
                self.assertEqual(report["duplicate_agents"], 0)
                self.assertTrue(report["residue_clear"])
                self.assertTrue(report["server_stopped"])
                self.assertEqual(report["endpoint_residue"], 0)

                # Load-bearing negative proof, both directions (blocker j#85754):
                # nothing reached the ambient endpoint, and nothing had to be refused.
                self.assertTrue(report["endpoint_bound"], report)
                self.assertEqual(report["operator_endpoint_requests"], 0, report)
                self.assertEqual(report["endpoint_escape_refusals"], 0, report)
                self.assertFalse(report["operator_server_connected"], report)
                self.assertFalse(report["graceful_stop_refused"], report)

                # Operator process/state invariance: still running, still answering,
                # and its own state tree still present.
                self.assertTrue(stand_in.process_alive, "the ambient server was stopped")
                probe = stand_in.runner(
                    [herdr, "workspace", "list"],
                    capture_output=True,
                    text=True,
                    timeout=10.0,
                )
                self.assertEqual(probe.returncode, 0, probe.stderr)
                self.assertTrue(stand_in.binding.socket_path.exists())

            self.assertTrue(stand_in.stopped)
            self.assertFalse((base / "stand-in-operator").exists())


if __name__ == "__main__":
    unittest.main()
