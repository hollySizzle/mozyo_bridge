"""Argument-level fail-closed validation for a managed session start (Redmine #14242 R4).

The checks ``_prepare_session_locked`` performs on its own arguments **before any side
effect** — an unknown coordinator placement mode, a duplicate ``(provider, lane)`` slot, an
invalid managed permission policy, and (Redmine #13647 Tranche 2) the caller-supplied
whole-plan role / profile / provider / argv resolution. They share a shape: pure functions of the request that either
return or raise, producing no state the caller threads onward.

Extracted verbatim from the launch module as a leaf so that module stays inside the
module-health budget without an allowlist entry (integration disposition j#85316: the #14242
transplant onto the latest ``origin/main-next`` composition pushed it to 1009 lines). The
behaviour, the order, and every message are unchanged — this is a boundary move, not a rewrite.

Why these three and not a bigger slice: everything after them either resolves identity, reads
the store, or touches Herdr, so it is not argument validation and does not belong in a leaf that
promises "no side effect". Keeping the extraction to the pure prefix is what makes it reviewable
as behaviour-preserving.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (  # noqa: E501
    InvalidPermissionMode,
    permission_mode_argv,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.coordinator_placement_mode import (  # noqa: E501
    COORDINATOR_PLACEMENT_MODES,
    CoordinatorPlacementConfig,
    CoordinatorPlacementError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.lane_placement import (  # noqa: E501
    LANE_PLACEMENT_PROVIDERS as PAIR_ORDER_PROVIDERS,
)


def validate_pair_order(
    pair_order: "Sequence[str] | None",
    providers: Sequence[str],
    *,
    error_type: type,
) -> None:
    """Reject a malformed ``pair_order`` BEFORE any side effect (Redmine #14569 j#91284 R3-F1).

    ``pair_order`` is the ratio's ``order[0]``-side authority for a request the caller shrank
    to a subset, so it is held to the SAME domain the declared ``order`` already is
    (``lane_placement._normalize_order``): an **exact permutation** of the canonical provider
    vocabulary. Anything else — an unknown provider, a duplicate, a missing one, a non-string
    element, a non-sequence — is refused here rather than coerced downstream.

    Coercing it is not a theoretical hazard: ``pair_order=("unknown", "codex")`` used to be
    accepted, which made ``codex`` *not* the effective primary, so a gateway target-only heal
    resized the pair and handed the gateway's declared share to the surviving worker — and
    reported ``applied`` (measured, j#91299). Authority that nobody validated is not authority.

    The requested ``providers`` must also be a **subset** of it. A caller that names a stable
    pair order not containing what it is actually launching has contradicted itself, and the
    side the ratio would pick from that contradiction is meaningless.
    """
    if pair_order is None:
        return
    if isinstance(pair_order, (str, bytes)) or not isinstance(pair_order, (list, tuple)):
        raise error_type(
            f"pair_order must be a list naming each provider "
            f"{sorted(PAIR_ORDER_PROVIDERS)} exactly once, got "
            f"{type(pair_order).__name__}"
        )
    seen: list = []
    for element in pair_order:
        if not isinstance(element, str) or element not in PAIR_ORDER_PROVIDERS:
            raise error_type(
                f"pair_order element must be one of {sorted(PAIR_ORDER_PROVIDERS)}, "
                f"got {element!r}"
            )
        if element in seen:
            raise error_type(
                f"pair_order lists provider {element!r} more than once; it must be an "
                "exact permutation"
            )
        seen.append(element)
    if set(seen) != set(PAIR_ORDER_PROVIDERS):
        missing = sorted(set(PAIR_ORDER_PROVIDERS) - set(seen))
        raise error_type(
            f"pair_order must name every provider {sorted(PAIR_ORDER_PROVIDERS)} exactly "
            f"once; missing {missing}"
        )
    unknown = [p for p in providers if p not in seen]
    if unknown:
        raise error_type(
            f"pair_order {seen!r} does not contain the requested provider(s) {unknown!r}; "
            "a stable pair order that excludes what this run launches cannot say which "
            "side the declared ratio belongs to"
        )


def validate_session_request(
    *,
    providers: Sequence[str],
    lane_id: str,
    coordinator_placement_mode: str,
    coordinator_top_workspace_id: str,
    claude_permission_mode_default,
    env: Mapping[str, str],
    error_type: type,
    launch_context: object = None,
    pair_order: "Sequence[str] | None" = None,
    workflow_role_by_provider: object = None,
    launch_argv_by_provider: object = None,
) -> None:
    """Reject a malformed session request BEFORE any side effect (pure; raises ``error_type``).

    - **Unknown coordinator placement mode** (Redmine #14139). The composition roots pass a
      value the config loader already validated; this makes the pure entry point reject a bad
      string directly too, so an unknown mode can never silently degrade to per-project.
    - **Duplicate ``(provider, lane)`` slot** (spec §5 slot-uniqueness). Every requested provider
      shares this run's lane, so a repeated provider is a repeated slot: it would mint the SAME
      ``mzb1_<ws>_<role>_<lane>`` name twice (two launches / two renames), and the read side then
      fails closed with ``multiple_matches``, leaving the session unusable. Fail-closed rejection
      (not silent de-dup) matches the spec wording, so the CLI can keep its repeatable
      ``--agent`` flag.
    - **Unresolvable / contradictory whole plan** (Redmine #13647 Tranche 2). When the caller
      supplied a role-bearing per-slot plan, it is resolved and validated HERE — the pair is
      the unit, so a cross-slot defect (duplicate workflow role, two entries for one physical
      slot, one slot asked for two profiles, an unknown role / unregistered provider, an
      ambiguous governance anchor) is refused while nothing has been launched. A context with
      no ``slot_specs`` skips it entirely, so every pre-#13647 caller is byte-invariant **on
      this axis** — this step can only refuse, never place. It says nothing about the
      independent geometry axis, where an undeclared lane class lands on the #14568 product
      default (``split: down``).
    - **Invalid managed permission policy** (review j#73404). The lane chokepoint requests
      (codex, claude), so a validation that only fired inside the claude slot's launch would
      leave the codex gateway already started — a partial lane — when the env override is
      invalid. Applicability is data-driven (#13441 R1-F2): every requested provider is asked,
      and one answers only if its profile declares the managed permission concept. Validating
      here (rather than only in the launch preflight) keeps an invalid override fail-closed even
      on an adopt-only run.
    """
    validate_pair_order(pair_order, providers, error_type=error_type)
    validate_coordinator_placement_request(
        coordinator_placement_mode,
        coordinator_top_workspace_id,
        error_type=error_type,
    )
    seen_slots: set = set()
    lane_norm = _norm(lane_id)
    for provider in providers:
        slot = (provider, lane_norm)
        if slot in seen_slots:
            raise error_type(
                f"duplicate requested slot for provider {provider!r} in lane "
                f"{lane_norm or 'default'!r}; each (provider, lane) may be prepared "
                "once — remove the duplicate `--agent` argument"
            )
        seen_slots.add(slot)
    for provider in providers:
        try:
            permission_mode_argv(
                provider, policy_default=claude_permission_mode_default, env=env
            )
        except InvalidPermissionMode as exc:
            raise error_type(str(exc)) from exc
    _validate_slot_plan(
        providers=providers, lane_id=lane_id, launch_context=launch_context, error_type=error_type
    )
    _validate_runtime_role_projection(
        providers=providers,
        lane_id=lane_id,
        workflow_role_by_provider=workflow_role_by_provider,
        launch_argv_by_provider=launch_argv_by_provider,
        error_type=error_type,
    )


def _validate_runtime_role_projection(
    *,
    providers: Sequence[str],
    lane_id: str,
    workflow_role_by_provider: object,
    launch_argv_by_provider: object,
    error_type: type,
) -> None:
    """Validate the configured default-unit role/argv projection before writes."""
    if workflow_role_by_provider is None and launch_argv_by_provider is None:
        return
    if _norm(lane_id):
        raise error_type(
            "workflow-role runtime projection is valid only for the default coordinator "
            "unit; a sublane keeps its delegated coordinator / implementation roles"
        )
    if not isinstance(workflow_role_by_provider, Mapping) or not isinstance(
        launch_argv_by_provider, Mapping
    ):
        raise error_type(
            "workflow-role runtime projection requires both provider->role and "
            "provider->argv mappings"
        )
    requested = set(providers)
    if set(workflow_role_by_provider) != requested or set(launch_argv_by_provider) != requested:
        raise error_type(
            "workflow-role runtime projection must describe exactly the requested "
            f"providers {sorted(requested)}"
        )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (  # noqa: E501
        ROLE_COORDINATOR,
        ROLE_COORDINATOR_ASSISTANT,
    )
    from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.agent_launch_argv import (  # noqa: E501
        AgentLaunchArgvError,
        _reject_reserved_managed_flags,
        _validate_launch_argv_token,
    )

    allowed_roles = {ROLE_COORDINATOR, ROLE_COORDINATOR_ASSISTANT}
    seen_roles: set[str] = set()
    for provider in providers:
        role = workflow_role_by_provider[provider]
        if not isinstance(role, str) or role not in allowed_roles:
            raise error_type(
                f"default coordinator-unit provider {provider!r} has invalid workflow "
                f"role {role!r}; expected one of {sorted(allowed_roles)}"
            )
        if role in seen_roles:
            raise error_type(
                f"default coordinator-unit workflow role {role!r} is assigned twice"
            )
        seen_roles.add(role)
        argv = launch_argv_by_provider[provider]
        if isinstance(argv, (str, bytes)) or not isinstance(argv, (list, tuple)):
            raise error_type(
                f"default coordinator-unit launch argv for {provider!r} must be an "
                "ordered token sequence"
            )
        try:
            for token in argv:
                _validate_launch_argv_token(
                    token, source=f"coordinator-unit role {role!r}"
                )
            _reject_reserved_managed_flags(
                provider, tuple(argv), source=f"coordinator-unit role {role!r}"
            )
        except AgentLaunchArgvError as exc:
            raise error_type(str(exc)) from exc


def validate_coordinator_placement_request(
    mode: str,
    top_workspace_id: str,
    *,
    error_type: type,
) -> None:
    """Validate the complete operator placement authority without side effects.

    The public session-start API is also called directly by tests and adapters,
    bypassing the YAML loader. Constructing the domain config here keeps that
    path fail-closed on a missing/inert top authority exactly like production.
    """
    if mode not in COORDINATOR_PLACEMENT_MODES:
        raise error_type(
            f"unknown coordinator placement mode {mode!r}; "
            f"expected one of {sorted(COORDINATOR_PLACEMENT_MODES)}"
        )
    try:
        CoordinatorPlacementConfig(
            mode=mode,
            top_workspace_id=top_workspace_id,
        )
    except CoordinatorPlacementError as exc:
        raise error_type(str(exc)) from exc


def _validate_slot_plan(
    *,
    providers: Sequence[str],
    lane_id: str,
    launch_context: object,
    error_type: type,
) -> None:
    """Resolve + validate the caller's whole pair plan, or fail closed (#13647 Tranche 2).

    Deferred imports keep the pure domain plan out of this leaf's import-time surface and
    the adapter's provider vocabulary out of the domain (the plan takes its vocabularies as
    injected data — see ``herdr_lane_launch_plan``).

    The plan is reconciled against ``providers`` — the exact slot set this launch starts —
    so a partial / foreign plan cannot pass by being internally consistent.

    The resolved plan is deliberately NOT returned: Tranche 2 is the fail-closed gate, and
    the launch still builds its argv exactly as before. Composing the plan into the argv
    build is the later tranche, so this step can only refuse — never change a launch that
    would otherwise have succeeded.
    """
    specs = tuple(getattr(launch_context, "slot_specs", ()) or ())
    if not specs:
        # No role-bearing plan supplied: this gate contributes nothing, byte-for-byte the
        # pre-#13647 role-plan handling. NOT a pre-#13647 launch — the geometry axis is
        # resolved elsewhere and defaults to #14568's `split: down` when undeclared.
        return
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (  # noqa: E501
        WORKFLOW_ROLES,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_lane_launch_plan import (  # noqa: E501
        LaneLaunchPlanError,
        resolve_lane_launch_plan,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (  # noqa: E501
        AGENT_PROVIDERS,
    )
    from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.lane_placement import (  # noqa: E501
        LANE_PLACEMENT_LANE_CLASSES,
        LANE_PLACEMENT_SPLIT_DIRECTIONS,
    )

    try:
        resolve_lane_launch_plan(
            lane_class="default" if not _norm(lane_id) else "sublane",
            slot_specs=specs,
            # The launch's ACTUAL slot set: the plan must account for exactly it (review
            # j#85859 F2). Passing the request in is what makes this a whole-plan gate
            # instead of a structural check on data unrelated to what will be started.
            request_providers=tuple(providers),
            known_providers=AGENT_PROVIDERS,
            known_roles=WORKFLOW_ROLES,
            # The canonical closed geometry vocabularies (#13646 §5.1), injected here so the
            # pure plan leaf validates the geometry without importing the config context.
            known_lane_classes=LANE_PLACEMENT_LANE_CLASSES,
            known_splits=LANE_PLACEMENT_SPLIT_DIRECTIONS,
            lane_kind=getattr(launch_context, "lane_kind", None),
            anchors=tuple(getattr(launch_context, "anchors", ()) or ()),
        )
    except LaneLaunchPlanError as exc:
        raise error_type(
            f"managed-launch plan refused: {exc}. No workspace / tab / agent was created."
        ) from exc


__all__ = ("validate_session_request",)
