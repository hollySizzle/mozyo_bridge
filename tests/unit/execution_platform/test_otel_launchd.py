"""launchd residency tests (Redmine #11690 / #11691).

Real launchctl is never invoked: every subprocess call is mocked, so
these tests pin the safety boundary — plist content (no environment
block, loopback default, structured argv), command construction
(bootstrap / bootout / kickstart shapes), idempotent install, exact-file
uninstall — without touching the host's LaunchAgents. Live launchd
operation is an operator verification item recorded in the Redmine
journal.
"""

from __future__ import annotations

import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application import otel_launchd


def ok_result(stdout: str = ""):
    return type(
        "R", (), {"returncode": 0, "stdout": stdout, "stderr": ""}
    )()


class RenderPlistTest(unittest.TestCase):
    def test_plist_is_minimal_and_carries_no_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            raw = otel_launchd.render_plist(
                ["/opt/bin/mozyo-bridge", "otel", "serve"], home=home
            )
        payload = plistlib.loads(raw)
        self.assertEqual(otel_launchd.LAUNCHD_LABEL, payload["Label"])
        self.assertEqual(
            ["/opt/bin/mozyo-bridge", "otel", "serve"],
            payload["ProgramArguments"],
        )
        self.assertTrue(payload["RunAtLoad"])
        self.assertTrue(payload["KeepAlive"])
        # The credential boundary: no environment block exists at all, so
        # no code path can serialize a secret into the plist.
        self.assertNotIn("EnvironmentVariables", payload)
        text = raw.decode("utf-8")
        for token in ("API_KEY", "REDMINE", "TOKEN", "SECRET"):
            self.assertNotIn(token, text)

    def test_plist_with_secret_in_daemon_env_still_excludes_it(self) -> None:
        # Even when the operator's shell carries the key, rendering must
        # not pick it up implicitly.
        with patch.dict(
            "os.environ",
            {"MOZYO_REDMINE_API_KEY": "SECRET-KEY-VALUE"},
            clear=False,
        ), tempfile.TemporaryDirectory() as tmp:
            raw = otel_launchd.render_plist(
                ["/opt/bin/mozyo-bridge", "otel", "serve"], home=Path(tmp)
            )
        self.assertNotIn(b"SECRET-KEY-VALUE", raw)

    def test_no_host_argument_keeps_loopback_default(self) -> None:
        with patch(
            "mozyo_bridge.application.otel_launchd.shutil.which",
            return_value="/opt/bin/mozyo-bridge",
        ):
            command = otel_launchd.resolve_serve_command(port=43999)
        self.assertEqual(
            ["/opt/bin/mozyo-bridge", "otel", "serve", "--port", "43999"],
            command,
        )
        self.assertNotIn("--host", command)

    def test_missing_executable_dies_with_guidance(self) -> None:
        import contextlib
        import io

        with patch(
            "mozyo_bridge.application.otel_launchd.shutil.which",
            return_value=None,
        ), contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                otel_launchd.resolve_serve_command()
        self.assertIn("pipx install", stderr.getvalue())


class LaunchctlCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def test_install_writes_plist_and_bootstraps(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv, capture_output, text, check):
            calls.append(argv)
            return ok_result()

        with patch(
            "mozyo_bridge.application.otel_launchd.shutil.which",
            return_value="/opt/bin/mozyo-bridge",
        ), patch(
            "mozyo_bridge.application.otel_launchd.subprocess.run",
            side_effect=fake_run,
        ), patch(
            "mozyo_bridge.application.otel_launchd.os.getuid",
            return_value=501,
        ):
            result = otel_launchd.install(home=self.home)
        plist_file = otel_launchd.plist_path(self.home)
        self.assertTrue(plist_file.exists())
        payload = plistlib.loads(plist_file.read_bytes())
        self.assertEqual(otel_launchd.LAUNCHD_LABEL, payload["Label"])
        # bootout (ignore-failure) then bootstrap, structured argv only.
        self.assertEqual(
            [
                ["launchctl", "bootout", "gui/501/biz.asile.mozyo-bridge.otel"],
                ["launchctl", "bootstrap", "gui/501", str(plist_file)],
            ],
            calls,
        )
        self.assertEqual("install", result["action"])

    def test_install_is_idempotent(self) -> None:
        with patch(
            "mozyo_bridge.application.otel_launchd.shutil.which",
            return_value="/opt/bin/mozyo-bridge",
        ), patch(
            "mozyo_bridge.application.otel_launchd.subprocess.run",
            return_value=ok_result(),
        ), patch(
            "mozyo_bridge.application.otel_launchd.os.getuid",
            return_value=501,
        ):
            otel_launchd.install(home=self.home)
            first = otel_launchd.plist_path(self.home).read_bytes()
            otel_launchd.install(home=self.home)
            second = otel_launchd.plist_path(self.home).read_bytes()
        self.assertEqual(first, second)

    def test_uninstall_boots_out_and_removes_exactly_our_plist(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv, capture_output, text, check):
            calls.append(argv)
            return ok_result()

        plist_file = otel_launchd.plist_path(self.home)
        plist_file.parent.mkdir(parents=True)
        plist_file.write_bytes(b"placeholder")
        bystander = plist_file.parent / "some.other.agent.plist"
        bystander.write_bytes(b"untouched")
        with patch(
            "mozyo_bridge.application.otel_launchd.subprocess.run",
            side_effect=fake_run,
        ), patch(
            "mozyo_bridge.application.otel_launchd.os.getuid",
            return_value=501,
        ):
            result = otel_launchd.uninstall(home=self.home)
        self.assertFalse(plist_file.exists())
        self.assertTrue(bystander.exists())
        self.assertTrue(result["removed"])
        self.assertEqual(
            [["launchctl", "bootout", "gui/501/biz.asile.mozyo-bridge.otel"]],
            calls,
        )

    def test_restart_kickstarts_with_kill_flag(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv, capture_output, text, check):
            calls.append(argv)
            return ok_result()

        with patch(
            "mozyo_bridge.application.otel_launchd.subprocess.run",
            side_effect=fake_run,
        ), patch(
            "mozyo_bridge.application.otel_launchd.os.getuid",
            return_value=501,
        ):
            otel_launchd.restart()
        self.assertEqual(
            [
                [
                    "launchctl",
                    "kickstart",
                    "-k",
                    "gui/501/biz.asile.mozyo-bridge.otel",
                ]
            ],
            calls,
        )

    def test_status_reports_loaded_state_and_pid(self) -> None:
        def fake_run(argv, capture_output, text, check):
            return ok_result("state = running\n\tpid = 4242\n")

        with patch(
            "mozyo_bridge.application.otel_launchd.subprocess.run",
            side_effect=fake_run,
        ), patch(
            "mozyo_bridge.application.otel_launchd.os.getuid",
            return_value=501,
        ):
            payload = otel_launchd.status(home=self.home)
        self.assertTrue(payload["loaded"])
        self.assertEqual("4242", payload["pid"])
        self.assertFalse(payload["plist_exists"])

    def test_status_when_not_loaded(self) -> None:
        def fake_run(argv, capture_output, text, check):
            return type(
                "R",
                (),
                {"returncode": 113, "stdout": "", "stderr": "not found"},
            )()

        with patch(
            "mozyo_bridge.application.otel_launchd.subprocess.run",
            side_effect=fake_run,
        ), patch(
            "mozyo_bridge.application.otel_launchd.os.getuid",
            return_value=501,
        ):
            payload = otel_launchd.status(home=self.home)
        self.assertFalse(payload["loaded"])
        self.assertIsNone(payload["pid"])


if __name__ == "__main__":
    unittest.main()
