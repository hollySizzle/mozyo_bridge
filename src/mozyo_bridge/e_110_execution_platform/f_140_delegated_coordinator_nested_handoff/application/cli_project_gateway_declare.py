"""`mozyo-bridge sublane declare-project-gateway` — project-gateway create/adopt declaration.

The explicit, default-dry-run execute surface for the project-gateway create/adopt canonical
lifecycle declaration (Redmine #13811 T2; design #13780 j#78386 §2). The read-only
``project-gateway adopt`` decision printer (f_120) stays read-only — this is the separate
mutating actuator the design calls for. It resolves the live gateway/worker pair for the
derived ``pgwv1_...`` lane and declares its canonical lifecycle owner row through
:func:`declare_project_gateway_owner_row`; ``--execute`` opts into the write, the default is a
zero-write plan. Route binding (a derivational 正本) is never touched.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.project_gateway_declaration import (  # noqa: E501
    ObservedGatewayRoute,
    ProjectGatewayDeclarationOutcome,
    declare_project_gateway_owner_row,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (  # noqa: E501
    WorkflowRoleAuthorityError,
    project_gateway_lane_id,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
)

def _canonical_path(raw: object) -> str:
    """Resolve ``raw`` to a canonical absolute path (symlinks / ``..`` / relative -> absolute).

    The SAME normalization the semantic project-gateway resolver applies to a repo root
    (``Path.expanduser().resolve()``), so a ``--repo .`` invocation and the resolver's absolute
    repo root compare equal instead of failing the route-identity join on a spelling difference
    (Redmine #13811 T2 R3 F4). Fails open to the trimmed input when the path cannot be resolved.
    """
    text = _norm(raw)
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return text


#: The live inventory could not be read at declaration time (herdr down / unconfigured binary)
#: — never folded to a *confirmed-empty* pair (which would fail closed as ``incomplete_pair``
#: and hide the outage). An unreadable inventory is its own zero-write outcome.
PG_DECL_UNREADABLE = "unreadable_inventory"
#: The gateway / worker provider binding could not be resolved (unbound provider) — zero-write.
PG_DECL_UNRESOLVED_PROVIDER = "unresolved_provider"


@runtime_checkable
class ProjectGatewayDeclareOps(Protocol):
    """The side effects the declaration use case needs, injected so tests drive fakes."""

    def workspace_id(self) -> str: ...

    def read_inventory(self) -> tuple[Sequence[Mapping[str, object]], bool]: ...

    def providers(self) -> tuple[str, str]: ...

    def resolve_route(
        self, project_scope: str
    ) -> tuple[str, str, Optional[ObservedGatewayRoute]]: ...


@dataclass
class LiveProjectGatewayDeclareOps:
    """Live adapter: project workspace segment + live herdr inventory + provider binding."""

    repo_root: Path
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))

    def workspace_id(self) -> str:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
            herdr_workspace_segment,
        )

        try:
            return herdr_workspace_segment(self.repo_root)
        except (OSError, ValueError):
            return ""

    def read_inventory(self) -> tuple[Sequence[Mapping[str, object]], bool]:
        """``(rows, readable)`` — an unreadable inventory is NOT folded to empty (R1-F1)."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            list_herdr_agent_rows,
        )

        try:
            return list(list_herdr_agent_rows(self.env)), True
        except Exception:  # noqa: BLE001 — inventory unreadable -> fail closed (NOT empty)
            return (), False

    def providers(self) -> tuple[str, str]:
        """The ``(gateway, worker)`` provider pair from the binding, or ``("", "")`` unbound."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
            WorkflowProviderUnresolved,
            resolve_gateway_provider,
            resolve_worker_provider,
        )

        try:
            root = str(self.repo_root)
            return (resolve_gateway_provider(root), resolve_worker_provider(root))
        except WorkflowProviderUnresolved:
            return ("", "")

    def resolve_route(
        self, project_scope: str
    ) -> tuple[str, str, Optional[ObservedGatewayRoute]]:
        """``(declared_repo_root, declared_project_path, observed_route)`` (Redmine #13811 R3).

        Resolves the DECLARED canonical identity from the adopted project metadata
        (:func:`_gateway_identity`) and the OBSERVED live gateway by SEMANTIC identity over the
        live candidate list (:func:`resolve_launch_or_adopt`) — an ``adopt`` decision means a
        live pane matched ``repo_root + project_scope + role``, so a foreign / alias-equivalent
        pane never resolves. ``observed_route`` is that adopted pane's stamped identity, or
        ``None`` when no live gateway matched (launch / blocked / discovery error) — the
        declaration then fails closed owner-unbound.
        """
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application.cli_project_gateway import (  # noqa: E501
            _discover_candidates,
            _gateway_identity,
        )
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway_identity import (  # noqa: E501
            ACTION_ADOPT,
            resolve_launch_or_adopt,
        )

        # Canonicalize the repo root with the resolver's own normalization (R3 F4), so a
        # relative ``--repo .`` and the resolver's absolute repo root compare equal.
        canon_repo = _canonical_path(self.repo_root)
        try:
            identity = _gateway_identity(canon_repo, project_scope)
            decision = resolve_launch_or_adopt(_discover_candidates(), identity)
        except Exception:  # noqa: BLE001 — discovery / resolution unreadable -> owner-unbound
            return ("", "", None)
        observed: Optional[ObservedGatewayRoute] = None
        if decision.action == ACTION_ADOPT and decision.adopted is not None:
            cand = decision.adopted
            # Keep the pane's LIVE cwd (resolved) — the authoritative gate the declaration
            # re-checks against the canonical project path (R3 F2), not just the cached stamp.
            observed = ObservedGatewayRoute(
                repo_root=_canonical_path(cand.repo_root or ""),
                project_scope=cand.project_scope,
                project_path=cand.project_path,
                cwd=_canonical_path(cand.cwd),
                locator=cand.pane_id,
            )
        return (identity.repo_root, identity.project_path, observed)


@dataclass(frozen=True)
class ProjectGatewayDeclareRequest:
    issue: str
    journal: str
    project_scope: str
    worktree_identity: str = ""


@dataclass
class ProjectGatewayDeclareUseCase:
    """Resolve the project-gateway unit + provider pair, then declare (or dry-run plan).

    ``store_factory`` / ``attestation_store_factory`` default to ``None`` (the live shared
    stores, resolved inside :func:`declare_project_gateway_owner_row`); tests inject temp-home
    factories so the fail-closed gate is exercised without touching real state.
    """

    ops: ProjectGatewayDeclareOps
    store_factory: Optional[Callable[[], Any]] = None
    attestation_store_factory: Optional[Callable[[], object]] = None

    def run(
        self, request: ProjectGatewayDeclareRequest, *, execute: bool
    ) -> ProjectGatewayDeclarationOutcome:
        scope = _norm(request.project_scope)
        workspace_id = _norm(self.ops.workspace_id())
        # The derived lane id is authoritative inside declare_project_gateway_owner_row (F2);
        # here it is computed only for the pre-declaration fail-closed outcomes' display.
        try:
            lane_id = project_gateway_lane_id(scope) if scope else ""
        except WorkflowRoleAuthorityError:
            lane_id = ""
        # Read the live inventory ONCE, keeping readability explicit (R1-F1). An unreadable
        # inventory is a zero-write outcome, never a confirmed-empty pair.
        rows, readable = self.ops.read_inventory()
        if not readable:
            return self._fail(
                PG_DECL_UNREADABLE,
                scope,
                workspace_id,
                lane_id,
                execute,
                "live inventory unreadable; owner-unbound zero-write",
            )
        gateway_provider, worker_provider = self.ops.providers()
        if not (gateway_provider and worker_provider):
            return self._fail(
                PG_DECL_UNRESOLVED_PROVIDER,
                scope,
                workspace_id,
                lane_id,
                execute,
                "gateway / worker provider binding unresolved; zero-write",
            )
        # Resolve the DECLARED canonical identity + the OBSERVED live gateway route (R3). The
        # declaration verifies they exactly match before writing; an unresolved / mismatched
        # route is owner-unbound zero-write.
        expected_repo_root, expected_project_path, observed_route = self.ops.resolve_route(scope)
        kwargs: dict[str, Any] = {}
        if self.store_factory is not None:
            kwargs["store_factory"] = self.store_factory
        if self.attestation_store_factory is not None:
            kwargs["attestation_store_factory"] = self.attestation_store_factory
        return declare_project_gateway_owner_row(
            journal=request.journal,
            issue=request.issue,
            project_scope=scope,
            workspace_id=workspace_id,
            providers=(gateway_provider, worker_provider),
            rows=rows,
            expected_repo_root=expected_repo_root,
            expected_project_path=expected_project_path,
            observed_route=observed_route,
            dry_run=not execute,
            worktree_identity=request.worktree_identity,
            **kwargs,
        )

    @staticmethod
    def _fail(
        status: str,
        scope: str,
        workspace_id: str,
        lane_id: str,
        execute: bool,
        detail: str,
    ) -> ProjectGatewayDeclarationOutcome:
        return ProjectGatewayDeclarationOutcome(
            status=status,
            dry_run=not execute,
            workspace_id=workspace_id,
            lane_id=lane_id,
            project_scope=scope,
            detail=detail,
        )


def format_declare_text(outcome: ProjectGatewayDeclarationOutcome) -> str:
    lines = [
        f"project-gateway declare: {outcome.lane_id or '(unresolved)'} "
        f"(scope {outcome.project_scope or '(none)'})",
        f"  status: {outcome.status} dry_run: {outcome.dry_run} "
        f"applied: {outcome.applied} would_declare: {outcome.would_declare}",
    ]
    for role, provider, name, locator in outcome.planned_slots:
        lines.append(f"    - {role} provider={provider} {name} @ {locator}")
    if outcome.detail:
        lines.append(f"  {outcome.detail}")
    if outcome.dry_run and outcome.would_declare:
        lines.append("  (dry-run; re-run with --execute to declare the lane)")
    return "\n".join(lines)


def cmd_sublane_declare_project_gateway(args: argparse.Namespace) -> int:
    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    request = ProjectGatewayDeclareRequest(
        issue=getattr(args, "issue", "") or "",
        journal=getattr(args, "journal", "") or "",
        project_scope=getattr(args, "project_scope", "") or "",
        worktree_identity=getattr(args, "worktree_identity", "") or "",
    )
    ops = LiveProjectGatewayDeclareOps(repo_root=repo_root, env=dict(os.environ))
    outcome = ProjectGatewayDeclareUseCase(ops=ops).run(
        request, execute=bool(getattr(args, "execute", False))
    )
    if bool(getattr(args, "json", False)):
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_declare_text(outcome), file=sys.stdout)
    # A dry-run plan and a successful declaration are exit 0; a fail-closed zero-write is
    # non-zero so a scripted create/adopt sees the refusal.
    return 0 if outcome.would_declare else 1


def register_sublane_declare_project_gateway_parser(sublane_sub: Any) -> None:
    """Register ``sublane declare-project-gateway`` (Redmine #13811 T2).

    The explicit execute surface for the project-gateway lifecycle declaration, kept next to
    its use case (the core CLI module is at the module-health ceiling), mirroring the
    ``register_sublane_hibernate_parser`` placement.
    """
    from mozyo_bridge.application.cli_common import add_repo_option

    parser = sublane_sub.add_parser(
        "declare-project-gateway",
        help=(
            "Redmine #13811: declare the canonical lifecycle owner row for a PROJECT-GATEWAY "
            "lane (binding_kind=project_gateway) from its live, attested gateway/worker pair. "
            "Default is DRY-RUN (zero-write plan); --execute performs the declaration. An "
            "exact-duplicate active declaration is an idempotent adopt; a conflicting / "
            "divergent / unattested / ambiguous pair is a zero-write refusal (exit non-zero). "
            "The read-only `project-gateway adopt` decision surface is unchanged; route "
            "binding is never touched."
        ),
    )
    parser.add_argument(
        "--project-scope",
        dest="project_scope",
        required=True,
        help="The canonical full project scope the gateway lane owns (never inferred).",
    )
    parser.add_argument(
        "--issue",
        required=True,
        help="Redmine issue id the decision anchor is filed on (the project lane owns a "
        "scope, not an issue, but the journal is issue-addressable).",
    )
    parser.add_argument(
        "--journal",
        required=True,
        help="Redmine journal id that authorizes the declaration (durable anchor).",
    )
    parser.add_argument(
        "--worktree-identity",
        dest="worktree_identity",
        default="",
        help="Optional canonical worktree identity token to bind (empty allowed).",
    )
    parser.add_argument(
        "--execute",
        dest="execute",
        action="store_true",
        help="Perform the declaration (write the owner row). Without it this is a "
        "zero-write dry-run plan.",
    )
    add_repo_option(parser)
    parser.add_argument(
        "--json", action="store_true", help="Emit structured JSON output"
    )
    parser.set_defaults(func=cmd_sublane_declare_project_gateway)


__all__ = (
    "PG_DECL_UNREADABLE",
    "PG_DECL_UNRESOLVED_PROVIDER",
    "LiveProjectGatewayDeclareOps",
    "ProjectGatewayDeclareOps",
    "ProjectGatewayDeclareRequest",
    "ProjectGatewayDeclareUseCase",
    "cmd_sublane_declare_project_gateway",
    "format_declare_text",
    "register_sublane_declare_project_gateway_parser",
)
