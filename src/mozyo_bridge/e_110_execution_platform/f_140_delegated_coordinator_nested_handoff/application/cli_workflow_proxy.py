"""``workflow proxy`` / ``workflow proxy-fence``: the external-client delegation rail (Redmine #14546).

``workflow proxy`` is the sanctioned way for an **external coordinator client** — an operator shell
or API caller that is not an attested lane agent — to hand one already durably resolved high-level
action to the live attested default coordinator, exactly once.

It exists because the two entrypoints an external client would otherwise reach both stop, correctly,
before any effect: ``workflow step`` at ``herdr_sender_identity_unresolved`` and ``sublane create
--execute`` at ``missing_identity`` + ``sender_attestation``. Those gates are right — the caller
genuinely has no launch-time identity — but with no third option the only ways forward were forging
``MOZYO_*`` by hand or typing into the coordinator's pane, both of which defeat the audit boundary
they protect (Redmine #14500 / #14546 j#89697, j#89712).

The rail does not weaken those gates. It never claims an identity for the caller: the workspace comes
from the repo checkout's registry anchor, the role from the durable repo-local authority, the
provider from ``provider_binding``, and the target from the coordinator's **own** mzb1 startup
attestation. The delegated action must already be carried by a structured gate marker on the named
Redmine issue, and it must be the current one — a superseded journal is refused. Delivery is a
single ordinary anchored handoff, fenced so the same durable decision is delegated once.

DRY-RUN by default: without ``--execute`` the command resolves and reports every link and writes
nothing (no fence row, no send).
"""

from __future__ import annotations

import argparse
import json
import os

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.coordinator_proxy import (  # noqa: E501
    PROXY_ACTIONS,
    ZERO_SEND,
    decide_proxy_delegation,
    normalize_action,
)


def _envelope(context, *, action: str, executed: bool, result=None) -> dict:
    """The single structured envelope both the dry run and the execute leg emit."""
    links = context.links
    payload = {
        "action": action,
        "executed": executed,
        "workspace_id": context.workspace_id,
        "role": context.role,
        "project_scope": context.project_scope,
        "provider": context.provider,
        "lane_id": "default",
        "issue": context.issue,
        "journal": context.journal,
        "decisions": [
            {
                "journal": d.journal,
                "token": d.token,
                "lane": d.lane,
                "lane_generation": d.lane_generation,
            }
            for d in context.decisions
        ],
        "links": {
            "action": links.action,
            "workspace": links.workspace,
            "authority": links.authority,
            "provider": links.provider,
            "target": links.target,
            "anchor": links.anchor,
            "fence": links.fence,
        },
        "target": {
            "status": context.target.status,
            "assigned_name": context.target.assigned_name,
            "live": context.target.live,
            "with_locator": context.target.with_locator,
            "attestation_state": context.target.attestation_state,
            "attestation_reason": context.target.attestation_reason,
        },
        "authority_reason": context.authority_reason,
    }
    if result is None:
        # Dry run: decide with the fence deliberately unconsulted, and say so rather than
        # implying the fence would have opened.
        decision = decide_proxy_delegation(links)
        payload["decision"] = ZERO_SEND if not executed else decision.decision
        payload["reason"] = decision.reason
        payload["detail"] = decision.detail
        payload["sent"] = False
        payload["fence_consulted"] = False
    else:
        payload["decision"] = result.decision
        payload["reason"] = result.reason
        payload["detail"] = result.detail
        payload["sent"] = result.sent
        payload["proxy_action_id"] = result.action_id
        payload["fence_consulted"] = True
        if result.send is not None:
            payload["send"] = {"result": result.send.result, "rc": result.send.rc}
    return payload


def cmd_workflow_proxy(args: argparse.Namespace) -> int:
    """Delegate one durably resolved action to the live attested default coordinator (once).

    Returns 0 when the delegation was delivered, or when a dry run resolves a deliverable set
    (every non-fence link permits delivery). Returns 1 for any zero-send — a broken authority link,
    an unverified / superseded anchor, an unaddressable target, or a duplicate / stale / unavailable
    fence. A zero-send performs no send and, when it is refused before the fence, consumes no
    generation.
    """
    from mozyo_bridge.application.commands_common import repo_root_from_args
    from mozyo_bridge.core.state.coordinator_proxy_fence import CoordinatorProxyFence
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send import (  # noqa: E501
        OrchestrateHandoffProxySendPort,
        execute_proxy_delegation,
        resolve_proxy_context,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.coordinator_proxy import (  # noqa: E501
        REASON_FENCE_UNAVAILABLE,
    )

    repo_root = repo_root_from_args(args)
    action = normalize_action(getattr(args, "action", ""))
    as_json = bool(getattr(args, "as_json", False))
    execute = bool(getattr(args, "execute", False))

    context = resolve_proxy_context(
        args,
        action=getattr(args, "action", "") or "",
        issue=getattr(args, "issue", "") or "",
        journal=getattr(args, "journal", "") or "",
        repo_root=repo_root,
        env=os.environ,
    )

    if not execute:
        payload = _envelope(context, action=action, executed=False)
        # A dry run that only lacks the (deliberately unconsulted) fence is a resolved plan.
        rc = 0 if payload["reason"] in ("", REASON_FENCE_UNAVAILABLE) else 1
        payload["decision"] = "deliver" if rc == 0 else ZERO_SEND
        if rc == 0:
            payload["reason"] = ""
            payload["detail"] = (
                "every authority link resolves; the delegation fence is not consulted on a dry run "
                "(re-run with --execute to reserve it and deliver once)"
            )
        return _emit(payload, as_json=as_json, rc=rc)

    result = execute_proxy_delegation(
        context,
        args=args,
        action=action,
        fence=CoordinatorProxyFence(),
        send_port=OrchestrateHandoffProxySendPort(
            repo_root=str(repo_root), receiver_provider=context.provider
        ),
    )
    payload = _envelope(context, action=action, executed=True, result=result)
    return _emit(payload, as_json=as_json, rc=0 if result.sent else 1)


def _emit(payload: dict, *, as_json: bool, rc: int) -> int:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return rc
    print(f"workflow proxy: {payload['decision']} (sent={payload['sent']})")
    print(f"  action      : {payload['action'] or '-'}")
    print(f"  anchor      : redmine:issue={payload['issue']}:journal={payload['journal']}")
    print(f"  role/lane   : {payload['role'] or '-'} / {payload['lane_id']}")
    print(f"  provider    : {payload['provider'] or '-'}")
    print(
        f"  target      : {payload['target']['status']} "
        f"(live={payload['target']['live']}, with_locator={payload['target']['with_locator']}, "
        f"attestation={payload['target']['attestation_state'] or '-'})"
    )
    links = payload["links"]
    print(
        "  links       : "
        + " ".join(f"{name}={links[name]}" for name in sorted(links))
    )
    if payload["reason"]:
        print(f"  reason      : {payload['reason']}")
    print(f"  detail      : {payload['detail']}")
    if not payload["executed"]:
        print("  (dry run — re-run with --execute to reserve the fence and deliver once)")
    return rc


def cmd_workflow_proxy_ack(args: argparse.Namespace) -> int:
    """Acknowledge a delivered delegation so the route may take its next decision.

    The completion half of the exactly-once lifecycle (review j#89918 finding 1). Without it the
    rail delivers once and then wedges: a ``delivered`` generation holds the route, and only a
    completion lets a strictly newer durable decision mint the next one. The acknowledgement is a
    **production surface** rather than an implicit side effect because the thing being asserted —
    "the coordinator acted on the delegated decision" — is not something the delivery path can
    observe. A delegation whose outcome is unknown must keep holding the route.

    The coordinator (or an operator on its behalf) passes the opaque ``proxy_action_id`` the
    delegation carried. It advances only a positively **delivered** generation in this workspace;
    an unknown / stale id, a reserved or uncertain generation, and a foreign workspace all no-op.
    """
    from mozyo_bridge.application.commands_common import repo_root_from_args
    from mozyo_bridge.core.state.coordinator_proxy_fence import CoordinatorProxyFence
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send import (  # noqa: E501
        live_workspace_id,
        resolve_ack_authority,
    )

    repo_root = repo_root_from_args(args)
    workspace_id = live_workspace_id(repo_root)
    action_id = (getattr(args, "proxy_action_id", "") or "").strip()
    as_json = bool(getattr(args, "as_json", False))

    # The ack authority is checked BEFORE the store is touched: possession of the action id is not
    # a credential, and the external client that received it must never be able to complete its own
    # delegation (review j#89969 finding 1).
    authorized, auth_reason, auth_detail = resolve_ack_authority(repo_root, env=os.environ)

    fence = CoordinatorProxyFence()
    completed = False
    if not authorized:
        reason = auth_reason
        detail = auth_detail
    elif not action_id:
        reason = "proxy_action_id_required"
        detail = "--proxy-action-id is required; it is the opaque id the delegation carried"
    elif not fence.is_bootstrapped():
        reason = "proxy_fence_unavailable"
        detail = "the delegation store is not bootstrapped; nothing to acknowledge"
    else:
        completed = fence.complete_by_action_id(action_id, workspace_id=workspace_id)
        reason = "" if completed else "proxy_ack_no_match"
        detail = (
            "the delivered generation was completed; the route may now take a strictly newer "
            "durable decision"
            if completed
            else "no positively delivered generation in this workspace carries that action id "
            "(unknown / stale id, a still-reserved or uncertain generation, or another workspace)"
        )

    payload = {
        "action": "proxy-ack",
        "proxy_action_id": action_id,
        "workspace_id": workspace_id,
        "authorized": authorized,
        "completed": completed,
        "reason": reason,
        "detail": detail,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"workflow proxy-ack: completed={completed}")
        print(f"  proxy_action_id: {action_id or '-'}")
        if reason:
            print(f"  reason         : {reason}")
        print(f"  detail         : {detail}")
    return 0 if completed else 1


def cmd_workflow_proxy_fence(args: argparse.Namespace) -> int:
    """Operator surface for the coordinator-proxy delegation store (Redmine #14546).

    The ``workflow proxy --execute`` path NEVER auto-creates this store: an auto-create after a
    total loss would resurrect a lost store and let an already-delivered delegation be sent again.
    ``--bootstrap`` is the safe first init (both artifacts absent); ``--recover`` is the deliberate
    loss recovery (a fresh store under a new nonce — invoke ONLY after reconciling the lost
    delegation with the coordinator). With no flag, reports status.
    """
    from mozyo_bridge.core.state.coordinator_proxy_fence import (
        CoordinatorProxyFence,
        CoordinatorProxyFenceError,
    )

    fence = CoordinatorProxyFence()
    try:
        if getattr(args, "fence_recover", False):
            fence.recover()
            print(f"proxy delegation store recovered (fresh store) at {fence.path}")
            print("reconcile the lost delegation with the coordinator before relying on it")
            return 0
        if getattr(args, "fence_bootstrap", False):
            fence.bootstrap()
            print(f"proxy delegation store bootstrapped at {fence.path}")
            return 0
    except CoordinatorProxyFenceError as exc:
        print(f"proxy delegation store error: {exc}")
        print("a store loss/replacement needs `workflow proxy-fence --recover`")
        return 1
    state = "bootstrapped" if fence.is_bootstrapped() else "absent / not bootstrapped"
    print(f"proxy delegation store: {state} at {fence.path}")
    return 0


def register_proxy_parsers(workflow_sub) -> None:
    """Register ``workflow proxy`` + ``workflow proxy-fence`` onto the ``workflow`` group."""
    proxy = workflow_sub.add_parser(
        "proxy",
        description=(
            "Delegate ONE already durably resolved high-level action to the live attested default "
            "coordinator, exactly once (Redmine #14546). For an external coordinator client — an "
            "operator shell or API caller that is not itself an attested lane agent. Every "
            "authority link is re-derived at action time from something the caller cannot assert: "
            "the workspace from the repo checkout's registry anchor, the role from the durable "
            "repo-local role authority, the provider from provider_binding, and the target from the "
            "coordinator's own startup attestation. The action must be carried by a structured gate "
            "marker on the named issue AND be the current one — a superseded journal is refused. "
            "DRY-RUN unless --execute. Caller-supplied MOZYO_* is never read as authority."
        ),
        help="Delegate one durably resolved action to the live default coordinator, exactly once.",
    )
    proxy.add_argument(
        "--action",
        default="",
        choices=list(PROXY_ACTIONS),
        help="The already-resolved coordinator action to delegate.",
    )
    proxy.add_argument(
        "--source",
        default="redmine",
        choices=["redmine"],
        help="The durable anchor's source system (Redmine only).",
    )
    proxy.add_argument("--issue", default="", help="The durable anchor's Redmine issue id.")
    proxy.add_argument(
        "--journal",
        default="",
        help="The durable anchor's Redmine journal id (must be the issue's CURRENT gate marker).",
    )
    proxy.add_argument(
        "--execute",
        action="store_true",
        help="Reserve the delegation fence and deliver once (without it, this is a dry run).",
    )
    proxy.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit exactly one structured envelope as JSON.",
    )
    proxy.set_defaults(func=cmd_workflow_proxy)

    ack = workflow_sub.add_parser(
        "proxy-ack",
        description=(
            "Acknowledge a delivered coordinator-proxy delegation (Redmine #14546). The completion "
            "half of the exactly-once lifecycle: a delivered generation holds its route until it is "
            "acknowledged, so without this the rail delivers once and then refuses every later "
            "decision as a duplicate. Pass the opaque proxy_action_id the delegation carried. It "
            "advances only a positively delivered generation in this workspace; an unknown / stale "
            "id, a reserved or uncertain generation, and a foreign workspace all no-op with a "
            "nonzero exit."
        ),
        help="Acknowledge a delivered delegation so the route may take its next decision.",
    )
    ack.add_argument(
        "--proxy-action-id",
        dest="proxy_action_id",
        default="",
        help="The opaque id the delegation carried (required).",
    )
    ack.add_argument(
        "--json", dest="as_json", action="store_true",
        help="Emit exactly one structured envelope as JSON.",
    )
    ack.set_defaults(func=cmd_workflow_proxy_ack)

    fence_p = workflow_sub.add_parser(
        "proxy-fence",
        description=(
            "Operator surface for the coordinator-proxy delegation store (Redmine #14546). "
            "`--bootstrap` initializes it; `--recover` mints a fresh store after a loss (only after "
            "reconciling the lost delegation); no flag reports status. The `workflow proxy "
            "--execute` path never auto-creates the store."
        ),
        help="Bootstrap / recover / status the coordinator-proxy delegation store.",
    )
    fence_p.add_argument(
        "--bootstrap", dest="fence_bootstrap", action="store_true",
        help="Initialize the delegation store (safe first init; refuses on a detected loss).",
    )
    fence_p.add_argument(
        "--recover", dest="fence_recover", action="store_true",
        help="Deliberate loss recovery: mint a fresh delegation store under a new nonce.",
    )
    fence_p.set_defaults(func=cmd_workflow_proxy_fence)


__all__ = (
    "cmd_workflow_proxy",
    "cmd_workflow_proxy_ack",
    "cmd_workflow_proxy_fence",
    "register_proxy_parsers",
)
