"""Trusted process and provider bindings for the offline-rollout runner (#14838)."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (  # noqa: E501
    resolve_agent_launch,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (  # noqa: E501
    AGENT_PROVIDER_PROFILES,
)


RUNNER_ENV = "MOZYO_OFFLINE_ROLLOUT_RUNNER_ACTION_ID"


class OfflineRolloutRunnerBindingError(ValueError):
    """A provider executable binding is absent, foreign, or no longer executable."""


def run_command(argv: Sequence[str], *, timeout: float, env=None, cwd=None):
    return subprocess.run(
        list(argv),
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
        env=env,
        cwd=cwd,
    )


def bounded_result(result: subprocess.CompletedProcess[str]) -> str:
    return ((result.stderr or "") or (result.stdout or "")).strip()[:1000]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sanitized_runtime_env(env: Mapping[str, str]) -> dict[str, str]:
    """Keep source-tree injection from impersonating the installed candidate."""
    clean = dict(env)
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
        clean.pop(name, None)
    return clean


def reports_exact_version(stdout: object, expected: object) -> bool:
    """Accept one exact CLI version token, never a substring such as a4 in a41."""
    if not isinstance(stdout, str) or not isinstance(expected, str) or not expected:
        return False
    return stdout.splitlines() == [f"mozyo-bridge {expected}"]


def _providers(agents: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    providers = tuple(sorted({str(row.get("provider") or "") for row in agents}))
    if not providers or any(not provider for provider in providers):
        raise OfflineRolloutRunnerBindingError("provider_set_invalid")
    return providers


def capture_provider_launch_bindings(
    *, agents: Sequence[Mapping[str, object]], env: Mapping[str, str]
) -> dict[str, dict[str, str]]:
    """Resolve each provider before stop and seal both alias and execution target."""
    captured: dict[str, dict[str, str]] = {}
    for provider in _providers(agents):
        profile = AGENT_PROVIDER_PROFILES.require(provider)
        resolved = resolve_agent_launch(provider, env)
        key = profile.executable.env_override
        if key in captured:
            raise OfflineRolloutRunnerBindingError("provider_env_override_duplicate")
        captured[key] = {
            "provider": provider,
            "argv0": resolved.argv0,
            "exec_target": resolved.exec_target,
        }
    return dict(sorted(captured.items()))


def validate_provider_launch_bindings(
    *, agents: Sequence[Mapping[str, object]], bindings: object
) -> dict[str, str]:
    """Re-resolve sealed aliases without laundering a retargeted symlink or new PATH."""
    if not isinstance(bindings, Mapping):
        raise OfflineRolloutRunnerBindingError("provider_environment_missing")
    providers = _providers(agents)
    expected = {
        AGENT_PROVIDER_PROFILES.require(provider).executable.env_override: provider
        for provider in providers
    }
    if set(bindings) != set(expected):
        raise OfflineRolloutRunnerBindingError("provider_environment_set_mismatch")
    validated: dict[str, str] = {}
    for key, provider in sorted(expected.items()):
        record = bindings.get(key)
        if not isinstance(record, Mapping) or set(record) != {
            "provider",
            "argv0",
            "exec_target",
        }:
            raise OfflineRolloutRunnerBindingError("provider_binding_shape_invalid")
        argv0 = record.get("argv0")
        exec_target = record.get("exec_target")
        if (
            record.get("provider") != provider
            or not isinstance(argv0, str)
            or not isinstance(exec_target, str)
            or not os.path.isabs(argv0)
            or not os.path.isabs(exec_target)
        ):
            raise OfflineRolloutRunnerBindingError("provider_environment_path_invalid")
        resolved = resolve_agent_launch(provider, {key: argv0})
        if resolved.argv0 != argv0 or resolved.exec_target != exec_target:
            raise OfflineRolloutRunnerBindingError("provider_executable_drift")
        validated[key] = argv0
    return validated


__all__ = (
    "RUNNER_ENV",
    "OfflineRolloutRunnerBindingError",
    "bounded_result",
    "capture_provider_launch_bindings",
    "file_sha256",
    "reports_exact_version",
    "run_command",
    "sanitized_runtime_env",
    "validate_provider_launch_bindings",
)
