"""Operator Unit-board source file: home-level IO + parse (Redmine #15138).

The thin *file-IO and parse* layer over the pure schema in
:mod:`...f_110_cockpit_read_model.domain.unit_board_sources`, following the same
split as :mod:`...application.coordinator_placement_loader`: the domain owns
*meaning* and does no IO; this module owns *IO*.

The file lives under the mozyo-bridge **home**, never in a repo.  That placement
is the mechanism behind one of this issue's close conditions rather than a
convention: the file is where the ssh destinations and container names live, and
those must not travel with a checkout, appear in a diff, or reach a public
document.  Nothing in this module copies a connection value into a message —
errors name the file and the offending *key*, never its value.

Fail-closed contract, identical in shape to the placement loader:

- **A missing file is the behavior-preserving default** — the local server only,
  which is exactly what the board observed before multi-source observation
  existed.  An empty document is the same default.
- **``yaml.safe_load`` only**, through a duplicate-key-rejecting loader: an
  operator file that declares the same key twice is ambiguous, and resolving it
  by declaration order would silently pick a source the operator did not mean.
- **No raw parser / IO exception leaks.** Parse and read failures surface as
  :class:`UnitBoardSourcesLoadError`, a :class:`UnitBoardSourceError`, so one
  ``except`` at the call site covers schema, parse, and IO alike.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

import yaml

from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.unit_board_sources import (  # noqa: E501
    UnitBoardSourceError,
    UnitBoardSourcesConfig,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home


#: Home-root-relative location of the operator source file.  Its own small file,
#: like the placement file, so it never collides with a repo-local schema and
#: never becomes a general operator config by accident.
UNIT_BOARD_SOURCES_RELPATH = Path("unit-board-sources.yaml")


class UnitBoardSourcesLoadError(UnitBoardSourceError):
    """An operator source file could not be read or parsed (file-IO layer)."""


class _NoDuplicateKeyLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` that fails closed on a duplicate mapping key."""


def _construct_mapping_no_duplicates(
    loader: "_NoDuplicateKeyLoader", node: "yaml.MappingNode", deep: bool = False
) -> dict:
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            # A YAML complex key (`? [a, b]`) is unhashable, so the duplicate
            # check itself raises before any schema rule runs.  Left bare it
            # escapes as a TypeError past the loader's `yaml.YAMLError` guard
            # and reaches the CLI as an unhandled error rather than a
            # fail-closed diagnostic (Redmine #15138 review j#101787 f9).
            raise UnitBoardSourcesLoadError(
                "operator Unit board sources file has a non-scalar mapping key; "
                "keys must be plain strings"
            ) from exc
        if duplicate:
            raise UnitBoardSourcesLoadError(
                f"operator Unit board sources file has a duplicate key {key!r}; a "
                "conflicting value is rejected rather than resolved by declaration order"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_NoDuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_no_duplicates
)


def unit_board_sources_path(home: Union[str, Path, None] = None) -> Path:
    """Resolve the absolute path of the operator source file under ``home``."""
    base = Path(home).expanduser().resolve() if home is not None else mozyo_bridge_home()
    return base / UNIT_BOARD_SOURCES_RELPATH


def load_unit_board_sources(
    home: Union[str, Path, None] = None,
) -> UnitBoardSourcesConfig:
    """Load + validate the operator's observable sources, or the local default."""
    return load_unit_board_sources_from_path(unit_board_sources_path(home))


def load_unit_board_sources_from_path(
    path: Union[str, Path],
) -> UnitBoardSourcesConfig:
    """Load + validate an operator source document from an explicit ``path``.

    A missing file is the local-only default.  A *present* file that cannot be
    read fails closed instead of silently defaulting: an operator who configured
    remote hosts and then made the file unreadable must be told, not quietly
    shown a local-only board that looks complete.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return UnitBoardSourcesConfig.default()
    except (OSError, UnicodeDecodeError) as exc:
        raise UnitBoardSourcesLoadError(
            f"could not read operator Unit board sources file {path.name}: {exc.__class__.__name__}"
        ) from exc

    try:
        parsed = yaml.load(text, Loader=_NoDuplicateKeyLoader)
    except yaml.YAMLError as exc:
        raise UnitBoardSourcesLoadError(
            f"could not parse operator Unit board sources file {path.name} as YAML: "
            f"{exc.__class__.__name__}"
        ) from exc

    if parsed is None:
        return UnitBoardSourcesConfig.default()
    return UnitBoardSourcesConfig.from_record(parsed)


__all__ = (
    "UNIT_BOARD_SOURCES_RELPATH",
    "UnitBoardSourcesLoadError",
    "load_unit_board_sources",
    "load_unit_board_sources_from_path",
    "unit_board_sources_path",
)
