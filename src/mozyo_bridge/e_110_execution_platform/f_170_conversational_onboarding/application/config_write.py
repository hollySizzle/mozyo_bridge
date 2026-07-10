"""Typed write-once ``.mozyo-bridge/config.yaml`` tool (Redmine #13498).

The onboarding runner never lets the model author or merge YAML. This tool
writes exactly one typed record — ``{version: 1, terminal_transport: {backend:
herdr}}`` — with strict write-once semantics:

- **absent** → atomic create;
- **typed-equivalent** → no-op (a re-run, or a hand-written minimal herdr
  config, that already expresses this exact record);
- **any other existing config** → fail closed (``existing_config_requires_separate_merge``);
  the tool never overwrites or merges a divergent config.

Equivalence is judged on the parsed structure (not bytes), so YAML formatting
differences do not defeat the no-op. The write is atomic (temp file + fsync +
``os.replace``) so a crash can never leave a half-written config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
)

__all__ = (
    "CONFIG_WRITE_CREATED",
    "CONFIG_WRITE_NO_OP",
    "ConfigWriteResult",
    "ConfigWriteError",
    "TARGET_CONFIG_RECORD",
    "write_once_config",
)

CONFIG_WRITE_CREATED = "created"
CONFIG_WRITE_NO_OP = "no_op"

# The exact typed record this tool owns. version 1, herdr backend, nothing else.
TARGET_CONFIG_RECORD: dict[str, object] = {
    "version": 1,
    "terminal_transport": {"backend": BACKEND_HERDR},
}

_CONFIG_RELPATH = Path(".mozyo-bridge") / "config.yaml"


class ConfigWriteError(Exception):
    """A coded refusal to write config (e.g. a divergent existing config)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_record(self) -> dict[str, object]:
        return {"error": self.code, "message": self.message}


@dataclass(frozen=True)
class ConfigWriteResult:
    outcome: str  # created | no_op
    path: str


def _is_equivalent(parsed: object) -> bool:
    """True when ``parsed`` already expresses exactly the target typed record.

    Lenient only about an omitted ``version`` (the loader defaults it to 1) and
    key ordering; anything richer (extra top-level sections, a different backend,
    extra ``terminal_transport`` keys other than the recognised ``version``)
    is treated as a divergent config, not an equivalent one.
    """
    if not isinstance(parsed, Mapping):
        return False
    if set(parsed) - {"version", "terminal_transport"}:
        return False
    version = parsed.get("version", 1)
    if version != 1:
        return False
    transport = parsed.get("terminal_transport")
    if not isinstance(transport, Mapping):
        return False
    if set(transport) - {"version", "backend"}:
        return False
    if transport.get("version", 1) != 1:
        return False
    return transport.get("backend") == BACKEND_HERDR


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.onboarding.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def write_once_config(root: Path | str) -> ConfigWriteResult:
    """Write-once the typed herdr config under ``root``; idempotent + fail-closed.

    Raises :class:`ConfigWriteError` (``existing_config_requires_separate_merge``)
    when a divergent config already exists, or (``existing_config_unreadable``)
    when an existing config cannot be parsed.
    """
    config_path = Path(root) / _CONFIG_RELPATH

    if config_path.exists():
        try:
            existing_text = config_path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(existing_text)
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigWriteError(
                "existing_config_unreadable",
                f"existing {config_path} could not be read/parsed: {exc}",
            ) from exc
        if _is_equivalent(parsed):
            return ConfigWriteResult(outcome=CONFIG_WRITE_NO_OP, path=str(config_path))
        raise ConfigWriteError(
            "existing_config_requires_separate_merge",
            f"{config_path} already exists and is not the typed herdr record; "
            "refusing to overwrite — resolve it with a separate merge",
        )

    text = yaml.safe_dump(TARGET_CONFIG_RECORD, sort_keys=True, default_flow_style=False)
    _atomic_write(config_path, text)
    return ConfigWriteResult(outcome=CONFIG_WRITE_CREATED, path=str(config_path))
