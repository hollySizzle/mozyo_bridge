"""Project-gateway create/adopt canonical lifecycle declaration (Redmine #13811 T2).

The declaration actuator the project-gateway create/adopt path calls to bind a derived
``pgwv1_...`` gateway lane onto the common lane lifecycle / generation API (design #13780
j#78386 §2 / Coordinator Verdict j#78405). It is the *mutating* counterpart to the
read-only ``project-gateway adopt`` decision printer (which stays read-only, never grows a
mutation): the read-only surface answers "adopt this live gateway or launch one?"; this
surface, once a live gateway/worker pair is present, declares the lane's canonical
lifecycle owner row.

Fail-closed, default dry-run:

- The provider-bound slot set is resolved through the SHARED
  :func:`...sublane_adopt_declaration.resolve_declared_pins` — the same raw-multiplicity /
  liveness / startup-attestation gate the issue-lane adopt uses (j#78405 "共通 declaration
  helper でissue/project binding双方をfail-closedに扱い、重複実装しない"). Any
  unreadable / duplicate / stale / unattested / ambiguous live slot is zero-write.
- ``dry_run`` (the default) resolves the pins and produces a plan but writes NOTHING —
  ``declare_lane`` is never called, so the read-only-by-default contract holds and an
  ``--execute`` surface must be opted into for any mutation.
- On execute, the write goes through the common
  :meth:`...lane_declaration.LaneDeclarationStore.declare_lane` with
  ``binding_kind='project_gateway'`` + a canonical full ``project_scope`` (never derived
  here — the caller supplies it) + the resolved ``declared_slots`` + a complete Redmine
  ``DecisionPointer``. An exact-duplicate active declaration is an **idempotent** adopt; a
  divergent / foreign / owner-conflicting row is a zero-write refusal, never an overwrite —
  so a partial write / crash / retry resumes or fails closed, never re-mints authority.

A project-gateway lane owns a **scope, not an issue** — ``issue_id`` is empty on the row —
but the journal that authorizes each declaration is still filed on a real issue, so the
``DecisionPointer`` carries the anchor's ``(issue, journal)`` (the ``DecisionPointer`` R2-F1
contract). This surface never touches the derived route binding (a separate, derivational
正本) and never closes / relaunches a process.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
)
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore
from mozyo_bridge.core.state.lane_lifecycle import (
    BINDING_KIND_PROJECT_GATEWAY,
    DecisionPointer,
    DecisionPointerError,
    LaneLifecycleError,
    LaneLifecycleKey,
    ProcessPinError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E501
    ADOPT_DECL_BAD_ANCHOR,
    ADOPT_DECL_DECLARE_ERROR,
    ADOPT_DECL_DECLARED,
    ADOPT_DECL_NO_ANCHOR,
    ADOPT_DECL_OWNER_CONFLICT,
    ADOPT_DECL_UNRESOLVED_UNIT,
    resolve_declared_pins,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.project_scope import (  # noqa: E501
    path_under_repo_relative,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (  # noqa: E501
    WorkflowRoleAuthorityError,
    project_gateway_lane_id,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    _norm,
)

#: A project-gateway declaration that requires a canonical full ``project_scope``; a caller
#: that omits it addressed no lane and fails closed (the scope is never inferred, j#78386 §6).
PG_DECL_NO_SCOPE = "no_project_scope"
#: A live pane resolved for a declared slot SURFACES a ``project_scope`` stamp that disagrees
#: with the declared scope — a foreign / wrong-stamped-scope process at this lane. Zero-write
#: (Redmine #13811 T2 R2 F2). A secondary raw-row check; the primary fence is the route join.
PG_DECL_SCOPE_STAMP_MISMATCH = "scope_stamp_mismatch"
#: No live project-gateway was adopt-resolvable for the DECLARED canonical route identity
#: (repo_root + full project_scope + role), or the declared identity is incomplete (no adopted
#: project metadata / project_path). The write requires a live gateway whose semantic identity
#: the discovery resolver matched — missing / unresolvable is owner-unbound zero-write (Redmine
#: #13811 T2 R3; design #13780 j#78386 §2 action-time full-scope re-verify).
PG_DECL_ROUTE_UNRESOLVED = "route_identity_unresolved"
#: A live gateway WAS resolved, but its stamped canonical route identity (repo_root /
#: project_scope / project_path) does not exactly match the declaration — a foreign project's
#: gateway, a wrong repo/cwd, or a cross-revision alias-equivalent scope. Zero-write (Redmine
#: #13811 T2 R3; the 48-bit alias fence design j#78386 §2 folded into #13811).
PG_DECL_ROUTE_MISMATCH = "route_identity_mismatch"
#: The dry-run outcome: the live pair resolved and a declaration WOULD apply, but nothing was
#: written (the default surface). ``--execute`` opts into the actual declaration.
PG_DECL_DRY_RUN = "dry_run_plan"


@dataclass(frozen=True)
class ObservedGatewayRoute:
    """The live project-gateway's resolved canonical route identity (Redmine #13811 T2 R3).

    What the semantic discovery resolver (``resolve_launch_or_adopt`` over the live candidate
    list) observed for the DECLARED identity: the adopted gateway pane's stamped ``repo_root``
    / ``project_scope`` / ``project_path`` and its live ``locator``. The declaration exact-
    matches the stamps against the declared canonical identity AND re-checks the live ``cwd``
    is under the canonical project path (the stamp is a projection cache; a stale
    correct-looking stamp never bypasses the live-cwd gate, Redmine #13811 T2 R3 F2), so a
    foreign project's gateway, a wrong repo / cwd, or an alias-equivalent scope never binds the
    owner row. ``repo_root`` / ``cwd`` are already resolved (symlinks / ``..``) by the adapter.
    Absent (``None`` at the call site) means no live gateway matched — owner-unbound.
    """

    repo_root: str
    project_scope: str
    project_path: str
    cwd: str
    locator: str


@dataclass(frozen=True)
class ProjectGatewayDeclarationOutcome:
    """The result of a project-gateway create/adopt declaration attempt (dry-run or execute)."""

    status: str
    dry_run: bool
    workspace_id: str
    lane_id: str
    project_scope: str
    #: The provider-bound slots the declaration named, as ``(role, provider, assigned_name,
    #: locator)`` tuples — the plan a dry-run surfaces and an execute wrote.
    planned_slots: tuple[tuple[str, str, str, str], ...] = ()
    revision: int = 0
    detail: str = ""

    @property
    def applied(self) -> bool:
        """A lifecycle owner row was written (a fresh declaration or idempotent adopt).

        Only ``execute`` + a successful ``declare_lane`` is a write; a dry-run (even a clean
        one) and every fail-closed outcome wrote nothing.
        """
        return (not self.dry_run) and self.status == ADOPT_DECL_DECLARED

    @property
    def would_declare(self) -> bool:
        """The live pair resolved cleanly and a declaration is the next step.

        True for a clean dry-run plan and for a successful execute — i.e. the fail-closed
        gate passed. False for any zero-write refusal.
        """
        return self.status in (PG_DECL_DRY_RUN, ADOPT_DECL_DECLARED)

    def as_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "dry_run": self.dry_run,
            "applied": self.applied,
            "would_declare": self.would_declare,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "project_scope": self.project_scope,
            "planned_slots": [
                {"role": r, "provider": p, "assigned_name": n, "locator": loc}
                for r, p, n, loc in self.planned_slots
            ],
            "revision": self.revision,
            "detail": self.detail,
        }


def declare_project_gateway_owner_row(
    *,
    journal: str,
    issue: str,
    project_scope: str,
    workspace_id: str,
    providers: tuple[str, str],
    rows: Sequence[Mapping[str, object]],
    expected_repo_root: str,
    expected_project_path: str,
    observed_route: Optional[ObservedGatewayRoute],
    dry_run: bool = True,
    worktree_identity: str = "",
    attestation_home: Optional[Path] = None,
    attestation_store_factory: Optional[Callable[[], object]] = None,
    store_factory: Callable[[], LaneDeclarationStore] = LaneDeclarationStore,
) -> ProjectGatewayDeclarationOutcome:
    """Declare (or dry-run plan) a project-gateway lane's canonical lifecycle owner row.

    ``journal`` / ``issue`` are the declaration's durable decision anchor (a project lane owns
    a scope, but the journal is filed on a real issue, R2-F1). ``project_scope`` is the
    canonical full scope (never inferred here). ``providers`` is the ``(gateway, worker)``
    provider pair, ``rows`` the RAW herdr inventory. ``dry_run`` (default) writes nothing.

    **This boundary OWNS the scope -> lane derivation (Redmine #13811 T2 R2 F2).** The lane id
    is derived here as :func:`project_gateway_lane_id(scope)`, never taken from a caller.

    **It also action-time joins the live gateway's canonical ROUTE identity (T2 R3; design
    #13780 j#78386 §2, which folded the 48-bit alias fence into #13811).** ``expected_repo_root``
    / ``expected_project_path`` are the DECLARED canonical identity (from the adopted project
    metadata); ``observed_route`` is what the semantic discovery resolver
    (``resolve_launch_or_adopt``) matched for that identity — the live adopted gateway's stamped
    ``repo_root`` / ``project_scope`` / ``project_path``. The declaration REQUIRES a complete
    declared identity AND a live gateway whose stamped identity EXACTLY matches it; a missing /
    unresolvable route, an incomplete declared identity, or any repo / scope / path disagreement
    (a foreign project's gateway, a wrong cwd, a cross-revision alias-equivalent scope) is
    owner-unbound zero-write — never trusting the caller's raw rows alone.

    Returns a :class:`ProjectGatewayDeclarationOutcome`; every refusal is zero-write.
    """
    journal = _norm(journal)
    issue = _norm(issue)
    scope = _norm(project_scope)
    workspace = _norm(workspace_id)
    lane = ""

    def _fail(status: str, detail: str = "") -> ProjectGatewayDeclarationOutcome:
        return ProjectGatewayDeclarationOutcome(
            status=status,
            dry_run=dry_run,
            workspace_id=workspace,
            lane_id=lane,
            project_scope=scope,
            detail=detail,
        )

    if not (journal and issue):
        return _fail(ADOPT_DECL_NO_ANCHOR, "a project-gateway declaration requires an issue + journal anchor")
    if not scope:
        return _fail(PG_DECL_NO_SCOPE, "a project-gateway lane requires a canonical full project scope")
    # The lane id is DERIVED from the canonical scope here (never caller-supplied) so the
    # stored scope and lane are congruent by construction (F2).
    try:
        lane = project_gateway_lane_id(scope)
    except WorkflowRoleAuthorityError:
        return _fail(PG_DECL_NO_SCOPE, "the project scope does not derive a project-gateway lane")
    if not workspace:
        return _fail(ADOPT_DECL_UNRESOLVED_UNIT, "the project-gateway workspace is unresolved")

    # Action-time route-identity join (T2 R3; design j#78386 §2). The declared canonical
    # identity must be COMPLETE (an adopted project supplies repo_root + project_path) and a
    # live gateway must have been semantic-identity resolved for it; then its stamped
    # repo_root / project_scope / project_path must EXACTLY match. Any gap is owner-unbound.
    exp_repo = _norm(expected_repo_root)
    exp_path = _norm(expected_project_path)
    if not (exp_repo and exp_path):
        return _fail(
            PG_DECL_ROUTE_UNRESOLVED,
            "the declared canonical project identity (repo_root / project_path) is incomplete",
        )
    if observed_route is None:
        return _fail(
            PG_DECL_ROUTE_UNRESOLVED,
            "no live project-gateway resolved for the declared canonical identity",
        )
    if (
        _norm(observed_route.repo_root) != exp_repo
        or _norm(observed_route.project_scope) != scope
        or _norm(observed_route.project_path) != exp_path
    ):
        return _fail(
            PG_DECL_ROUTE_MISMATCH,
            "the live gateway's stamped route identity disagrees with the declaration",
        )
    # The stamps are a projection CACHE; the authoritative gate is the live cwd (design j#78386
    # §2 action-time cwd re-read). Re-verify the resolved live cwd is at / under the repo's
    # canonical project path — a stale-but-correct-looking stamp over a wrong live cwd never
    # binds the owner row (Redmine #13811 T2 R3 F2). ``exp_path`` is the repo-relative project
    # path; ``observed_route.cwd`` / ``exp_repo`` are adapter-resolved absolute paths.
    if not path_under_repo_relative(
        _norm(observed_route.cwd), repo_root=exp_repo, project_path=exp_path
    ):
        return _fail(
            PG_DECL_ROUTE_MISMATCH,
            "the live gateway's cwd is not under the canonical project path (stale stamp)",
        )

    try:
        decision = DecisionPointer(source="redmine", issue_id=issue, journal_id=journal)
    except (DecisionPointerError, ValueError):
        return _fail(ADOPT_DECL_BAD_ANCHOR, "the decision anchor is malformed")

    if attestation_store_factory is not None:
        attestation_store = attestation_store_factory()
    else:
        attestation_store = HerdrIdentityAttestationStore(home=attestation_home)
    pins, reason = resolve_declared_pins(
        rows,
        workspace_id=workspace,
        lane_id=lane,
        providers=providers,
        attestation_store=attestation_store,
    )
    if pins is None:
        # A fail-closed gate (unreadable / duplicate / stale / unattested / ambiguous slot):
        # zero-write, the live pair is not the exact declared generation.
        return _fail(reason, "the live pair did not resolve to an exact attested generation")

    # The adopted gateway the route resolver matched must be ONE of the resolved slots — so the
    # route join and the slot pins name the same live processes, not two unrelated resolutions.
    if _norm(observed_route.locator) not in {pin.locator for pin in pins}:
        return _fail(
            PG_DECL_ROUTE_MISMATCH,
            "the resolved live slots do not include the adopted gateway pane",
        )

    # Secondary raw-row check: a resolved live pane that ALSO surfaces a ``project_scope`` stamp
    # disagreeing with the declaration fails closed (belt-and-suspenders to the route join).
    declared_names = {pin.assigned_name for pin in pins}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if _norm(row.get(AGENT_KEY_NAME)) in declared_names:
            stamped = _norm(row.get("project_scope"))
            if stamped and stamped != scope:
                return _fail(
                    PG_DECL_SCOPE_STAMP_MISMATCH,
                    "a live pane's surfaced project_scope stamp disagrees with the declaration",
                )

    planned = tuple(
        (pin.role, pin.provider, pin.assigned_name, pin.locator) for pin in pins
    )
    if dry_run:
        # Default surface: the plan only. resolve_declared_pins read the inventory /
        # attestation store but wrote nothing; declare_lane is never reached.
        return ProjectGatewayDeclarationOutcome(
            status=PG_DECL_DRY_RUN,
            dry_run=True,
            workspace_id=workspace,
            lane_id=lane,
            project_scope=scope,
            planned_slots=planned,
            detail="dry-run: project-gateway declaration plan (nothing written)",
        )

    key = LaneLifecycleKey(workspace, lane)
    try:
        result = store_factory().declare_lane(
            key,
            decision=decision,
            binding_kind=BINDING_KIND_PROJECT_GATEWAY,
            project_scope=scope,
            declared_slots=pins,
            worktree_identity=_norm(worktree_identity),
        )
    except (LaneLifecycleError, DecisionPointerError, ProcessPinError, ValueError, OSError):
        return _fail(ADOPT_DECL_DECLARE_ERROR, "the lifecycle declaration store failed")

    if result.applied:
        # A fresh declaration OR an idempotent exact-duplicate adopt (same scope + live pins).
        return ProjectGatewayDeclarationOutcome(
            status=ADOPT_DECL_DECLARED,
            dry_run=False,
            workspace_id=workspace,
            lane_id=lane,
            project_scope=scope,
            planned_slots=planned,
            revision=result.revision,
            detail="project-gateway lane declared (fresh or idempotent adopt)",
        )
    # declare_lane refused: another active lane owns this scope, or a divergent row (different
    # scope / slots / non-active) already exists at the key — a legitimate zero-write, never
    # an overwrite (declare_lane's "a divergent re-declare must not overwrite").
    return ProjectGatewayDeclarationOutcome(
        status=ADOPT_DECL_OWNER_CONFLICT,
        dry_run=False,
        workspace_id=workspace,
        lane_id=lane,
        project_scope=scope,
        planned_slots=planned,
        revision=result.revision,
        detail=f"declaration refused ({result.reason}); zero-write",
    )


__all__ = (
    "PG_DECL_DRY_RUN",
    "PG_DECL_NO_SCOPE",
    "PG_DECL_ROUTE_MISMATCH",
    "PG_DECL_ROUTE_UNRESOLVED",
    "PG_DECL_SCOPE_STAMP_MISMATCH",
    "ObservedGatewayRoute",
    "ProjectGatewayDeclarationOutcome",
    "declare_project_gateway_owner_row",
)
