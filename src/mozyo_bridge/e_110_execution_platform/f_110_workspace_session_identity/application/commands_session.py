"""Command handlers for the session command family.

Split out of ``application/commands.py`` as part of the ``commands.py``
decomposition (Redmine #12749 / #12638 / #12785). The read-only session-identity surfaces are carried here, into the
``f_110_workspace_session_identity`` bounded context alongside the
``cli_session`` registrar and ``commands_workspace`` handlers: first
``cmd_session_list`` (cross-workspace inventory), then ``cmd_session_name`` /
``cmd_session_boundary_prompt`` / ``cmd_session_pane_decision`` (Redmine #12749 /
#12638 / #12785). ``commands.py`` re-exports them so the
``mozyo_bridge.application.commands.cmd_session_*`` identities (the ``cli`` /
``cli_session`` parser registrar imports and the ``test_session_inventory`` /
``test_session_boundary`` / ``test_workspace_registry`` test imports) keep their
``func.__name__``. The write-surface ``cmd_session_vscode_settings`` stays in
``commands.py`` for now (residual to #12638 / #12785). Behavior-preserving: the
handler bodies (with their lazy local imports) are moved verbatim.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mozyo_bridge.application.commands_common import repo_root_from_args
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    build_execution_root,
)
from mozyo_bridge.shared.errors import die
from mozyo_bridge.workspace_registry import resolve_canonical_session


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


def cmd_session_name(args: argparse.Namespace) -> int:
    """Print the tmux session name for the repo (Redmine #10796, #11429).

    Resolves the repo root from ``--repo`` (default cwd) and resolves the
    session name registry-first: the canonical session name registered in the
    home registry (or the workspace-local anchor) wins; a never-registered
    workspace falls back to deriving a collision-safe ASCII name (preferring
    the workspace-defaults Redmine identifier, otherwise a hash-suffixed
    repo-path fallback). Prints it on a single line for shell use. ``--json``
    emits the name plus its resolution source and workspace id. Read-only:
    does not touch tmux, Redmine, or write to disk.
    """
    repo_root = repo_root_from_args(args)
    result = resolve_canonical_session(repo_root)
    if getattr(args, "as_json", False):
        import json as _json

        payload = {
            "name": result.name,
            "source": result.source,
            "identifier": result.identifier,
            "repo_root": str(result.repo_root),
            "workspace_id": result.workspace_id,
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(result.name)
    return 0


def cmd_session_boundary_prompt(args: argparse.Namespace) -> int:
    """Emit the compact next-session boundary prompt (Redmine #12122).

    Renders a pasteable prompt a Codex session hands the operator / next Codex
    session so the next session re-anchors from the durable Redmine journal plus
    repo / execution root, not from pane scrollback or window/session naming.
    The repo is referenced by its **portable** canonical session name (resolved
    from ``--repo``); the absolute repo root and execution-root workdir appear
    only in ``--json`` so a pasted prompt never carries a private absolute path
    (``vibes/docs/rules/public-private-boundary.md``). Pure / read-only towards
    tmux, git, and Redmine.
    """
    from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.session_boundary import (
        BoundaryPrompt,
        SessionBoundaryError,
        build_boundary_prompt,
        classify_boundary,
    )

    repo_root = repo_root_from_args(args)
    session = resolve_canonical_session(repo_root)

    execution_root = None
    exec_root_arg = getattr(args, "execution_root", None)
    if exec_root_arg:
        workdir_abs = str(Path(exec_root_arg).expanduser().resolve())
        execution_root = build_execution_root(
            workdir_abs, repo_root_abs=str(repo_root)
        )

    signals = tuple(getattr(args, "signal", None) or ())
    try:
        if signals:
            # Validate the signal vocabulary up front so a typo fails loudly
            # instead of being pasted into a next-session prompt.
            classify_boundary(signals)
        prompt = BoundaryPrompt(
            issue=str(args.issue),
            journal=str(args.journal),
            repo_pointer=session.name,
            parent_issue=getattr(args, "parent", None),
            commit=getattr(args, "commit", None),
            target_lane=getattr(args, "target_lane", None),
            execution_root=execution_root,
            gate_state=getattr(args, "gate", None),
            verification_state=getattr(args, "verification", None),
            residual_risks=tuple(getattr(args, "residual", None) or ()),
            pending_action=getattr(args, "pending_action", None),
            next_actor=getattr(args, "next_actor", None),
            signals=signals,
        )
    except SessionBoundaryError as exc:
        die(str(exc))

    if getattr(args, "as_json", False):
        import json as _json

        payload = prompt.to_dict()
        # The structured outcome is allowed to carry runtime absolutes (the
        # execution_root.workdir already does); add the repo root here so an
        # automation consumer can resolve the checkout. Pasteable text never
        # gets these.
        payload["repo_root"] = str(repo_root)
        payload["prompt_markdown"] = build_boundary_prompt(prompt)
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print(build_boundary_prompt(prompt))
    return 0


def cmd_session_pane_decision(args: argparse.Namespace) -> int:
    """Decide the guarded Claude-pane lifecycle action (Redmine #12122).

    Encodes the parent US (#12113) acceptance criteria: default lean to a new
    pane, reuse only same-lane, orphan is non-destructive, and kill/discard is
    blocked whenever unfinished durable state is present (dirty diff, running
    process, pending approval, unrecorded journal) or no owner kill approval has
    been recorded. Exits non-zero (3) when the decision is ``blocked`` so an
    operator's ``&&`` chain cannot silently proceed to a kill. Pure / read-only.
    """
    from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.session_boundary import (
        PaneLifecycleState,
        SessionBoundaryError,
        decide_pane_lifecycle,
    )

    state = PaneLifecycleState(
        requested=getattr(args, "requested", None) or "new",
        same_lane=getattr(args, "same_lane", False),
        dirty_diff=getattr(args, "dirty_diff", False),
        running_process=getattr(args, "running_process", False),
        pending_approval=getattr(args, "pending_approval", False),
        unrecorded_journal=getattr(args, "unrecorded_journal", False),
        owner_approved_kill=getattr(args, "owner_approved_kill", False),
    )
    try:
        decision = decide_pane_lifecycle(state)
    except SessionBoundaryError as exc:
        die(str(exc))

    if getattr(args, "as_json", False):
        import json as _json

        print(_json.dumps(decision.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"decision: {decision.decision}")
        if decision.blockers:
            print("blockers: " + ", ".join(decision.blockers))
        print(f"rationale: {decision.rationale}")
    return 3 if decision.is_blocked else 0
