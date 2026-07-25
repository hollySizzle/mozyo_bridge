"""Repo-local YAML config loader — file IO + parse layer (Redmine #12190).

This is the thin *file-IO and parse* layer on top of the pure schema boundary
defined in :mod:`mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config` (Redmine #12189). The
split is deliberate and mirrors the rest of the adapter seams:

- the **domain** module owns *meaning* — the closed top-level record shape, the
  three typed sub-records, and every fail-closed schema rule — and does no file
  IO and no parsing (:meth:`RepoLocalConfig.from_record` normalizes an
  already-parsed mapping);
- this **application** module owns *IO* — it resolves where
  ``.mozyo-bridge/config.yaml`` lives, reads it, parses it with
  ``yaml.safe_load`` (and nothing else), and hands the parsed mapping to the
  domain layer. It imports ``yaml`` (a third-party dependency) precisely so the
  domain layer never has to.

The contract this layer adds, fail-closed throughout:

- **Missing file is the behavior-preserving default.** A repo with no
  ``.mozyo-bridge/config.yaml`` resolves to :meth:`RepoLocalConfig.default`,
  with no warning and no failure — the default ``mozyo-bridge`` behavior is
  unchanged. An empty file (``yaml.safe_load`` -> ``None``) is the same default.
- **``yaml.safe_load`` only.** Never ``yaml.load`` / ``yaml.full_load`` — the
  loader must never construct arbitrary Python objects from a repo-local file,
  so a config file can never execute code or instantiate a class.
- **No raw parser / IO exception leaks at the public boundary.** A malformed
  YAML document (``yaml.YAMLError``) or an unreadable present file
  (``OSError`` / ``UnicodeDecodeError``) is re-raised as
  :class:`RepoLocalConfigLoadError`, a subclass of the domain's
  :class:`~mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config.RepoLocalConfigError`. Schema
  violations raised by :meth:`RepoLocalConfig.from_record` already are
  ``RepoLocalConfigError``. So a single ``except RepoLocalConfigError`` at the
  call site catches every repo-local-config failure — parse, IO, and schema —
  and no caller ever sees a bare ``yaml`` exception.

Scope boundary (kept explicit): this layer does **not** wire the resolved
records into CLI composition or change any default CLI behavior — that is
Redmine #12191. It only turns "a repo root" into "a validated
:class:`RepoLocalConfig`".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Union

import yaml

from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
    REPO_LOCAL_CONFIG_VERSION,
    SUPPORTED_REPO_LOCAL_CONFIG_VERSIONS,
    RepoLocalConfig,
    RepoLocalConfigError,
)
from mozyo_bridge.shared.paths import resolve_repo_root

#: The repo-root-relative location of the repo-local config file. A single
#: definition so the path can never drift between the loader and any caller /
#: doctor surface that wants to point at the same file.
CONFIG_FILE_RELPATH = Path(".mozyo-bridge") / "config.yaml"


class RepoLocalConfigLoadError(RepoLocalConfigError):
    """A repo-local config file could not be read or parsed (file-IO layer).

    Subclasses :class:`~mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config.RepoLocalConfigError`
    so a caller catching the domain error catches *every* repo-local-config
    failure — schema, parse, and IO — with one ``except``. It is raised only for
    the IO / parse concerns this layer owns (an unreadable present file or a
    malformed YAML document); schema violations keep their own
    ``RepoLocalConfigError`` from the domain layer. A missing file is *not* an
    error: it resolves to the behavior-preserving default.
    """


def repo_local_config_path(
    repo_root: Union[str, Path, None] = None,
    *,
    start: Optional[Path] = None,
) -> Path:
    """Resolve the absolute path of ``.mozyo-bridge/config.yaml`` for a repo.

    ``repo_root`` (or ``MOZYO_REPO`` / a Git-root-first resolve from ``start``,
    Redmine #13641) is resolved
    through :func:`mozyo_bridge.shared.paths.resolve_repo_root`, the single repo-
    root contract the rest of the codebase uses, so the config file is always
    looked up at the same root the workspace identity resolves to. This does no
    IO — it only computes the path.
    """
    return resolve_repo_root(repo_root, start) / CONFIG_FILE_RELPATH


def load_repo_local_config(
    repo_root: Union[str, Path, None] = None,
    *,
    start: Optional[Path] = None,
) -> RepoLocalConfig:
    """Load + validate the repo-local config for a repo, or the default.

    Resolves the config path for ``repo_root`` (see
    :func:`repo_local_config_path`) and delegates to
    :func:`load_repo_local_config_from_path`. A missing file yields the
    behavior-preserving :meth:`RepoLocalConfig.default`; any present-but-broken
    file fails closed through :class:`RepoLocalConfigError`.
    """
    return load_repo_local_config_from_path(
        repo_local_config_path(repo_root, start=start)
    )


def load_repo_local_config_from_path(path: Union[str, Path]) -> RepoLocalConfig:
    """Load + validate a repo-local config from an explicit ``path``.

    The single place file IO and parsing happen. Steps, all fail-closed:

    - a missing file (``FileNotFoundError``) resolves to
      :meth:`RepoLocalConfig.default` — a missing config never changes behavior;
    - any other read failure (permission, a directory in the file's place, a
      non-UTF-8 file) is re-raised as :class:`RepoLocalConfigLoadError`, so a
      *present* file that cannot be read fails closed rather than silently
      defaulting;
    - the text is parsed with ``yaml.safe_load`` only; a malformed document is
      re-raised as :class:`RepoLocalConfigLoadError` — never a bare
      ``yaml.YAMLError``;
    - an empty document (``yaml.safe_load`` -> ``None``) resolves to the
      default;
    - any other parsed value is handed to :meth:`RepoLocalConfig.from_record`,
      which fails closed (as ``RepoLocalConfigError``) on a non-mapping document,
      unknown keys, an unsupported version, or a boundary- / authority- /
      credential-shaped field.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Missing config is the behavior-preserving default, not a failure.
        return RepoLocalConfig.default()
    except (OSError, UnicodeDecodeError) as exc:
        # A present file we cannot read/decode fails closed rather than silently
        # defaulting, so a misconfigured/unreadable config is never hidden.
        raise RepoLocalConfigLoadError(
            f"could not read repo-local config file {path}: {exc}"
        ) from exc

    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # Wrap the parser exception so no raw yaml.YAMLError leaks at the public
        # boundary; the call site only ever sees a RepoLocalConfigError.
        raise RepoLocalConfigLoadError(
            f"could not parse repo-local config file {path} as YAML: {exc}"
        ) from exc

    if parsed is None:
        # Empty / whitespace / comment-only file -> default behavior.
        return RepoLocalConfig.default()
    return RepoLocalConfig.from_record(parsed)


#: No repo-local config file exists — the behavior-preserving default (nothing to parse).
CONFIG_SCHEMA_ABSENT = "config_schema_absent"
#: A present file whose top-level ``version`` and key set were read, and whose version is
#: one THIS build supports.
CONFIG_SCHEMA_DECLARED = "config_schema_declared"
#: A present file whose schema could not be read at all (unreadable, malformed YAML, a
#: non-mapping document, a non-string key, or a non-integer ``version``).
CONFIG_SCHEMA_UNREADABLE = "config_schema_unreadable"
#: A present, readable file declaring a record version THIS build does not support.
CONFIG_SCHEMA_UNSUPPORTED = "config_schema_unsupported"


@dataclass(frozen=True)
class ConfigSchemaObservation:
    """The *schema shape* of a repo-local config file, read without validating it (#14258).

    Deliberately narrower than :func:`load_repo_local_config_from_path`: it reads only the
    two facts a launcher's config-parse capability is joined against — the declared top-level
    ``version`` and the set of present top-level keys — and does **not** validate any block.
    That separation is the point. The managed-launch preflight asks "could the selected
    launcher parse this file at all", which is decided by the version boundary and the
    closed top-level key set; whether some nested block is well-formed is a different
    question, and one this runtime's own load already answers on its own path. Folding the
    two would make an unrelated nested error look like a launcher incompatibility.

    ``upgrade_required`` is ``True`` when the declared version is *newer* than anything this
    build supports (as opposed to an unrecognized / foreign value), so a refusal can name the
    operator's real next action.
    """

    state: str
    version: Optional[int] = None
    keys: frozenset = frozenset()
    upgrade_required: bool = False


def probe_repo_local_config_schema(
    path: Union[str, Path],
) -> ConfigSchemaObservation:
    """Read-only probe of a repo-local config's schema shape (Redmine #14258).

    The input the managed-launch launcher preflight was missing. The #13748 / #13847 / #13882
    launcher checks all verify the launcher against the *attestation store*; none of them
    looks at the config the wrapper is about to be pointed at with ``--cwd <lane worktree>``.
    A launcher predating a config schema bump parses that file at startup, rejects it, and
    exits before ``exec``ing the provider — measured as ``unknown key 'agents'`` / exit 2
    against the v2 config (#14258 j#85834).

    Read-only and fail-closed: a missing file is :data:`CONFIG_SCHEMA_ABSENT` (there is
    nothing for any launcher to parse, so a config-less repo keeps working exactly as
    before), while a *present* file whose schema cannot be read is
    :data:`CONFIG_SCHEMA_UNREADABLE` rather than being folded into "absent" — an unreadable
    config is not a missing one. ``yaml.safe_load`` only, like the loader above, so a config
    file can never execute code or instantiate a class.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ConfigSchemaObservation(CONFIG_SCHEMA_ABSENT)
    except (OSError, UnicodeDecodeError):
        return ConfigSchemaObservation(CONFIG_SCHEMA_UNREADABLE)
    return probe_repo_local_config_schema_text(text)


def probe_repo_local_config_schema_text(text: str) -> ConfigSchemaObservation:
    """Probe a config's schema shape from its TEXT rather than a path (Redmine #14258).

    The same decision as :func:`probe_repo_local_config_schema`, split out because the
    ``sublane create`` launcher gate must ask the question about a config that is not on
    disk yet: the lane worktree does not exist at the moment the gate has to refuse, so the
    text comes from the committed blob at the lane's base ref (``git show <ref>:<path>``) —
    exactly the bytes ``git worktree add`` will materialize. Reading the *primary* checkout's
    file instead would be a proxy for the target, not the target.
    """
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        return ConfigSchemaObservation(CONFIG_SCHEMA_UNREADABLE)
    if parsed is None:
        # An empty / comment-only file declares nothing, which is the same as no file at
        # all for the parse question: `RepoLocalConfig.default()` needs no schema support.
        return ConfigSchemaObservation(CONFIG_SCHEMA_ABSENT)
    if not isinstance(parsed, Mapping) or any(
        not isinstance(key, str) for key in parsed
    ):
        return ConfigSchemaObservation(CONFIG_SCHEMA_UNREADABLE)
    keys = frozenset(parsed)
    version = parsed.get("version", REPO_LOCAL_CONFIG_VERSION)
    # `bool` is an `int` subclass, so it is rejected explicitly for the same reason
    # `_checked_top_level_version` rejects it: `version: true` must never read as 1.
    if isinstance(version, bool) or not isinstance(version, int):
        return ConfigSchemaObservation(CONFIG_SCHEMA_UNREADABLE, keys=keys)
    if version not in SUPPORTED_REPO_LOCAL_CONFIG_VERSIONS:
        return ConfigSchemaObservation(
            CONFIG_SCHEMA_UNSUPPORTED,
            version,
            keys,
            upgrade_required=version > max(SUPPORTED_REPO_LOCAL_CONFIG_VERSIONS),
        )
    return ConfigSchemaObservation(CONFIG_SCHEMA_DECLARED, version, keys)


__all__ = (
    "CONFIG_FILE_RELPATH",
    "CONFIG_SCHEMA_ABSENT",
    "CONFIG_SCHEMA_DECLARED",
    "CONFIG_SCHEMA_UNREADABLE",
    "CONFIG_SCHEMA_UNSUPPORTED",
    "ConfigSchemaObservation",
    "RepoLocalConfigLoadError",
    "load_repo_local_config",
    "load_repo_local_config_from_path",
    "probe_repo_local_config_schema",
    "probe_repo_local_config_schema_text",
    "repo_local_config_path",
)
