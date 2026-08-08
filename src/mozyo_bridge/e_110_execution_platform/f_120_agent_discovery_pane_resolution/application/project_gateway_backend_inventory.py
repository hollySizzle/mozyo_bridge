"""Backend-aware project-gateway inventory and Herdr delivery pinning.

The project-gateway command family historically discovered only tmux panes.  This
adapter keeps that tmux branch unchanged while projecting the Herdr backend's
durable workflow-role binding plus live, generation-attested agent rows onto the
same ``TargetCandidate`` resolver vocabulary.  Herdr locators remain transient:
selection uses the logical assigned name, and every delivery re-reads the backend,
binding, provider, inventory row, liveness, and launch generation before handing an
internal capability to the send rail.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    CONFIDENCE_STRONG,
    HOST_LOCAL,
    VIEW_KIND_NORMAL_WINDOW,
    TargetCandidate,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_binding_source import (
    load_workflow_binding,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_role_authority_source import (
    load_parsed_role_bindings,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (
    ROLE_COORDINATOR as PROVIDER_ROLE_COORDINATOR,
    ROLE_PROJECT_GATEWAY as PROVIDER_ROLE_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (
    DEFAULT_LANE,
    ROLE_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (
    ROLE_COORDINATOR,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.project_scope import (
    path_under_repo_relative,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (
    herdr_workspace_segment,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_LOCATOR,
    AGENT_KEY_LOCATOR_ALIAS,
    AGENT_KEY_LOCATOR_ALIAS_2,
    AGENT_KEY_NAME,
    _norm,
    _norm_lane,
    decode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (
    SLOT_LIVE,
    classify_named_slot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
    BACKEND_TMUX,
    TerminalTransportError,
    valid_target,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_discovery import (
    resolve_agent_lister,
)
from mozyo_bridge.shared.paths import (
    find_repo_root,
    infer_git_worktree_root,
    workspace_adoption_marker,
)


# Which semantic slice the shared inventory projects.  A project-gateway binding
# names the parent lane; child routing deliberately excludes every bound gateway
# lane and the default lane, matching the Herdr workflow-forward resolver.
SELECT_GATEWAY = "gateway"
SELECT_CHILD_ROUTE = "child_route"
SELECT_CHILD_INTAKE = "child_intake"
SELECT_NONE = "none"
INVENTORY_SELECTORS = frozenset(
    {SELECT_GATEWAY, SELECT_CHILD_ROUTE, SELECT_CHILD_INTAKE, SELECT_NONE}
)

STATUS_INVENTORY_UNAVAILABLE = "gateway_inventory_unavailable"

_LOCATOR_KEYS = (
    AGENT_KEY_LOCATOR,
    AGENT_KEY_LOCATOR_ALIAS,
    AGENT_KEY_LOCATOR_ALIAS_2,
)


def _locator_claims(row: Mapping[str, object]) -> tuple[bool, frozenset[str]]:
    """Return readable non-empty locator aliases without first-key collapse."""

    claims: set[str] = set()
    readable = True
    for key in _LOCATOR_KEYS:
        if key not in row or row.get(key) is None:
            continue
        value = row.get(key)
        if not isinstance(value, str):
            readable = False
            continue
        normalized = value.strip()
        if normalized:
            claims.add(normalized)
    return readable, frozenset(claims)


class ProjectGatewayInventoryError(ValueError):
    """A backend inventory authority is unavailable or contradictory."""

    def __init__(self, reason: str, detail: str, *, backend: str = "") -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.backend = backend

    def as_payload(self) -> dict[str, object]:
        return {
            "status": STATUS_INVENTORY_UNAVAILABLE,
            "backend": self.backend or None,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ProjectGatewayInventoryRequest:
    """The semantic inventory request retained for action-time revalidation."""

    repo_root: str
    project_scope: str
    provider: str
    selector: str = SELECT_GATEWAY
    session: Optional[str] = None
    required_backend: str = ""


@dataclass(frozen=True)
class ProjectPathAuthority:
    """Exact adopted path, or an explicit descriptor-less repo-root fallback."""

    path: str
    fallback_root_scope: bool = False


@dataclass(frozen=True)
class HerdrTargetCandidate(TargetCandidate):
    """Resolver-compatible candidate with a truthful Herdr JSON projection."""

    assigned_name: str = ""
    locator: str = ""
    target_repo_root: str = ""

    def to_dict(self) -> dict[str, object]:
        payload = super().to_dict()
        payload["runtime"] = {
            "provider": BACKEND_HERDR,
            "session": None,
            "window": None,
            "window_index": None,
            "pane_index": None,
            "pane_id": self.locator or None,
            "assigned_name": self.assigned_name,
            "locator": self.locator or None,
            "cwd": self.cwd,
        }
        repo = payload.get("repo")
        if isinstance(repo, dict):
            # ``TargetCandidate.repo_root`` remains the semantic route root used by
            # the pure resolver.  A child may run in another linked worktree, so the
            # runtime target root is surfaced separately rather than silently
            # replacing the route authority.
            repo["target_root"] = self.target_repo_root or self.repo_root
        return payload


@dataclass(frozen=True)
class HerdrTargetObservation:
    """One exact live Herdr generation behind a resolver candidate."""

    workspace_id: str
    lane_id: str
    workflow_role: str
    provider: str
    project_scope: str
    assigned_name: str
    locator: str
    generation_token: str
    target_cwd: str
    target_repo_root: str
    project_path: str = ""
    project_scope_root_fallback: bool = False


@dataclass(frozen=True)
class ProjectGatewayBackendInventory(Sequence[TargetCandidate]):
    """A backend-tagged immutable candidate snapshot."""

    backend: str
    request: ProjectGatewayInventoryRequest
    candidates: tuple[TargetCandidate, ...]
    observations: tuple[HerdrTargetObservation, ...] = ()
    gateway_assigned_name: str = ""

    def __iter__(self) -> Iterator[TargetCandidate]:
        return iter(self.candidates)

    def __len__(self) -> int:
        return len(self.candidates)

    def __getitem__(self, index):
        return self.candidates[index]

    def observation_for(self, assigned_name: str) -> HerdrTargetObservation:
        matches = [o for o in self.observations if o.assigned_name == assigned_name]
        if len(matches) != 1:
            raise ProjectGatewayInventoryError(
                "herdr_target_not_exact",
                "the selected Herdr target is no longer an exact inventory observation",
                backend=self.backend,
            )
        return matches[0]


@dataclass(frozen=True)
class PreparedProjectGatewayDelivery:
    """The fully revalidated target values a CLI may apply before orchestration."""

    target: str
    target_repo: str = ""
    target_lane: str = ""
    capability: object | None = None


class LiveProjectGatewayInventoryOps:
    """Production I/O for :class:`ProjectGatewayBackendInventoryUseCase`."""

    def backend(self, repo_root: Path) -> str:
        return load_repo_local_config(repo_root).terminal_transport.backend

    def tmux_candidates(self) -> list:
        # Imported lazily so the tmux branch is byte-compatible and the Herdr
        # branch never imports/calls the pane inventory path.
        from mozyo_bridge.application.commands import _agents_target_candidates
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (
            require_tmux,
        )

        require_tmux()
        return _agents_target_candidates(argparse.Namespace(agent=None, session=None))

    def parsed_role_bindings(self, repo_root: Path):
        return load_parsed_role_bindings(repo_root)

    def provider_binding(self, repo_root: Path):
        binding, _warnings = load_workflow_binding(repo_root)
        return binding

    def workspace_id(self, repo_root: Path) -> str:
        return herdr_workspace_segment(repo_root)

    def herdr_rows(self, repo_root: Path):
        config = load_repo_local_config(repo_root).terminal_transport
        lister = resolve_agent_lister(config)
        if lister is None:
            raise TerminalTransportError(
                "Herdr backend selected but no agent lister resolved"
            )
        return lister.list_agent_rows()

    def generation_token(
        self,
        *,
        assigned_name: str,
        workspace_id: str,
        provider: str,
        lane_id: str,
        locator: str,
    ) -> str:
        from mozyo_bridge.core.state.herdr_launch_generation import (
            verified_generation_token,
        )

        return verified_generation_token(
            None,
            assigned_name=assigned_name,
            workspace_id=workspace_id,
            role=provider,
            lane_id=lane_id,
            locator=locator,
            norm=_norm,
            norm_lane=_norm_lane,
        )

    def project_path(
        self, repo_root: Path, project_scope: str
    ) -> ProjectPathAuthority:
        from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.project_discovery import (
            resolve_project_scopes,
        )

        adopted, drift = resolve_project_scopes(str(repo_root))
        if drift:
            raise ValueError("project discovery cache drift")
        matches = [scope.path for scope in adopted if scope.scope == project_scope]
        if len(matches) > 1:
            raise ValueError("project scope resolves to multiple adopted paths")
        if matches:
            return ProjectPathAuthority(matches[0])
        if adopted:
            raise ValueError(
                "requested project scope is absent from the repo's adopted project paths"
            )
        # A durable workflow-role binding may define a single-project repo scope
        # without a project.env descriptor (the mozyo_bridge repo itself is the
        # production example).  In that case the exact path is the repo root;
        # discovery below requires cwd == target_root rather than widening `.` to
        # every nested directory.
        return ProjectPathAuthority(".", fallback_root_scope=True)

    def target_repo_root(self, cwd: str, fallback: Path) -> str:
        # ``fallback`` is retained on the injected ops protocol only for source
        # compatibility with the original test doubles.  It is deliberately not
        # an identity answer: a selected live row must establish its own cwd/root,
        # never inherit the caller's requested repo when that observation is
        # absent or malformed.
        del fallback
        if not cwd:
            return ""
        try:
            # The target row's cwd is the authority here.  ``resolve_repo_root``
            # would let an ambient MOZYO_REPO override it, which could make a
            # child worktree pass the wrong repo/project preflight.
            git_root = infer_git_worktree_root(Path(cwd))
            if git_root is not None:
                return str(git_root)
            root = find_repo_root(start=Path(cwd))
            # ``find_repo_root`` returns the starting directory when no root
            # marker exists.  Treat that as unestablished rather than blessing a
            # random directory as the requested project checkout.
            if workspace_adoption_marker(root):
                return str(root)
            return ""
        except (OSError, RuntimeError, ValueError):
            return ""


class ProjectGatewayBackendInventoryUseCase:
    """Resolve one backend snapshot without cross-backend fallback."""

    def __init__(self, ops: object | None = None) -> None:
        self._ops = ops or LiveProjectGatewayInventoryOps()

    @staticmethod
    def _error(reason: str, detail: str, backend: str = BACKEND_HERDR):
        raise ProjectGatewayInventoryError(reason, detail, backend=backend)

    def discover(
        self, request: ProjectGatewayInventoryRequest
    ) -> ProjectGatewayBackendInventory:
        if request.selector not in INVENTORY_SELECTORS:
            self._error(
                "inventory_selector_invalid",
                "the project-gateway inventory selector is not recognized",
                backend="",
            )
        repo_root = Path(request.repo_root).expanduser().resolve()
        scope = _norm(request.project_scope)
        provider = _norm(request.provider)
        try:
            backend = self._ops.backend(repo_root)
        except Exception as exc:  # noqa: BLE001 - config unreadable is typed refusal
            raise ProjectGatewayInventoryError(
                "backend_config_unavailable",
                "the repo-local terminal backend configuration is unreadable",
            ) from exc

        if request.required_backend and backend != request.required_backend:
            self._error(
                "backend_changed",
                "the terminal backend changed after target resolution; refusing cross-backend fallback",
                backend=backend,
            )

        normalized_request = ProjectGatewayInventoryRequest(
            repo_root=str(repo_root),
            project_scope=scope,
            provider=provider,
            selector=request.selector,
            session=request.session,
            required_backend=request.required_backend,
        )
        if backend == BACKEND_TMUX:
            try:
                candidates = tuple(self._ops.tmux_candidates())
            except ProjectGatewayInventoryError:
                raise
            return ProjectGatewayBackendInventory(
                backend=backend,
                request=normalized_request,
                candidates=candidates,
            )
        if backend != BACKEND_HERDR:
            self._error(
                "backend_unknown",
                "the selected terminal backend is not supported by project-gateway discovery",
                backend=backend,
            )
        if request.session:
            self._error(
                "herdr_session_selector_unsupported",
                "--session/--gateway-session is a tmux selector and cannot narrow Herdr inventory",
            )
        if not scope or not provider:
            self._error(
                "selector_gap",
                "Herdr project-gateway discovery requires project scope and provider",
            )
        if request.selector == SELECT_NONE:
            return ProjectGatewayBackendInventory(
                backend=backend,
                request=normalized_request,
                candidates=(),
            )

        try:
            parsed = self._ops.parsed_role_bindings(repo_root)
        except Exception as exc:  # noqa: BLE001 - durable authority unavailable
            raise ProjectGatewayInventoryError(
                "workflow_role_binding_unavailable",
                "the durable workflow-role binding could not be read",
                backend=backend,
            ) from exc
        if not getattr(parsed, "ok", False):
            self._error(
                "workflow_role_binding_invalid",
                "the durable workflow-role binding is malformed",
            )

        gateway_roles = frozenset({ROLE_PROJECT_GATEWAY, ROLE_COORDINATOR})
        authority_matches = [
            binding
            for binding in parsed.bindings
            if binding.role in gateway_roles and binding.project_scope == scope
        ]
        if len(authority_matches) != 1:
            self._error(
                "project_scope_binding_ambiguous"
                if authority_matches
                else "project_scope_binding_missing",
                "the project scope must have exactly one durable coordinator/project-gateway binding",
            )
        authority = authority_matches[0]

        try:
            role_binding = self._ops.provider_binding(repo_root)
        except Exception as exc:  # noqa: BLE001 - provider authority unavailable
            raise ProjectGatewayInventoryError(
                "provider_binding_unavailable",
                "the workflow provider binding could not be read",
                backend=backend,
            ) from exc
        gateway_provider_key = (
            PROVIDER_ROLE_COORDINATOR
            if authority.role == ROLE_COORDINATOR
            else PROVIDER_ROLE_PROJECT_GATEWAY
        )
        gateway_provider = _norm(role_binding.provider_for(gateway_provider_key))
        child_provider = _norm(role_binding.provider_for(PROVIDER_ROLE_COORDINATOR))
        required_providers = (
            (gateway_provider,)
            if request.selector == SELECT_GATEWAY
            else (child_provider,)
            if request.selector == SELECT_CHILD_ROUTE
            else (gateway_provider, child_provider)
        )
        if any(not item for item in required_providers):
            self._error(
                "provider_binding_unresolved",
                "provider_binding resolves no provider for the requested project-gateway route",
            )
        target_provider = (
            gateway_provider
            if request.selector == SELECT_GATEWAY
            else child_provider
        )
        if target_provider != provider:
            self._error(
                "provider_binding_mismatch",
                "the command receiver does not match provider_binding for this Herdr route",
            )

        try:
            workspace_id = _norm(self._ops.workspace_id(repo_root))
        except Exception as exc:  # noqa: BLE001 - workspace authority unavailable
            raise ProjectGatewayInventoryError(
                "workspace_identity_unavailable",
                "the checkout workspace identity could not be read",
                backend=backend,
            ) from exc
        if not workspace_id:
            self._error(
                "workspace_identity_unavailable",
                "the checkout has no durable workspace identity for Herdr discovery",
            )

        try:
            rows = tuple(self._ops.herdr_rows(repo_root))
        except Exception as exc:  # noqa: BLE001 - unreadable inventory is never empty
            raise ProjectGatewayInventoryError(
                "herdr_inventory_unavailable",
                "the live Herdr agent inventory could not be read",
                backend=backend,
            ) from exc

        gateway_lane_ids = {
            _norm_lane(binding.lane_id)
            for binding in parsed.bindings
            if binding.role in gateway_roles
        }
        gateway_lane_ids.add(_norm_lane(authority.lane_id))
        selected_rows: list[tuple[Mapping[str, object], object, str, str]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            decoded = decode_assigned_name(row.get(AGENT_KEY_NAME))
            identity = decoded.identity if decoded.ok else None
            if identity is None or identity.workspace_id != workspace_id:
                continue
            lane = _norm_lane(identity.lane_id)
            is_gateway = lane == _norm_lane(authority.lane_id) and identity.role == gateway_provider
            is_child = (
                lane != DEFAULT_LANE
                and lane not in gateway_lane_ids
                and identity.role == child_provider
            )
            include = (
                is_gateway
                if request.selector == SELECT_GATEWAY
                else is_child
                if request.selector == SELECT_CHILD_ROUTE
                else is_gateway or is_child
            )
            if include:
                selected_rows.append((row, identity, lane, _norm(row.get(AGENT_KEY_NAME))))

        names = [item[3] for item in selected_rows]
        if len(names) != len(set(names)):
            self._error(
                "herdr_assigned_name_ambiguous",
                "the live Herdr inventory contains duplicate rows for one durable assigned name",
            )

        try:
            project_path_authority = self._ops.project_path(repo_root, scope)
        except Exception as exc:  # noqa: BLE001 - adopted-scope source is an IO boundary
            raise ProjectGatewayInventoryError(
                "project_scope_path_unavailable",
                "the requested project scope's adopted path authority is unreadable or ambiguous",
                backend=backend,
            ) from exc
        if isinstance(project_path_authority, ProjectPathAuthority):
            project_path = project_path_authority.path
            project_scope_root_fallback = (
                project_path_authority.fallback_root_scope
            )
        else:
            # Injected legacy/test ops return the pre-#15118 string shape.  Treat
            # it as an explicit adopted path, never as implicit fallback
            # authority; production always returns ProjectPathAuthority.
            project_path = str(project_path_authority or "")
            project_scope_root_fallback = False
        if not project_path:
            self._error(
                "project_scope_path_unavailable",
                "the requested project scope does not resolve to exactly one adopted project path",
            )
        observations: list[HerdrTargetObservation] = []
        candidates: list[TargetCandidate] = []
        for row, identity, lane, assigned_name in selected_rows:
            if classify_named_slot(row) != SLOT_LIVE:
                self._error(
                    "herdr_slot_not_live",
                    "a matching durable Herdr slot is stale rather than a live managed agent",
                )
            detected_provider = _norm(row.get("agent"))
            if not detected_provider or detected_provider != identity.role:
                self._error(
                    "herdr_live_provider_mismatch",
                    "a matching Herdr row's detected live provider does not match its durable assigned name",
                )
            readable_locator, locator_claims = _locator_claims(row)
            if not readable_locator or len(locator_claims) != 1:
                self._error(
                    "herdr_locator_evidence_invalid",
                    "a matching Herdr row has malformed or conflicting locator aliases",
                )
            locator = next(iter(locator_claims))
            if not valid_target(locator):
                self._error(
                    "herdr_locator_missing",
                    "a matching durable Herdr slot has no valid live locator",
                )
            locator_rows = [
                candidate_row
                for candidate_row in rows
                if isinstance(candidate_row, Mapping)
                and locator in _locator_claims(candidate_row)[1]
            ]
            if len(locator_rows) != 1 or locator_rows[0] is not row:
                self._error(
                    "herdr_locator_ambiguous",
                    "the matching Herdr locator is aliased by multiple live inventory rows",
                )
            try:
                generation = self._ops.generation_token(
                    assigned_name=assigned_name,
                    workspace_id=workspace_id,
                    provider=identity.role,
                    lane_id=lane,
                    locator=locator,
                )
            except Exception as exc:  # noqa: BLE001 - attestation source is an IO boundary
                raise ProjectGatewayInventoryError(
                    "herdr_generation_unavailable",
                    "the current Herdr launch-generation authority could not be read",
                    backend=backend,
                ) from exc
            if not generation:
                self._error(
                    "herdr_generation_unverified",
                    "a matching live Herdr slot has no verified current launch generation",
                )
            raw_target_cwd = _norm(row.get("cwd"))
            if not raw_target_cwd:
                self._error(
                    "herdr_target_cwd_unavailable",
                    "a matching live Herdr row has no target cwd for repo/project identity",
                )
            try:
                target_cwd = str(Path(raw_target_cwd).expanduser().resolve())
            except (OSError, RuntimeError, ValueError) as exc:
                raise ProjectGatewayInventoryError(
                    "herdr_target_cwd_unavailable",
                    "the matching live Herdr row's target cwd is unreadable",
                    backend=backend,
                ) from exc
            target_root = self._ops.target_repo_root(target_cwd, repo_root)
            if not target_root:
                self._error(
                    "herdr_target_repo_unavailable",
                    "the matching Herdr target cwd does not establish an adopted repo root",
                )
            try:
                target_workspace = _norm(self._ops.workspace_id(Path(target_root)))
            except Exception as exc:  # noqa: BLE001 - target anchor is an IO boundary
                raise ProjectGatewayInventoryError(
                    "herdr_target_workspace_unavailable",
                    "the matching Herdr target's checkout workspace could not be read",
                    backend=backend,
                ) from exc
            if not target_workspace or target_workspace != workspace_id:
                self._error(
                    "herdr_target_workspace_mismatch",
                    "the matching Herdr target cwd belongs to another checkout workspace",
                )
            target_in_project = (
                Path(target_cwd) == Path(target_root)
                if project_scope_root_fallback
                else path_under_repo_relative(
                    target_cwd,
                    repo_root=target_root,
                    project_path=project_path,
                )
            )
            if not target_in_project:
                self._error(
                    "herdr_target_project_scope_mismatch",
                    "the matching Herdr target cwd is outside the requested adopted project path",
                )
            workflow_role = authority.role if lane == _norm_lane(authority.lane_id) else ROLE_COORDINATOR
            observation = HerdrTargetObservation(
                workspace_id=workspace_id,
                lane_id=lane,
                workflow_role=workflow_role,
                provider=identity.role,
                project_scope=scope,
                assigned_name=assigned_name,
                locator=locator,
                generation_token=generation,
                target_cwd=target_cwd,
                target_repo_root=target_root,
                project_path=project_path,
                project_scope_root_fallback=project_scope_root_fallback,
            )
            observations.append(observation)
            candidates.append(
                HerdrTargetCandidate(
                    # The durable assigned name, not the transient locator, is the
                    # candidate/self-fence identity.
                    pane_id=assigned_name,
                    role=identity.role,
                    role_source="herdr_assigned_name",
                    confidence=CONFIDENCE_STRONG,
                    ambiguous=False,
                    session=BACKEND_HERDR,
                    window_name=identity.role,
                    window_index="",
                    pane_index="",
                    active=False,
                    workspace_id=workspace_id,
                    workspace_label=repo_root.name,
                    lane_id=lane,
                    lane_label=None,
                    repo_short=repo_root.name,
                    repo_root=str(repo_root),
                    cwd=target_cwd,
                    host=HOST_LOCAL,
                    view_kind=VIEW_KIND_NORMAL_WINDOW,
                    branch=None,
                    project_scope=scope,
                    project_path=project_path,
                    project_label=scope,
                    project_scope_source="durable_binding",
                    assigned_name=assigned_name,
                    locator=locator,
                    target_repo_root=target_root,
                )
            )

        gateway_name = next(
            (
                obs.assigned_name
                for obs in observations
                if obs.lane_id == _norm_lane(authority.lane_id)
                and obs.provider == gateway_provider
            ),
            "",
        )
        # A child-intake command is itself parent-scoped.  If the durable parent
        # slot is absent, an asserted --from-pane must not turn a foreign shell into
        # a child-routing origin.
        if request.selector == SELECT_CHILD_INTAKE and not gateway_name:
            self._error(
                "herdr_parent_gateway_missing",
                "the durable parent gateway is not live for child-intake self-fencing",
            )

        candidates.sort(key=lambda item: item.pane_id)
        observations.sort(key=lambda item: item.assigned_name)
        return ProjectGatewayBackendInventory(
            backend=backend,
            request=normalized_request,
            candidates=tuple(candidates),
            observations=tuple(observations),
            gateway_assigned_name=gateway_name,
        )


def discover_project_gateway_inventory(
    *,
    repo_root: str,
    project_scope: str,
    provider: str,
    selector: str = SELECT_GATEWAY,
    session: Optional[str] = None,
    required_backend: str = "",
    ops: object | None = None,
) -> ProjectGatewayBackendInventory:
    """Convenience composition entry used by every project-gateway CLI sibling."""

    return ProjectGatewayBackendInventoryUseCase(ops).discover(
        ProjectGatewayInventoryRequest(
            repo_root=repo_root,
            project_scope=project_scope,
            provider=provider,
            selector=selector,
            session=session,
            required_backend=required_backend,
        )
    )


def normalize_child_intake_caller(
    inventory: Sequence[TargetCandidate], caller_reference: str
) -> str:
    """Normalize a Herdr caller locator/name to the durable parent assigned name."""

    if not isinstance(inventory, ProjectGatewayBackendInventory) or inventory.backend != BACKEND_HERDR:
        return (caller_reference or "").strip()
    reference = _norm(caller_reference)
    matches = [
        obs
        for obs in inventory.observations
        if reference in {obs.assigned_name, obs.locator}
    ]
    if len(matches) != 1 or matches[0].assigned_name != inventory.gateway_assigned_name:
        raise ProjectGatewayInventoryError(
            "herdr_caller_identity_mismatch",
            "--from-pane must resolve to the exact durable parent gateway generation",
            backend=BACKEND_HERDR,
        )
    return matches[0].assigned_name


def prepare_project_gateway_delivery(
    inventory: Sequence[TargetCandidate], selected: TargetCandidate
) -> PreparedProjectGatewayDelivery:
    """Action-time revalidate a selected target and build the internal capability."""

    if not isinstance(inventory, ProjectGatewayBackendInventory) or inventory.backend != BACKEND_HERDR:
        return PreparedProjectGatewayDelivery(target=selected.pane_id)

    request = inventory.request
    fresh = discover_project_gateway_inventory(
        repo_root=request.repo_root,
        project_scope=request.project_scope,
        provider=request.provider,
        selector=request.selector,
        session=request.session,
        required_backend=BACKEND_HERDR,
    )
    # Cardinality/identity drift is as material as locator drift: re-running only
    # the selected name would silently ignore a newly ambiguous route.
    if fresh.observations != inventory.observations:
        raise ProjectGatewayInventoryError(
            "herdr_inventory_generation_changed",
            "the Herdr route identity or generation changed before delivery",
            backend=BACKEND_HERDR,
        )
    observation = fresh.observation_for(selected.pane_id)
    if not observation.target_cwd:
        raise ProjectGatewayInventoryError(
            "herdr_target_cwd_unavailable",
            "the exact live Herdr row has no target cwd for repo/project preflight",
            backend=BACKEND_HERDR,
        )

    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_send_entry import (
        PROJECT_GATEWAY_TARGET_CAPABILITY_PURPOSE,
        ResolvedHerdrTargetCapability,
    )

    capability = ResolvedHerdrTargetCapability(
        workspace_id=observation.workspace_id,
        lane_id=observation.lane_id,
        provider=observation.provider,
        assigned_name=observation.assigned_name,
        locator=observation.locator,
        purpose=PROJECT_GATEWAY_TARGET_CAPABILITY_PURPOSE,
        generation_token=observation.generation_token,
        project_scope=observation.project_scope,
        target_repo_root=observation.target_repo_root,
        target_cwd=observation.target_cwd,
        project_path=observation.project_path,
        project_scope_root_fallback=observation.project_scope_root_fallback,
    )
    return PreparedProjectGatewayDelivery(
        target=observation.assigned_name,
        target_repo=observation.target_repo_root,
        target_lane=observation.lane_id,
        capability=capability,
    )


def render_inventory_error(error: ProjectGatewayInventoryError, *, as_json: bool) -> int:
    """Render a typed inventory refusal without collapsing it to gateway_missing."""

    if as_json:
        import json

        print(json.dumps(error.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"status: {STATUS_INVENTORY_UNAVAILABLE}")
        print(f"backend: {error.backend or '<unresolved>'}")
        print(f"reason: {error.reason}")
        print(f"detail: {error.detail}")
    return 1


__all__ = (
    "HerdrTargetCandidate",
    "HerdrTargetObservation",
    "INVENTORY_SELECTORS",
    "LiveProjectGatewayInventoryOps",
    "PreparedProjectGatewayDelivery",
    "ProjectGatewayBackendInventory",
    "ProjectGatewayBackendInventoryUseCase",
    "ProjectGatewayInventoryError",
    "ProjectGatewayInventoryRequest",
    "ProjectPathAuthority",
    "SELECT_CHILD_INTAKE",
    "SELECT_CHILD_ROUTE",
    "SELECT_GATEWAY",
    "SELECT_NONE",
    "STATUS_INVENTORY_UNAVAILABLE",
    "discover_project_gateway_inventory",
    "normalize_child_intake_caller",
    "prepare_project_gateway_delivery",
    "render_inventory_error",
)
