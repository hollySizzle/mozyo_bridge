"""Command handlers for the ``redmine-version`` family (Redmine #12651).

``mozyo-bridge redmine-version list-open-leaf`` enumerates the open *leaf* issues
of a Version from a flat ``GET /issues.json?fixed_version_id=<id>`` snapshot — the
read model the current MCP US-only surface cannot produce.

``mozyo-bridge redmine-version preflight`` runs the fail-closed rename / close /
lock / delete preflight against a Version state (resolved from a ``list_versions``
snapshot or supplied inline) and prints the allow/blocked decision plus the
concrete REST / operator-UI step a human or future live adapter must perform.

Both handlers are advisory: they read JSON snapshots and render a decision. They
perform **no** Redmine write and touch **no** network — there is no Version-write
credential wired and the Redmine adapter is read-only-by-design. A blocked
preflight exits non-zero; a bad/missing snapshot fails closed with a stderr
message, matching the ``state`` / ``doctor`` / ``health`` CLI convention.
"""
from __future__ import annotations

import argparse
import json as _json
import sys
from pathlib import Path
from typing import Mapping

from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.domain.redmine_version_enumeration import (
    MappingRedmineVersionIssueSource,
    VersionLeafEnumeration,
    enumerate_from_source,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_version_issue_source import (
    RedmineVersionReadUnavailable,
    live_version_issue_source_from_env,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.domain.redmine_version_operation import (
    VersionOperationError,
    VersionOperationRequest,
    VersionState,
    confirmation_token_for,
    decide_version_operation,
)


def _fail(message: str) -> int:
    print(f"mozyo-bridge redmine-version: {message}", file=sys.stderr)
    return 2


def _load_json(path_str: str) -> object:
    path = Path(path_str).expanduser()
    if not path.is_file():
        raise VersionOperationError(f"snapshot not found: {path}")
    try:
        return _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VersionOperationError(f"cannot read snapshot {path}: {exc}") from exc


def _print_json(payload: object) -> None:
    print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_redmine_version_list_open_leaf(args: argparse.Namespace) -> int:
    """Enumerate the open leaf issues of a Version.

    Reads from a static operator-exported ``issues.json`` snapshot, or — with the
    explicit ``--live`` opt-in — from a read-only live Redmine read. The live
    path fails closed with an explicit reason and a non-zero exit when no
    credential / provider is available, so an unreadable Version is never
    rendered as an empty one.
    """
    version_id = str(getattr(args, "version_id", "") or "").strip()
    if not version_id:
        return _fail("--version-id is required")

    if bool(getattr(args, "live", False)):
        try:
            source = live_version_issue_source_from_env()
            enumeration = enumerate_from_source(source, version_id)
        except RedmineVersionReadUnavailable as exc:
            # Fail closed: surface the explicit reason and a non-zero exit rather
            # than reporting a silent empty Version (#12923 acceptance).
            return _fail(f"live read unavailable ({exc.reason}): {exc}")
    else:
        try:
            payload = _load_json(args.issues_json)
        except VersionOperationError as exc:
            return _fail(str(exc))
        source = MappingRedmineVersionIssueSource(payload)
        enumeration = enumerate_from_source(source, version_id)

    return _render_enumeration(enumeration, version_id, bool(getattr(args, "as_json", False)))


def _render_enumeration(
    enumeration: VersionLeafEnumeration, version_id: str, as_json: bool
) -> int:
    """Print the open-leaf enumeration as JSON or human-readable text."""
    if as_json:
        _print_json(enumeration.as_dict())
        return 0

    print(
        f"Version #{version_id}: {len(enumeration.open_leaf_issues)} open leaf "
        f"issue(s) of {enumeration.total_open} open / {enumeration.total_issues} total"
    )
    if enumeration.counts_by_tracker:
        by_tracker = ", ".join(
            f"{tracker}={count}"
            for tracker, count in sorted(enumeration.counts_by_tracker.items())
        )
        print(f"  by tracker: {by_tracker}")
    for issue in enumeration.open_leaf_issues:
        parent = f" (parent #{issue.parent_id})" if issue.parent_id else ""
        print(f"  leaf  #{issue.issue_id} [{issue.tracker}] {issue.status_name}{parent}")
    for issue in enumeration.open_nonleaf_issues:
        print(
            f"  node  #{issue.issue_id} [{issue.tracker}] {issue.status_name} "
            "(has open child in-version)"
        )
    return 0


def _resolve_state(args: argparse.Namespace) -> VersionState:
    version_id = str(getattr(args, "version_id", "") or "").strip()
    if not version_id:
        raise VersionOperationError("--version-id is required")
    versions_json = getattr(args, "versions_json", None)
    if versions_json:
        payload = _load_json(versions_json)
        for entry in _versions_entries(payload):
            if str(entry.get("id", "")).strip() == version_id:
                return VersionState.from_mapping(entry)
        raise VersionOperationError(
            f"version #{version_id} not found in snapshot {versions_json}"
        )
    # Inline fallback: build the state from explicit counts/name/status. Route it
    # through VersionState.from_mapping so the counts_known / non-negative parse
    # rule is enforced in exactly one place — an omitted count leaves the key out
    # (counts_known stays False), and a supplied-but-invalid count (e.g. negative)
    # fails the same validation a snapshot would.
    entry: dict[str, object] = {"id": version_id}
    name = getattr(args, "name", None)
    if name is not None:
        entry["name"] = name
    status = getattr(args, "status", None)
    if status is not None:
        entry["status"] = status
    for key in ("issues_count", "open_issues_count", "closed_issues_count"):
        value = getattr(args, key, None)
        if value is not None:
            entry[key] = value
    return VersionState.from_mapping(entry)


def _versions_entries(payload: object) -> list[Mapping[str, object]]:
    raw = payload
    if isinstance(raw, Mapping):
        raw = raw.get("versions", [])
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, Mapping)]


def cmd_redmine_version_preflight(args: argparse.Namespace) -> int:
    """Run the fail-closed Version rename/close/lock/delete preflight (advisory)."""
    try:
        state = _resolve_state(args)
    except VersionOperationError as exc:
        return _fail(str(exc))

    request = VersionOperationRequest(
        operation=args.op,
        state=state,
        new_name=getattr(args, "new_name", None),
        confirmation=getattr(args, "confirm", None),
        allow_open_issues=bool(getattr(args, "allow_open_issues", False)),
        historical_protected=bool(getattr(args, "historical_protected", False)),
    )
    decision = decide_version_operation(request)

    if bool(getattr(args, "as_json", False)):
        _print_json(decision.as_dict())
        return 0 if decision.allowed else 1

    verdict = "ALLOWED" if decision.allowed else "BLOCKED"
    print(f"{verdict}: {decision.operation} version #{decision.version_id}")
    if decision.warnings:
        print(f"  warnings: {', '.join(decision.warnings)}")
    if decision.allowed:
        print(f"  REST step    : {decision.rest_step}")
        print(f"  operator step: {decision.operator_ui_step}")
    else:
        print(f"  blocked: {', '.join(decision.blocked_reasons)}")
        if not decision.confirmation_satisfied:
            print(f"  required --confirm token: {decision.required_confirmation}")
    return 0 if decision.allowed else 1


__all__ = (
    "cmd_redmine_version_list_open_leaf",
    "cmd_redmine_version_preflight",
    "confirmation_token_for",
)
