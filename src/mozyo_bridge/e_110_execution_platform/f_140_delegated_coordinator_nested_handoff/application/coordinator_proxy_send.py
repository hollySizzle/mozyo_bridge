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
  anchor; the target's identity comes from its own boot-time evidence, which the caller cannot mint;
- it **never relaxes the receiver's own gates**. The delegation is an ordinary anchored handoff to
  an attested agent; the coordinator still runs its own preflight, and ``sublane create`` still
  requires *its* sender attestation — which the coordinator has and the caller does not. The proxy
  moves the *decision*, not the *authority to act on it*;
- it **never resolves a target by pane, title, or "closest match"**, and never by assigned name
  alone. Exactly one live agent whose name decodes to (this workspace, the bound role's provider,
  the default lane) **and** whose generation-bound startup self-attestation joins that live slot is
  a target; zero, two-or-more, and unattested are all zero-send;
- it **never treats a fired send as a delivery**. A send that did not positively land leaves an
  ``uncertain`` generation and is reported as a non-delivery, because the caller has no runtime of
  its own and will branch on the exit code.

Resolution is injectable end to end (``rows`` / ``decision_journals`` / ``attestation_join`` /
``fence`` / ``send_port``) so the whole choreography — including every zero-send path and the send
count — is testable without a live herdr or Redmine.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
from typing import Callable, Mapping, Optional, Protocol, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.canonical_note_scan import (  # noqa: E501
    MARKER_CHANNEL_WORKFLOW_EVENT,
    canonical_note_text,
)
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
    TARGET_OK,
    DELIVER,
    REASON_DELIVERY_UNCERTAIN,
    REASON_FENCE_UNAVAILABLE,
    WORKSPACE_RESOLVED,
    WORKSPACE_UNRESOLVED,
    ZERO_SEND,
    ProxyDecision,
    ProxyLinks,
    ACTION_DECISION_TOKENS,
    ACTION_SCOPES,
    SCOPE_ISSUE,
    DecisionRecord,
    IssueExpectation,
    LaneExpectation,
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
    """The live-resolved delegation target, or a fail-closed cardinality (value object).

    ``attestation_state`` / ``attestation_reason`` carry the generation-bound startup
    self-attestation join for the single candidate, so a refusal names *which* attestation state
    stopped it (absent / stale / conflict / missing / unavailable) rather than only "unattested".
    """

    status: str
    assigned_name: str = ""
    locator: str = ""
    live: int = 0
    with_locator: int = 0
    attestation_state: str = ""
    attestation_reason: str = ""


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
    #: the issue's durable decisions in note order (the anchor evidence).
    decisions: "tuple[DecisionRecord, ...]" = ()
    #: the action-time live facts the decision is matched against (lane- or issue-scoped;
    #: ``None`` = unresolved).
    lane_expectation: object = None
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


#: The workflow-event marker channel whose ``gate`` / ``kind`` field names a durable decision. Bound
#: to the shared channel constant rather than re-spelled: the channel set is the scan authority's,
#: and a second literal here is a token this rail could drift on alone.
_WORKFLOW_EVENT_CHANNEL = MARKER_CHANNEL_WORKFLOW_EVENT

#: The marker field that binds a decision to the proxy action it authorizes (Design Answer j#90329
#: contract 5). Without it the same ``implementation_request`` token had to serve every purpose, so
#: "which action does this decision authorize" was never expressed and had to be guessed from the
#: issue's history — which is what let a quotation elsewhere on the issue become authority, and then
#: what let the anti-quotation rule poison the issue permanently.
DECISION_ACTION_FIELD = "proxy_action"

def canonical_decision_in_journal(
    notes: str, *, action: str
) -> "tuple[Optional[DecisionRecord], str]":
    """The single canonical decision a NAMED journal carries for ``action``, or a refusal reason.

    Reads exactly one journal — the one the invocation named — instead of scanning the issue's
    history (Design Answer j#90329 contract 5). The history scan was the root of both failures: a
    quotation anywhere on the issue became a candidate, and the rule that refused two candidates
    then made the issue permanently unusable. Neither can happen when the only text considered is
    the named journal's, with quotations stripped first.

    Canonicality is exact **twice over**, and the second half was missing (Redmine #14667). It is
    not enough that the note carry exactly one accepted marker: that marker's BODY must be one the
    canonical producer (:func:`render_bootstrap_decision_marker`) could have rendered. The reader
    used to fold each body to a dict with last-write-wins, which erases the evidence that it could
    not — so three bodies measured on ``origin/main-next@4f0d765b`` each decided a proxy SEND:

        gate=some_other:gate=implementation_request      (repeated key, last-write-wins)
        proxy_action=dispatch_next:proxy_action=…        (the same, on the action field)
        gate = implementation_request:proxy_action = …   (whitespace-contaminated fields)

    So the body is judged from its **uncollapsed components** by the shared strict reader every
    authority consumer uses (:func:`...redmine_journal_source.strict_marker_fields`), and which
    gate a readable body declares comes from :func:`...redmine_journal_source.marker_logical_gates`
    — both aliases read as a SET, never first-non-empty, because a second gate spelled in the other
    alias is a second authority claim rather than a fallback. Every strictness rule here is that
    shared authority's; this module adds none of its own, or the two would drift the way the two
    notions of "quoted" once did.

    An unreadable marker is **not dropped**. A marker that claims one of this action's tokens and
    is not countable as exactly that token refuses the whole journal
    (``unreadable_canonical_decision``), so a clean sibling written beside a forged one can never
    make the journal read like a clean one — which is how "parse strictly" turns a duplicate
    refusal into an acceptance if the unreadable marker is simply skipped. The claim itself is
    asked of the RAW components (:func:`...redmine_journal_source.marker_declares_gate`), because
    "does this marker claim this gate" and "is its body readable" are different questions and only
    the second one had been asked.

    Zero, two-or-more, an unreadable claim, or a marker that names a different action are all
    refusals with a fixed reason; the caller turns those into zero-send statuses.

    What counts as a quotation is **not** decided here (Redmine #14585), and neither is where a
    marker may be scanned from. Both live in the shared :mod:`...domain.canonical_note_scan`
    authority, which :func:`...redmine_journal_source.marker_components_in_note` scans **per
    canonical line** over :func:`...canonical_note_scan.canonical_note_lines`'s output. The
    per-line property is load-bearing, not decorative: the marker grammar's body is ``[^\\]]*``,
    which spans newlines, so scanning the blanked note as one string would let an unclosed
    ``[mozyo:`` on a quoted line close on a ``]`` further down and read as a marker that no single
    line contains. That property now comes from the shared scan rather than from a loop here plus a
    promise about an injected parser — one authority for both which text is the writer's own voice
    and where a marker may be read from.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
        marker_components_in_note,
        marker_declares_gate,
        marker_logical_gates,
        strict_marker_fields,
    )

    declared_action = normalize_action(action)
    accepted = ACTION_DECISION_TOKENS.get(declared_action, ())
    found: list = []
    unreadable = False
    for channel, components in marker_components_in_note(notes):
        if channel != _WORKFLOW_EVENT_CHANNEL:
            continue
        fields = strict_marker_fields(components)
        gates = marker_logical_gates(fields)
        if len(gates) == 1 and next(iter(gates)) in accepted:
            found.append((next(iter(gates)), fields))
            continue
        # Not countable as one of this action's decisions. If it CLAIMS one anyway, it is a
        # same-kind claim this rail cannot honour, and the journal is fail-closed.
        if any(marker_declares_gate(components, token) for token in accepted):
            unreadable = True
    if unreadable:
        return None, "unreadable_canonical_decision"
    if not found:
        return None, "no_canonical_decision"
    if len(found) >= 2:
        return None, "duplicate_canonical_decision"
    token, fields = found[0]
    # No ``.strip()`` on any field read below: the strict reader has already refused every body
    # carrying whitespace around a key or a value, so stripping here would only hide that
    # guarantee — and a reader that re-normalizes what its producer is required to render exactly
    # is how the lenient fold looked correct in the first place.
    if fields.get(DECISION_ACTION_FIELD, "") != declared_action:
        return None, "action_not_declared"
    return (
        DecisionRecord(
            journal="",  # filled by the caller with the OWNING entry id, never self-reported
            token=token,
            lane=fields.get("lane", ""),
            lane_generation=fields.get("lane_generation", ""),
        ),
        "",
    )


def render_bootstrap_decision_marker(lane: str = "", lane_generation: str = "") -> str:
    """The canonical decision marker a coordinator writes to authorize a proxy action (producer).

    ``proxy_action`` is what makes the decision unambiguous about *what it authorizes*; the reader
    refuses a marker that omits it. A lane-scoped action additionally names its lane and generation.
    """
    marker = f"[mozyo:workflow-event:gate=implementation_request:{DECISION_ACTION_FIELD}="
    if lane.strip():
        return (
            marker + f"dispatch_next:lane={lane.strip()}:lane_generation={(lane_generation or '').strip()}]"
        )
    return marker + "bootstrap_lane]"


def live_named_journal_note(args: argparse.Namespace, issue: str, journal: str) -> "tuple[str, bool]":
    """The verbatim note of the ONE journal the invocation named, and whether it was read.

    Reads only the named journal (Design Answer j#90329 contract 5) through the credential-gated
    source-of-truth boundary. Returns ``(notes, read_ok)``; an unreachable / unconfigured Redmine or
    a journal that is not on this issue yields ``("", False)`` so the anchor simply fails to verify —
    an unreadable record is never a decision.
    """
    issue_id = (issue or "").strip()
    journal_id = (journal or "").strip()
    if not issue_id or not journal_id:
        return "", False
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
            LiveRedmineJournalSource,
        )

        entries = LiveRedmineJournalSource.from_environment().read_entries(issue_id)
    except Exception:  # noqa: BLE001 - any live-read failure fails the anchor gate closed
        return "", False
    for entry in entries or ():
        if str(getattr(entry, "issue_id", "")).strip() != issue_id:
            continue
        if str(getattr(entry, "journal_id", "")).strip() != journal_id:
            continue
        return str(getattr(entry, "notes", "") or ""), True
    return "", False


def live_attestation_join(assigned_name: str, *, locator: str, workspace_id: str, provider: str):
    """Join the live slot with its generation-bound startup self-attestation record.

    Reuses the **existing** read-side policy :func:`...herdr_identity_attestation.evaluate_attestation`
    — the one the adopt classifier and doctor already share — rather than writing a second one that
    could drift from it. Returns ``(ok, state, reason)``; an unreadable store yields
    ``(False, "unavailable", …)`` so a missing store fails closed instead of decaying to a
    name-only match (review j#89878 finding 2).
    """
    try:
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            HerdrIdentityAttestationStore,
            evaluate_attestation,
        )

        record = HerdrIdentityAttestationStore().read(assigned_name)
        join = evaluate_attestation(
            record,
            live_locator=locator,
            expected_workspace_id=workspace_id,
            expected_role=provider,
            expected_lane=DEFAULT_LANE,
        )
    except Exception:  # noqa: BLE001 - an unreadable attestation store is never an attestation
        return False, "unavailable", "the startup self-attestation store could not be read"
    return bool(join.ok), str(join.state), str(join.reason)


def resolve_proxy_target(
    rows: Sequence[Mapping],
    *,
    workspace_id: str,
    provider: str,
    attestation_join: Optional[Callable[..., "tuple[bool, str, str]"]] = None,
) -> ProxyTarget:
    """Resolve the single live, **attested** default-lane coordinator target (pure over ``rows``).

    A row qualifies only when its **mzb1 assigned name** decodes to this workspace, this provider,
    and the default lane. Rows from another workspace are excluded by that decode, which is what
    makes a cross-workspace delegation structurally impossible rather than merely discouraged.

    The decode is necessary but **not sufficient** (review j#89878 finding 2). An assigned name is
    what a slot was launched to be; only the generation-bound startup self-attestation record
    attests that *this* process actually booted with that identity and still occupies that live
    locator. So a single decoded candidate with a usable locator is joined against that record, and
    a record that is absent / stale (a different process generation) / conflicting / missing yields
    :data:`TARGET_UNATTESTED` rather than a target. ``attestation_join`` is injectable; the live
    default is :func:`live_attestation_join`.
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

    attested: Optional[bool] = None
    attestation_state = ""
    attestation_reason = ""
    if live == 1 and with_locator == 1:
        join = attestation_join or live_attestation_join
        attested, attestation_state, attestation_reason = join(
            assigned_name, locator=locator, workspace_id=ws, provider=want_provider
        )
    status = target_status_from_cardinality(live, with_locator, attested=attested)
    return ProxyTarget(
        status=status,
        assigned_name=assigned_name,
        locator=locator,
        live=live,
        with_locator=with_locator,
        attestation_state=attestation_state,
        attestation_reason=attestation_reason,
    )


def live_lane_expectation(repo_root, lane: str) -> "Optional[LaneExpectation]":
    """The action-time lifecycle facts for ``lane``, or ``None`` when unreadable (fail-closed).

    Reads the SAME lane lifecycle authority the worker-dispatch admission joins
    (:class:`...lane_lifecycle.LaneLifecycleStore`), so the proxy and the dispatch rail agree on
    what a lane's current generation and decision anchor are. Only an ``active`` row with a positive
    generation counts: a retired / unbound / generation-zero lane has no decision to act on.

    ``None`` is returned for a lane with no row, a disposition that is not active, or any store
    failure — the classifier then fails closed rather than matching the decision against itself
    (review j#89969 finding 2).
    """
    lane_id = (lane or "").strip()
    if not lane_id:
        return None
    try:
        from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
        from mozyo_bridge.core.state.lane_lifecycle_model import LaneLifecycleKey
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            repo_scope_workspace_id,
        )

        scope = repo_scope_workspace_id(repo_root)
        if not scope:
            return None
        record = LaneLifecycleStore().get(LaneLifecycleKey(scope, lane_id))
    except Exception:  # noqa: BLE001 - an unreadable lifecycle authority resolves no expectation
        return None
    if record is None:
        return None
    generation = int(getattr(record, "lane_generation", 0) or 0)
    if getattr(record, "lane_disposition", "") != "active" or generation <= 0:
        return None
    return LaneExpectation(
        lane=lane_id,
        generation=generation,
        decision_journal=str(getattr(record, "decision_journal", "") or "").strip(),
    )


#: Acknowledgement is NOT an authority on this rail (Design Consultation Answer j#90329 contract 2).
#: A marker grammar and its reader lived here across two drafts, asking first "is the caller the
#: coordinator?" (answered from caller-supplied env) and then "did the coordinator record an ack?"
#: (answered from a Redmine note anyone with an API key can write). Neither question is answerable on
#: this transport, so the claim was withdrawn rather than relocated a third time: a positively
#: recorded DELIVERY is the proxy's terminal success, and a generation whose fate is genuinely
#: unknown is resolved by an operator disposition (`workflow proxy-reconcile`), never by a marker.


def live_issue_expectation(repo_root, issue: str, _decisions=(), *, action: str = ""):
    """The action-time live facts for an issue-scoped (bootstrap) decision, or ``None``.

    The bootstrap precondition is that the issue owns **no active lane** — the state the observed
    dead end leaves behind. Read through the lifecycle authority's own owner resolver, which is
    fail-closed by construction: zero owners and many owners both resolve to "no owner", so a caller
    can never fall back to "the newest lane". ``None`` (fail-closed) only when the workspace scope or
    the store cannot be read at all.
    """
    issue_id = (issue or "").strip()
    if not issue_id:
        return None
    try:
        from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
        from mozyo_bridge.core.state.lane_lifecycle_model import LaneLifecycleKey  # noqa: F401
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            repo_scope_workspace_id,
        )

        scope = repo_scope_workspace_id(repo_root)
        if not scope:
            return None
        owner = LaneLifecycleStore().resolve_owner(scope, issue_id)
    except Exception:  # noqa: BLE001 - an unreadable lifecycle authority resolves no expectation
        return None
    return IssueExpectation(
        issue=issue_id,
        owns_active_lane=bool(getattr(owner, "resolved", False)),
        latest_decision_journal="",  # the named journal IS the decision now (contract 5)
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
    named_journal_provider: Optional[Callable[..., "tuple[str, bool]"]] = None,
    workspace_provider: Optional[Callable[..., str]] = None,
    attestation_join: Optional[Callable[..., "tuple[bool, str, str]"]] = None,
    lane_expectation_provider: Optional[Callable[..., "Optional[LaneExpectation]"]] = None,
    issue_expectation_provider: Optional[Callable[..., "Optional[IssueExpectation]"]] = None,
) -> ProxyContext:
    """Re-derive every authority link at action time and assemble the matrix input (no fence).

    The fence link is left :data:`FENCE_UNAVAILABLE` here: it is filled in by
    :func:`execute_proxy_delegation` only when every other link already permits delivery, so a
    delegation that was going to be refused anyway never consumes a generation. A dry run therefore
    reports the full resolution while writing nothing.
    """
    resolve_ws = workspace_provider or live_workspace_id
    resolve_rows = rows_provider or live_agent_rows
    read_note = named_journal_provider or (lambda i, j: live_named_journal_note(args, i, j))

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
        resolve_proxy_target(
            resolve_rows(env),
            workspace_id=workspace_id,
            provider=provider,
            attestation_join=attestation_join,
        )
        if workspace_id and provider
        else ProxyTarget(status=target_status_from_cardinality(0, 0))
    )

    decision = None
    decision_refusal = "no_canonical_decision"
    if issue and journal:
        notes, read_ok = read_note(issue, journal)
        if read_ok:
            decision, decision_refusal = canonical_decision_in_journal(notes, action=action)
            if decision is not None:
                # The journal id is the OWNING entry's, never the marker's self-report.
                decision = replace(decision, journal=journal)
    decisions = (decision,) if decision is not None else ()
    # WHICH live facts the decision is matched against depends on the action's scope. A lane-scoped
    # action resolves the lane the DECISION names — never one the caller supplies, and never the
    # coordinator's own lane. An issue-scoped (bootstrap) action resolves the issue's ownership
    # instead, because its whole precondition is that no lane exists yet (review j#90068 F1).
    if ACTION_SCOPES.get(normalize_action(action)) == SCOPE_ISSUE:
        resolve_issue = issue_expectation_provider or live_issue_expectation
        expectation = resolve_issue(repo_root, issue, decisions, action=action)
    else:
        declared_lane = decision.lane if decision is not None else ""
        resolve_expectation = lane_expectation_provider or live_lane_expectation
        expectation = resolve_expectation(repo_root, declared_lane) if declared_lane else None
    anchor_status = anchor_status_for(
        action=action, decision=decision, decision_refusal=decision_refusal, expected=expectation
    )

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
        decisions=decisions,
        lane_expectation=expectation,
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
    #
    # The send itself may raise (review j#90250 finding 3). An exception escaping here skipped the
    # outcome write entirely and left the generation `reserved` — a state nothing auto-resolves and
    # which is not safely re-sendable, so the only ways out were a blind re-run or the whole-store
    # recovery. An unknown effect boundary is exactly what `uncertain` means, so it is recorded as
    # that and reported as a typed non-delivery.
    try:
        outcome = send_port.send(context, reserve.action_id, args=args)
    except Exception as exc:  # noqa: BLE001 - an unknown send outcome is uncertain, never an escape
        try:
            recorded = fence.mark_uncertain(
                route, reserve.action_id, detail=f"send raised {type(exc).__name__}",
                issue=context.issue, journal=context.journal,
            )
        except CoordinatorProxyFenceError:
            recorded = False  # the store also failed; say so rather than claim a recording
        return ProxyExecutionResult(
            sent=False, decision=ZERO_SEND, reason=REASON_DELIVERY_UNCERTAIN,
            action_id=reserve.action_id, fence_state=FENCE_RECONCILE,
            detail=(
                f"the send raised {type(exc).__name__} and its effect boundary is unknown. "
                + (
                    "The generation is recorded `uncertain`"
                    if recorded
                    else "The generation could NOT be recorded `uncertain` (the store write did not "
                    "land), so its stored state is unknown"
                )
                + " and is never blind-retried. Resolve it with `workflow proxy-reconcile` against "
                f"action id {reserve.action_id} after establishing what happened."
            ),
        )
    context.links = replace(context.links, fence=FENCE_OPEN)
    if outcome.result == SEND_DELIVERED:
        # The outcome write is a CAS, and its result is the only evidence that the delivery and
        # the durable record agree (review j#90032 finding 2). It fails whenever the generation
        # this send belongs to is no longer `reserved` — the ordinary way that happens is a
        # concurrent retry re-entering the reserve mid-send, which transitions the row to
        # `uncertain`. Ignoring the CAS reported success to the caller while the store said
        # `uncertain`, and since `proxy-ack` completes only a `delivered` generation, the route
        # then wedged with no way forward. A delivery the store did not record is not a delivery.
        try:
            recorded = fence.mark_delivered(
                route, reserve.action_id, detail=outcome.detail,
                issue=context.issue, journal=context.journal,
            )
        except CoordinatorProxyFenceError as exc:
            # A store failure is the same class of unknown as a lost CAS (review j#90068 F2): the
            # send fired and the durable record does not reflect it. Typed, nonzero, never raised
            # at the caller and never blind-retried.
            return ProxyExecutionResult(
                sent=False, decision=ZERO_SEND, reason=REASON_DELIVERY_UNCERTAIN,
                action_id=reserve.action_id, fence_state=FENCE_RECONCILE,
                detail=(
                    "the send was positively delivered but its outcome could not be recorded (the "
                    f"delegation store failed): {exc}. Reconcile against the durable state before "
                    "deciding whether the action was taken."
                ),
                send=outcome,
            )
        if not recorded:
            return ProxyExecutionResult(
                sent=False, decision=ZERO_SEND, reason=REASON_DELIVERY_UNCERTAIN,
                action_id=reserve.action_id, fence_state=FENCE_RECONCILE,
                detail=(
                    "the send was positively delivered but its generation was no longer reserved "
                    "when the outcome was recorded (a concurrent retry advanced it). The durable "
                    "state is authoritative: reconcile with the coordinator before deciding "
                    "whether the action was taken. This is never blind-retried."
                ),
                send=outcome,
            )
        return ProxyExecutionResult(
            sent=True, decision=DELIVER, action_id=reserve.action_id, fence_state=FENCE_OPEN,
            detail=f"delegated once to {context.target.assigned_name}", send=outcome,
        )
    # A send that fired but did not positively land is NOT a delivery (review j#89878 finding 3).
    # The fence holds an `uncertain` generation for an operator reconcile, and the caller — which
    # has no runtime of its own and will typically branch on the exit code — must be told the
    # delegation did not land. Reporting `sent` here would make a lost delegation script as success.
    # The same principle applies to the non-delivery write: its CAS result is observed, not
    # assumed. Either way this is a non-delivery, so the caller's verdict does not change — but
    # a write that did not land means the row is already held by another generation state, and
    # the detail must say so rather than claim a recording that never happened.
    try:
        recorded = fence.mark_uncertain(
            route, reserve.action_id, detail=outcome.detail or f"send {outcome.result}",
            issue=context.issue, journal=context.journal,
        )
    except CoordinatorProxyFenceError as exc:
        return ProxyExecutionResult(
            sent=False, decision=ZERO_SEND, reason=REASON_DELIVERY_UNCERTAIN,
            action_id=reserve.action_id, fence_state=FENCE_RECONCILE,
            detail=(
                "the send did not positively land AND its outcome could not be recorded (the "
                f"delegation store failed): {exc}. Reconcile against the durable state."
            ),
            send=outcome,
        )
    return ProxyExecutionResult(
        sent=False, decision=ZERO_SEND, reason=REASON_DELIVERY_UNCERTAIN,
        action_id=reserve.action_id,
        fence_state=FENCE_OPEN if recorded else FENCE_RECONCILE,
        detail=(
            (
                "the single send fired but did not positively land; the delegation generation is "
                "recorded `uncertain` and is never blind-retried. Reconcile with the coordinator, "
                "then decide explicitly. "
                if recorded
                else "the single send fired but did not positively land, AND its generation was "
                "no longer reserved when the outcome was recorded (a concurrent retry advanced "
                "it). Reconcile against the durable state before deciding. "
            )
            + outcome.detail
        ),
        send=outcome,
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
            f"journal={context.journal}. Read the durable anchor and perform this one "
            f"already-resolved action from your own attested runtime. Record the outcome on the "
            f"durable anchor. No acknowledgement command is required or accepted: this delegation "
            f"is delivered exactly once and the same decision is never delegated again; a strictly "
            f"newer canonical decision authorizes the next one."
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
    "live_named_journal_note",
    "canonical_note_text",
    "canonical_decision_in_journal",
    "render_bootstrap_decision_marker",
    "DECISION_ACTION_FIELD",
    "live_attestation_join",
    "live_lane_expectation",
    "live_issue_expectation",
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
