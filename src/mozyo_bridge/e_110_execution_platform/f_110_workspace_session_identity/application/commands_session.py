"""Command handlers for the session command family.

Split out of ``application/commands.py`` as part of the ``commands.py``
decomposition (Redmine #12749 / #12638 / #12785). The read-only
``cmd_session_list`` cross-workspace inventory surface is carried here first,
into the ``f_110_workspace_session_identity`` bounded context alongside the
``cli_session`` registrar and ``commands_workspace`` handlers. ``commands.py``
re-exports it so ``mozyo_bridge.application.commands.cmd_session_list`` (the
``cli`` / ``cli_session`` parser registrar import and the
``test_session_inventory`` import) keeps its identity and ``func.__name__``.
The remaining ``cmd_session_*`` handlers (name / vscode-settings /
boundary-prompt / pane-decision) stay in ``commands.py`` for now (residual to
#12638 / #12785). Behavior-preserving: the handler body (with its lazy local
import) is moved verbatim.
"""
from __future__ import annotations

import argparse
import sys


def cmd_session_list(args: argparse.Namespace) -> int:
    """Cross-workspace session inventory (Redmine #11422).

    Lists every tmux pane folded by ``pane_id`` (Redmine #11628: grouped
    sessions are views of one agent, not extra rows) together with the
    workspace identity its repo root resolves to (registry → anchor →
    derivation, NFC-normalized per Redmine #11625). The live tmux runtime is
    the source of truth; each runtime listing refreshes the SQLite cache in
    ``${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/inventory.sqlite``, and when tmux
    is unavailable the cache is served instead, explicitly marked stale.
    Read-only towards tmux and the workspace registry.
    """
    from mozyo_bridge.session_inventory import take_inventory

    snapshot = take_inventory()
    if getattr(args, "as_json", False):
        import json as _json

        print(
            _json.dumps(
                snapshot.as_payload(), ensure_ascii=False, indent=2, sort_keys=True
            )
        )
        return 0
    for note in snapshot.notes:
        print(f"note: {note}", file=sys.stderr)
    if snapshot.stale:
        print(
            f"stale: cached snapshot from {snapshot.collected_at or 'unknown'} "
            "(tmux runtime unavailable)",
            file=sys.stderr,
        )
    print(
        "PANE\tSESSION\tWINDOW\tKIND\tACTIVITY\tPROCESS\tWORKSPACE\t"
        "REPO_ROOT\tOTHER_VIEWS"
    )
    for record in snapshot.records:
        workspace = record.workspace
        workspace_label = "-"
        if workspace is not None:
            workspace_label = workspace.project_name or workspace.canonical_session
        other_views = ",".join(
            view.session for view in record.views if not view.canonical
        )
        activity = record.activity or {}
        print(
            "\t".join(
                [
                    record.pane_id or "-",
                    record.session or "-",
                    record.window_name or "-",
                    record.agent_kind,
                    activity.get("state") or "unknown",
                    record.process or "-",
                    workspace_label,
                    record.repo_root or "-",
                    other_views or "-",
                ]
            )
        )
    return 0
