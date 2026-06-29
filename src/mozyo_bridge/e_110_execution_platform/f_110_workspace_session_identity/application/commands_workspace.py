"""Command handlers for the workspace command family (defaults + list).

Split out of ``application/commands.py`` (Redmine #12142). ``commands.py``
re-exports these so existing imports / patch targets keep working.
``cmd_workspace_defaults`` moved first (#12142); the read-only
``cmd_workspace_list`` inventory surface was carried here next as part of the
``commands.py`` decomposition (Redmine #12749 / #12638 / #12785) — the "later
wave" the original docstring noted. The ``workspace register`` / ``workspace
inspect`` handlers stay in ``commands.py`` for now (residual to #12638 / #12785).
Behavior-preserving: handler bodies (with their lazy local imports) are moved
verbatim.
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
