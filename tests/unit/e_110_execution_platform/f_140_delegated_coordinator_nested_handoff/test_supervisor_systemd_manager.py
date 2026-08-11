"""Typed systemd manager-effective definition attestation (Redmine #15192 r17)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    supervisor_systemd_manager as manager,
    supervisor_systemd_unit as systemd_unit,
)


def _result(returncode=0, stdout=""):
    return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": "redacted"})()


class TypedManagerRunner:
    """Documented busctl JSON shapes with per-property drift/malformed seams."""

    def __init__(self, *, service_path, timer_path, argv, interval=180, overrides=None):
        self.calls = []
        self.service_path = str(service_path)
        self.timer_path = str(timer_path)
        self.argv = list(argv)
        self.interval = interval
        self.overrides = overrides or {}

    @staticmethod
    def _reply(signature, data):
        return _result(stdout=json.dumps({"type": signature, "data": data}))

    def __call__(self, raw):
        argv = list(raw)
        self.calls.append(argv)
        operation = argv[3]
        if operation == "call":
            unit = argv[-1].replace("-", "_2d").replace(".", "_2e")
            return self._reply("o", [f"/org/freedesktop/systemd1/unit/{unit}"])
        prop = argv[-1]
        object_kind = "timer" if "timer" in argv[5] else "service"
        key = (object_kind, prop)
        override = self.overrides.get(key, self.overrides.get(prop))
        if override == "malformed":
            return _result(stdout="not-json")
        if override == "missing":
            return _result(returncode=1)
        if override is not None:
            return self._reply(*override)
        empty_exec = (manager._EXEC_SIGNATURE, [])
        properties = {
            "Version": ("s", "test-systemd"),
            "FragmentPath": (
                "s", self.timer_path if object_kind == "timer" else self.service_path
            ),
            "DropInPaths": ("as", []),
            "NeedDaemonReload": ("b", False),
            "Type": ("s", "oneshot"),
            "RemainAfterExit": ("b", False),
            "Restart": ("s", "no"),
            "Environment": ("as", []),
            "EnvironmentFiles": ("a(sb)", []),
            "PassEnvironment": ("as", []),
            "UnsetEnvironment": ("as", list(systemd_unit.MASKED_MANAGER_ENVIRONMENT)),
            "ExecStart": (
                manager._EXEC_SIGNATURE,
                [[self.argv[0], self.argv, False, *([0] * 7)]],
            ),
            "ExecCondition": empty_exec,
            "ExecStartPre": empty_exec,
            "ExecStartPost": empty_exec,
            "ExecReload": empty_exec,
            "ExecReloadPost": empty_exec,
            "ExecStop": empty_exec,
            "ExecStopPost": empty_exec,
            "Unit": ("s", "mozyo-bridge-callback-supervisor.service"),
            "TimersMonotonic": (
                "a(stt)",
                [["OnActiveUSec", 0, 0],
                 ["OnUnitActiveUSec", self.interval * 1_000_000, 0]],
            ),
            "TimersCalendar": ("a(sst)", []),
            "Persistent": ("b", False),
            "RandomizedDelayUSec": ("t", 0),
            "OnClockChange": ("b", False),
            "OnTimezoneChange": ("b", False),
        }
        return self._reply(*properties[prop])


class ManagerDefinitionAttestationTest(unittest.TestCase):
    def setUp(self):
        self.service_path = Path(
            "/workspace/operator-fixture/.config/systemd/user/supervisor.service"
        )
        self.timer_path = Path(
            "/workspace/operator-fixture/.config/systemd/user/supervisor.timer"
        )
        self.argv = [
            "/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once",
            "--home", "/workspace/operator-fixture/.mozyo-bridge",
        ]

    def _attest(self, overrides=None):
        runner = TypedManagerRunner(
            service_path=self.service_path, timer_path=self.timer_path,
            argv=self.argv, overrides=overrides,
        )
        inspector = manager.SystemdManagerInspector(runner)
        result = inspector.attest(
            service_unit="mozyo-bridge-callback-supervisor.service",
            timer_unit="mozyo-bridge-callback-supervisor.timer",
            service_path=self.service_path,
            timer_path=self.timer_path,
            expected_argv=self.argv,
            interval_seconds=180,
        )
        return result, runner

    def test_exact_manager_effective_definition_matches(self):
        result, runner = self._attest()
        self.assertEqual(manager.MANAGER_DEFINITION_MATCHED, result.state)
        self.assertTrue(result.matched)
        self.assertTrue(runner.calls)
        for call in runner.calls:
            self.assertEqual(["busctl", "--user", "--json=short"], call[:3])

    def test_any_effective_path_argv_home_hook_or_timer_drift_refuses(self):
        drifted_exec = list(self.argv)
        drifted_exec[-1] = "/home/other"
        cases = {
            "fragment": {("service", "FragmentPath"): ("s", "/tmp/other.service")},
            "dropin": {("service", "DropInPaths"): ("as", ["/tmp/override.conf"])},
            "home": {"ExecStart": (
                manager._EXEC_SIGNATURE,
                [[drifted_exec[0], drifted_exec, False, *([0] * 7)]],
            )},
            "stop_hook": {"ExecStop": (
                manager._EXEC_SIGNATURE,
                [["/tmp/hook", ["/tmp/hook"], False, *([0] * 7)]],
            )},
            "environment": {"Environment": ("as", ["FOO=bar"])},
            "environment_file": {"EnvironmentFiles": ("a(sb)", [["/tmp/env", False]])},
            "pass_environment": {"PassEnvironment": ("as", ["FOO"])},
            "unset_environment": {"UnsetEnvironment": ("as", ["BAR"])},
            "timer": {"TimersMonotonic": (
                "a(stt)", [["OnActiveUSec", 0, 0], ["OnUnitActiveUSec", 60_000_000, 0]],
            )},
        }
        for name, overrides in cases.items():
            with self.subTest(name=name):
                result, _runner = self._attest(overrides)
                self.assertEqual(manager.MANAGER_DEFINITION_DRIFT, result.state)
                self.assertFalse(result.matched)

    def test_missing_malformed_or_wrong_typed_property_is_unreadable(self):
        cases = {
            "missing": {"ExecStart": "missing"},
            "malformed": {"ExecStart": "malformed"},
            "wrong_type": {"ExecStart": ("s", "not-an-exec-array")},
        }
        for name, overrides in cases.items():
            with self.subTest(name=name):
                result, _runner = self._attest(overrides)
                self.assertEqual(manager.MANAGER_DEFINITION_UNREADABLE, result.state)
                self.assertFalse(result.matched)

    def test_capability_is_typed_and_never_inferred_from_command_success(self):
        exact = TypedManagerRunner(
            service_path=self.service_path, timer_path=self.timer_path, argv=self.argv
        )
        self.assertTrue(manager.SystemdManagerInspector(exact).capability_available())
        wrong = TypedManagerRunner(
            service_path=self.service_path, timer_path=self.timer_path, argv=self.argv,
            overrides={"Version": ("as", ["test-systemd"])},
        )
        self.assertFalse(manager.SystemdManagerInspector(wrong).capability_available())


if __name__ == "__main__":
    unittest.main()
