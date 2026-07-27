"""``workflow proxy`` and its operator surfaces: the external-client delegation rail (#14546).

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
    """DEPRECATED no-op: acknowledgement is no longer a route-completion authority.

    Withdrawn by Design Consultation Answer j#90329 (contract 2). The proxy's job ends at delivery:
    a positively recorded delivery is the terminal success for that durable decision, and the same
    decision is a duplicate forever after. Nothing about "the coordinator acted" is provable on this
    transport — neither caller env, nor possession of the action id, nor a bare Redmine marker, nor
    the Redmine author — so the rail stopped claiming it rather than relocating the claim a third
    time.

    The command remains so an existing runbook does not fail hard, but it advances no fence state
    and admits no decision. It exits nonzero to make a script that still relies on it visible.
    """
    payload = {
        "action": "proxy-ack",
        "deprecated": True,
        "completed": False,
        "reason": "proxy_ack_withdrawn",
        "detail": (
            "acknowledgement is no longer a completion authority (Redmine #14546, Design Answer "
            "j#90329). A positively recorded delivery is the proxy's terminal success; the same "
            "durable decision is never delegated again, and a strictly newer canonical decision "
            "mints the next generation. This command changes nothing."
        ),
    }
    if bool(getattr(args, "as_json", False)):
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print("workflow proxy-ack: DEPRECATED no-op")
        print(f"  detail: {payload['detail']}")
    return 1


#: The reconcile dispositions an operator may apply to ONE stuck generation (Design Answer j#90329
#: contract 4). Each names what was *established*, not what the tool guesses.
DISPOSITION_CONFIRMED = "confirmed-delivered"
DISPOSITION_NOT_SENT = "proven-not-sent"
DISPOSITION_UNKNOWN = "unknown"
RECONCILE_DISPOSITIONS = (DISPOSITION_CONFIRMED, DISPOSITION_NOT_SENT, DISPOSITION_UNKNOWN)


def cmd_workflow_proxy_reconcile(args: argparse.Namespace) -> int:
    """Apply an operator's finding about ONE delegation generation (Redmine #14546, j#90329 c4).

    The previous surface only moved ``reserved`` to ``uncertain`` and called that "reconciled" — but
    ``uncertain`` is exactly the state nothing can leave: it admits no next decision and, now that
    acknowledgement is withdrawn, nothing completes it. A generation parked there held its route
    forever. So the dispositions are terminal-capable and say what was established:

    - ``confirmed-delivered`` — the send DID land. The generation reaches ``delivered``, the proxy's
      terminal success, and a strictly newer canonical decision may follow.
    - ``proven-not-sent`` — the send never left. The generation reaches ``abandoned``, which
      releases the route for the coordinator's NEXT decision (it does not replay the abandoned one:
      a decision is delegated once). This is the strongest assertion available, so it needs evidence
      and the exact anchor.
    - ``unknown`` — nothing was established. A stuck ``reserved`` generation is moved to
      ``uncertain`` so it is visibly awaiting an operator; an already-``uncertain`` one is left
      alone. Nothing terminal is claimed.

    Every transition is joined to the exact ``route + proxy_action_id + stored issue + stored
    journal``, so naming a different anchor changes nothing. DRY-RUN unless ``--execute``.
    """
    from mozyo_bridge.application.commands_common import repo_root_from_args
    from mozyo_bridge.core.state.coordinator_proxy_fence import (
        CoordinatorProxyFence,
        CoordinatorProxyFenceError,
        ProxyRouteKey,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send import (  # noqa: E501
        live_workspace_id,
        resolve_default_lane_authority,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.coordinator_proxy import (  # noqa: E501
        normalize_action,
    )

    repo_root = repo_root_from_args(args)
    workspace_id = live_workspace_id(repo_root)
    action = normalize_action(getattr(args, "action", ""))
    action_id = (getattr(args, "proxy_action_id", "") or "").strip()
    issue = (getattr(args, "issue", "") or "").strip()
    journal = (getattr(args, "journal", "") or "").strip()
    disposition = (getattr(args, "disposition", "") or "").strip()
    evidence = (getattr(args, "evidence", "") or "").strip()
    as_json = bool(getattr(args, "as_json", False))
    execute = bool(getattr(args, "execute", False))

    applied = False
    reason = ""
    detail = ""
    state = ""
    if not workspace_id:
        reason, detail = "proxy_workspace_unresolved", "no workspace anchor for this checkout"
    elif not action:
        reason, detail = "proxy_action_unknown", "--action must name a delegable action"
    elif not action_id or not issue or not journal:
        reason, detail = "proxy_reconcile_anchor_required", (
            "--proxy-action-id, --issue and --journal are all required: the transition is joined to "
            "the generation's exact stored anchor"
        )
    elif disposition not in RECONCILE_DISPOSITIONS:
        reason, detail = "proxy_reconcile_disposition_unknown", (
            f"--disposition must be one of {list(RECONCILE_DISPOSITIONS)}"
        )
    elif disposition != DISPOSITION_UNKNOWN and not evidence:
        reason, detail = "proxy_reconcile_evidence_required", (
            f"--evidence is required for {disposition!r}: it asserts what was established, and "
            "`proven-not-sent` releases the route"
        )
    else:
        status, role, _scope, _r = resolve_default_lane_authority(repo_root)
        if status != "resolved":
            reason, detail = "proxy_coordinator_authority_missing", "no bound default-lane role"
        else:
            fence = CoordinatorProxyFence()
            route = ProxyRouteKey(
                workspace_id=workspace_id, lane_id="default", role=role, action=action
            )
            try:
                current = fence.active(route)
                state = current.state
                if current.action_id != action_id:
                    reason, detail = "proxy_reconcile_no_match", (
                        "the route's current generation does not carry that action id"
                    )
                elif current.issue != issue or current.journal != journal:
                    reason, detail = "proxy_reconcile_anchor_mismatch", (
                        "the generation's stored decision anchor differs from the one supplied"
                    )
                elif not execute:
                    detail = f"would apply {disposition!r} to a {state!r} generation (--execute)"
                else:
                    note = f"operator reconcile ({disposition}): {evidence}" if evidence else (
                        "operator reconcile: outcome still unknown"
                    )
                    if disposition == DISPOSITION_CONFIRMED:
                        applied = fence.confirm_delivered(
                            route, action_id, detail=note, issue=issue, journal=journal
                        )
                    elif disposition == DISPOSITION_NOT_SENT:
                        applied = fence.mark_abandoned(
                            route, action_id, detail=note, issue=issue, journal=journal
                        )
                    else:
                        applied = fence.mark_uncertain(
                            route, action_id, detail=note, issue=issue, journal=journal
                        )
                    state = fence.active(route).state
                    reason = "" if applied else "proxy_reconcile_not_applicable"
                    detail = (
                        f"generation is now {state!r}"
                        if applied
                        else f"a {current.state!r} generation does not admit {disposition!r}"
                    )
            except CoordinatorProxyFenceError as exc:
                reason, detail = "proxy_fence_unavailable", f"delegation store unusable: {exc}"

    payload = {
        "action": "proxy-reconcile",
        "delegated_action": action,
        "disposition": disposition,
        "proxy_action_id": action_id,
        "issue": issue,
        "journal": journal,
        "workspace_id": workspace_id,
        "generation_state": state,
        "executed": execute,
        "applied": applied,
        "reason": reason,
        "detail": detail,
    }
    rc = 0 if (applied or (not reason and not execute)) else 1
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"workflow proxy-reconcile: applied={applied} state={state or '-'}")
        if reason:
            print(f"  reason: {reason}")
        print(f"  detail: {detail}")
    return rc


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
    """Register ``workflow proxy`` / ``proxy-reconcile`` / ``proxy-ack`` / ``proxy-fence``."""
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
            "DEPRECATED no-op (Redmine #14546). Acknowledgement is no longer a route-completion "
            "authority: the proxy cannot prove the coordinator acted, so a positively recorded "
            "DELIVERY is its terminal success, the same durable decision is never delegated twice, "
            "and a strictly newer canonical decision mints the next generation with no "
            "acknowledgement of any kind. A generation whose fate is genuinely unknown is resolved "
            "by `workflow proxy-reconcile`, not here. Retained so an existing runbook does not fail "
            "hard: it reaches no store, changes nothing, and exits nonzero."
        ),
        help="DEPRECATED no-op: acknowledgement is not a completion authority (see proxy-reconcile).",
    )
    ack.add_argument("--issue", default="", help=argparse.SUPPRESS)
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

    reconcile = workflow_sub.add_parser(
        "proxy-reconcile",
        description=(
            "Apply an operator's finding about ONE delegation generation (Redmine #14546). "
            "`--disposition confirmed-delivered` resolves it to the proxy's terminal success so a "
            "strictly newer decision may follow; `proven-not-sent` abandons it, releasing the "
            "route for the coordinator's NEXT decision (evidence required; it does not replay the "
            "abandoned one); `unknown` parks a stuck reserve as "
            "uncertain without claiming anything terminal. Every transition is joined to the "
            "generation's exact stored issue+journal, so naming another anchor changes nothing. "
            "DRY-RUN unless --execute."
        ),
        help="Apply an operator's finding about one stuck delegation generation.",
    )
    reconcile.add_argument("--action", default="", choices=list(PROXY_ACTIONS),
                          help="The delegated action naming the route.")
    reconcile.add_argument("--proxy-action-id", dest="proxy_action_id", default="",
                           help="The generation's opaque id (required).")
    reconcile.add_argument("--issue", default="",
                           help="The generation's stored decision issue (required).")
    reconcile.add_argument("--journal", default="",
                           help="The generation's stored decision journal (required).")
    reconcile.add_argument("--disposition", default="", choices=list(RECONCILE_DISPOSITIONS),
                           help="What the operator established about the send.")
    reconcile.add_argument("--evidence", default="",
                           help="What established it (required except for `unknown`).")
    reconcile.add_argument("--execute", action="store_true",
                           help="Perform the transition (without it, this is a dry run).")
    reconcile.add_argument("--json", dest="as_json", action="store_true",
                           help="Emit exactly one structured envelope as JSON.")
    reconcile.set_defaults(func=cmd_workflow_proxy_reconcile)

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
    "cmd_workflow_proxy_reconcile",
    "cmd_workflow_proxy_fence",
    "register_proxy_parsers",
)
