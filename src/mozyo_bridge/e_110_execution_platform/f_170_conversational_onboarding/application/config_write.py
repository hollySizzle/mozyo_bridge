"""Typed write-once ``.mozyo-bridge/config.yaml`` tool (Redmine #13498 / #14148).

The onboarding runner never lets the model author or merge YAML. This tool
writes exactly one typed record — the role-canonical v2 minimal herdr record
``{version: 2, terminal_transport: {backend: herdr}}`` (no ``agents`` block, so
it resolves to the built-in default topology) — with strict write-once semantics:

- **absent** → atomic create of the v2 record;
- **typed-equivalent** → no-op. This covers a re-run *and* an existing minimal
  herdr record written by an earlier onboarding at ``version: 1``: the v1 and v2
  minimal records are behaviorally identical (default topology + herdr, nothing
  to migrate), so a legacy v1 minimal record is treated as **explicitly
  compatible** and left untouched — never silently rewritten to v2 (Redmine
  #14148 review j#84516 finding 1);
- **any other existing config** → fail closed (``existing_config_requires_separate_merge``);
  the tool never overwrites or merges a divergent config. A v1 config carrying
  provider-keyed blocks (``provider_binding`` / ``agent_launch``) is divergent and
  the operator resolves it with ``mozyo-bridge config migrate``.

Equivalence is judged on the parsed structure (not bytes), so YAML formatting
differences do not defeat the no-op. The write is atomic (temp file + fsync +
``os.link``) so a crash can never leave a half-written config.
"""

from __future__ import annotations

import os
import tempfile
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

# The exact typed record this tool owns. Role-canonical v2, herdr backend, nothing else
# (no ``agents`` block -> the built-in default topology). Redmine #14148 item 7.
TARGET_CONFIG_RECORD: dict[str, object] = {
    "version": 2,
    "terminal_transport": {"backend": BACKEND_HERDR},
}

#: Schema versions of the *minimal herdr record* (only terminal_transport) that are
#: behaviorally identical to the v2 target and therefore an explicit-compatible no-op.
#: A v1 minimal record carries nothing to migrate, so re-running onboarding over it must
#: not rewrite it; a v1 config with provider-keyed blocks is divergent (see _is_equivalent).
_EQUIVALENT_MINIMAL_VERSIONS: frozenset[int] = frozenset({1, 2})

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
    """True when ``parsed`` is the minimal herdr record (v1 or v2), an explicit no-op.

    The target is the v2 minimal record, but a legacy v1 minimal record (only
    ``terminal_transport``, no provider-keyed blocks) is behaviorally identical and is
    treated as explicitly compatible so re-running onboarding never silently rewrites it
    (Redmine #14148 finding 1). Lenient about an omitted ``version`` (the loader defaults
    it to 1) and key ordering; anything richer (extra top-level sections such as
    ``provider_binding`` / ``agent_launch`` / ``agents``, a different backend, unexpected
    ``terminal_transport`` keys) is a divergent config, not an equivalent one.
    """
    if not isinstance(parsed, Mapping):
        return False
    if set(parsed) - {"version", "terminal_transport"}:
        return False
    version = parsed.get("version", 1)
    if version not in _EQUIVALENT_MINIMAL_VERSIONS:
        return False
    transport = parsed.get("terminal_transport")
    if not isinstance(transport, Mapping):
        return False
    if set(transport) - {"version", "backend"}:
        return False
    if transport.get("version", 1) != 1:
        return False
    return transport.get("backend") == BACKEND_HERDR


def _reconcile_existing(config_path: Path) -> ConfigWriteResult:
    """A config already exists: no-op if typed-equivalent, else fail closed."""
    try:
        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
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


def write_once_config(root: Path | str) -> ConfigWriteResult:
    """Write-once the typed herdr config under ``root``; idempotent + fail-closed.

    The create is a true exclusive create (temp file + ``os.link`` into place),
    so a writer that lost the race — a divergent config created between the
    existence check and the link — is detected as an existing file (``EEXIST``)
    and reconciled by re-reading it (equivalent = no-op / divergent =
    fail-closed), never overwritten (Redmine #13501 review F6). There is no
    check-then-replace window.
    """
    config_path = Path(root) / _CONFIG_RELPATH

    if config_path.exists():
        return _reconcile_existing(config_path)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(TARGET_CONFIG_RECORD, sort_keys=True, default_flow_style=False)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(config_path.parent), prefix=".config.", suffix=".onboarding.tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, config_path)  # atomic exclusive create; EEXIST if present
        except FileExistsError:
            # Lost the race: another writer created the destination after our
            # existence check. Reconcile against whatever they wrote.
            return _reconcile_existing(config_path)
        return ConfigWriteResult(outcome=CONFIG_WRITE_CREATED, path=str(config_path))
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
