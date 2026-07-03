"""Command handlers for the whole workspace command family.

Split out of ``application/commands.py`` (Redmine #12142). ``commands.py``
re-exports these so existing imports / patch targets keep working.
``cmd_workspace_defaults`` moved first (#12142); the read-only
``cmd_workspace_list`` / ``cmd_workspace_inspect`` identity surfaces and then the
``cmd_workspace_register`` write surface were carried here as part of the
``commands.py`` decomposition (Redmine #12749 / #12638 / #12785) — the "later
wave" the original docstring noted. The full ``workspace`` command family now
lives here. Behavior-preserving: handler bodies (with their lazy local imports)
are moved verbatim.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mozyo_bridge.application.commands_common import repo_root_from_args


def cmd_workspace_defaults(args: argparse.Namespace) -> int:
    """Render or check the workspace-local Redmine default-project snippet.

    Operates on the workspace at ``--repo`` (default cwd). The single
    source is ``<repo>/.mozyo-bridge/workspace-defaults.yaml`` and the
    generated output is whatever target(s) the YAML declares (default:
    ``.mozyo-bridge/redmine-defaults.md``). ``--check`` re-renders in
    memory and fails on drift; without ``--check`` the rendered output
    is written to disk.
    """
    from mozyo_bridge.workspace_defaults import (
        collect_render_results,
        write_render_results,
    )

    repo_root = repo_root_from_args(args)
    results = collect_render_results(repo_root)
    check_only = bool(getattr(args, "check", False))

    def _relative(path: Path) -> str:
        try:
            return path.relative_to(repo_root).as_posix()
        except ValueError:
            return path.as_posix()

    if check_only:
        drifted = [result for result in results if result.drift]
        if drifted:
            for result in drifted:
                print(
                    f"{_relative(result.output_path)} is {result.reason}; rerun "
                    f"`mozyo-bridge workspace-defaults` (without --check, from the repo root) to regenerate.",
                    file=sys.stderr,
                )
            return 1
        for result in results:
            print(f"{_relative(result.output_path)} is up to date")
        return 0

    written = write_render_results(results)
    for path in written:
        print(_relative(path))
    return 0


def cmd_workspace_list(args: argparse.Namespace) -> int:
    """List registered workspaces from the home registry (#11429). Read-only."""
    from mozyo_bridge.workspace_registry import list_workspaces, registry_path

    records = list_workspaces()
    if getattr(args, "as_json", False):
        import json as _json

        payload = {
            "registry_path": str(registry_path()),
            "workspaces": [record.as_payload() for record in records],
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if not records:
        print(
            f"no workspaces registered in {registry_path()} "
            "(run `mozyo-bridge workspace register` from a workspace root)"
        )
        return 0
    print("SESSION\tNAME\tPATH\tLAST_SEEN")
    for record in records:
        print(
            f"{record.canonical_session}\t{record.project_name}\t"
            f"{record.display_path}\t{record.last_seen or '-'}"
        )
    return 0


def cmd_workspace_inspect(args: argparse.Namespace) -> int:
    """Show how this workspace's identity resolves (#11429). Read-only.

    Surfaces all three identity layers side by side — home-registry row,
    workspace-local anchor, and the path-derived fallback — plus the
    effective resolution, so registry/anchor drift is visible before it
    bites a handoff gate.
    """
    from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.session_naming import derive_session_name as _derive
    from mozyo_bridge.workspace_registry import (
        ANCHOR_LEGACY_RELATIVE,
        ANCHOR_RELATIVE,
        anchor_path,
        anchor_resolution,
        legacy_anchor_path,
        load_workspace_by_path,
        read_anchor,
        registry_path,
        resolve_canonical_session,
    )

    repo_root = repo_root_from_args(args)
    record = load_workspace_by_path(repo_root)
    anchor = read_anchor(repo_root)
    anchor_names = anchor_resolution(repo_root)
    derived = _derive(repo_root)
    resolved = resolve_canonical_session(repo_root)

    if getattr(args, "as_json", False):
        import json as _json

        payload = {
            "repo_root": str(resolved.repo_root),
            "registry_path": str(registry_path()),
            "anchor_path": str(anchor_path(resolved.repo_root)),
            "anchor_legacy_path": str(legacy_anchor_path(resolved.repo_root)),
            "anchor_name_state": (
                "both"
                if anchor_names.both_exist
                else "legacy"
                if anchor_names.using_legacy
                else "new"
                if anchor_names.new_exists
                else "none"
            ),
            "registered": record.as_payload() if record else None,
            "anchor": anchor,
            "derived_fallback": {
                "name": derived.name,
                "source": derived.source,
                "identifier": derived.identifier,
            },
            "resolved": {
                "name": resolved.name,
                "source": resolved.source,
                "workspace_id": resolved.workspace_id,
            },
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print(f"repo_root: {resolved.repo_root}")
    print(f"resolved session: {resolved.name} (source: {resolved.source})")
    if record:
        print(
            f"registry: {record.canonical_session} "
            f"(workspace_id {record.workspace_id}, last_seen {record.last_seen or '-'})"
        )
    else:
        print(f"registry: not registered in {registry_path()}")
    if anchor:
        anchor_loc = (
            legacy_anchor_path(resolved.repo_root)
            if anchor_names.using_legacy
            else anchor_path(resolved.repo_root)
        )
        print(
            f"anchor: {anchor['canonical_session']} "
            f"(workspace_id {anchor['workspace_id']}) at {anchor_loc}"
        )
    else:
        print(f"anchor: none at {anchor_path(resolved.repo_root)}")
    print(f"derived fallback: {derived.name} (source: {derived.source})")
    if anchor_names.both_exist:
        print(
            f"warning: both {ANCHOR_RELATIVE.as_posix()} and "
            f"{ANCHOR_LEGACY_RELATIVE.as_posix()} exist; the new name is "
            f"authoritative — remove the legacy "
            f"{ANCHOR_LEGACY_RELATIVE.as_posix()} (no silent merge)."
        )
    elif anchor_names.using_legacy:
        print(
            f"warning: anchor uses the legacy name "
            f"{ANCHOR_LEGACY_RELATIVE.as_posix()}; run `mozyo-bridge workspace "
            f"register` to migrate it to {ANCHOR_RELATIVE.as_posix()}."
        )
    if record and anchor and record.workspace_id != anchor["workspace_id"]:
        print(
            "warning: registry row and anchor disagree on workspace_id; "
            "re-run `mozyo-bridge workspace register` to reconcile "
            "(the anchor wins)."
        )
    return 0


def cmd_workspace_register(args: argparse.Namespace) -> int:
    """Register (or refresh) this workspace in the home registry (#11429).

    The explicit, manual write surface of the workspace registry (smart
    ``init`` also registers via the same :func:`register_workspace` API since
    Redmine #11427): upserts the registry
    row in ``${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/registry.sqlite`` and
    rewrites the workspace-local anchor
    (``<repo>/.mozyo-bridge/workspace-anchor.json``; the legacy
    ``workspace.json`` stays readable but is never written). Idempotent: re-running keeps
    the existing workspace id and canonical session name; when the home
    registry was lost, the anchor restores the same identity. The canonical
    session name is derived from the path only on first registration.

    Registration refuses to relocate an already-registered identity's
    canonical_path onto a linked git worktree, and refuses to move it off a
    still-live checkout without ``--move`` (Redmine #13152), so a worktree /
    clone cannot hijack the coordinator lane.
    """
    from mozyo_bridge.workspace_registry import register_workspace

    repo_root = repo_root_from_args(args)
    result = register_workspace(
        repo_root,
        project_name=getattr(args, "name", None),
        allow_move=bool(getattr(args, "move", False)),
    )
    if getattr(args, "as_json", False):
        import json as _json

        payload = {
            "outcome": result.outcome,
            "registry_path": str(result.registry_path),
            "anchor_path": str(result.anchor_path),
            "workspace": result.record.as_payload(),
            "notes": list(result.notes),
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    record = result.record
    print(
        f"{result.outcome}: workspace '{record.project_name}' "
        f"({record.display_path})"
    )
    print(f"  workspace_id:      {record.workspace_id}")
    print(f"  canonical_session: {record.canonical_session}")
    if record.preset:
        version = f" {record.preset_version}" if record.preset_version else ""
        print(f"  preset:            {record.preset}{version}")
    print(f"  registry:          {result.registry_path}")
    print(f"  anchor:            {result.anchor_path}")
    for note in result.notes:
        print(f"  note: {note}")
    return 0
