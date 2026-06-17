"""Command handler for the workspace-defaults command family.

Split out of ``application/commands.py`` (Redmine #12142). ``commands.py``
re-exports ``cmd_workspace_defaults`` so existing imports / patch targets keep
working. The workspace register/list/inspect handlers stay in ``commands.py``
for a later wave. Behavior-preserving: the handler body (with its lazy local
import) is moved verbatim.
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
