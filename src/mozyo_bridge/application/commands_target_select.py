"""Shared semantic-target resolver for ``handoff send`` / ``message`` (Redmine #12663).

Both the cross-agent ``handoff send`` rail and the operator / ticketless
``message`` rail used to require a hand-copied ``%pane`` id; this module is the
single resolver they share so "send to the Codex gateway for this repo/project"
is expressible by *semantic route identity* (role + session + repo root +
optional project scope) instead of a volatile pane id. It wires the pure
:func:`~mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.target_selector.select_target`
policy to the live candidate discovery and the shared Unicode path normaliser,
and fails closed (printing classified diagnostics, then :func:`die`) on a zero /
many / cross-workspace-Claude selection so nothing is ever sent to a guessed
pane.

The resolver only *narrows* to a single pane and returns its id plus the
concrete repo root it matched; the caller still passes ``--target-repo`` /
``--target-project`` into ``orchestrate_handoff`` so the existing identity gates
re-validate the chosen pane. The selector therefore never weakens those gates.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    TargetCandidate,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.target_selector import (
    TargetSelection,
    TargetSelectorQuery,
    render_selection_diagnostics,
    select_target,
)
from mozyo_bridge.shared.errors import die
from mozyo_bridge.shared.paths import normalize_path_unicode

# `--target-repo auto` is the explicit-pane inference token on the handoff rail;
# in semantic-selection mode there is no pre-chosen pane to infer from, so `auto`
# (like an omitted flag) means "the sender's own repo".
REPO_AUTO = "auto"


@dataclass(frozen=True)
class SelectedTarget:
    """A uniquely resolved semantic target ready to feed the existing gates."""

    pane_id: str
    repo_root: Optional[str]
    project_scope: Optional[str]
    session: Optional[str]
    selection: TargetSelection


def discover_all_candidates() -> list[TargetCandidate]:
    """Discover every classified target candidate (no role/session pre-filter).

    Unlike ``commands._agents_target_candidates`` (which narrows by
    ``args.agent`` / ``args.session``), the selector wants the full set so its
    pure narrowing can report *why* nothing matched. Routed through the same
    :class:`ResolveAgentTargetsUseCase` / ``AgentDiscoveryPort`` seam as
    ``agents targets`` so the two never drift. Patchable as
    ``commands_target_select.discover_all_candidates`` for tests.
    """
    from mozyo_bridge.application.agent_discovery_port import LiveAgentDiscovery
    from mozyo_bridge.application.commands_agents import ResolveAgentTargetsUseCase

    return ResolveAgentTargetsUseCase(LiveAgentDiscovery()).resolve(
        agent_filter=None, session_filter=None
    )


def resolve_expected_repo(repo_arg: Optional[str], *, sender_cwd: str) -> Optional[str]:
    """Resolve the selection's expected repo root to a concrete path.

    An explicit ``--target-repo <path>`` resolves to its absolute root; an
    omitted flag or ``auto`` defaults to the *sender's* own workspace root (the
    "send to the gateway for this repo" UX). ``None`` when no marker is
    reachable — the selector then treats repo as "any" and stays fail-closed on
    ambiguity downstream.
    """
    from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.project_discovery import (
        resolve_workspace_root,
    )

    if repo_arg and repo_arg != REPO_AUTO:
        return str(Path(repo_arg).expanduser().resolve())
    return resolve_workspace_root(sender_cwd)


def select_semantic_target(
    *,
    role: str,
    repo: Optional[str],
    session: Optional[str],
    project: Optional[str],
    sender_cwd: str,
    sender_session: Optional[str],
    candidates: Optional[Sequence[TargetCandidate]] = None,
) -> SelectedTarget:
    """Resolve a semantic route identity to one pane, or fail closed (Redmine #12663).

    Discovers candidates (unless injected), resolves the expected repo root,
    runs the pure :func:`select_target` policy with the shared Unicode path
    normaliser, and — on any non-``resolved`` outcome — prints the classified
    candidate diagnostics to stderr and :func:`die`\\ s without sending. On
    success returns the chosen ``pane_id`` and the concrete ``repo_root`` it
    matched so the caller can pass that root straight into the unchanged
    ``--target-repo`` identity gate.
    """
    pool = list(candidates) if candidates is not None else discover_all_candidates()
    expected_repo = resolve_expected_repo(repo, sender_cwd=sender_cwd)
    query = TargetSelectorQuery(
        role=role,
        repo_root=expected_repo,
        session=session,
        project_scope=project,
        sender_session=sender_session,
    )
    selection = select_target(pool, query, normalize=normalize_path_unicode)
    if not selection.resolved:
        print(render_selection_diagnostics(selection), file=sys.stderr)
        die(
            "semantic target selection failed "
            f"({selection.reason}); no message was sent. "
            "Resolve the candidates above (or use an explicit `%pane` as a "
            "debug escape hatch)."
        )
        raise AssertionError("unreachable")
    return SelectedTarget(
        pane_id=selection.pane_id or "",
        repo_root=expected_repo,
        project_scope=project,
        session=session,
        selection=selection,
    )


def _sender_context() -> tuple[str, Optional[str]]:
    """The sender's own cwd + tmux session for the selection query."""
    import os

    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver import (
        current_session_name,
    )

    return os.getcwd(), current_session_name()


def apply_handoff_selection(args) -> None:
    """Resolve ``handoff send --select`` into a concrete ``%pane`` (Redmine #12663).

    Writes the resolved pane id back to ``args.target`` and the matched concrete
    repo root to ``args.target_repo`` so the unchanged ``orchestrate_handoff``
    identity gates re-validate the chosen pane — the selector narrows, the gates
    still enforce. No-op unless ``--select`` is set; mutually exclusive with an
    explicit ``--target``.
    """
    if not getattr(args, "select", False):
        return
    if getattr(args, "target", None):
        die(
            "--select resolves the target pane semantically and is mutually "
            "exclusive with an explicit --target; drop one of them."
        )
    sender_cwd, sender_session = _sender_context()
    selected = select_semantic_target(
        role=getattr(args, "to", None),
        repo=getattr(args, "target_repo", None),
        session=getattr(args, "target_session", None),
        project=getattr(args, "target_project", None),
        sender_cwd=sender_cwd,
        sender_session=sender_session,
    )
    args.target = selected.pane_id
    if selected.repo_root:
        args.target_repo = selected.repo_root


def resolve_message_target(args) -> str:
    """Resolve the ``message`` target pane, semantically under ``--select-role`` (#12663).

    Exactly one of the positional ``target`` or ``--select-role`` must be given.
    With ``--select-role`` the operator/ticketless message names the receiver by
    role + repo (+ optional session / project), reusing the same fail-closed
    selector ``handoff send --select`` uses. Without it the legacy explicit
    ``target`` resolution is unchanged.
    """
    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver import (
        resolve_target,
    )

    select_role = getattr(args, "select_role", None)
    explicit_target = getattr(args, "target", None)
    if select_role:
        if explicit_target:
            die(
                "--select-role resolves the target pane semantically and is "
                "mutually exclusive with a positional target; drop one of them."
            )
        sender_cwd, sender_session = _sender_context()
        return select_semantic_target(
            role=select_role,
            repo=getattr(args, "target_repo", None),
            session=getattr(args, "target_session", None),
            project=getattr(args, "target_project", None),
            sender_cwd=sender_cwd,
            sender_session=sender_session,
        ).pane_id
    if not explicit_target:
        die(
            "message requires a target: pass a positional `%pane` / agent label, "
            "or `--select-role {claude,codex}` to resolve it semantically."
        )
    return resolve_target(explicit_target)
