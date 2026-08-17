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

from typing import Any, Mapping, get_args

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

#: The SublaneActuationOutcome members the sublane tool may publish. Since #15152
#: R7 (review j#107015 finding_projectiontokensopen) none is copied VERBATIM: every
#: field is routed through a typed category below (bool / caller-echo /
#: producer-token / presence). Absent by decision: `worktree_path` (a private
#: filesystem path), `gateway_pane` / `worker_pane` / `dispatch_target` (pane
#: identities), `steps` (replayable command lines that interpolate both), and —
#: since #15152 R4 (review j#106903 finding_reasonproseleak) — `reason`: the
#: actuation producer builds it by concatenating a gate's free-text detail (e.g.
#: `evaluate_dispatch_sender`'s "workspace anchor unreadable ({exc})", which can
#: name a private path). The closed `blocked_reasons` tokens ARE published; the
#: public prose `reason` is RECONSTRUCTED from them below.
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
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_application_service import (  # noqa: E501
    STATUS_COMPLETED,
    STATUS_FAIL_CLOSED,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (  # noqa: E501
    MODES,
    NextActionOwner,
    Reason,
    Status,
    build_marker,
    normalize_anchor,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (  # noqa: E501
    INJECTION_STAGES,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    ACTUATE_STATES,
    BLOCKED_REASONS,
    DISPATCH_RESULTS,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (  # noqa: E501
    LAUNCH_ACTIONS,
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

# The CLOSED runtime projection for the handoff outcome (#15152 R7, review j#107015
# finding_handoffprojectionopen). R7 wrongly reasoned that DeliveryOutcome's
# ``Literal`` annotations (Status / Reason / NextActionOwner) make the fields safe:
# a ``Literal`` is a STATIC hint, NOT a runtime guard, and this module is not a
# mypy-checked island, so a producer contract drift can put a private path in
# ``reason`` and it reaches the public MCP response verbatim. Each producer-owned
# field is now validated at runtime against a closed set derived from its own type
# (``get_args``) or its producing registry; an unknown value maps to a fixed
# ``unknown_<field>`` token. ``receiver`` / ``kind`` / ``notification_marker`` are
# caller-derived (the caller's ``--to`` / ``--kind`` and the tool-composed marker),
# echoed as a distinct category.
_HANDOFF_PRODUCER_REGISTRIES = {
    "status": frozenset(get_args(Status)),
    "reason": frozenset(get_args(Reason)),
    "next_action_owner": frozenset(get_args(NextActionOwner)),
    # #15152 R8 (finding_handoffmodevocabularypartial): derive the mode set from the
    # producer's canonical MODES (which includes `standard`), never a hand-picked
    # subset — otherwise a legitimate mode is falsely mapped to unknown_mode.
    "mode": frozenset(MODES),
}
#: The anchor members the caller supplies; sourced from the validated caller INPUT
#: (never the producer outcome) so producer drift cannot substitute a private value.
_HANDOFF_ANCHOR_INPUT_FIELDS = ("source", "issue", "journal", "task_id", "comment_id")
#: The closed top-level handoff-result status set (HandoffResult.status).
_HANDOFF_RESULT_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAIL_CLOSED})


def _public_handoff_token(field: str, value: Any) -> str:
    """One producer-owned handoff field validated against its closed set."""
    text = str(value)
    return text if text in _HANDOFF_PRODUCER_REGISTRIES[field] else f"unknown_{field}"


def _reconstructed_marker(inp: Any):
    """The notification marker rebuilt from the validated caller anchor/kind/to.

    #15152 R8 (finding_callerechobindingopen): the marker must NOT be echoed from
    the producer outcome (drift could substitute a private string). It is rebuilt
    canonically with the same ``build_marker`` the producer uses, from the caller's
    own anchor input; an anchor that does not normalize (or no kind/receiver)
    yields ``None`` and the field is omitted.
    """
    to = getattr(inp, "to", None)
    kind = getattr(inp, "kind", None)
    if not to or not kind:
        return None
    try:
        anchor = normalize_anchor(
            getattr(inp, "source", None) or "",
            issue=getattr(inp, "issue", None),
            journal=getattr(inp, "journal", None),
            task_id=getattr(inp, "task_id", None),
            comment_id=getattr(inp, "comment_id", None),
        )
    except Exception:
        return None
    return build_marker(anchor, str(kind), str(to))


def _public_bool(value: Any):
    """An exact bool only (#15152 R7 finding_booleantruthinessoverclaim).

    ``bool("false")`` is ``True``; a string / int / container producer value must
    never coerce to an affirmative. Returns the bool unchanged, or ``None`` for
    anything that is not exactly ``True`` / ``False`` so the caller can omit it.
    """
    return value if isinstance(value, bool) else None

# The TYPED projection schema for the sublane outcome (#15152 R7, review j#107015
# finding_projectiontokensopen). R6 closed only `status` and `blocked_reasons`; the
# sibling producer-owned string fields (launch_action / dispatch_result /
# dispatch_injection_stage / fill_decision) were still copied VERBATIM by the field
# loop, so a private path / pane id / operator detail in any of them reached the
# public structuredContent. The fix closes the projection AS A CLASS: every field
# the projection can emit is assigned exactly one category, and a producer-owned
# token is republished only when it is an exact member of its producing registry.
# The exhaustiveness drift test fails if a field escapes categorization, so a new
# outcome field cannot silently leak.
#
#: Booleans — coerced to ``bool``; a producer string can never ride these out.
_BOOL_PUBLIC_FIELDS = frozenset(
    {"execute", "adopted", "worker_dispatch_confirmed", "gateway_ready"}
)
#: Caller-supplied identifiers the caller itself declared — echoed as short text,
#: explicitly separated from producer-owned tokens (the reviewer's required split).
_CALLER_ECHO_PUBLIC_FIELDS = frozenset({"issue", "lane_label", "branch"})
#: Producer-owned enumerated tokens — each validated against its producing registry;
#: an unknown / hostile value maps to the field's fixed ``unclassified_<field>``.
_PRODUCER_TOKEN_REGISTRIES = {
    "launch_action": frozenset(LAUNCH_ACTIONS),
    "dispatch_result": frozenset(DISPATCH_RESULTS),
    "dispatch_injection_stage": frozenset(INJECTION_STAGES),
    "fill_decision": frozenset(FILL_DECISIONS),
}
#: Producer-owned free text that must NEVER publish raw (it can name a durable
#: anchor path / marker) — replaced by a ``<field>_present`` boolean.
_PRESENCE_ONLY_PUBLIC_FIELDS = frozenset({"durable_anchor"})
#: ``status`` is projected on its own via :func:`_public_status`.
_STATUS_PUBLIC_FIELDS = frozenset({"status"})


def _unclassified_producer_token(field: str) -> str:
    """The fixed public token for an out-of-registry producer value."""
    return f"unclassified_{field}"


def _public_producer_token(field: str, value: str) -> str:
    """One producer-owned token validated against its producing registry."""
    registry = _PRODUCER_TOKEN_REGISTRIES[field]
    return value if value in registry else _unclassified_producer_token(field)


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


def _public_delivery_outcome(outcome: Any, inp: Any) -> dict:
    """The runtime-closed projection of one DeliveryOutcome (#15152 R7/R8).

    Producer-owned fields (status/reason/next_action_owner/mode) are validated
    against a closed set (unknown -> ``unknown_<field>``). Caller-derived fields
    (receiver/kind/anchor/notification_marker) are sourced SOLELY from the
    validated caller input ``inp`` — never the producer outcome — so producer
    drift cannot substitute a private/runtime value (#15152 R8
    finding_callerechobindingopen). The marker is rebuilt canonically.
    """
    payload: dict = {}
    for name in _HANDOFF_PRODUCER_REGISTRIES:
        value = getattr(outcome, name, None)
        if value is not None:
            payload[name] = _public_handoff_token(name, value)
    # Caller-echo: the caller's own declared identity, from the validated input.
    receiver = getattr(inp, "to", None)
    if receiver is not None:
        payload["receiver"] = str(receiver)
    kind = getattr(inp, "kind", None)
    if kind is not None:
        payload["kind"] = str(kind)
    anchor = {
        key: str(getattr(inp, key))
        for key in _HANDOFF_ANCHOR_INPUT_FIELDS
        if getattr(inp, key, None) is not None
    }
    if anchor:
        payload["anchor"] = anchor
    marker = _reconstructed_marker(inp)
    if marker is not None:
        payload["notification_marker"] = marker
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
        _public_delivery_outcome(result.outcome, inp) if result.outcome is not None else {}
    )
    is_error = result.fail_closed or result.exit_code != 0
    # Top-level fields are runtime-closed too (#15152 R7): the handoff-result
    # status against its closed set, `delivered` to an EXACT bool (a string
    # `false` must not read as delivered), and `injection_stage` against the
    # closed INJECTION_STAGES.
    status = (
        result.status
        if result.status in _HANDOFF_RESULT_STATUSES
        else "unknown_status"
    )
    delivered = _public_bool(result.delivered) is True
    stage = injection_stage_for_outcome(result.outcome) if result.outcome else None
    injection_stage = stage if stage in INJECTION_STAGES else "unknown_injection_stage"
    payload = {
        "operation": operation,
        "status": status,
        "exit_code": int(result.exit_code),
        "delivered": delivered,
        "injection_stage": injection_stage if result.outcome is not None else "",
        "outcome": outcome_payload,
        "refusal": HANDOFF_REFUSAL_SENTENCE if result.fail_closed else "",
    }
    reason = outcome_payload.get("reason") or "none"
    summary = (
        f"handoff {operation}: {status} "
        f"(delivered={str(delivered).lower()}, reason {reason})"
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


def _public_sublane_outcome(outcome: Any, command: Any) -> dict:
    """The allowlisted projection of one SublaneActuationOutcome.

    Every field is routed through its declared category (#15152 R7,
    finding_projectiontokensopen): booleans are coerced, caller identifiers are
    echoed, producer-owned tokens are validated against their producing registry
    (unknown -> a fixed ``unclassified_<field>``), and producer free text is
    reduced to a ``<field>_present`` boolean. No producer string reaches the
    public boundary raw. `blocked_reasons` and the reconstructed `reason` are
    both built from the validated token list, feeding all three surfaces
    (structured blocked_reasons, reason, and the summary) identically.
    """
    payload: dict = {}
    # status: closed ACTUATE_STATES vocabulary (the field is a plain str with no
    # runtime invariant, so it is validated, not published verbatim).
    status = _public_status(str(getattr(outcome, "status", "") or ""))
    payload["status"] = status
    for name in _BOOL_PUBLIC_FIELDS:
        # Exact bool only (#15152 R7 finding_booleantruthinessoverclaim): a string
        # `false` must not coerce to a published `true`. A non-bool is omitted.
        value = _public_bool(getattr(outcome, name, None))
        if value is not None:
            payload[name] = value
    # Caller-echo (issue/lane_label/branch): sourced SOLELY from the validated
    # caller command, never the producer outcome (#15152 R8
    # finding_callerechobindingopen), so producer drift cannot substitute a value.
    for name in _CALLER_ECHO_PUBLIC_FIELDS:
        value = getattr(command, name, None)
        if value:
            payload[name] = str(value)
    for name in _PRODUCER_TOKEN_REGISTRIES:
        value = getattr(outcome, name, None)
        if value is not None:
            payload[name] = _public_producer_token(name, str(value))
    for name in _PRESENCE_ONLY_PUBLIC_FIELDS:
        payload[f"{name}_present"] = getattr(outcome, name, None) is not None
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
    projected = _public_sublane_outcome(outcome, command)
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
