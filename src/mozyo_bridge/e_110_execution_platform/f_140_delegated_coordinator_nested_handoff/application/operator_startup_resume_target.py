"""Action-time exact target / generation resolver for the resume leg (Redmine #13813 F2).

The Design Answer (j#79332 §2) requires re-resolving the exact live target + generation at
action time — never from a saved projection / pane title / cache — and failing soft to
zero-send on any drift. This resolver, given the durable gate, validates the live world
against the gate's pins in order and returns ``None`` (the leg then feeds an ``unresolved``
observation to the orchestrator -> zero-send) unless every check passes:

1. the lane lifecycle record exists, is **active**, is bound to the gate's original issue,
   and its ``lane_generation`` equals the gate's pinned ``agent_generation`` (the exact
   generation gate; a recycled lane bumps ``lane_generation`` and fails closed);
2. the repo's action-time provider binding still resolves the workflow ``target_role`` to the
   gate's pinned ``provider_id`` slot (a binding drift / unbound role fails closed), and exactly
   one declared pin has the gate's runtime identity ``(runtime_role, provider_id,
   assigned_name)`` ``stable_identity`` — the pin's ``role`` is the runtime role, NOT the
   workflow role (Design Answer j#79405 §A);
3. exactly one live inventory row carries that ``assigned_name``, and its live locator
   equals the declared pin's ``locator`` (the ``ProcessGenerationPin`` live-generation
   discriminant) — missing / duplicate / foreign / recycled fail closed;
4. the herdr identity self-attestation for that live locator is ``ok`` for the gate's
   workspace / ``runtime_role`` / lane.

Only then does it bind the #13760 visible read to the live locator and return the live
:class:`GateTarget` (the gate's pinned identity with ``agent_generation = lane_generation``,
which the projection re-verifies against the pin). Every live read is an injectable seam so
the leg is proven through the production composition root with fakes.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (
    GateTarget,
    OperatorStartupGate,
)

#: How many visible lines to read for the startup-clear classification (matches the pre-send
#: admission gate's read budget).
RESUME_READ_LINES = 60

#: Read the lane lifecycle record for (workspace_id, lane_id), or None. Injectable.
LifecycleGet = Callable[[str, str], Optional[object]]
#: List the live inventory rows. Injectable.
InventoryList = Callable[[Mapping[str, str]], Sequence[Mapping[str, object]]]
#: Read the identity attestation record for an assigned name, or None. Injectable.
AttestationRead = Callable[[str], Optional[object]]
#: Read a live pane's visible content (``locator, lines -> text``). Injectable.
CaptureVisible = Callable[[str, int], object]


def _default_lifecycle_get(workspace_id: str, lane_id: str) -> Optional[object]:
    try:
        from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
        from mozyo_bridge.core.state.lane_lifecycle_model import LaneLifecycleKey

        return LaneLifecycleStore().get(
            LaneLifecycleKey(repo_workspace_id=workspace_id, lane_id=lane_id)
        )
    except Exception:  # noqa: BLE001 - unreadable lifecycle store -> no target (zero-send)
        return None


def _default_inventory(env: Mapping[str, str]) -> Sequence[Mapping[str, object]]:
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (
            list_herdr_agent_rows,
        )

        return list(list_herdr_agent_rows(env))
    except Exception:  # noqa: BLE001 - inventory failure -> empty (zero-send)
        return []


def _default_attestation_read(assigned_name: str) -> Optional[object]:
    try:
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            HerdrIdentityAttestationStore,
        )

        return HerdrIdentityAttestationStore().read(assigned_name)
    except Exception:  # noqa: BLE001 - attestation store unreadable -> None (fails the ok gate)
        return None


def _default_capture(locator: str, lines: int) -> object:
    """Read the live locator's visible pane via the runtime transport binding (herdr-bound).

    Builds the same backend-neutral binding the send rail uses (``run_tmux`` / ``capture_pane``
    mapped onto the herdr ``read_pane`` visible source) and reads the pane. Fail-soft to ``""``
    on any error — the projection treats an unreadable pane as unreadable and zero-sends,
    never decaying to "clear".
    """
    try:
        from mozyo_bridge.application import commands
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.transport_binding import (
            resolve_runtime_transport_binding,
        )

        binding = resolve_runtime_transport_binding(
            tmux_run_tmux=commands.run_tmux,
            tmux_capture_pane=commands.capture_pane,
        )
        return binding.capture_pane(locator, lines)
    except Exception:  # noqa: BLE001 - unreadable pane / unconfigured backend -> "" (zero-send)
        return ""


#: Re-resolve the live action-time repo identity from the explicit target repo root + the gate's
#: repo-relative ``execution_root``: ``(workspace_id, repo_identity_digest, execution_root)``, or
#: None (unresolved / execution_root escapes the repo -> zero-send). Injectable.
WorkspaceResolve = Callable[[str, str, Mapping[str, str]], Optional["tuple[str, str, str]"]]
#: Resolve the action-time runtime provider bound to a workflow ``target_role`` from the repo's
#: current provider binding, or None if unbound (fail-soft zero-send). Injectable.
BindingResolve = Callable[[str, str, Mapping[str, str]], Optional[str]]


def _default_workspace_resolve(
    repo_root: str, execution_root: str, env: Mapping[str, str]
) -> Optional["tuple[str, str, str]"]:
    """Re-resolve the live action-time repo identity from ``repo_root``, or None (zero-send).

    Resolves the workspace id from the **explicit target repo root** via the registry / anchor
    authority (read-only) — never the anchor-less sender identity, which is always ``missing_anchor``
    and made the default resolver inert (review j#79392 F1). Derives the pasteable
    ``repo_identity_digest`` over the registry workspace id (re-derivable without a checkout path).

    The execution root is **re-derived, not forced to** ``"."`` (review j#79481 F3): the gate's
    repo-relative ``execution_root`` is confined under the live repo root with symlink-aware
    :func:`resolve_execution_workdir`, and the confined pointer is returned. A nested
    ``projects/x`` that resolves under the repo is honored; an unresolved workspace or an
    execution_root that escapes the repo (``..`` or symlink) returns None (Design Answer j#79405 §C).
    """
    try:
        from pathlib import Path

        from mozyo_bridge.core.state.workspace_registry import resolve_canonical_session
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_send import (
            resolve_execution_workdir,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (
            repo_identity_digest,
        )

        if not str(repo_root).strip():
            return None
        if resolve_execution_workdir(repo_root, execution_root) is None:
            return None  # execution_root escapes the repo (traversal / symlink) -> zero-send.
        resolved = resolve_canonical_session(Path(repo_root))
        workspace_id = str(getattr(resolved, "workspace_id", "") or "").strip()
        if not workspace_id:
            return None
        return (workspace_id, repo_identity_digest(workspace_id), execution_root)
    except Exception:  # noqa: BLE001 - unresolved live identity -> None (zero-send)
        return None


def _default_binding_resolve(
    target_role: str, repo_root: str, env: Mapping[str, str]
) -> Optional[str]:
    """The runtime provider the repo's action-time binding assigns to ``target_role``, or None.

    Reads the repo-local ``provider_binding`` config from the explicit target repo root and
    resolves the workflow role to its provider, fail-closed (an unbound role or a config-load
    failure returns None -> the resolver zero-sends). This is the action-time consistency gate the
    Design Answer (j#79405 §A) requires: the workflow ``target_role`` must still bind to the gate's
    pinned ``provider_id`` slot, so a later binding change is caught rather than silently trusted.
    """
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (
            resolve_role_provider,
        )

        return resolve_role_provider(target_role, repo_root=str(repo_root) or None)
    except Exception:  # noqa: BLE001 - unbound role / unreadable config -> None (zero-send)
        return None


def _live_row_for(
    rows: Sequence[Mapping[str, object]], assigned_name: str
) -> Optional[Mapping[str, object]]:
    """The single live inventory row for ``assigned_name``, or None (missing / duplicate)."""
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
        AGENT_KEY_NAME,
    )

    matches = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get(AGENT_KEY_NAME, "")).strip() == assigned_name
    ]
    return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True)
class ResumeTargetResolver:
    """Resolves the action-time live target for a gate, or None (fail-soft zero-send)."""

    env: Mapping[str, str]
    repo_root: str = ""
    lifecycle_get: LifecycleGet = field(default=_default_lifecycle_get)
    inventory: InventoryList = field(default=_default_inventory)
    attestation_read: AttestationRead = field(default=_default_attestation_read)
    capture: CaptureVisible = field(default=_default_capture)
    workspace_resolve: WorkspaceResolve = field(default=_default_workspace_resolve)
    binding_resolve: BindingResolve = field(default=_default_binding_resolve)
    read_lines: int = RESUME_READ_LINES

    def resolve(self, gate: OperatorStartupGate, env: Mapping[str, str]) -> Optional[object]:
        target = gate.target

        # 1. Lane lifecycle record: exists, active, issue binding, exact generation AND exact
        #    CAS revision (review j#79366 F1 — a recycled/mutated lane fails closed).
        record = self.lifecycle_get(target.workspace_id, target.lane_id)
        if record is None:
            return None
        try:
            from mozyo_bridge.core.state.lane_lifecycle_model import DISPOSITION_ACTIVE
        except Exception:  # noqa: BLE001
            return None
        if getattr(record, "lane_disposition", None) != DISPOSITION_ACTIVE:
            return None
        if str(getattr(record, "issue_id", "")).strip() != gate.original_request.issue:
            return None
        if getattr(record, "lane_generation", None) != target.agent_generation:
            return None
        if getattr(record, "revision", None) != target.lane_revision:
            return None

        # 2a. Action-time provider-binding consistency (Design Answer j#79405 §A): the repo's
        #     current binding must still resolve the workflow ``target_role`` to the gate's pinned
        #     ``provider_id`` slot. A binding drift (or an unbound role) fails closed — the workflow
        #     role is NOT silently converted to a provider; the runtime_role / provider_id the gate
        #     pinned are matched against the live pin below.
        bound_provider = self.binding_resolve(target.target_role, self.repo_root, env)
        if not bound_provider or bound_provider != target.provider_id:
            return None

        # 2b. Exactly one declared pin with the gate's declared identity
        #     ``(runtime_role, provider_id, assigned_name)``. ``runtime_role`` is the declared
        #     pin's ``role`` (``ProcessGenerationPin.role``), NOT the workflow role (j#79405 §A)
        #     — since #13920 that is the SLOT label (``gateway`` / ``worker``), and it is matched
        #     here against the pin it was produced from, so the two always speak one vocabulary.
        #     The herdr identity role (the provider token) is a DIFFERENT axis, compared as
        #     ``provider_id`` here and passed to the self-attestation join in step 4.
        wanted = (target.runtime_role, target.provider_id, target.target_assigned_name)
        try:
            pins = [
                pin
                for pin in getattr(record, "declared_pins", ())
                if getattr(pin, "stable_identity", None) == wanted
            ]
        except Exception:  # noqa: BLE001 - malformed declared slots -> fail closed
            return None
        if len(pins) != 1:
            return None
        pin = pins[0]

        # 3. Exactly one live inventory row for the assigned name, and its FULL
        #    ProcessGenerationPin.match_key (role/provider/name/locator/runtime_revision) equals
        #    the declared pin's (review j#79366 F1 — a foreign provider / runtime-revision drift
        #    fails closed, not just a locator match).
        from mozyo_bridge.core.state.lane_lifecycle_model import ProcessGenerationPin
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
            _agent_locator,
        )

        row = _live_row_for(self.inventory(env), target.target_assigned_name)
        if row is None:
            return None
        live_locator = str(_agent_locator(row)).strip()
        if not live_locator:
            return None
        try:
            live_pin = ProcessGenerationPin(
                # ``role`` is the DECLARED slot label (``gateway`` / ``worker``), not an
                # observable of the live row (Redmine #13920). A herdr `agent list` row carries
                # no slot label at all, and its mzb1 identity ``role`` segment is the PROVIDER
                # token — a different vocabulary (``herdr_target_resolution``: "the mzb1 role
                # field is a runtime *provider*"). Reading it here would compare a provider
                # against a slot label and fail closed on every canonical pin; the provider is
                # compared on its own axis below.
                role=target.runtime_role,
                provider=str(row.get("provider") or target.provider_id).strip(),
                assigned_name=target.target_assigned_name,
                locator=live_locator,
                runtime_revision=str(row.get("runtime_revision") or "").strip(),
            )
        except Exception:  # noqa: BLE001 - malformed live pin -> fail closed
            return None
        if live_pin.match_key != pin.match_key:
            return None

        # 4. Identity self-attestation for the live locator is ok.
        #    ``expected_role`` is the PROVIDER, not the gate's ``runtime_role`` (Redmine #13920).
        #    A startup self-attestation records its herdr identity role, which IS the provider
        #    token (``codex`` / ``claude``) — the same value #13809's adopt gate passes here
        #    (``expected_role=provider``). Since #13920 the declared pin's ``role`` is a SLOT
        #    label (``gateway`` / ``worker``), so feeding ``runtime_role`` in would compare
        #    ``worker`` against a recorded ``claude`` and join ATTEST_CONFLICT on every
        #    canonical pin — resolving no resume target for a perfectly healthy lane.
        try:
            from mozyo_bridge.core.state.herdr_identity_attestation import evaluate_attestation

            join = evaluate_attestation(
                self.attestation_read(target.target_assigned_name),
                live_locator=live_locator,
                expected_workspace_id=target.workspace_id,
                expected_role=target.provider_id,
                expected_lane=target.lane_id,
            )
        except Exception:  # noqa: BLE001 - attestation evaluation failure -> fail closed
            return None
        if not getattr(join, "ok", False):
            return None

        # 5. Re-resolve the live action-time repo identity FROM THE EXPLICIT REPO ROOT (registry
        #    authority, not the gate's own identity nor the anchor-less sender identity — j#79405
        #    §C) and require it EXACTLY matches the gate's workspace_id / repo_identity_digest /
        #    execution_root. The gate's execution_root is re-derived + symlink-confined under the
        #    live repo root (review j#79481 F3); a wrong repo / escaping root fails closed.
        live_identity = self.workspace_resolve(self.repo_root, target.execution_root, env)
        if live_identity is None:
            return None
        live_workspace_id, live_digest, live_execution_root = live_identity
        if (
            live_workspace_id != target.workspace_id
            or live_digest != target.repo_identity_digest
            or live_execution_root != target.execution_root
        ):
            return None

        # Confirmed. Build the live target (gate identity + confirmed live generation +
        # revision) and bind the #13760 visible read to the live locator.
        live_target = dataclasses.replace(
            target, agent_generation=record.lane_generation, lane_revision=record.revision
        )
        captured_locator = live_locator
        read_lines = self.read_lines
        capture = self.capture

        def _read_visible() -> object:
            return capture(captured_locator, read_lines)

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_gate_projection import (
            RESOLUTION_RESOLVED,
            ObservedStartupTarget,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_leg import (
            ObservedTargetResolution,
        )

        return ObservedTargetResolution(
            observed=ObservedStartupTarget(resolution=RESOLUTION_RESOLVED, target=live_target),
            read_visible=_read_visible,
            profile_version=gate.classification.profile_version,
            classifier_version=gate.classification.classifier_version,
            locator=captured_locator,
        )


__all__ = (
    "RESUME_READ_LINES",
    "LifecycleGet",
    "InventoryList",
    "AttestationRead",
    "CaptureVisible",
    "WorkspaceResolve",
    "BindingResolve",
    "ResumeTargetResolver",
)
