"""Systemd user-manager I/O and typed supervisor-definition attestation.

Status reads parse only requested ``systemctl show`` properties and reject contradictory answers.
Lifecycle effects use structured argv and return a closed phase result.  Unit-file bytes alone are
not the command consumed by ``systemctl restart``, so loaded-definition checks read documented,
typed D-Bus properties through ``busctl --json=short`` and expose only a closed verdict.  Raw
manager output, argv, paths, and stderr never cross the boundary (Redmine #15192
j#103091/j#103093).

The calls are serialized with the shared lifecycle lock by the caller.  They are not an OS-level
transaction or a security boundary against a same-uid process that ignores that advisory lock.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_systemd_unit import (  # noqa: E501
    MASKED_MANAGER_ENVIRONMENT,
    render_service_unit,
    render_timer_unit,
)


MANAGER_DEFINITION_MATCHED = "matched"
MANAGER_DEFINITION_DRIFT = "drift"
MANAGER_DEFINITION_UNREADABLE = "unreadable"

_BUSCTL = "busctl"
_DESTINATION = "org.freedesktop.systemd1"
_MANAGER_PATH = "/org/freedesktop/systemd1"
_MANAGER_INTERFACE = "org.freedesktop.systemd1.Manager"
_UNIT_INTERFACE = "org.freedesktop.systemd1.Unit"
_SERVICE_INTERFACE = "org.freedesktop.systemd1.Service"
_TIMER_INTERFACE = "org.freedesktop.systemd1.Timer"
_MAX_JSON_BYTES = 256 * 1024
_EXEC_SIGNATURE = "a(sasbttttuii)"
_SYSTEMCTL = "systemctl"

UNINSTALL_COMPLETE = "complete"
UNINSTALL_DISABLE_FAILED = "disable_failed"
UNINSTALL_STOP_FAILED = "stop_failed"
UNINSTALL_RELOAD_FAILED = "reload_failed"
UNINSTALL_RESET_FAILED = "reset_failed"

Runner = Callable[[Sequence[str]], object]
RemoveUnits = Callable[[], bool]


@dataclass(frozen=True)
class ManagerDefinitionAttestation:
    """Closed verdict only; manager-provided values are intentionally not retained."""

    state: str

    @property
    def matched(self) -> bool:
        return self.state == MANAGER_DEFINITION_MATCHED


@dataclass(frozen=True)
class UninstallManagerOutcome:
    """The exact manager phase reached around one injected owned-unit removal."""

    phase: str
    removed: Optional[bool]

    @property
    def completed(self) -> bool:
        return self.phase == UNINSTALL_COMPLETE


def disk_definition_matches(
    service_payload: bytes,
    timer_payload: bytes,
    *,
    expected_argv: Sequence[str],
    interval_seconds: int,
) -> bool:
    """Whether both disk definitions exactly match the renderer-owned closed schema."""
    return (
        service_payload == render_service_unit(expected_argv).encode("utf-8")
        and timer_payload
        == render_timer_unit(interval_seconds=interval_seconds).encode("utf-8")
    )


def run_systemctl(runner: Runner, args: Sequence[str]) -> object:
    """Invoke the user manager with structured argv and no shell."""
    return runner([_SYSTEMCTL, "--user", *args])


def user_manager_available(runner: Runner) -> bool:
    """Whether a systemd user manager answers a read-only version probe."""
    try:
        result = run_systemctl(runner, ["show", "--property=Version"])
    except (FileNotFoundError, OSError):
        return False
    return getattr(result, "returncode", 1) == 0


def show_properties(
    runner: Runner, unit_name: str, properties: Sequence[str]
) -> dict[str, str]:
    """Read exact ``systemctl show`` properties; contradictions are unreadable."""
    args = ["show", unit_name, *[f"--property={item}" for item in properties]]
    try:
        result = run_systemctl(runner, args)
    except (FileNotFoundError, OSError):
        return {}
    if getattr(result, "returncode", 1) != 0:
        return {}
    values: dict[str, str] = {}
    for line in (getattr(result, "stdout", "") or "").splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        if key in values and values[key] != value:
            return {}
        values[key] = value
    return values


def run_uninstall_manager_sequence(
    runner: Runner,
    *,
    timer_unit: str,
    service_unit: str,
    remove_units: RemoveUnits,
) -> UninstallManagerOutcome:
    """Run the fail-stopping systemd manager/unlink sequence with closed phase evidence."""
    if getattr(
        run_systemctl(runner, ["disable", "--now", timer_unit]), "returncode", 1
    ) != 0:
        return UninstallManagerOutcome(UNINSTALL_DISABLE_FAILED, False)
    if getattr(run_systemctl(runner, ["stop", service_unit]), "returncode", 1) != 0:
        return UninstallManagerOutcome(UNINSTALL_STOP_FAILED, False)
    removed = remove_units()
    if getattr(run_systemctl(runner, ["daemon-reload"]), "returncode", 1) != 0:
        return UninstallManagerOutcome(UNINSTALL_RELOAD_FAILED, removed)
    if getattr(
        run_systemctl(runner, ["reset-failed", service_unit, timer_unit]),
        "returncode",
        1,
    ) != 0:
        return UninstallManagerOutcome(UNINSTALL_RESET_FAILED, removed)
    return UninstallManagerOutcome(UNINSTALL_COMPLETE, removed)


class SystemdManagerInspector:
    """Read documented manager properties through an injected structured-argv runner."""

    def __init__(self, runner: Runner) -> None:
        self._runner = runner

    def capability_available(self) -> bool:
        """Whether typed JSON property reads are available before any scheduler mutation."""
        value = self._property(_MANAGER_PATH, _MANAGER_INTERFACE, "Version")
        return self._typed(value, "s", str) is not None

    def attest(
        self,
        *,
        service_unit: str,
        timer_unit: str,
        service_path: Path,
        timer_path: Path,
        expected_argv: Sequence[str],
        interval_seconds: int,
    ) -> ManagerDefinitionAttestation:
        """Compare the manager-loaded command, home, path, and timer contract."""
        try:
            service_object = self._unit_object(service_unit)
            timer_object = self._unit_object(timer_unit)
            if service_object is None or timer_object is None:
                return ManagerDefinitionAttestation(MANAGER_DEFINITION_UNREADABLE)
            values = self._definition_values(service_object, timer_object)
            if values is None:
                return ManagerDefinitionAttestation(MANAGER_DEFINITION_UNREADABLE)
            return ManagerDefinitionAttestation(
                MANAGER_DEFINITION_MATCHED
                if self._matches(
                    values,
                    service_unit=service_unit,
                    service_path=service_path,
                    timer_path=timer_path,
                    expected_argv=expected_argv,
                    interval_seconds=interval_seconds,
                )
                else MANAGER_DEFINITION_DRIFT
            )
        except (TypeError, ValueError, OverflowError):
            return ManagerDefinitionAttestation(MANAGER_DEFINITION_UNREADABLE)

    def _definition_values(
        self, service_object: str, timer_object: str
    ) -> Optional[dict[str, object]]:
        requests = {
            "service_fragment": (service_object, _UNIT_INTERFACE, "FragmentPath", "s"),
            "service_dropins": (service_object, _UNIT_INTERFACE, "DropInPaths", "as"),
            "service_reload": (service_object, _UNIT_INTERFACE, "NeedDaemonReload", "b"),
            "service_type": (service_object, _SERVICE_INTERFACE, "Type", "s"),
            "service_remain": (service_object, _SERVICE_INTERFACE, "RemainAfterExit", "b"),
            "service_restart": (service_object, _SERVICE_INTERFACE, "Restart", "s"),
            "service_environment": (service_object, _SERVICE_INTERFACE, "Environment", "as"),
            "service_environment_files": (
                service_object, _SERVICE_INTERFACE, "EnvironmentFiles", "a(sb)"
            ),
            "service_pass_environment": (
                service_object, _SERVICE_INTERFACE, "PassEnvironment", "as"
            ),
            "service_unset_environment": (
                service_object, _SERVICE_INTERFACE, "UnsetEnvironment", "as"
            ),
            "exec_start": (service_object, _SERVICE_INTERFACE, "ExecStart", _EXEC_SIGNATURE),
            "exec_condition": (service_object, _SERVICE_INTERFACE, "ExecCondition", _EXEC_SIGNATURE),
            "exec_start_pre": (service_object, _SERVICE_INTERFACE, "ExecStartPre", _EXEC_SIGNATURE),
            "exec_start_post": (service_object, _SERVICE_INTERFACE, "ExecStartPost", _EXEC_SIGNATURE),
            "exec_reload": (service_object, _SERVICE_INTERFACE, "ExecReload", _EXEC_SIGNATURE),
            "exec_reload_post": (service_object, _SERVICE_INTERFACE, "ExecReloadPost", _EXEC_SIGNATURE),
            "exec_stop": (service_object, _SERVICE_INTERFACE, "ExecStop", _EXEC_SIGNATURE),
            "exec_stop_post": (service_object, _SERVICE_INTERFACE, "ExecStopPost", _EXEC_SIGNATURE),
            "timer_fragment": (timer_object, _UNIT_INTERFACE, "FragmentPath", "s"),
            "timer_dropins": (timer_object, _UNIT_INTERFACE, "DropInPaths", "as"),
            "timer_reload": (timer_object, _UNIT_INTERFACE, "NeedDaemonReload", "b"),
            "timer_unit": (timer_object, _TIMER_INTERFACE, "Unit", "s"),
            "timers_monotonic": (timer_object, _TIMER_INTERFACE, "TimersMonotonic", "a(stt)"),
            "timers_calendar": (timer_object, _TIMER_INTERFACE, "TimersCalendar", "a(sst)"),
            "persistent": (timer_object, _TIMER_INTERFACE, "Persistent", "b"),
            "randomized_delay": (timer_object, _TIMER_INTERFACE, "RandomizedDelayUSec", "t"),
            "on_clock_change": (timer_object, _TIMER_INTERFACE, "OnClockChange", "b"),
            "on_timezone_change": (timer_object, _TIMER_INTERFACE, "OnTimezoneChange", "b"),
        }
        values: dict[str, object] = {}
        for key, (object_path, interface, prop, signature) in requests.items():
            value = self._property(object_path, interface, prop)
            if value is None or value.get("type") != signature:
                return None
            values[key] = value
        return values

    @classmethod
    def _matches(
        cls,
        values: dict[str, object],
        *,
        service_unit: str,
        service_path: Path,
        timer_path: Path,
        expected_argv: Sequence[str],
        interval_seconds: int,
    ) -> bool:
        expected_service_path = os.path.abspath(os.fspath(service_path))
        expected_timer_path = os.path.abspath(os.fspath(timer_path))
        if cls._scalar(values["service_fragment"], "s") != expected_service_path:
            return False
        if cls._scalar(values["timer_fragment"], "s") != expected_timer_path:
            return False
        for key in ("service_dropins", "timer_dropins"):
            if cls._array(values[key], "as") != []:
                return False
        for key in ("service_reload", "timer_reload"):
            if cls._scalar(values[key], "b") is not False:
                return False
        if cls._scalar(values["service_type"], "s") != "oneshot":
            return False
        if cls._scalar(values["service_remain"], "b") is not False:
            return False
        if cls._scalar(values["service_restart"], "s") != "no":
            return False
        for key, signature in (
            ("service_environment", "as"),
            ("service_environment_files", "a(sb)"),
            ("service_pass_environment", "as"),
        ):
            if cls._array(values[key], signature) != []:
                return False
        if cls._array(values["service_unset_environment"], "as") != list(
            MASKED_MANAGER_ENVIRONMENT
        ):
            return False
        if not cls._exec_start_matches(values["exec_start"], expected_argv):
            return False
        for key in (
            "exec_condition",
            "exec_start_pre",
            "exec_start_post",
            "exec_reload",
            "exec_reload_post",
            "exec_stop",
            "exec_stop_post",
        ):
            if cls._array(values[key], _EXEC_SIGNATURE) != []:
                return False
        if cls._scalar(values["timer_unit"], "s") != service_unit:
            return False
        if not cls._timers_match(values["timers_monotonic"], interval_seconds):
            return False
        if cls._array(values["timers_calendar"], "a(sst)") != []:
            return False
        if cls._scalar(values["persistent"], "b") is not False:
            return False
        if cls._scalar(values["randomized_delay"], "t") != 0:
            return False
        if cls._scalar(values["on_clock_change"], "b") is not False:
            return False
        if cls._scalar(values["on_timezone_change"], "b") is not False:
            return False
        return True

    @classmethod
    def _exec_start_matches(cls, value: object, expected_argv: Sequence[str]) -> bool:
        commands = cls._array(value, _EXEC_SIGNATURE)
        if not isinstance(commands, list) or len(commands) != 1:
            return False
        command = commands[0]
        if not isinstance(command, list) or len(command) != 10:
            return False
        binary, argv, ignore_failure = command[:3]
        expected = list(expected_argv)
        return (
            isinstance(binary, str)
            and expected
            and binary == expected[0]
            and isinstance(argv, list)
            and all(isinstance(token, str) for token in argv)
            and argv == expected
            and ignore_failure is False
        )

    @classmethod
    def _timers_match(cls, value: object, interval_seconds: int) -> bool:
        timers = cls._array(value, "a(stt)")
        if not isinstance(timers, list):
            return False
        found: dict[str, int] = {}
        for item in timers:
            if not isinstance(item, list) or len(item) != 3:
                return False
            base, offset, next_elapse = item
            if (
                not isinstance(base, str)
                or not cls._plain_int(offset)
                or not cls._plain_int(next_elapse)
                or base in found
            ):
                return False
            found[base] = offset
        return found == {
            "OnActiveUSec": 0,
            "OnUnitActiveUSec": max(1, int(interval_seconds)) * 1_000_000,
        }

    def _unit_object(self, unit: str) -> Optional[str]:
        payload = self._json(
            [
                _BUSCTL,
                "--user",
                "--json=short",
                "call",
                _DESTINATION,
                _MANAGER_PATH,
                _MANAGER_INTERFACE,
                "GetUnit",
                "s",
                unit,
            ]
        )
        if payload is None or payload.get("type") != "o":
            return None
        data = payload.get("data")
        if (
            not isinstance(data, list)
            or len(data) != 1
            or not isinstance(data[0], str)
            or not data[0].startswith("/org/freedesktop/systemd1/unit/")
        ):
            return None
        return data[0]

    def _property(self, object_path: str, interface: str, prop: str) -> Optional[dict]:
        return self._json(
            [
                _BUSCTL,
                "--user",
                "--json=short",
                "get-property",
                _DESTINATION,
                object_path,
                interface,
                prop,
            ]
        )

    def _json(self, argv: Sequence[str]) -> Optional[dict]:
        try:
            result = self._runner(list(argv))
        except (FileNotFoundError, OSError):
            return None
        if getattr(result, "returncode", 1) != 0:
            return None
        raw = getattr(result, "stdout", "") or ""
        if not isinstance(raw, str) or len(raw.encode("utf-8", errors="ignore")) > _MAX_JSON_BYTES:
            return None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or set(payload) != {"type", "data"}:
            return None
        return payload

    @staticmethod
    def _typed(value: Optional[dict], signature: str, kind: type) -> Optional[object]:
        if value is None or value.get("type") != signature:
            return None
        data = value.get("data")
        if kind is bool:
            return data if isinstance(data, bool) else None
        if kind is int:
            return data if SystemdManagerInspector._plain_int(data) else None
        return data if isinstance(data, kind) else None

    @classmethod
    def _scalar(cls, value: object, signature: str) -> Optional[object]:
        if not isinstance(value, dict):
            return None
        kind = {"s": str, "b": bool, "t": int}.get(signature)
        return None if kind is None else cls._typed(value, signature, kind)

    @staticmethod
    def _array(value: object, signature: str) -> Optional[list]:
        if not isinstance(value, dict) or value.get("type") != signature:
            return None
        data = value.get("data")
        return data if isinstance(data, list) else None

    @staticmethod
    def _plain_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0


__all__ = (
    "MANAGER_DEFINITION_MATCHED",
    "MANAGER_DEFINITION_DRIFT",
    "MANAGER_DEFINITION_UNREADABLE",
    "ManagerDefinitionAttestation",
    "SystemdManagerInspector",
    "UNINSTALL_COMPLETE",
    "UNINSTALL_DISABLE_FAILED",
    "UNINSTALL_STOP_FAILED",
    "UNINSTALL_RELOAD_FAILED",
    "UNINSTALL_RESET_FAILED",
    "UninstallManagerOutcome",
    "disk_definition_matches",
    "run_systemctl",
    "run_uninstall_manager_sequence",
    "show_properties",
    "user_manager_available",
)
