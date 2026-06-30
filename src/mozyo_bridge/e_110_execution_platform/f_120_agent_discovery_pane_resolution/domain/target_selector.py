"""Fail-closed semantic target selector over discovered panes (Redmine #12663).

Multi-column cockpit routing kept forcing the operator / LLM to read ``agents
targets`` and hand-copy a volatile ``%pane`` id, which once sent a handoff to a
Claude pane instead of the intended Codex gateway (#12659). This module turns the
*semantic route identity* — receiver role + session + repo root + optional project
scope — into a single fail-closed selection over the already-classified
:class:`~mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery.TargetCandidate`
list, so the standard ``handoff send`` / ``message`` UX never has to address a
pane id by hand. It is the implementation of
``vibes/docs/logics/ticketless-project-gateway-runtime-ux.md`` "意味的ターゲット解決
の要件": exactly one candidate selects; zero or many fail closed with classified
diagnostics and resolution guidance, never a silent pick of the active pane.

Pure: no tmux / git / registry I/O. The caller resolves the candidate list and
the path normaliser and hands them in, so this stays a unit-testable policy. It is
deliberately *non-weakening*: the selector only narrows the candidate set and
returns a ``pane_id``; the existing ``--target-repo`` / ``--target-project``
identity gates in ``orchestrate_handoff`` still re-validate the chosen pane
downstream, so a selector match can never substitute for those gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    AGENT_KIND_CLAUDE,
    AGENT_KIND_CODEX,
    CONFIDENCE_STRONG,
    TargetCandidate,
)

# The two roles a semantic selection may target. ``unknown`` is in
# ``AGENT_KINDS`` for classification but is never a selectable receiver.
SELECTABLE_ROLES = frozenset({AGENT_KIND_CLAUDE, AGENT_KIND_CODEX})

# --- Selection outcome statuses ----------------------------------------------
# `resolved` is the only success; every other status is fail-closed (no send).
SELECT_RESOLVED = "resolved"
SELECT_INVALID_ROLE = "invalid_role"
SELECT_NO_CANDIDATE = "no_candidate"
SELECT_AMBIGUOUS = "ambiguous"
SELECT_CROSS_WORKSPACE_CLAUDE = "cross_workspace_claude"

# --- Narrowing-stage codes for a `no_candidate` outcome ----------------------
# Names the first filter that emptied the surviving set, so the diagnostic can
# say *why* nothing matched instead of a bare "0 candidates".
STAGE_ROLE = "role"
STAGE_BINDING = "binding"
STAGE_REPO = "repo"
STAGE_SESSION = "session"
STAGE_PROJECT = "project"


def candidate_binds_role(candidate: TargetCandidate, role: str) -> bool:
    """True when ``candidate`` *strongly, unambiguously* resolves to ``role``.

    Mirrors :meth:`PreflightTarget.binds_receiver` (the queue-enter explicit-pane
    gate predicate): a weak / process-inferred signal or an ambiguous resolution
    is never a selectable target, so the selector cannot auto-pick a pane whose
    role is in doubt. Keeping one predicate here and at the handoff preflight
    means the two never drift.
    """
    return (
        candidate.role == role
        and candidate.confidence == CONFIDENCE_STRONG
        and not candidate.ambiguous
    )


@dataclass(frozen=True)
class TargetSelectorQuery:
    """Semantic route identity to resolve to exactly one pane (Redmine #12663).

    ``role`` is required (``claude`` / ``codex``). ``repo_root`` narrows to a
    checkout root (already resolved to a concrete path by the caller; ``None``
    means "any repo", discouraged but allowed when the sender has no resolvable
    workspace). ``session`` and ``project_scope`` are optional further
    discriminators. ``sender_session`` is the sender's own tmux session, carried
    so the cross-workspace ``--to claude`` refusal can be modelled purely here
    (a foreign workspace's Claude pane must be reached via its Codex gateway,
    never addressed directly).
    """

    role: str
    repo_root: str | None = None
    session: str | None = None
    project_scope: str | None = None
    sender_session: str | None = None


@dataclass(frozen=True)
class TargetSelection:
    """Result of a fail-closed semantic selection.

    ``status`` is one of the ``SELECT_*`` codes; ``pane_id`` / ``selected`` are
    populated only when ``status == SELECT_RESOLVED``. ``matches`` is the final
    surviving candidate set (the basis of an ``ambiguous`` diagnostic), and
    ``role_matches`` is the role-filtered set before repo / session / project
    narrowing (so the ``no_candidate`` diagnostic can show what *did* exist).
    ``narrowing_stage`` names the filter that emptied the set for
    ``no_candidate``. ``reason`` is a short machine token; ``detail`` is the
    human-facing resolution guidance.
    """

    status: str
    query: TargetSelectorQuery
    pane_id: str | None
    selected: TargetCandidate | None
    matches: tuple[TargetCandidate, ...]
    role_matches: tuple[TargetCandidate, ...]
    narrowing_stage: str | None
    reason: str
    detail: str

    @property
    def resolved(self) -> bool:
        return self.status == SELECT_RESOLVED


def _same_repo(
    candidate_root: str | None,
    expected_root: str | None,
    normalize: Callable[[str], str],
) -> bool:
    """Normalised repo-root equality (fail-closed when either side is unknown)."""
    if not candidate_root or not expected_root:
        return False
    return normalize(candidate_root) == normalize(expected_root)


def select_target(
    candidates: Iterable[TargetCandidate],
    query: TargetSelectorQuery,
    *,
    normalize: Callable[[str], str] = lambda path: path,
) -> TargetSelection:
    """Resolve ``query`` to exactly one candidate pane, fail-closed (Redmine #12663).

    Narrowing pipeline, each stage applied to the survivors of the last:

    1. ``role`` — keep candidates whose classified role equals ``query.role``.
    2. ``binding`` — keep only *strongly bound* role matches
       (:func:`candidate_binds_role`); a weak / ambiguous pane is never
       auto-selected.
    3. ``repo`` — when ``query.repo_root`` is set, keep panes whose repo root
       normalises equal to it (``--target-repo`` parity, never weakened here).
    4. ``session`` — when ``query.session`` is set, keep panes in that session.
    5. ``project_scope`` — when set, keep panes carrying that adopted project
       scope.

    Exactly one survivor → ``SELECT_RESOLVED``. Zero → ``SELECT_NO_CANDIDATE``
    tagged with the stage that emptied the set. More than one →
    ``SELECT_AMBIGUOUS``. A uniquely resolved Claude pane in a different tmux
    session than ``query.sender_session`` → ``SELECT_CROSS_WORKSPACE_CLAUDE``
    (route via the target repo's Codex gateway). ``normalize`` is the path
    identity normaliser (the caller injects the shared Unicode normaliser); the
    default keeps the function pure for tests that pass canonical roots.
    """
    role = query.role
    if role not in SELECTABLE_ROLES:
        return TargetSelection(
            status=SELECT_INVALID_ROLE,
            query=query,
            pane_id=None,
            selected=None,
            matches=(),
            role_matches=(),
            narrowing_stage=None,
            reason="invalid_role",
            detail=(
                f"role must be one of {sorted(SELECTABLE_ROLES)}; got {role!r}"
            ),
        )

    all_candidates = tuple(candidates)
    role_matched = tuple(c for c in all_candidates if c.role == role)
    bound = tuple(c for c in role_matched if candidate_binds_role(c, role))
    repo_matched = (
        bound
        if query.repo_root is None
        else tuple(
            c for c in bound if _same_repo(c.repo_root, query.repo_root, normalize)
        )
    )
    session_matched = (
        repo_matched
        if query.session is None
        else tuple(c for c in repo_matched if c.session == query.session)
    )
    project_matched = (
        session_matched
        if query.project_scope is None
        else tuple(
            c
            for c in session_matched
            if (c.project_scope or "") == query.project_scope
        )
    )

    if len(project_matched) == 1:
        chosen = project_matched[0]
        if (
            role == AGENT_KIND_CLAUDE
            and query.sender_session
            and chosen.session
            and chosen.session != query.sender_session
        ):
            return TargetSelection(
                status=SELECT_CROSS_WORKSPACE_CLAUDE,
                query=query,
                pane_id=None,
                selected=chosen,
                matches=(chosen,),
                role_matches=role_matched,
                narrowing_stage=None,
                reason="cross_workspace_claude",
                detail=(
                    "the resolved Claude pane lives in a different tmux session "
                    f"(sender_session={query.sender_session!r} "
                    f"target_session={chosen.session!r}). Addressing a foreign "
                    "workspace's Claude pane directly bypasses its audit "
                    "boundary; route through that repo's Codex gateway instead "
                    "(select `--to codex` for the same repo and ask it to perform "
                    "the local Claude handoff)."
                ),
            )
        return TargetSelection(
            status=SELECT_RESOLVED,
            query=query,
            pane_id=chosen.pane_id,
            selected=chosen,
            matches=(chosen,),
            role_matches=role_matched,
            narrowing_stage=None,
            reason="resolved",
            detail=f"selected pane {chosen.pane_id} ({role})",
        )

    if len(project_matched) == 0:
        stage, detail = _no_candidate_detail(
            query,
            role_matched=role_matched,
            bound=bound,
            repo_matched=repo_matched,
            session_matched=session_matched,
        )
        return TargetSelection(
            status=SELECT_NO_CANDIDATE,
            query=query,
            pane_id=None,
            selected=None,
            matches=(),
            role_matches=role_matched,
            narrowing_stage=stage,
            reason=f"no_candidate:{stage}",
            detail=detail,
        )

    return TargetSelection(
        status=SELECT_AMBIGUOUS,
        query=query,
        pane_id=None,
        selected=None,
        matches=project_matched,
        role_matches=role_matched,
        narrowing_stage=None,
        reason="ambiguous",
        detail=_ambiguous_detail(query, project_matched),
    )


def _no_candidate_detail(
    query: TargetSelectorQuery,
    *,
    role_matched: Sequence[TargetCandidate],
    bound: Sequence[TargetCandidate],
    repo_matched: Sequence[TargetCandidate],
    session_matched: Sequence[TargetCandidate],
) -> tuple[str, str]:
    """Name the stage that emptied the set and give a concrete next action."""
    role = query.role
    if not role_matched:
        return (
            STAGE_ROLE,
            f"no {role} pane is discoverable; start the {role} lane (or check "
            "`mozyo-bridge agents targets`) before selecting it.",
        )
    if not bound:
        return (
            STAGE_BINDING,
            f"{len(role_matched)} {role}-looking pane(s) exist but none resolve "
            "to a strong, unambiguous role binding; stamp the pane's "
            "`@mozyo_agent_role` (cockpit-managed pane) or disambiguate before "
            "selecting.",
        )
    if not repo_matched:
        return (
            STAGE_REPO,
            f"{len(bound)} bound {role} pane(s) exist but none are in repo "
            f"{query.repo_root!r}; pass the correct `--target-repo`, or start the "
            f"{role} gateway for that repo.",
        )
    if not session_matched:
        return (
            STAGE_SESSION,
            f"{len(repo_matched)} {role} pane(s) match the repo but none are in "
            f"session {query.session!r}; drop or correct the session filter.",
        )
    return (
        STAGE_PROJECT,
        f"{len(session_matched)} {role} pane(s) match repo/session but none "
        f"carry project scope {query.project_scope!r}; drop or correct the "
        "project filter.",
    )


def _ambiguous_detail(
    query: TargetSelectorQuery, matches: Sequence[TargetCandidate]
) -> str:
    """Suggest the discriminator that would make the selection unique."""
    discriminators: list[str] = []
    if query.session is None and len({c.session for c in matches}) > 1:
        discriminators.append("--target-session")
    if query.project_scope is None and len({c.project_scope or "" for c in matches}) > 1:
        discriminators.append("--target-project")
    if query.repo_root is None and len({c.repo_root or "" for c in matches}) > 1:
        discriminators.append("--target-repo")
    hint = (
        f" add {' / '.join(discriminators)} to disambiguate"
        if discriminators
        else " the panes share session / repo / project; pick the explicit "
        "`%pane` from `mozyo-bridge agents targets` (debug escape hatch)"
    )
    return (
        f"{len(matches)} {query.role} pane(s) match the selection;"
        f"{hint}."
    )


def render_selection_diagnostics(selection: TargetSelection) -> str:
    """Pure multi-line diagnostic for a fail-closed selection (Redmine #12663).

    Lists the relevant candidate panes (the final matches for an ``ambiguous``
    outcome, else the role-matched set for ``no_candidate``) with the
    disambiguating fields — pane / session / repo / project / lane — and appends
    the resolution guidance. Never prints absolute private paths beyond the
    repo basename, matching the compact ``agents targets`` text exposure.
    """
    lines = [f"semantic target selection {selection.status}: {selection.detail}"]
    shown = selection.matches or selection.role_matches
    if shown:
        lines.append(
            "candidates: PANE\tROLE\tCONF\tAMBIG\tSESSION\tREPO\tPROJECT\tLANE"
        )
        for c in shown:
            lines.append(
                "  "
                + "\t".join(
                    [
                        c.pane_id or "-",
                        c.role,
                        c.confidence,
                        "1" if c.ambiguous else "0",
                        c.session or "-",
                        c.repo_short or "-",
                        c.project_scope or "-",
                        c.lane_id or "-",
                    ]
                )
            )
    else:
        lines.append("candidates: (none discovered for this role)")
    return "\n".join(lines)
