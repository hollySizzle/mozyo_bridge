"""``layout preset apply`` use case: typed input/result + config-document port (Redmine #15708).

The application-service layer between the ``layout preset apply`` CLI handler and the
pure preset transform (:mod:`..domain.layout_preset`), introduced for the j#108183
``finding_oopboundary`` fix: the OOP-first policy
(``vibes/docs/logics/object-oriented-architecture-policy.md``) forbids a command handler
from owning filesystem orchestration, so the whole load → transform → validate →
serialize → atomic-replace flow lives here as :class:`LayoutPresetApplyService`, behind
the :class:`ConfigDocumentPort` protocol. The handler keeps exactly its allowed
responsibilities — argument reading, output-format selection, exit-code mapping — and
unit tests express the flow against a fake port instead of monkeypatching IO.

Boundary:

- **The service decides, the port touches disk.** :class:`LayoutPresetApplyService`
  never imports an adapter; it sees only the protocol. The YAML adapter
  (:class:`YamlConfigDocumentAdapter`) reuses the ``config migrate`` helpers so the
  read / serialize / atomic-replace discipline stays single-sourced.
- **Typed outcome, closed vocabulary.** :class:`LayoutPresetApplyResult.status` is one
  of :data:`LAYOUT_PRESET_APPLY_STATUSES`; error shapes are results, not exceptions, so
  the handler maps them to exit codes without re-deriving semantics.
- **Display-only invariant unchanged** (#15708 acceptance 2): the produced document
  differs from the loaded one in exactly the ``lane_placement`` key, and the whole
  record is re-validated through :meth:`RepoLocalConfig.from_record` before any write.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.layout_preset import (  # noqa: E501
    LayoutPresetError,
    apply_preset_to_lane_placement,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501
    RepoLocalConfig,
    RepoLocalConfigError,
)

#: The closed status vocabulary of :class:`LayoutPresetApplyResult`. Error statuses map
#: to exit code 1 in the CLI; the four success statuses map to exit code 0.
LAYOUT_PRESET_APPLY_STATUSES: frozenset[str] = frozenset(
    {
        # success
        "already_matching",  # the record already declares this geometry; nothing to do
        "previewed",         # dry-run: the would-be document was produced, nothing written
        "written",           # the declaration was atomically written
        # failure (typed, no exception crosses the service boundary)
        "config_unreadable",  # the existing document could not be read / parsed
        "not_a_mapping",      # the existing document is not a YAML mapping
        "invalid_input",      # preset / ratio / lane_placement shape rejected (fail-closed)
        "invalid_config",     # the produced record failed the full-config re-validation
        "write_failed",       # the atomic replace itself failed (nothing landed)
    }
)


class ConfigDocumentPort(Protocol):
    """The config-document boundary the apply use case depends on (no direct IO).

    ``load_raw`` returns the parsed raw mapping (``None`` for a missing / empty
    document) or raises :class:`RepoLocalConfigError`; ``render`` serializes a record
    to the document text; ``replace_atomic`` writes text atomically with a backup and
    returns the backup path when one was taken (``None`` otherwise), raising
    :class:`OSError` on failure. ``path`` names the document for display only.
    """

    @property
    def path(self) -> Path: ...

    def load_raw(self) -> object: ...

    def render(self, record: "dict[str, object]") -> str: ...

    def replace_atomic(self, text: str) -> "Optional[Path]": ...


@dataclass(frozen=True)
class LayoutPresetApplyInput:
    """The typed command input (finding_oopboundary: no ``argparse.Namespace`` deeper)."""

    preset_name: str
    ratio: "Optional[float]" = None
    write: bool = False


@dataclass(frozen=True)
class LayoutPresetApplyResult:
    """The typed outcome of one apply attempt (pure value; no IO handle).

    ``status`` is a :data:`LAYOUT_PRESET_APPLY_STATUSES` token. ``error`` is set on the
    failure statuses only. ``document`` carries the would-be text on ``previewed``.
    ``backup`` is the backup path taken by a successful write, when one existed.
    """

    status: str
    path: Path
    preset: "Optional[str]" = None
    split: "Optional[str]" = None
    ratio: "Optional[float]" = None
    changes: "tuple[str, ...]" = ()
    shadowed_by_lane_kind: "tuple[str, ...]" = ()
    document: "Optional[str]" = None
    backup: "Optional[Path]" = None
    error: "Optional[str]" = None

    @property
    def ok(self) -> bool:
        return self.status in ("already_matching", "previewed", "written")


class LayoutPresetApplyService:
    """The ``layout preset apply`` use case: expand, re-validate, preview or write.

    Owns the whole state transition the handler previously carried, against the
    injected :class:`ConfigDocumentPort` only. Every outcome — including every
    failure — is a typed :class:`LayoutPresetApplyResult`; no exception crosses the
    service boundary, so the CLI's exit-code mapping is a pure projection.
    """

    def __init__(self, config_document: ConfigDocumentPort) -> None:
        self._config_document = config_document

    def apply(self, command: LayoutPresetApplyInput) -> LayoutPresetApplyResult:
        path = self._config_document.path
        try:
            raw_record = self._config_document.load_raw()
        except RepoLocalConfigError as exc:
            return LayoutPresetApplyResult(
                status="config_unreadable", path=path, error=str(exc)
            )
        if raw_record is None:
            raw_record = {}
        if not isinstance(raw_record, dict):
            return LayoutPresetApplyResult(
                status="not_a_mapping",
                path=path,
                error=f"{path} is not a YAML mapping; refusing to rewrite it",
            )

        try:
            application = apply_preset_to_lane_placement(
                raw_record.get("lane_placement"),
                preset=command.preset_name,
                ratio=command.ratio,
            )
        except LayoutPresetError as exc:
            return LayoutPresetApplyResult(
                status="invalid_input", path=path, error=str(exc)
            )

        # The rewrite touches EXACTLY the `lane_placement` key (#15708 acceptance 2: a
        # display / placement declaration must never reach any other block).
        new_record = dict(raw_record)
        new_record["lane_placement"] = {
            key: (dict(value) if isinstance(value, dict) else value)
            for key, value in application.lane_placement_record.items()
        }

        # Re-validate the WHOLE produced config through the same fail-closed loader
        # every command uses, before anything could be written.
        try:
            RepoLocalConfig.from_record(new_record)
        except RepoLocalConfigError as exc:
            return LayoutPresetApplyResult(
                status="invalid_config",
                path=path,
                error=f"refusing to write an invalid config: {exc}",
            )

        common = dict(
            path=path,
            preset=application.preset,
            split=application.split,
            ratio=application.ratio,
            changes=application.changes,
            shadowed_by_lane_kind=application.shadowed_lane_kinds,
        )

        if application.already_matching:
            return LayoutPresetApplyResult(status="already_matching", **common)

        document = self._config_document.render(new_record)
        if not command.write:
            return LayoutPresetApplyResult(
                status="previewed", document=document, **common
            )

        try:
            backup = self._config_document.replace_atomic(document)
        except OSError as exc:
            return LayoutPresetApplyResult(
                status="write_failed",
                path=path,
                error=f"could not write {path}: {exc}",
            )
        return LayoutPresetApplyResult(status="written", backup=backup, **common)


class YamlConfigDocumentAdapter:
    """The live :class:`ConfigDocumentPort`: `.mozyo-bridge/config.yaml` on disk.

    Reuses the ``config migrate`` helpers (`_load_raw_record` / `_dump_v2` /
    `_atomic_write`) so the read, serialization, and atomic-replace-with-backup
    discipline stays single-sourced with the sibling config-writing surface.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load_raw(self) -> object:
        from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.application.cli_config import (  # noqa: E501
            _load_raw_record,
        )

        return _load_raw_record(self._path)

    def render(self, record: "dict[str, object]") -> str:
        from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.application.cli_config import (  # noqa: E501
            _dump_v2,
        )

        return _dump_v2(record)

    def replace_atomic(self, text: str) -> "Optional[Path]":
        from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.application.cli_config import (  # noqa: E501
            _atomic_write,
        )

        backup = _atomic_write(self._path, text)
        return backup if backup.exists() else None


__all__ = (
    "LAYOUT_PRESET_APPLY_STATUSES",
    "ConfigDocumentPort",
    "LayoutPresetApplyInput",
    "LayoutPresetApplyResult",
    "LayoutPresetApplyService",
    "YamlConfigDocumentAdapter",
)
