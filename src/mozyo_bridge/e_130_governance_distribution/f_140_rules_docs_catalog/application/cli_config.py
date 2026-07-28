"""Public ``config`` CLI: repo-local config schema inspection + migration (Redmine #14148).

The public surface for the ``.mozyo-bridge/config.yaml`` *schema* — distinct from the
misnamed ``runtime-config`` group, which targets the LLM runtime config
(``.codex/config.toml``). Today it carries one subcommand:

- ``config migrate`` upgrades a legacy provider-keyed v1 config to the role-canonical v2
  ``agents`` topology. It is **dry-run first**: ``--check`` (the default) prints the plan and
  the would-be v2 document and writes nothing; ``--write`` applies it with an atomic
  replace + a ``.bak`` backup, and only after re-validating the produced record.

Boundary, kept enforced in code:

- **Dry-run default.** Nothing is written unless ``--write`` is passed explicitly.
- **Fail-closed.** A malformed / unknown / cross-version input fails closed through the
  validating parse before any transform; the produced v2 record is re-validated before it
  is written, so a write never lands an unparseable config.
- **Atomic + reversible.** ``--write`` backs the original up to ``config.yaml.bak`` and
  replaces via a temp file + ``os.replace`` (atomic on POSIX); a mid-write failure leaves
  the original untouched.
- **No credentials, no home writes.** Only ``<repo>/.mozyo-bridge/config.yaml`` is read /
  written; the schema forbids credential-shaped fields, so nothing secret is ever printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.application.repo_local_config_loader import repo_local_config_path
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.config_migration import (
    migrate_record,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config_status import (  # noqa: E501
    classify_config_sources,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
    RepoLocalConfig,
    RepoLocalConfigError,
)


def register(sub) -> None:
    """Register the `config` group and its `migrate` subcommand onto ``sub``."""
    config = sub.add_parser(
        "config",
        help=(
            "Repo-local `.mozyo-bridge/config.yaml` schema commands: `migrate` (v1 -> v2, "
            "dry-run by default). Distinct from `runtime-config` (LLM runtime config)."
        ),
    )
    config_sub = config.add_subparsers(dest="config_command", required=True)

    status = config_sub.add_parser(
        "status",
        help=(
            "Report the repo-local config schema version and any actionable "
            "deprecation warning (read-only; the v1 -> v2 deprecation surface)."
        ),
    )
    add_repo_option(status)
    status.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON result instead of human text.",
    )
    status.set_defaults(func=cmd_config_status)

    # Redmine #14258 (review j#87752 R4): the managed-launch preflight must prove the
    # SELECTED launcher can parse the target repo's config, and no advertised summary of the
    # grammar can prove it. A version set plus the top-level key set is only a *necessary*
    # condition: commit `d28e59e2` added the nested `lane_placement.by_lane_kind` key without
    # touching either, and a launcher predating it rejects that config as an unknown nested
    # key while advertising an identical version + top-level contract (measured against real
    # pre-`d28e59e2` source). So the preflight stops summarizing the grammar and instead runs
    # THIS command on the candidate launcher against the exact target bytes: the launcher's
    # own parser is the only authority that can answer the question, and it covers every
    # axis — version, top-level, nested, and any axis added later — at once.
    check_parse = config_sub.add_parser(
        "check-parse",
        help=(
            "Read-only: can THIS build parse the given repo-local config file? Exit 0 when "
            "it parses under this build's full schema, 2 when it does not (the error goes "
            "to stderr). Writes nothing. Used by the managed-launch launcher preflight to "
            "prove a candidate launcher can read a target repo's config."
        ),
    )
    check_parse.add_argument(
        "--file",
        dest="config_file",
        required=True,
        help="Absolute path of the `.mozyo-bridge/config.yaml`-shaped document to parse.",
    )
    check_parse.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON result instead of human text.",
    )
    check_parse.set_defaults(func=cmd_config_check_parse)

    migrate = config_sub.add_parser(
        "migrate",
        help=(
            "Upgrade a legacy v1 config to the role-canonical v2 `agents` topology. "
            "Dry-run (`--check`) by default; pass `--write` to apply."
        ),
    )
    add_repo_option(migrate)
    mode = migrate.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        dest="write",
        action="store_false",
        help="Preview the migration plan and the would-be v2 document; write nothing (default).",
    )
    mode.add_argument(
        "--write",
        dest="write",
        action="store_true",
        help="Apply the migration: atomic replace of config.yaml with a .bak backup.",
    )
    migrate.set_defaults(write=False, func=cmd_config_migrate)
    migrate.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON result instead of human text.",
    )


def _load_raw_record(path: Path):
    """Read + parse the config file as a raw mapping (or ``None`` if missing / empty).

    Returns the ``yaml.safe_load`` mapping so the migration copies unrelated blocks
    verbatim. A missing file is ``None`` (migrates to a bare v2). A parse failure is
    re-raised as :class:`RepoLocalConfigError` so the caller reports it uniformly.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise RepoLocalConfigError(f"could not read config file {path}: {exc}") from exc
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RepoLocalConfigError(
            f"could not parse config file {path} as YAML: {exc}"
        ) from exc
    return parsed


def _dump_v2(record: "dict[str, object]") -> str:
    """Serialize a migrated v2 record to a stable, readable YAML document."""
    return yaml.safe_dump(
        record, sort_keys=False, default_flow_style=False, allow_unicode=True
    )


def _atomic_write(path: Path, text: str) -> Path:
    """Write ``text`` to ``path`` atomically, backing up any existing file to ``.bak``.

    Returns the backup path (or a non-existent ``.bak`` path if there was no original).
    The temp file + ``os.replace`` keeps the original intact on any mid-write failure.
    """
    backup = path.with_name(path.name + ".bak")
    if path.exists():
        backup.write_bytes(path.read_bytes())
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        # Leave the original untouched; clean up the partial temp file.
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    return backup


#: Exit code of ``config check-parse`` when the document does NOT parse under this build's
#: schema. Distinct from ``1`` (which every other config command uses for "could not read
#: the repo's own config"), so the launcher preflight can tell "this build rejects that
#: document" from a mechanical failure to run the command at all.
CONFIG_CHECK_PARSE_REJECTED = 2


def cmd_config_check_parse(args) -> int:
    """Handle ``config check-parse`` — can THIS build parse the given document? (#14258).

    The whole command is one question with a binary answer, and the answer is the **exit
    code** so a caller in another process can consume it without parsing prose: ``0`` when
    the document parses under this build's full schema (every nested block included), and
    :data:`CONFIG_CHECK_PARSE_REJECTED` when it does not, with the real error on stderr.

    Read-only by construction: it parses the bytes and returns. It never migrates, never
    writes, and never touches the repo's own config — the path is supplied explicitly,
    because the managed-launch preflight asks about a document that may not be the caller's
    (for a lane this run will create, it is the committed blob at the lane's base commit,
    materialized to a temporary file).

    A missing / unreadable / malformed file is a *rejection*, not a crash: the caller's
    question is "can you parse this", and the honest answer for a file that cannot be read
    is no. Nothing here prints credential-shaped data — the schema rejects such fields at
    load time, so this surface inherits that guarantee without a new check.
    """
    from mozyo_bridge.application.repo_local_config_loader import (
        load_repo_local_config_from_path,
    )

    as_json = bool(getattr(args, "json", False))
    path = Path(getattr(args, "config_file", "") or "")
    try:
        if not path.is_file():
            # The loader treats a MISSING file as the empty default record, which is
            # right for "load this repo's config" and wrong here: the caller asked whether a
            # specific document parses, and answering "yes" for a document that is not there
            # would let the preflight credit a launcher it never actually exercised.
            raise RepoLocalConfigError(f"no such config file: {path}")
        config = load_repo_local_config_from_path(path)
    except RepoLocalConfigError as exc:
        if as_json:
            print(json.dumps({"ok": False, "parsed": False, "error": str(exc)}))
        else:
            print(f"config check-parse: rejected: {exc}", file=sys.stderr)
        return CONFIG_CHECK_PARSE_REJECTED
    if as_json:
        print(json.dumps({"ok": True, "parsed": True, "version": config.schema_version}))
    else:
        print(f"config check-parse: parsed (schema version {config.schema_version})")
    return 0


def cmd_config_status(args) -> int:
    """Handle ``config status`` — the public read-only config visibility surface.

    Loads the repo-local config through the same fail-closed loader every command uses and
    surfaces its schema version, any actionable deprecation warning (Redmine #14148 review
    j#84516 finding 2), and — Redmine #14222/#14223 — the per-key effective value + source
    (``declared`` vs the silent ``default``) for every top-level config
    block, via :func:`~...domain.repo_local_config_status.classify_config_sources`. This
    stays the ONE status command (#14223 close condition: no second ``status`` surface);
    nothing here writes, and nothing prints credential-shaped data (the schema forbids such
    fields at load time, so this surface inherits that guarantee without a new check).
    """
    from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config

    as_json = bool(getattr(args, "json", False))
    path = repo_local_config_path(getattr(args, "repo", None))
    try:
        config = load_repo_local_config(getattr(args, "repo", None))
    except RepoLocalConfigError as exc:
        if as_json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"config status: cannot read {path}: {exc}", file=sys.stderr)
        return 1

    warnings = list(config.deprecation_warnings())
    # Redmine #14222/#14223: per-key effective value + source (declared vs the silent
    # default), reusing THIS surface rather than a second status
    # command. `_load_raw_record` already exists for `config migrate`'s dry-run plan; a
    # parse failure here cannot happen (the loader above already proved the file parses),
    # so any raise would be a genuine bug, not a reachable user-facing path.
    raw_record = _load_raw_record(path)
    settings = [
        status.as_payload()
        for status in classify_config_sources(
            raw_record=raw_record,
            config=config,
            schema_version=config.schema_version,
            legacy_migratable=bool(warnings),
        )
    ]
    if as_json:
        print(json.dumps({
            "ok": True,
            "path": str(path),
            "schema_version": config.schema_version,
            "deprecated": bool(warnings),
            "warnings": warnings,
            "settings": settings,
        }))
    else:
        print(f"config: {path}")
        print(f"  schema version: {config.schema_version}")
        for warning in warnings:
            print(f"  deprecation: {warning}")
        if not warnings:
            print("  deprecation: none")
        print("  settings:")
        for setting in settings:
            line = f"    {setting['key']}: {setting['source']}"
            if setting["note"]:
                line += f" ({setting['note']})"
            print(line)
    return 0


def cmd_config_migrate(args) -> int:
    """Handle ``config migrate`` — preview (default) or apply the v1 -> v2 migration."""
    path = repo_local_config_path(getattr(args, "repo", None))
    as_json = bool(getattr(args, "json", False))
    write = bool(getattr(args, "write", False))

    try:
        record = _load_raw_record(path)
        result = migrate_record(record)
    except RepoLocalConfigError as exc:
        # Fail closed: report the schema / parse failure, write nothing.
        if as_json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"config migrate: cannot migrate {path}: {exc}", file=sys.stderr)
        return 1

    document = _dump_v2(result.migrated)

    if result.already_current:
        if as_json:
            print(json.dumps({
                "ok": True, "path": str(path), "already_current": True,
                "source_version": result.source_version,
                "target_version": result.target_version, "written": False,
            }))
        else:
            print(f"config migrate: {path} is already version {result.target_version}; "
                  "nothing to do.")
        return 0

    if not write:
        # Dry-run: show the plan + the would-be document, write nothing.
        if as_json:
            print(json.dumps({
                "ok": True, "path": str(path), "already_current": False,
                "source_version": result.source_version,
                "target_version": result.target_version, "written": False,
                "changes": list(result.changes), "document": document,
            }))
        else:
            print(f"config migrate (dry-run): {path}")
            print(f"  version {result.source_version} -> {result.target_version}")
            for change in result.changes:
                print(f"  - {change}")
            print("\n--- would write ---")
            print(document, end="" if document.endswith("\n") else "\n")
            print("--- end (pass --write to apply) ---")
        return 0

    # --write: re-validate the produced record, then apply atomically.
    try:
        RepoLocalConfig.from_record(result.migrated)
    except RepoLocalConfigError as exc:
        msg = f"config migrate: refusing to write an invalid migrated config: {exc}"
        if as_json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 1
    try:
        backup = _atomic_write(path, document)
    except OSError as exc:
        msg = f"config migrate: could not write {path}: {exc}"
        if as_json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 1

    if as_json:
        print(json.dumps({
            "ok": True, "path": str(path), "already_current": False,
            "source_version": result.source_version,
            "target_version": result.target_version, "written": True,
            "backup": str(backup) if backup.exists() else None,
            "changes": list(result.changes),
        }))
    else:
        print(f"config migrate: wrote {path} (version {result.target_version}).")
        if backup.exists():
            print(f"  backup: {backup}")
        for change in result.changes:
            print(f"  - {change}")
    return 0


__all__ = ("register", "cmd_config_migrate", "cmd_config_status")
