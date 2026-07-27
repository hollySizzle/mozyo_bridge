"""Action-time resolution + fenced single delegation for ``workflow proxy`` (Redmine #14546).

The impure side of the pure matrix (:mod:`...domain.coordinator_proxy`). It re-derives every
authority link from something the **external caller cannot assert**, hands the resulting statuses to
the matrix, and — only on a ``deliver`` decision — reserves the dedicated exactly-once fence and
performs **exactly one** send through an injected port.

What this module deliberately does *not* do is as important as what it does:

- it **never reads ``MOZYO_WORKSPACE_ID`` / ``MOZYO_AGENT_ROLE`` / ``MOZYO_LANE_ID``**. The whole
  point of the rail is that the caller has no attested identity, so accepting one from the caller's
  env would turn "I am not attested" into "I claim to be attested" — the exact forgery the observed
  dead end (#14500) must not be routed around. The workspace comes from the repo checkout's registry
  anchor; the target's identity comes from its own mzb1 assigned name, which the caller cannot mint;
- it **never relaxes the receiver's own gates**. The delegation is an ordinary anchored handoff to
  an attested agent; the coordinator still runs its own preflight, and ``sublane create`` still
  requires *its* sender attestation — which the coordinator has and the caller does not. The proxy
  moves the *decision*, not the *authority to act on it*;
- it **never resolves a target by pane, title, or "closest match"**. Exactly one live agent whose
  assigned name decodes to (this workspace, the bound role's provider, the default lane) is a
  target; zero and two-or-more are both zero-send.

Resolution is injectable end to end (``rows`` / ``gate_markers`` / ``fence`` / ``send_port``) so the
whole choreography — including every zero-send path and the send count — is testable without a live
herdr or Redmine.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
from typing import Callable, Mapping, Optional, Protocol, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.coordinator_proxy import (  # noqa: E501
    AUTHORITY_BLOCKED,
    AUTHORITY_MISSING,
    AUTHORITY_RESOLVED,
    FENCE_DUPLICATE,
    FENCE_OPEN,
    FENCE_RECONCILE,
    FENCE_STALE,
    FENCE_UNAVAILABLE,
    PROVIDER_RESOLVED,
    PROVIDER_UNRESOLVED,
    REASON_FENCE_UNAVAILABLE,
    WORKSPACE_RESOLVED,
    WORKSPACE_UNRESOLVED,
    ZERO_SEND,
    ProxyDecision,
    ProxyLinks,
    anchor_status_for,
    decide_proxy_delegation,
    normalize_action,
    target_status_from_cardinality,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (  # noqa: E501
    DEFAULT_LANE,
    DEFAULT_LANE_ROLES,
)

#: The env keys the proxy must never consult for authority. Pinned as data (not a comment) so a
#: test can assert the resolution is invariant under a caller that sets them (Redmine #14546).
CALLER_ENV_KEYS_NEVER_AUTHORITY: tuple[str, ...] = (
    "MOZYO_WORKSPACE_ID",
    "MOZYO_AGENT_ROLE",
    "MOZYO_LANE_ID",
)


@dataclass(frozen=True)
class ProxyTarget:
    """The live-resolved delegation target, or a fail-closed cardinality (value object)."""

    status: str
    assigned_name: str = ""
    locator: str = ""
    live: int = 0
    with_locator: int = 0


@dataclass
class ProxyContext:
    """Everything the action-time resolution derived, alongside the matrix's link statuses."""

    links: ProxyLinks
    workspace_id: str = ""
    role: str = ""
    project_scope: str = ""
    provider: str = ""
    target: ProxyTarget = field(default_factory=lambda: ProxyTarget(status="missing"))
    issue: str = ""
    journal: str = ""
    latest_gate_journal: str = ""
    authority_reason: str = ""
    detail: str = ""


# ---------------------------------------------------------------------------
# Live probes (each injectable; the live default is resolved lazily so importing this module
# never touches the registry / inventory / network).
# ---------------------------------------------------------------------------


def live_workspace_id(repo_root) -> str:
    """The registry + workspace anchor derived from the repo checkout (never from caller env)."""
    try:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            herdr_workspace_segment,
        )

        return herdr_workspace_segment(repo_root) or ""
    except Exception:  # noqa: BLE001 - an unreadable registry resolves no workspace (fail closed)
        return ""


def live_agent_rows(env: Mapping[str, str]) -> Sequence[Mapping]:
    """The live herdr ``agent list`` rows, or an empty sequence on an unreadable inventory."""
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            list_herdr_agent_rows,
        )

        return list_herdr_agent_rows(env)
    except Exception:  # noqa: BLE001 - an unreadable inventory yields no target (fail closed)
        return ()


def live_gate_markers(args: argparse.Namespace, issue: str) -> "tuple[tuple[str, ...], str]":
    """The issue's structured gate-marker journals + the latest one, from source-of-truth Redmine.

    Reuses the credential-gated :class:`LiveRedmineJournalSource` read boundary and the structured
    ``[mozyo:…]`` marker extraction — prose is never a source. Any unconfigured-credential /
    transport / decode failure yields ``((), "")`` so the anchor simply fails to verify (an
    unreachable Redmine must never read as a verified anchor). Errors are already
    credential/URL-redacted upstream and are not re-raised here.
    """
    issue = (issue or "").strip()
    if not issue:
        return (), ""
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
            LiveRedmineJournalSource,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            markers_from_source,
        )

        markers = markers_from_source(LiveRedmineJournalSource.from_environment(), issue)
    except Exception:  # noqa: BLE001 - any live-read failure fails the anchor gate closed
        return (), ""
    journals: list = []
    for marker in markers:
        if str(getattr(marker, "issue", "")).strip() != issue:
            continue  # a marker for another issue never verifies this anchor
        journal = str(getattr(marker, "journal", "")).strip()
        if journal:
            journals.append(journal)
    latest = journals[-1] if journals else ""  # markers are note-ordered
    return tuple(journals), latest


def resolve_proxy_target(
    rows: Sequence[Mapping], *, workspace_id: str, provider: str
) -> ProxyTarget:
    """Resolve the single live default-lane coordinator target (pure over ``rows``).

    A row qualifies only when its **mzb1 assigned name** decodes to this workspace, this provider,
    and the default lane — the agent's own launch-time attestation. Rows from another workspace are
    excluded by that decode, which is what makes a cross-workspace delegation structurally
    impossible rather than merely discouraged.
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        AGENT_KEY_NAME,
        _agent_locator,
        _norm_lane,
        decode_assigned_name,
    )

    ws = (workspace_id or "").strip()
    want_provider = (provider or "").strip()
    if not ws or not want_provider:
        return ProxyTarget(status=target_status_from_cardinality(0, 0))

    assigned_name = ""
    locator = ""
    live = 0
    with_locator = 0
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        decode = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not getattr(decode, "ok", False) or decode.identity is None:
            continue
        identity = decode.identity
        if identity.workspace_id != ws or identity.role != want_provider:
            continue
        if _norm_lane(identity.lane_id) != DEFAULT_LANE:
            continue
        live += 1
        row_locator = _agent_locator(row) or ""
        if row_locator:
            with_locator += 1
            if not locator:
                locator = row_locator
                assigned_name = str(row.get(AGENT_KEY_NAME) or "")
        elif not assigned_name:
            assigned_name = str(row.get(AGENT_KEY_NAME) or "")
    status = target_status_from_cardinality(live, with_locator)
    return ProxyTarget(
        status=status,
        assigned_name=assigned_name,
        locator=locator,
        live=live,
        with_locator=with_locator,
    )


def resolve_default_lane_authority(repo_root) -> "tuple[str, str, str, str]":
    """The default lane's durable role + scope, its status, and a blocked reason (if any).

    Returns ``(status, role, project_scope, reason)`` where ``status`` is one of
    :data:`AUTHORITY_RESOLVED` / :data:`AUTHORITY_MISSING` / :data:`AUTHORITY_BLOCKED`. A malformed
    declaration is **blocked**, not missing: an unreadable authority must not read as "none
    declared", because "none declared" is an ordinary state with a documented remedy while an
    unreadable one is a defect an operator has to see.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_role_authority_source import (  # noqa: E501
        load_parsed_role_bindings,
    )

    parsed = load_parsed_role_bindings(repo_root)
    if not parsed.ok:
        return AUTHORITY_BLOCKED, "", "", parsed.reason
    matches = [b for b in parsed.bindings if b.lane_id == DEFAULT_LANE]
    if not matches:
        return AUTHORITY_MISSING, "", "", ""
    if len(matches) >= 2:  # defensive: parse already rejects a slot collision
        return AUTHORITY_BLOCKED, "", "", "herdr_role_binding_ambiguous"
    binding = matches[0]
    if binding.role not in DEFAULT_LANE_ROLES:
        return AUTHORITY_BLOCKED, binding.role, binding.project_scope, "herdr_role_binding_invalid"
    return AUTHORITY_RESOLVED, binding.role, binding.project_scope, ""


def resolve_expected_provider(repo_root, role: str) -> str:
    """The provider ``provider_binding`` expects for a bound default-lane role, or ``""``."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_binding_source import (  # noqa: E501
        load_workflow_binding,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (  # noqa: E501
        ROLE_COORDINATOR as BINDING_COORDINATOR,
        ROLE_ROOT_COORDINATOR as BINDING_ROOT_COORDINATOR,
    )
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
        ROLE_GRANDPARENT_COORDINATOR,
    )

    key = BINDING_ROOT_COORDINATOR if role == ROLE_GRANDPARENT_COORDINATOR else BINDING_COORDINATOR
    try:
        binding, _warnings = load_workflow_binding(repo_root)
    except Exception:  # noqa: BLE001 - a broken provider config confirms no surface (fail closed)
        return ""
    if binding is None:
        return ""
    return binding.provider_for(key) or ""


# ---------------------------------------------------------------------------
# Resolution (everything except the fence, which is consulted only on an otherwise-deliverable set).
# ---------------------------------------------------------------------------


def resolve_proxy_context(
    args: argparse.Namespace,
    *,
    action: str,
    issue: str,
    journal: str,
    repo_root,
    env: Mapping[str, str],
    rows_provider: Optional[Callable[[Mapping[str, str]], Sequence[Mapping]]] = None,
    gate_markers_provider: Optional[Callable[..., "tuple[tuple[str, ...], str]"]] = None,
    workspace_provider: Optional[Callable[..., str]] = None,
) -> ProxyContext:
    """Re-derive every authority link at action time and assemble the matrix input (no fence).

    The fence link is left :data:`FENCE_UNAVAILABLE` here: it is filled in by
    :func:`execute_proxy_delegation` only when every other link already permits delivery, so a
    delegation that was going to be refused anyway never consumes a generation. A dry run therefore
    reports the full resolution while writing nothing.
    """
    resolve_ws = workspace_provider or live_workspace_id
    resolve_rows = rows_provider or live_agent_rows
    resolve_markers = gate_markers_provider or (lambda i: live_gate_markers(args, i))

    issue = (issue or "").strip()
    journal = (journal or "").strip()

    workspace_id = (resolve_ws(repo_root) or "").strip()
    workspace_status = WORKSPACE_RESOLVED if workspace_id else WORKSPACE_UNRESOLVED

    authority_status, role, project_scope, authority_reason = resolve_default_lane_authority(
        repo_root
    )
    provider = (
        resolve_expected_provider(repo_root, role) if authority_status == AUTHORITY_RESOLVED else ""
    )
    provider_status = PROVIDER_RESOLVED if provider else PROVIDER_UNRESOLVED

    target = (
        resolve_proxy_target(resolve_rows(env), workspace_id=workspace_id, provider=provider)
        if workspace_id and provider
        else ProxyTarget(status=target_status_from_cardinality(0, 0))
    )

    gate_journals, latest = ((), "")
    if issue and journal:
        gate_journals, latest = resolve_markers(issue)
    anchor_status = anchor_status_for(journal, gate_journals, latest=latest)

    links = ProxyLinks(
        action=action,
        workspace=workspace_status,
        authority=authority_status,
        provider=provider_status,
        target=target.status,
        anchor=anchor_status,
        fence=FENCE_UNAVAILABLE,
    )
    return ProxyContext(
        links=links,
        workspace_id=workspace_id,
        role=role,
        project_scope=project_scope,
        provider=provider,
        target=target,
        issue=issue,
        journal=journal,
        latest_gate_journal=latest,
        authority_reason=authority_reason,
    )


# ---------------------------------------------------------------------------
# The injected send port + its outcome.
# ---------------------------------------------------------------------------

SEND_DELIVERED = "delivered"
SEND_FAILED = "failed"


@dataclass(frozen=True)
class ProxySendOutcome:
    """The outcome of the single delegation send (value object)."""

    result: str
    rc: int = 0
    detail: str = ""


class ProxySendPort(Protocol):
    """The single delegation send seam (injected so tests count sends without a live herdr)."""

    def send(
        self, context: ProxyContext, action_id: str, *, args: argparse.Namespace
    ) -> ProxySendOutcome:
        ...


@dataclass(frozen=True)
class ProxyExecutionResult:
    """The result of an attempted single delegation (value object)."""

    sent: bool
    decision: str
    reason: str = ""
    detail: str = ""
    action_id: str = ""
    fence_state: str = ""
    send: Optional[ProxySendOutcome] = None


def execute_proxy_delegation(
    context: ProxyContext,
    *,
    args: argparse.Namespace,
    action: str,
    fence,
    send_port: ProxySendPort,
) -> ProxyExecutionResult:
    """Fence and perform the single delegation for an otherwise-deliverable context (fail-closed).

    Sequence: (1) every non-fence link must already permit delivery — a refusal here consumes no
    generation and never touches the store; (2) the store must be **usable**, and the execution path
    never bootstraps it (a silent re-create after a loss would let an already-delivered delegation
    be sent again); (3) reserve the route for this exact ``(issue, journal)`` — duplicate / stale /
    unresolved-prior-reserve each refuse with a fixed reason; (4) perform **exactly one** send with
    the minted action id; (5) record ``delivered`` / ``uncertain`` guarded by that id. An unknown
    outcome is recorded uncertain and never blind-retried.
    """
    from mozyo_bridge.core.state.coordinator_proxy_fence import (
        CoordinatorProxyFenceError,
        ProxyRouteKey,
        RESERVE_DUPLICATE,
        RESERVE_NEEDS_RECONCILE,
        RESERVE_STALE,
    )

    # (1) decide on everything except the fence; a refusal never reaches the store.
    pre = decide_proxy_delegation(context.links)
    if not pre.delivers and pre.reason != REASON_FENCE_UNAVAILABLE:
        return ProxyExecutionResult(
            sent=False, decision=ZERO_SEND, reason=pre.reason, detail=pre.detail,
            fence_state=context.links.fence,
        )

    route = ProxyRouteKey(
        workspace_id=context.workspace_id,
        lane_id=DEFAULT_LANE,
        role=context.role,
        action=normalize_action(action),
    )

    # (2) the store must be usable; the execution path never auto-creates it.
    if not fence.is_bootstrapped():
        return _fenced_zero_send(
            context, FENCE_UNAVAILABLE,
            "the delegation store is not bootstrapped (missing / lost); run `workflow proxy-fence "
            "--bootstrap` (or --recover after reconciling the lost delegation). The execution path "
            "never auto-creates it.",
        )

    # (3) reserve this exact durable decision.
    try:
        reserve = fence.reserve(route, issue=context.issue, journal=context.journal)
    except CoordinatorProxyFenceError as exc:
        return _fenced_zero_send(context, FENCE_UNAVAILABLE, f"delegation store unusable: {exc}")
    if not reserve.won:
        state = {
            RESERVE_DUPLICATE: FENCE_DUPLICATE,
            RESERVE_STALE: FENCE_STALE,
            RESERVE_NEEDS_RECONCILE: FENCE_RECONCILE,
        }.get(reserve.verdict, FENCE_UNAVAILABLE)
        return _fenced_zero_send(context, state, reserve.detail)

    # (4) exactly one send with the minted action id.
    outcome = send_port.send(context, reserve.action_id, args=args)
    if outcome.result == SEND_DELIVERED:
        fence.mark_delivered(route, reserve.action_id, detail=outcome.detail)
    else:
        fence.mark_uncertain(
            route, reserve.action_id, detail=outcome.detail or f"send {outcome.result}"
        )
    context.links = replace(context.links, fence=FENCE_OPEN)
    return ProxyExecutionResult(
        sent=True, decision="deliver", action_id=reserve.action_id, fence_state=FENCE_OPEN,
        detail=f"delegated once to {context.target.assigned_name}", send=outcome,
    )


def _fenced_zero_send(context: ProxyContext, fence_state: str, detail: str) -> ProxyExecutionResult:
    """Re-run the matrix with the observed fence state so the reason is the matrix's, not ad hoc."""
    context.links = replace(context.links, fence=fence_state)
    decision: ProxyDecision = decide_proxy_delegation(context.links)
    return ProxyExecutionResult(
        sent=False, decision=ZERO_SEND, reason=decision.reason,
        detail=f"{decision.detail}; {detail}", fence_state=fence_state,
    )


class OrchestrateHandoffProxySendPort:
    """The concrete delegation send: one anchored handoff to the resolved live coordinator.

    Reuses the ordinary anchored ``handoff send`` rail — the same preflight, receiver binding, and
    landing gates every other send goes through. Nothing about being a proxy relaxes them: the
    delegation is addressed to an explicit live locator with an explicit target lane and repo, and
    the receiver is the provider ``provider_binding`` resolved for the bound role.

    The kind is the existing ``custom`` label with a machine-readable summary rather than a new kind
    token: the handoff kind vocabulary is closed, and a delegation is not an implementation request,
    a review request, or a consultation. The summary carries the action, the durable anchor, and the
    opaque proxy action id so the coordinator's own record can correlate what it was handed.
    """

    def __init__(self, *, repo_root: str, receiver_provider: str) -> None:
        self._repo_root = repo_root
        self._receiver_provider = receiver_provider

    def send(
        self, context: ProxyContext, action_id: str, *, args: argparse.Namespace
    ) -> ProxySendOutcome:
        import contextlib
        import io

        from mozyo_bridge.application.commands import orchestrate_handoff

        send_args = argparse.Namespace(**vars(args))
        send_args.to = self._receiver_provider
        send_args.target = context.target.locator
        send_args.target_lane = DEFAULT_LANE
        send_args.target_repo = self._repo_root
        send_args.repo = self._repo_root
        send_args.mode = "queue-enter"
        send_args.source = "redmine"
        send_args.issue = context.issue
        send_args.journal = context.journal
        send_args.kind = "custom"
        send_args.summary = (
            f"coordinator proxy delegation: action={normalize_action(getattr(args, 'action', ''))} "
            f"role={context.role} lane={DEFAULT_LANE} anchor=redmine:issue={context.issue}:"
            f"journal={context.journal} proxy_action_id={action_id}. Read the durable anchor and "
            f"perform this one already-resolved action from your own attested runtime."
        )

        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = int(orchestrate_handoff(send_args, default_kind="custom") or 0)
        except SystemExit as exc:  # die() fail-closed leg -> a non-delivered send
            code = exc.code
            rc = code if isinstance(code, int) and code != 0 else 1
        captured = buf.getvalue().strip()
        if rc == 0:
            return ProxySendOutcome(
                result=SEND_DELIVERED, rc=0, detail="delegation handed to the live coordinator"
            )
        return ProxySendOutcome(
            result=SEND_FAILED, rc=rc,
            detail=f"delegation send fail-closed (rc={rc}): {captured[:200]}",
        )


__all__ = (
    "CALLER_ENV_KEYS_NEVER_AUTHORITY",
    "ProxyTarget",
    "ProxyContext",
    "live_workspace_id",
    "live_agent_rows",
    "live_gate_markers",
    "resolve_proxy_target",
    "resolve_default_lane_authority",
    "resolve_expected_provider",
    "resolve_proxy_context",
    "SEND_DELIVERED",
    "SEND_FAILED",
    "ProxySendOutcome",
    "ProxySendPort",
    "ProxyExecutionResult",
    "execute_proxy_delegation",
    "OrchestrateHandoffProxySendPort",
)
