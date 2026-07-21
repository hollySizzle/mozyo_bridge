"""Pure per-key effective-value / source classification for `config status` (Redmine #14223).

`.mozyo-bridge/config.yaml` composes several behavior-preserving-by-default blocks
(:data:`CONFIG_BLOCK_KEYS`, mirroring :data:`repo_local_config.REPO_LOCAL_CONFIG_KEYS` minus
the meta ``version`` key). Before this module, the only public surface (`config status`,
Redmine #14148 review j#84516) reported the schema version and a v1-deprecation warning —
never *which* blocks the operator actually declared versus which are silently running on
their default. That silence is #14222's whole subject: operator intent and runtime
resolution read identically from every existing surface.

This module is pure (no IO): it takes the already-parsed raw YAML mapping (``None`` for a
missing / empty file — the loader's own behavior-preserving-default input) and the already-
loaded typed :class:`~.repo_local_config.RepoLocalConfig`, and classifies each top-level
block as :data:`SOURCE_DECLARED` (the raw record carries this key, even if the declared
value happens to equal the default — declaring intent counts) or :data:`SOURCE_DEFAULT` (the
key is silently absent). A v1 config whose ``agent_launch`` / ``provider_binding`` blocks
carry migratable legacy content additionally gets a ``note`` pointing at ``config migrate`` —
the effective VALUE is still legitimately ``declared`` (the operator did write something),
the note only flags that it is expressed in the pre-#14148 legacy shape rather than the
role-canonical ``agents`` topology.

Value-safety: every effective value serialized here comes off :class:`RepoLocalConfig`,
whose closed schema already rejects credential- / secret-shaped fields at load time
(``repo_local_config.py``'s module docstring). This module adds no new field and performs
no new read, so it carries the exact same guarantee.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Mapping, Optional

#: The block was found as a key in the parsed record — the operator declared it, whether
#: or not the declared value happens to equal the behavior-preserving default.
SOURCE_DECLARED = "declared"
#: The block is absent from the parsed record; the effective value is the silent,
#: behavior-preserving default (Redmine #14222's subject).
SOURCE_DEFAULT = "default"

SOURCES: frozenset[str] = frozenset({SOURCE_DECLARED, SOURCE_DEFAULT})

#: The top-level configurable blocks this surface classifies — every
#: :data:`repo_local_config.REPO_LOCAL_CONFIG_KEYS` member except the meta ``version`` key,
#: which is a schema marker, not an operator-facing setting.
CONFIG_BLOCK_KEYS: tuple[str, ...] = (
    "work_unit",
    "sublane_integration",
    "terminal_transport",
    "presentation",
    "agents",
    "agent_launch",
    "provider_binding",
    "cli",
    "providers",
    "delegation",
    "lane_placement",
)

#: Blocks whose SCHEMA is not yet cross-integrated with a sibling US (#14148 role-canonical
#: launch / #13647 lane placement provider wiring) — per #14223 scope ("#14148/#13647未統合
#: schemaを先取りせず、表現不能な項目は明示的なintentional-default/blockerとしてstatusに出す"),
#: these are surfaced with an explicit note rather than a value this surface would otherwise
#: silently imply is fully expressible today. Not a THIRD source token: their source is still
#: exactly declared/default by the same rule as every other block, only the note differs.
_UNINTEGRATED_SCHEMA_NOTES: dict[str, str] = {
    "lane_placement": (
        "per-provider window/placement schema integration with #13647 is not yet "
        "complete; this block's effective value is behavior-preserving but not a full "
        "declaration surface yet"
    ),
}


@dataclasses.dataclass(frozen=True)
class ConfigKeyStatus:
    """One block's effective value + source classification (pure, JSON-serializable)."""

    key: str
    source: str
    effective_value: Any
    note: str = ""

    def as_payload(self) -> dict:
        return {
            "key": self.key,
            "source": self.source,
            "effective_value": _json_safe(self.effective_value),
            "note": self.note,
        }


def _json_safe(value: Any) -> Any:
    """Recursively coerce a value into JSON-native shapes (pure, never raises).

    ``dataclasses.asdict`` already unwraps nested dataclasses into plain ``dict`` /
    ``list`` / scalar shapes, but the config domain also uses ``frozenset`` (an
    ``__all__``-exported closed vocabulary type used as a field value, e.g.
    ``ProviderSelectionConfig.disabled``) and ``tuple``, neither of which
    ``json.dumps`` accepts natively. Sorted so the JSON output is byte-stable across
    runs (a ``frozenset`` / ``set`` has no defined iteration order).
    """
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (frozenset, set)):
        return sorted(_json_safe(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def classify_config_sources(
    *,
    raw_record: Optional[Mapping[str, object]],
    config: Any,
    schema_version: int,
    legacy_migratable: bool,
) -> tuple[ConfigKeyStatus, ...]:
    """Classify each top-level config block's effective value + source (pure).

    ``raw_record`` is the as-parsed YAML mapping (``None`` for a missing / empty file,
    exactly the loader's own default-record input). ``config`` is the loaded
    :class:`RepoLocalConfig`. ``legacy_migratable`` is
    ``bool(config.deprecation_warnings())`` — whether this v1 config carries content
    `config migrate` would actually change.
    """
    present = set(raw_record) if isinstance(raw_record, Mapping) else set()
    statuses: list[ConfigKeyStatus] = []
    for key in CONFIG_BLOCK_KEYS:
        value = getattr(config, key)
        effective = dataclasses.asdict(value) if dataclasses.is_dataclass(value) else value
        source = SOURCE_DECLARED if key in present else SOURCE_DEFAULT
        note = _UNINTEGRATED_SCHEMA_NOTES.get(key, "")
        if (
            schema_version == 1
            and legacy_migratable
            and key in ("agent_launch", "provider_binding")
            and key in present
        ):
            note = (
                "declared in the legacy v1 shape; run `config migrate` to express as "
                "the role-canonical v2 `agents` topology"
            )
        statuses.append(
            ConfigKeyStatus(key=key, source=source, effective_value=effective, note=note)
        )
    return tuple(statuses)


__all__ = (
    "CONFIG_BLOCK_KEYS",
    "SOURCE_DECLARED",
    "SOURCE_DEFAULT",
    "SOURCES",
    "ConfigKeyStatus",
    "classify_config_sources",
)
