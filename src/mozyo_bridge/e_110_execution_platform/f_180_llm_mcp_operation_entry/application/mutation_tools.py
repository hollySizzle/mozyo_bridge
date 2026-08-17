"""The three mutating tool handlers (Redmine #15152).

``handoff_send`` / ``handoff_reply`` / ``sublane_start``, each calling the SAME
typed shared application processing the corresponding CLI command calls,
in-process:

- the handoff tools call :func:`run_handoff` (#15149's typed operation API),
  which applies the core-owned entry policy and runs the full shared
  orchestration — durable-anchor ownership, receiver vocabulary, identity,
  gateway-route and send-safety gates included. No judgement is restated here,
  and none can be skipped (``cli-mcp-shared-application-api.md`` Invariants 2/3).
- the sublane tool calls :func:`run_sublane_start` (#15152's typed shared
  service), which runs the work-unit config gate, the #15146
  delegated_coordinator parent-authority admission, the provider launchability
  preflight, and the actuation use case — all decided before any worktree /
  pair / dispatch side effect, exactly as the CLI's ``sublane create/start``.

Result projection follows the #15151 allowlist discipline (reviews j#103251
r4f3 / j#106183 r5f1): a payload member is either a closed token, a
caller-supplied identity, or a fixed sentence reconstructed over closed tokens.
Producer free text — the CLI ``die`` message, a gate's operator prose, a step's
replayable command line — is dropped, never scrubbed, because it can carry pane
ids, private paths, and session names this surface must not emit. The operator
detail stays reachable through the CLI, which is an operator surface and may
say where things live.
"""

from __future__ import annotations

from typing import Any, Mapping

from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.read_plan_tools import (  # noqa: E501
    ReadPlanContext,
    ToolOutcome,
)

#: The DeliveryOutcome members a mutating handoff tool republishes. Closed
#: tokens and caller-echoed identities only — an allowlist, never a denylist,
#: so a field added to the outcome later stays private until someone decides,
#: in review, that it is public. `target` (a pane locator), `next_action`
#: (producer prose), `execution_root` (paths) are deliberately absent.
HANDOFF_OUTCOME_PUBLIC_FIELDS = (
    "status",
    "reason",
    "receiver",
    "kind",
    "mode",
    "next_action_owner",
    "notification_marker",
)

#: Anchor members echoed back: the caller supplied them; nothing here is a pane
#: or a path.
HANDOFF_ANCHOR_PUBLIC_FIELDS = ("source", "issue", "journal", "task_id", "comment_id")

#: The SublaneActuationOutcome members the sublane tool republishes VERBATIM.
#: Absent by decision: `worktree_path` (a private filesystem path), `gateway_pane`
#: / `worker_pane` / `dispatch_target` (pane identities), `steps` (replayable
#: command lines that interpolate both), and — since #15152 R4 (review j#106903
#: finding_reasonproseleak) — `reason`: the actuation producer builds it by
#: concatenating a gate's free-text detail (e.g. `evaluate_dispatch_sender`'s
#: "workspace anchor unreadable ({exc})", which can name a private path), and
#: this allowlist is a copy, not a scrub. The closed `blocked_reasons` tokens ARE
#: published; the public prose `reason` is RECONSTRUCTED from them below.
SUBLANE_OUTCOME_PUBLIC_FIELDS = (
    "status",
    "execute",
    "issue",
    "lane_label",
    "branch",
    "launch_action",
    "adopted",
    "dispatch_result",
    "worker_dispatch_confirmed",
    "dispatch_injection_stage",
    "durable_anchor",
    "fill_decision",
    "gateway_ready",
)

# The CLOSED public vocabulary for a blocker token (#15152 R6, review j#107004
# finding_projectionvocabopen). R5 used a lowercase-identifier REGEX, which is an
# infinite grammar — an identifier-shaped internal detail (no `/` `%` space) still
# leaked. The doc requires a FINITE closed vocabulary, so the public set is
# IMPORTED from the producing registries (never hand-duplicated) and a token is
# published only if it is an exact member, or a `<prefix>:<value>` form whose
# prefix AND value both belong to a closed registry. Everything else — including a
# launcher verdict's free-text `gate_reason` — maps to _UNCLASSIFIED_BLOCKER.
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    ACTUATE_STATES,
    BLOCKED_REASONS,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.work_unit_granularity import (  # noqa: E501
    WORK_UNIT_EXPLICIT_DECISION_RECORDED,
    WORK_UNIT_EXPLICIT_DECISION_REQUIRED,
    WORK_UNIT_LEAF_DECISION_RECORDED,
    WORK_UNIT_LEAF_DECISION_REQUIRED,
    WORK_UNIT_LEAF_STANDALONE,
    WORK_UNIT_STANDARD,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (  # noqa: E501
    FILL_DECISIONS,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (  # noqa: E501
    AGENT_PROVIDERS,
)

#: The exact blocker tokens producers append that are NOT in ``BLOCKED_REASONS``.
#: Pinned to their producers by :mod:`sublane_actuator_gates` /
#: :mod:`sublane_actuator_use_case`; a drift test asserts the union stays complete.
_EXTRA_BLOCKER_TOKENS = frozenset(
    {"sender_attestation", "sender_authority_capability_missing", "base_ref_unpinnable"}
)

#: The work-unit decision diagnostics that ride in blocked_reasons.
_WORK_UNIT_DIAGNOSTICS = frozenset(
    {
        WORK_UNIT_STANDARD,
        WORK_UNIT_LEAF_STANDALONE,
        WORK_UNIT_LEAF_DECISION_RECORDED,
        WORK_UNIT_LEAF_DECISION_REQUIRED,
        WORK_UNIT_EXPLICIT_DECISION_RECORDED,
        WORK_UNIT_EXPLICIT_DECISION_REQUIRED,
    }
)

#: The exact closed public blocker vocabulary (imported, not duplicated).
_PUBLIC_BLOCKER_TOKENS = (
    frozenset(BLOCKED_REASONS)
    | frozenset(FILL_DECISIONS)
    | _WORK_UNIT_DIAGNOSTICS
    | _EXTRA_BLOCKER_TOKENS
)

#: The identity fields a `missing_field:<name>` blocker may name (#13432). Pinned
#: to ``SublaneCreateRequest.missing_fields`` by a drift test.
_MISSING_FIELD_NAMES = frozenset({"issue", "lane_label", "branch", "worktree_path"})

#: `<prefix>:<value>` blocker forms: the value must belong to the mapped registry.
_PREFIXED_BLOCKER_REGISTRIES = {
    "missing_field": _MISSING_FIELD_NAMES,
    "unattested": frozenset(AGENT_PROVIDERS),
}

#: The closed public status vocabulary and the fixed unknown fallbacks.
_PUBLIC_STATUSES = frozenset(ACTUATE_STATES)
_UNCLASSIFIED_BLOCKER = "unclassified_blocker"
_UNKNOWN_STATUS = "unknown_status"


def _public_blocker_token(token: str) -> str:
    """One blocker token validated against the closed public vocabulary."""
    if token in _PUBLIC_BLOCKER_TOKENS:
        return token
    prefix, sep, value = token.partition(":")
    if sep:
        allowed = _PREFIXED_BLOCKER_REGISTRIES.get(prefix)
        if allowed is not None and value in allowed:
            return token
    return _UNCLASSIFIED_BLOCKER


def _public_status(status: str) -> str:
    """The actuation status validated against ``ACTUATE_STATES`` (#15152 R6)."""
    return status if status in _PUBLIC_STATUSES else _UNKNOWN_STATUS


def _public_blocked_reasons(outcome: Any) -> list:
    """The blocked_reasons validated to the closed public vocabulary (#15152 R6)."""
    return [
        _public_blocker_token(str(raw))
        for raw in getattr(outcome, "blocked_reasons", ()) or ()
    ]

#: Fixed refusal sentence for a fail-closed handoff run. The gate's own message
#: is NOT forwarded (it is operator prose that can name panes and paths); the
#: typed reason, when the gate emitted a structured outcome, is in `outcome`.
HANDOFF_REFUSAL_SENTENCE = (
    "a shared-orchestration gate refused this operation before delivery; the "
    "structured outcome carries the typed reason when one was emitted. Run the "
    "equivalent `mozyo-bridge handoff` command from the lane for the "
    "operator-facing detail."
)

#: Fixed, reconstructed sentences per sublane admission-refusal token (#15151
#: r5f1 discipline: fixed templates over closed tokens, never producer prose).
_SUBLANE_REFUSAL_SENTENCES = {
    "invalid_repo_local_config": (
        "the repo-local `.mozyo-bridge/config.yaml` is present but invalid; "
        "fix it and retry."
    ),
    "parent_authority_bindings_invalid": (
        "the workflow-role binding declaration is invalid, so the asserted "
        "parent project gateway cannot be verified; repair the declaration "
        "first. No worktree, pair, or dispatch was created."
    ),
    "parent_gateway_binding_missing": (
        "lane_kind delegated_coordinator asserts a parent project gateway, and "
        "no durable project_gateway role binding is declared for this "
        "workspace. Declare the parent tier first (`mozyo-bridge sublane "
        "declare-project-gateway` after adding the binding), or create the "
        "lane without the three-tier claim. No worktree, pair, or dispatch "
        "was created."
    ),
    "parent_gateway_owner_row_missing": (
        "project_gateway binding(s) are declared, but no declared gateway lane "
        "owns an ACTIVE canonical owner row; declare it via `mozyo-bridge "
        "sublane declare-project-gateway` from a live attested pair. No "
        "worktree, pair, or dispatch was created."
    ),
    "parent_workspace_scope_unresolved": (
        "this repo's workspace identity did not resolve, so the parent "
        "gateway's owner row cannot be scoped or verified. No worktree, pair, "
        "or dispatch was created."
    ),
    "provider_unresolved": (
        "a lane role's bound agent provider did not resolve; check the "
        "repo-local workflow role bindings. No lane was created."
    ),
    "provider_not_launchable": (
        "a lane role's bound agent provider is not a launchable agent "
        "provider; check the repo-local workflow role bindings. No lane was "
        "created."
    ),
    # #15152 R2 (review j#106834 finding_authoritybypass): the durable-anchor
    # ownership verification now runs BEFORE any worktree / pair mutation, for
    # dispatch and create-only alike.
    "anchor_issue_not_found": (
        "the durable-anchor issue was not found; supply a real issue id. No "
        "worktree, pair, or dispatch was created."
    ),
    "anchor_journal_not_found": (
        "the durable-anchor journal was not found under the given issue; "
        "supply a journal id that belongs to it. No worktree, pair, or "
        "dispatch was created."
    ),
    "anchor_issue_journal_mismatch": (
        "the durable-anchor journal does not belong to the given issue. No "
        "worktree, pair, or dispatch was created."
    ),
    "anchor_provider_unreadable": (
        "the durable-anchor provider could not be read, so the anchor's "
        "ownership could not be verified; an unverifiable authority admits "
        "nothing. No worktree, pair, or dispatch was created."
    ),
}

_SUBLANE_REFUSAL_FALLBACK = (
    "a typed admission gate refused this operation before any side effect; "
    "run `mozyo-bridge sublane create` from the lane for the operator-facing "
    "detail."
)


# --- handoff_send / handoff_reply ------------------------------------------ #


def _public_delivery_outcome(outcome: Any) -> dict:
    """The allowlisted projection of one DeliveryOutcome."""
    payload: dict = {}
    for name in HANDOFF_OUTCOME_PUBLIC_FIELDS:
        value = getattr(outcome, name, None)
        if value is not None:
            payload[name] = value
    anchor = getattr(outcome, "anchor", None)
    if isinstance(anchor, Mapping):
        payload["anchor"] = {
            key: anchor[key]
            for key in HANDOFF_ANCHOR_PUBLIC_FIELDS
            if key in anchor and anchor[key] is not None
        }
    return payload


def _run_handoff_operation(
    operation: str, arguments: Mapping[str, Any], context: ReadPlanContext
) -> ToolOutcome:
    """Run one anchored handoff operation through the typed shared API."""
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_application_service import (  # noqa: E501
        HandoffRequest,
        run_handoff,
    )
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_command_input import (  # noqa: E501
        HandoffCommandInput,
    )
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (  # noqa: E501
        injection_stage_for_outcome,
    )

    def _text(name: str) -> str | None:
        value = str(arguments.get(name, "") or "").strip()
        return value or None

    inp = HandoffCommandInput(
        to=_text("to"),
        source=_text("source"),
        issue=_text("issue"),
        journal=_text("journal"),
        task_id=_text("task_id"),
        comment_id=_text("comment_id"),
        kind=_text("kind"),
        summary=_text("summary"),
        target_lane=_text("lane"),
        target_repo=_text("target_repo") or "auto",
    )
    result = run_handoff(
        HandoffRequest(operation=operation, input=inp, repo_root=context.repo_root)
    )

    outcome_payload = (
        _public_delivery_outcome(result.outcome) if result.outcome is not None else {}
    )
    is_error = result.fail_closed or result.exit_code != 0
    payload = {
        "operation": operation,
        "status": result.status,
        "exit_code": int(result.exit_code),
        "delivered": bool(result.delivered),
        "injection_stage": (
            str(injection_stage_for_outcome(result.outcome) or "")
            if result.outcome is not None
            else ""
        ),
        "outcome": outcome_payload,
        "refusal": HANDOFF_REFUSAL_SENTENCE if result.fail_closed else "",
    }
    reason = outcome_payload.get("reason") or "none"
    summary = (
        f"handoff {operation}: {result.status} "
        f"(delivered={str(bool(result.delivered)).lower()}, reason {reason})"
    )
    return ToolOutcome(payload=payload, is_error=is_error, summary=summary)


def run_handoff_send(
    arguments: Mapping[str, Any], context: ReadPlanContext
) -> ToolOutcome:
    """The `handoff_send` tool: the anchored cross-agent send operation."""
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_operation import (  # noqa: E501
        OP_SEND,
    )

    return _run_handoff_operation(OP_SEND, arguments, context)


def run_handoff_reply(
    arguments: Mapping[str, Any], context: ReadPlanContext
) -> ToolOutcome:
    """The `handoff_reply` tool: the anchored reply rail."""
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_operation import (  # noqa: E501
        OP_REPLY,
    )

    return _run_handoff_operation(OP_REPLY, arguments, context)


# --- sublane_start ---------------------------------------------------------- #


def _reconstructed_sublane_reason(status: str, blocked: list) -> str:
    """A fixed public sentence over the status + validated blocker tokens.

    #15152 R4/R5 (reviews j#106903 / j#106995): the raw `outcome.reason` carries
    a gate's free-text detail (private paths, exception text) and never reaches
    the public surface. This reconstructs a reason from the `status` and the
    ALREADY-VALIDATED blocker tokens (see :func:`_public_blocked_reasons`), with
    the operator-facing detail reachable through the CLI.
    """
    status = status or "unknown"
    if blocked:
        return (
            f"status {status}; blocked reasons {', '.join(blocked)}. Run "
            "`mozyo-bridge sublane create` from the lane for the operator-facing "
            "detail."
        )
    return f"status {status}."


def _public_sublane_outcome(outcome: Any) -> dict:
    """The allowlisted projection of one SublaneActuationOutcome.

    `blocked_reasons` and the reconstructed `reason` are both built from the
    validated token list — the same output feeds all three surfaces
    (structured blocked_reasons, reason, and the summary), so no unvalidated
    producer string reaches the public boundary (#15152 R5, j#106995).
    """
    payload: dict = {}
    for name in SUBLANE_OUTCOME_PUBLIC_FIELDS:
        value = getattr(outcome, name, None)
        if isinstance(value, tuple):
            value = list(value)
        if value is not None:
            payload[name] = value
    # #15152 R6 (finding_projectionvocabopen): status is validated against the
    # closed ACTUATE_STATES vocabulary too, not published verbatim (the field is
    # a plain str with no runtime invariant).
    status = _public_status(str(getattr(outcome, "status", "") or ""))
    payload["status"] = status
    blocked = _public_blocked_reasons(outcome)
    payload["blocked_reasons"] = blocked
    payload["reason"] = _reconstructed_sublane_reason(status, blocked)
    return payload


def run_sublane_start_tool(
    arguments: Mapping[str, Any], context: ReadPlanContext
) -> ToolOutcome:
    """The `sublane_start` tool: plan (default) or actuate a sublane.

    Calls the typed shared service — the same body the CLI's
    ``sublane create/start`` runs — so the #15146 parent-authority admission and
    every other gate decide identically on both entries, before any side
    effect. ``actuate=false`` (the default) is the side-effect-free plan.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_start_service import (  # noqa: E501
        SublaneStartCommand,
        run_sublane_start,
    )

    def _text(name: str) -> str:
        return str(arguments.get(name, "") or "").strip()

    command = SublaneStartCommand(
        repo_root=context.repo_root,
        issue=_text("issue"),
        lane_label=_text("lane_label"),
        branch=_text("branch"),
        worktree_path=_text("worktree"),
        journal=_text("journal") or None,
        work_unit=_text("work_unit") or None,
        work_unit_decision_anchor=_text("work_unit_decision_journal") or None,
        leaf_standalone=bool(arguments.get("leaf_standalone", False)),
        base_ref=_text("base_ref") or None,
        lane_kind=_text("lane_kind"),
        execute=bool(arguments.get("actuate", False)),
        dispatch=bool(arguments.get("dispatch", True)),
        target_repo=_text("target_repo") or "auto",
        # stdout carries MCP frames only; composed progress goes to stderr.
        quiet_stdout=True,
    )
    result = run_sublane_start(command)

    if result.refused:
        reason = result.refusal.reason
        payload = {
            "status": "refused",
            "executed": False,
            "exit_code": int(result.exit_code),
            "refusal_reason": reason,
            "refusal": _SUBLANE_REFUSAL_SENTENCES.get(
                reason, _SUBLANE_REFUSAL_FALLBACK
            ),
            "outcome": {},
        }
        return ToolOutcome(
            payload=payload,
            is_error=True,
            summary=f"sublane_start refused before any side effect ({reason})",
        )

    outcome = result.outcome
    projected = _public_sublane_outcome(outcome)
    # #15152 R6: the top-level status uses the SAME validated value as the
    # projection — never the raw outcome.status.
    payload = {
        "status": projected["status"],
        "executed": bool(outcome.executed),
        "exit_code": int(result.exit_code),
        "refusal_reason": "",
        "refusal": "",
        "outcome": projected,
    }
    # #15152 R4/R5/R6 (finding_reasonproseleak / blockedreasonleak /
    # projectionvocabopen): the summary uses the SAME validated status and blocker
    # tokens the projection publishes — never the raw outcome.reason (private-path
    # detail) nor an unvalidated status / blocked_reasons value.
    blocked = projected.get("blocked_reasons", [])
    summary = (
        f"sublane_start: {projected['status']} "
        f"(executed={str(bool(outcome.executed)).lower()}"
        + (f", blocked {', '.join(blocked)}" if blocked else "")
        + ")"
    )
    return ToolOutcome(payload=payload, is_error=outcome.is_blocked, summary=summary)


__all__ = (
    "HANDOFF_ANCHOR_PUBLIC_FIELDS",
    "HANDOFF_OUTCOME_PUBLIC_FIELDS",
    "HANDOFF_REFUSAL_SENTENCE",
    "SUBLANE_OUTCOME_PUBLIC_FIELDS",
    "run_handoff_reply",
    "run_handoff_send",
    "run_sublane_start_tool",
)
