"""Operator placement mode loader — home-level file IO + parse (Redmine #14139).

The thin *file-IO and parse* layer over the pure schema boundary in
:mod:`...domain.coordinator_placement_mode`, mirroring
:mod:`mozyo_bridge.application.repo_local_config_loader`: the domain owns
*meaning* (closed vocabulary, fail-closed rules) and does no IO; this module
owns *IO* — it resolves the operator file under the mozyo-bridge **home** root,
reads it, parses it with ``yaml.safe_load`` (and nothing else), and hands the
parsed mapping to the domain layer.

Home-level, not repo-committed (Redmine #14139 design point 1): the file lives
under :func:`mozyo_bridge.shared.paths.mozyo_bridge_home` — the same per-operator
home the rules store and workspace registry resolve through — so the placement
mode is operator-scoped and never travels with a repo. ``home`` is injectable so
a fixture can isolate a temp home and never touch the real operator config.

Fail-closed contract:

- **Missing file is the behavior-preserving default.** No operator file resolves
  to :meth:`CoordinatorPlacementConfig.default` (``per_project_space``), with no
  warning — an operator who never opts in launches unchanged. An empty file
  (``yaml.safe_load`` -> ``None``) is the same default.
- **``yaml.safe_load`` only** — never ``yaml.load`` / ``yaml.full_load``; the
  loader never constructs arbitrary Python objects from a home file.
- **No raw parser / IO exception leaks.** A malformed document
  (``yaml.YAMLError``) or an unreadable present file (``OSError`` /
  ``UnicodeDecodeError``) is re-raised as
  :class:`CoordinatorPlacementLoadError`, a subclass of the domain's
  :class:`CoordinatorPlacementError`, so a single ``except
  CoordinatorPlacementError`` at the call site catches parse, IO, and schema
  failures alike.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

import yaml

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.coordinator_placement_mode import (  # noqa: E501
    CoordinatorPlacementConfig,
    CoordinatorPlacementError,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home

#: The home-root-relative location of the operator placement file. A single
#: definition so the path can never drift between the loader and any diagnostic
#: surface that names the same file. Deliberately its OWN small file (not the
#: repo config, not a broad operator config) so it never collides with the
#: repo-local schema and does not pre-empt a future home-config schema (#14148).
COORDINATOR_PLACEMENT_RELPATH = Path("coordinator-placement.yaml")


class CoordinatorPlacementLoadError(CoordinatorPlacementError):
    """An operator placement file could not be read or parsed (file-IO layer).

    Subclasses :class:`CoordinatorPlacementError` so a caller catching the domain
    error catches *every* placement-config failure — schema, parse, and IO — with
    one ``except``. Raised only for the IO / parse concerns this layer owns; a
    missing file is *not* an error (it resolves to the default).
    """


def coordinator_placement_path(home: Union[str, Path, None] = None) -> Path:
    """Resolve the absolute path of the operator placement file under ``home``.

    ``home`` defaults to :func:`mozyo_bridge_home` (``MOZYO_BRIDGE_HOME`` or
    ``~/.mozyo_bridge``), the single home contract the rest of the codebase uses,
    so the file is always looked up at the same root the operator's home store
    resolves to. This does no IO — it only computes the path.
    """
    base = Path(home).expanduser().resolve() if home is not None else mozyo_bridge_home()
    return base / COORDINATOR_PLACEMENT_RELPATH


def load_coordinator_placement(
    home: Union[str, Path, None] = None,
) -> CoordinatorPlacementConfig:
    """Load + validate the operator placement mode, or the default.

    Resolves the file for ``home`` (see :func:`coordinator_placement_path`) and
    delegates to :func:`load_coordinator_placement_from_path`. A missing file
    yields the behavior-preserving :meth:`CoordinatorPlacementConfig.default`; any
    present-but-broken file fails closed through :class:`CoordinatorPlacementError`.
    """
    return load_coordinator_placement_from_path(coordinator_placement_path(home))


def load_coordinator_placement_from_path(
    path: Union[str, Path],
) -> CoordinatorPlacementConfig:
    """Load + validate an operator placement config from an explicit ``path``.

    The single place file IO and parsing happen. Steps, all fail-closed:

    - a missing file (``FileNotFoundError``) resolves to
      :meth:`CoordinatorPlacementConfig.default` — a missing file never changes
      behavior;
    - any other read failure (permission, a directory in the file's place, a
      non-UTF-8 file) is re-raised as :class:`CoordinatorPlacementLoadError`, so a
      *present* file that cannot be read fails closed rather than silently
      defaulting;
    - the text is parsed with ``yaml.safe_load`` only; a malformed document is
      re-raised as :class:`CoordinatorPlacementLoadError` — never a bare
      ``yaml.YAMLError``;
    - an empty document (``yaml.safe_load`` -> ``None``) resolves to the default;
    - any other parsed value is handed to
      :meth:`CoordinatorPlacementConfig.from_record`, which fails closed on a
      non-mapping document, unknown keys, an unsupported version, or an unknown
      mode.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Missing file is the behavior-preserving default, not a failure.
        return CoordinatorPlacementConfig.default()
    except (OSError, UnicodeDecodeError) as exc:
        raise CoordinatorPlacementLoadError(
            f"could not read operator coordinator placement file {path}: {exc}"
        ) from exc

    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CoordinatorPlacementLoadError(
            f"could not parse operator coordinator placement file {path} as YAML: {exc}"
        ) from exc

    if parsed is None:
        return CoordinatorPlacementConfig.default()
    return CoordinatorPlacementConfig.from_record(parsed)


def resolve_coordinator_placement_mode(home: Union[str, Path, None] = None) -> str:
    """The operator's effective placement mode string (fail-closed).

    Convenience for the launch composition roots: load the config and return its
    :attr:`mode`. A missing file yields ``per_project_space``; a present-but-broken
    file fails closed through :class:`CoordinatorPlacementError` (the caller turns
    it into an actionable refusal).
    """
    return load_coordinator_placement(home).mode


__all__ = (
    "COORDINATOR_PLACEMENT_RELPATH",
    "CoordinatorPlacementLoadError",
    "coordinator_placement_path",
    "load_coordinator_placement",
    "load_coordinator_placement_from_path",
    "resolve_coordinator_placement_mode",
)
