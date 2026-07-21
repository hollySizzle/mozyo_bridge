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
block AND each curated operator-relevant leaf path (:data:`CONFIG_LEAF_KEYS`, Redmine
#14222 review j#85125 F3) as :data:`SOURCE_DECLARED` (the raw record carries this key,
even if the declared value happens to equal the default — declaring intent counts),
:data:`SOURCE_DEFAULT` (the key is silently absent), or :data:`SOURCE_COMPATIBILITY`
(the effective value is produced by the pre-#14148 legacy translation — the v1
``agent_launch`` / ``provider_binding`` declarations and the ``agents`` topology they
derive). Rows carry a machine-readable ``action`` token (:data:`ACTIONS`) alongside the
human ``note``, so a consumer can branch on the actionable drift (``config migrate`` /
pending schema integration) without parsing prose. Leaf rows exist precisely so a
PARTIALLY declared block can never bury an undeclared nested default under the block's
own ``declared``.

Value-safety: every effective value serialized here comes off :class:`RepoLocalConfig`,
whose closed schema already rejects credential- / secret-shaped fields at load time
(``repo_local_config.py``'s module docstring). This module adds no new field and performs
no new read, so it carries the exact same guarantee.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Mapping, Optional

#: The key was found in the parsed record — the operator declared it, whether or not
#: the declared value happens to equal the behavior-preserving default.
SOURCE_DECLARED = "declared"
#: The key is absent from the parsed record; the effective value is the silent,
#: behavior-preserving default (Redmine #14222's subject).
SOURCE_DEFAULT = "default"
#: The effective value is produced by a LEGACY-compatibility translation (Redmine
#: #14222 review j#85125 F3): the operator declared the pre-#14148 v1 shape
#: (``agent_launch`` / ``provider_binding``) and the loader derives today's effective
#: topology from it. Machine-readably distinct from both ``declared`` (the current
#: canonical shape) and ``default`` (nothing declared at all) — the row's ``action``
#: says what resolves it.
SOURCE_COMPATIBILITY = "compatibility"

SOURCES: frozenset[str] = frozenset(
    {SOURCE_DECLARED, SOURCE_DEFAULT, SOURCE_COMPATIBILITY}
)

#: Machine-readable actionable-drift tokens (j#85125 F3 — the human ``note`` stays, but
#: an operator surface must be able to branch without parsing prose).
ACTION_CONFIG_MIGRATE = "config_migrate"
ACTION_SCHEMA_INTEGRATION_PENDING = "schema_integration_pending"
ACTIONS: frozenset[str] = frozenset(
    {ACTION_CONFIG_MIGRATE, ACTION_SCHEMA_INTEGRATION_PENDING}
)

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

#: Operator-relevant LEAF key paths (Redmine #14222 review j#85125 F3): the nested
#: settings the parent issue's close condition 1 / #14223's scope enumerate, classified
#: at stable dotted-path granularity so a PARTIAL block declaration (e.g. a
#: ``presentation`` block that declares grouping rules but not
#: ``delegation_window_policy``) can never bury an undeclared leaf under a block-level
#: ``declared``. Each entry maps the TYPED effective path (attribute walk on
#: :class:`RepoLocalConfig`) to the RAW record paths that count as declaring it — more
#: than one raw path where the loader accepts an alias (the ``presentation`` grouping
#: sub-keys are declared flat under ``presentation`` per
#: ``repo_local_config.PRESENTATION_GROUPING_SUBKEYS``, and tolerated nested under
#: ``presentation.grouping``). This list is deliberately curated, not reflective: it
#: names exactly the operator-decision surface the issues enumerate, so adding a leaf
#: is a reviewable act.
CONFIG_LEAF_KEYS: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...] = (
    ("work_unit.granularity", (("work_unit", "granularity"),)),
    (
        "sublane_integration.integration_branch",
        (("sublane_integration", "integration_branch"),),
    ),
    (
        "sublane_integration.merge_on_retire",
        (("sublane_integration", "merge_on_retire"),),
    ),
    (
        "sublane_integration.manage_worktree",
        (("sublane_integration", "manage_worktree"),),
    ),
    ("terminal_transport.backend", (("terminal_transport", "backend"),)),
    ("presentation.surface", (("presentation", "surface"),)),
    (
        "presentation.grouping.project_group_presentation",
        (
            ("presentation", "project_group_presentation"),
            ("presentation", "grouping", "project_group_presentation"),
        ),
    ),
    (
        "presentation.grouping.delegation_window_policy",
        (
            ("presentation", "delegation_window_policy"),
            ("presentation", "grouping", "delegation_window_policy"),
        ),
    ),
)


@dataclasses.dataclass(frozen=True)
class ConfigKeyStatus:
    """One key's effective value + source classification (pure, JSON-serializable).

    ``key`` is a top-level block name or a dotted leaf path (:data:`CONFIG_LEAF_KEYS`).
    ``action`` is a machine-readable actionable-drift token from :data:`ACTIONS` (or
    empty — nothing actionable); ``note`` is its human explanation.
    """

    key: str
    source: str
    effective_value: Any
    note: str = ""
    action: str = ""

    def as_payload(self) -> dict:
        return {
            "key": self.key,
            "source": self.source,
            "effective_value": _json_safe(self.effective_value),
            "note": self.note,
            "action": self.action,
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
    legacy_declared = schema_version == 1 and legacy_migratable
    statuses: list[ConfigKeyStatus] = []
    for key in CONFIG_BLOCK_KEYS:
        value = getattr(config, key)
        effective = dataclasses.asdict(value) if dataclasses.is_dataclass(value) else value
        source = SOURCE_DECLARED if key in present else SOURCE_DEFAULT
        note = _UNINTEGRATED_SCHEMA_NOTES.get(key, "")
        action = ACTION_SCHEMA_INTEGRATION_PENDING if note else ""
        if legacy_declared and key in ("agent_launch", "provider_binding") and key in present:
            # j#85125 F3: the operator DID write this — in the legacy shape the loader
            # translates. A distinct source token (not a prose-only note) so a machine
            # consumer can tell "compatibility-derived" from both silence and canonical.
            source = SOURCE_COMPATIBILITY
            action = ACTION_CONFIG_MIGRATE
            note = (
                "declared in the legacy v1 shape; run `config migrate` to express as "
                "the role-canonical v2 `agents` topology"
            )
        elif legacy_declared and key == "agents" and key not in present:
            # The effective agents topology exists only via the legacy translation:
            # neither a canonical declaration nor a built-in default.
            source = SOURCE_COMPATIBILITY
            action = ACTION_CONFIG_MIGRATE
            note = (
                "effective topology is derived from the legacy v1 `agent_launch` / "
                "`provider_binding` blocks; run `config migrate` to declare it as the "
                "role-canonical v2 `agents` topology"
            )
        statuses.append(
            ConfigKeyStatus(
                key=key, source=source, effective_value=effective, note=note, action=action
            )
        )
    statuses.extend(_classify_leaves(raw_record=raw_record, config=config))
    return tuple(statuses)


def _raw_path_present(raw_record: Optional[Mapping[str, object]], path: tuple[str, ...]) -> bool:
    """Whether the operator's parsed record declares this exact nested path (pure)."""
    node: object = raw_record
    for segment in path:
        if not isinstance(node, Mapping) or segment not in node:
            return False
        node = node[segment]
    return True


def _effective_leaf(config: Any, dotted: str) -> Any:
    """Walk the TYPED config by attribute path for one leaf's effective value (pure)."""
    node: Any = config
    for segment in dotted.split("."):
        node = getattr(node, segment)
    return node


def _classify_leaves(
    *, raw_record: Optional[Mapping[str, object]], config: Any
) -> list[ConfigKeyStatus]:
    """Per-leaf declared/default rows for :data:`CONFIG_LEAF_KEYS` (j#85125 F3).

    A leaf is ``declared`` iff the operator's record carries ANY of its accepted raw
    paths — so a partially-declared block never buries an undeclared nested default
    under the block's own ``declared``. The effective value always comes off the typed
    config (the same closed, credential-rejecting schema every block row uses).
    """
    rows: list[ConfigKeyStatus] = []
    for dotted, raw_paths in CONFIG_LEAF_KEYS:
        declared = any(_raw_path_present(raw_record, path) for path in raw_paths)
        rows.append(
            ConfigKeyStatus(
                key=dotted,
                source=SOURCE_DECLARED if declared else SOURCE_DEFAULT,
                effective_value=_effective_leaf(config, dotted),
            )
        )
    return rows


__all__ = (
    "ACTION_CONFIG_MIGRATE",
    "ACTION_SCHEMA_INTEGRATION_PENDING",
    "ACTIONS",
    "CONFIG_BLOCK_KEYS",
    "CONFIG_LEAF_KEYS",
    "SOURCE_COMPATIBILITY",
    "SOURCE_DECLARED",
    "SOURCE_DEFAULT",
    "SOURCES",
    "ConfigKeyStatus",
    "classify_config_sources",
)
