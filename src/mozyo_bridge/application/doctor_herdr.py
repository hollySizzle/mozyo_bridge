"""Doctor herdr backend probe boundary (Redmine #13355).

The tmux→herdr gap audit (#13331 j#73370) found doctor carries zero herdr
references: a pure-herdr operator has no CLI way to see whether the herdr server
is reachable, which managed agents are live, and whether this repo's herdr
workspace identity resolves. This module is the doctor section closing that gap,
in the same OOP-first shape as every other ``doctor_*`` boundary (#12833 family):

- :class:`HerdrDoctorReads` is the port for the external read, and
  :class:`LiveHerdrDoctorReads` the live adapter driving the shared
  :func:`~mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider
  .application.herdr_observability.read_herdr_inventory` read model at call time.
- :func:`evaluate_herdr_section` is the pure verdict policy building the section
  dict from the inventory view alone (no I/O, no ``argparse.Namespace``).
- :class:`HerdrSectionUseCase` composes both.

Backend gating (tmux byte-invariance): the section is **conditional** — when the
target repo's config does not select the herdr backend the use case returns
``None`` and the doctor section map carries no ``herdr`` key at all, so the
``backend: tmux`` doctor output (text and ``--json``) stays byte-for-byte
unchanged. Handler placement follows the #12612 precedent: the collector lives
in this dedicated module, not in an allowlisted oversized module.

Fail-closed semantics (the acceptance the US pins): a selected-but-broken herdr
transport — binary unconfigured / not found, server down (spawn error, timeout,
non-zero exit), unreadable payload — is an ``error`` section with the transport
reason and an operator next_action, dragging the overall doctor verdict to
"needs attention". Read-only and diagnostic-only: the probe never launches,
repairs, or asserts workflow truth.
"""

from __future__ import annotations

import argparse
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

from mozyo_bridge.core.state.herdr_identity_attestation import (
    AttestationJoin,
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
    evaluate_attestation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
    HerdrInventoryView,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (  # noqa: E501
    REASON_BINARY_AMBIGUOUS,
    REASON_BINARY_NOT_FOUND,
    REASON_BINARY_UNCONFIGURED,
    REASON_BINARY_UNSAFE_PATH,
)

# Operator guidance per fail-closed inventory reason. The binary reasons are a
# trusted-environment configuration gap; everything else means the herdr server
# itself did not answer with a readable inventory (down / hung / incompatible).
_BINARY_NEXT_ACTION = (
    "make herdr resolvable from the trusted environment (Redmine #13496): either "
    "put an executable `herdr` on the trusted PATH, or set MOZYO_HERDR_BINARY "
    "(daemon env) to the herdr executable, then re-run `mozyo-bridge doctor`"
)
_BINARY_AMBIGUOUS_NEXT_ACTION = (
    "more than one distinct `herdr` executable resolved from the trusted PATH "
    "(Redmine #13496): pin the intended one by setting MOZYO_HERDR_BINARY (daemon "
    "env) to its absolute path, or remove the extra PATH entry, then re-run "
    "`mozyo-bridge doctor`"
)
_BINARY_UNSAFE_PATH_NEXT_ACTION = (
    "the trusted PATH has an empty or relative component that a shell would "
    "resolve against the current directory (Redmine #13496): remove the unsafe "
    "component (no leading/trailing/double `:` and no `.`), or set "
    "MOZYO_HERDR_BINARY (daemon env) to the herdr absolute path, then re-run "
    "`mozyo-bridge doctor`"
)
_SERVER_NEXT_ACTION = (
    "herdr did not return a readable agent inventory (server down or "
    "unresponsive): start/restart the herdr server, then re-run "
    "`mozyo-bridge doctor`"
)
_WORKSPACE_SEGMENT_WARNING = (
    "this repo's herdr workspace segment did not resolve (no registry anchor / "
    "worktree token); herdr sends from this repo will fail identity attestation"
)
_WORKSPACE_SEGMENT_NEXT_ACTION = (
    "resolve the workspace identity (run `mozyo-bridge doctor` from the repo "
    "root of a registered checkout, or re-run the workspace bootstrap) so the "
    "mzb1 workspace segment can be derived"
)
_ATTESTATION_NEXT_ACTION = (
    "one or more managed agents have no valid startup self-attestation (their "
    "injected identity env is unverified, so their handoff sends fail closed): "
    "herdr cannot read or repair a running process's env, so recover with an "
    "owner-approved close of the affected pane(s) + a same-slot relaunch via "
    "`mozyo-bridge herdr session-start` (the relaunch re-runs the self-check). "
    "First check `mozyo-bridge herdr attestation-store status` (Redmine #13882): "
    "when the whole home reads unattested at once, the cause is usually the store's "
    "schema, not the panes — and relaunching cannot fix that"
)


def build_attestation_joins(
    view: HerdrInventoryView,
    reader: Callable[[str], Optional[IdentityAttestationRecord]],
) -> dict[str, AttestationJoin]:
    """Join each managed workspace agent with its recorded startup self-attestation.

    The doctor-side read: for every managed row in THIS repo's workspace, read its
    self-attestation record (via the injected ``reader``) and evaluate it against the
    live locator + decoded identity (Redmine #13637). Returns ``{assigned_name ->
    AttestationJoin}``. Empty when the inventory is unreadable or the workspace
    segment is unresolvable (those are already their own non-green states). Reading a
    process's live env is impossible (herdr exposes no such surface); this joins the
    durable *self*-attestation the agent wrote at boot, never a live-env read.
    """
    joins: dict[str, AttestationJoin] = {}
    if not view.ok or not view.workspace_segment:
        return joins
    for agent in view.workspace_agents():
        joins[agent.name] = evaluate_attestation(
            reader(agent.name),
            live_locator=agent.locator,
            expected_workspace_id=agent.workspace_id,
            expected_role=agent.role,
            expected_lane=agent.lane_id,
        )
    return joins


@runtime_checkable
class HerdrDoctorReads(Protocol):
    """Port: read the herdr observability inventory view for the doctor target.

    ``describe`` returns the shared :class:`HerdrInventoryView`; implementations
    own the external read (repo-local config, workspace segment, live
    ``agent list``). The policy depends only on the returned view.
    """

    def describe(self) -> HerdrInventoryView:
        ...


class LiveHerdrDoctorReads:
    """Live adapter: drive the shared herdr inventory read for the doctor target.

    Resolves the doctor target through the ``doctor`` module *at call time*
    (matching the other section adapters, so ``doctor.doctor_target`` patches
    keep intercepting it) and hands it to the one shared
    :func:`read_herdr_inventory` read model — decode / status-mapping semantics
    can never drift from the ``status`` / ``observe reload`` surfaces.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self._args = args

    def describe(self) -> HerdrInventoryView:
        from mozyo_bridge.application import doctor as _doctor
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
            read_herdr_inventory,
        )

        return read_herdr_inventory(_doctor.doctor_target(self._args))


def evaluate_herdr_section(
    view: HerdrInventoryView,
    *,
    attestations: Optional[Mapping[str, AttestationJoin]] = None,
) -> Optional[dict[str, Any]]:
    """Pure policy: derive the doctor ``herdr`` section from the inventory view.

    Returns ``None`` when the herdr backend is not selected (the section is then
    absent from the doctor map — tmux byte-invariance). Otherwise:

    - an unreadable inventory is ``error`` (fail-closed: a down herdr server is
      a hard diagnostic fail) with the reason-matched operator next_action;
    - a readable inventory with an unresolvable workspace segment is ``warning``
      (herdr identity attestation for this repo is broken even though the
      server answers);
    - a readable inventory whose managed workspace agents have an absent / stale /
      missing / conflicting startup self-attestation is ``warning`` (Redmine #13637):
      their injected identity env is unverified so their handoff sends fail closed.
      Each such agent is noted with its value-free self-attestation state (never a
      live-env read — herdr has no such surface — and never an env value / secret);
    - otherwise ``ok``, listing every observed agent (managed rows with their
      decoded slot + runtime state, unmanaged rows kept visible with their
      decode reason) and noting when this repo's workspace has no live agent.

    ``attestations`` (``{assigned_name -> AttestationJoin}``, from
    :func:`build_attestation_joins`) is optional so the tmux / server-down / no-segment
    paths and callers that do not join stay byte-invariant; when omitted no
    attestation verdict is applied.
    """
    if not view.backend_selected:
        return None

    section: dict[str, Any] = {
        "backend": "herdr",
        "workspace_segment": view.workspace_segment,
        "server": {
            "reachable": view.ok,
            "reason": view.reason,
            "detail": view.detail,
        },
        "agents": [agent.to_record() for agent in view.agents],
        "counts": {
            "total": len(view.agents),
            "managed": len(view.managed_agents),
            "workspace": len(view.workspace_agents()),
            "unmanaged": len(view.unmanaged_agents),
        },
        "notes": [],
        "next_action": [],
    }

    if not view.ok:
        section["status"] = "error"
        if view.reason == REASON_BINARY_AMBIGUOUS:
            section["next_action"].append(_BINARY_AMBIGUOUS_NEXT_ACTION)
        elif view.reason == REASON_BINARY_UNSAFE_PATH:
            section["next_action"].append(_BINARY_UNSAFE_PATH_NEXT_ACTION)
        elif view.reason in (REASON_BINARY_UNCONFIGURED, REASON_BINARY_NOT_FOUND):
            section["next_action"].append(_BINARY_NEXT_ACTION)
        else:
            section["next_action"].append(_SERVER_NEXT_ACTION)
        return section

    if not view.workspace_segment:
        section["status"] = "warning"
        section["notes"].append(_WORKSPACE_SEGMENT_WARNING)
        section["next_action"].append(_WORKSPACE_SEGMENT_NEXT_ACTION)
        return section

    if not view.workspace_agents():
        section["status"] = "ok"
        section["notes"].append(
            "no live managed agent in this repo's herdr workspace "
            f"({view.workspace_segment}); launch panes with `mozyo` or "
            "`mozyo-bridge herdr session-start` if agents are expected"
        )
        return section

    # Startup self-attestation join (Redmine #13637): a managed live agent whose
    # injected identity env is unverified (absent / stale / missing / conflicting
    # self-attestation) is non-green — its handoff sends fail closed. Value-free by
    # construction (the join reason names states / variables, never env values).
    joins = attestations or {}
    unattested = [
        (agent, joins[agent.name])
        for agent in view.workspace_agents()
        if agent.name in joins and not joins[agent.name].ok
    ]
    if unattested:
        section["status"] = "warning"
        for agent, join in unattested:
            section["notes"].append(
                f"managed agent {agent.name} ({agent.role}, lane {agent.lane_id or 'default'}): "
                f"startup self-attestation {join.state} — {join.reason}"
            )
        section["next_action"].append(_ATTESTATION_NEXT_ACTION)
        return section

    section["status"] = "ok"
    return section


class HerdrSectionUseCase:
    """Use case: read the inventory via the port, apply the pure verdict policy.

    ``execute`` returns the legacy-shaped section dict, or ``None`` when the
    herdr backend is not selected for the doctor target (the caller then omits
    the ``herdr`` key entirely — tmux byte-invariance).
    """

    def __init__(
        self,
        reads: HerdrDoctorReads,
        *,
        attestation_reader: Optional[
            Callable[[str], Optional[IdentityAttestationRecord]]
        ] = None,
    ) -> None:
        self._reads = reads
        self._attestation_reader = (
            attestation_reader or HerdrIdentityAttestationStore().read
        )

    def execute(self) -> Optional[dict[str, Any]]:
        view = self._reads.describe()
        # A herdr-selected, readable inventory joins each managed workspace agent
        # with its startup self-attestation (Redmine #13637); the tmux / server-down /
        # no-segment sections need no join and stay byte-invariant.
        joins = build_attestation_joins(view, self._attestation_reader)
        return evaluate_herdr_section(view, attestations=joins)


__all__ = (
    "HerdrDoctorReads",
    "HerdrSectionUseCase",
    "LiveHerdrDoctorReads",
    "build_attestation_joins",
    "evaluate_herdr_section",
)
