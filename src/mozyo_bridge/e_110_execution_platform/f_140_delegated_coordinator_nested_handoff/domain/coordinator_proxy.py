"""Pure single-step coordinator-proxy decision matrix (Redmine #14546).

An **external coordinator client** — an operator shell or API caller that is not itself an attested
lane agent — needs a sanctioned way to hand one *already durably resolved* high-level action to the
live attested default coordinator. Without one, the observed dead end (#14500 / #14546 j#89697) is:
``workflow step`` stops at ``herdr_sender_identity_unresolved`` and ``sublane create --execute``
stops pre-effect at ``missing_identity`` + ``sender_attestation``, because both require a
launch-time sender identity the external client structurally does not have. The two "fixes" that
must stay unavailable are exporting ``MOZYO_*`` by hand (forging the identity) and typing into the
coordinator's pane directly (bypassing every gate).

This module is the pure decision matrix for the sanctioned third option. It answers one question —
*may this delegation be delivered, exactly once, right now?* — from facts the caller's adapter
resolved, and names a fixed reason for every refusal. It is pure: value objects and total functions
over plain tokens. It reads no env, opens no file, scans no inventory, and performs no send.

The authority chain it encodes, in order, is deliberately **not** "attest the caller". The caller is
never attested — it has no identity to attest. Instead every link is re-derived at action time from
something the caller cannot assert:

1. the **workspace** comes from the repo checkout's registry anchor, never from the caller's env;
2. the **role** comes from the durable repo-local role authority, never from placement or provider;
3. the **provider** comes from ``provider_binding`` for that role, never from the caller;
4. the **target** is the single live agent whose mzb1 assigned name decodes to that
   (workspace, provider, default lane) AND whose generation-bound startup self-attestation record
   joins that live slot — the agent's own boot-time evidence, which the caller cannot forge. The
   name alone is only what the slot was launched to *be*;
5. the **(action, journal) pair** must be a decision the durable record already carries: the exact
   journal must hold a workflow-event marker whose token authorizes that specific action, and be
   the current one of its kind, verified against source-of-truth Redmine at action time. Verifying
   the action and the journal separately verifies neither;
6. the **delivery** is fenced so the same durable decision is delegated exactly once.

Any link that is missing, ambiguous, drifted, superseded, or already delegated is a **zero-send**
with a fixed reason. There is no path through this matrix that delivers on a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .marker_value_contract import is_canonical_positive_decimal

# ---------------------------------------------------------------------------
# The closed action vocabulary, and — inseparably — the durable decision each action must be
# authorized by (review j#89878 finding 1).
#
# The first draft of this rail had a closed action vocabulary and a closed anchor check, but did
# not join them: any in-vocabulary action could ride on *any* gate-bearing journal, so an
# ``implementation_done`` could authorize a ``dispatch_next``. A proxy that delegates "an action"
# and verifies "a journal" separately has not verified the *decision* at all — the pair is the
# unit of authority, not either half.
#
# So an action exists here only when it can be tied to a durable decision token. The
# ``workflow_step`` action was withdrawn for exactly that reason: "advance one step" is authorized
# by whatever gate currently names the next action, which is not a fixed token, and inventing a
# mapping would be the same unverified join in a different shape. A narrower delegable surface is
# the fail-closed answer; it can be widened when a concrete decision token is identified.
# ---------------------------------------------------------------------------
#: Delegate the coordinator's already-resolved dispatch decision for an EXISTING managed lane.
ACTION_DISPATCH_NEXT = "dispatch_next"
#: Delegate the coordinator's already-resolved decision to materialize the issue's FIRST managed
#: lane (review j#90068 finding 1). This is the action the observed dead end actually needs: the
#: external client's `sublane create --execute` stopped pre-effect with zero lane / worktree / pair,
#: so a rail that can only act on an existing lane cannot solve "no lane can be created".
ACTION_BOOTSTRAP_LANE = "bootstrap_lane"

PROXY_ACTIONS: tuple[str, ...] = (ACTION_BOOTSTRAP_LANE, ACTION_DISPATCH_NEXT)

# ---------------------------------------------------------------------------
# Scope kinds. An action's decision is matched against live facts, but WHICH live facts depends on
# whether the decision is about a lane that exists or one that does not yet.
# ---------------------------------------------------------------------------
#: The decision names a lane; it is matched against that lane's live lifecycle facts.
SCOPE_LANE = "lane_scoped"
#: The decision names only an issue, and its precondition is that the issue owns NO active lane
#: yet. Requiring lane/generation fields here would be incoherent — the lane does not exist — so
#: the fields must be ABSENT, and the live fact matched against is the absence of an owner.
SCOPE_ISSUE = "issue_scoped"

#: The CLOSED action -> accepted decision-token map. A journal authorizes an action only when it
#: carries a workflow-event marker naming one of that action's tokens. ``implementation_request``
#: is the coordinator's dispatch decision; it is deliberately NOT in the callback-required
#: ``GATE_BEARING_KINDS`` vocabulary (a dispatch wakes nobody), which is why the adapter reads the
#: generic workflow-event token rather than the callback-gate reader.
ACTION_DECISION_TOKENS: "dict[str, tuple[str, ...]]" = {
    ACTION_BOOTSTRAP_LANE: ("implementation_request",),
    ACTION_DISPATCH_NEXT: ("implementation_request",),
}

#: action -> the scope its decision is matched in. Both actions ride the same decision token — the
#: coordinator's implementation request — but they are distinguished by what the durable record must
#: currently look like: a bootstrap needs no owning lane and a lane-less decision, a dispatch needs
#: the lane it names to be live at the generation it names.
ACTION_SCOPES: "dict[str, str]" = {
    ACTION_BOOTSTRAP_LANE: SCOPE_ISSUE,
    ACTION_DISPATCH_NEXT: SCOPE_LANE,
}

# ---------------------------------------------------------------------------
# Per-link status tokens the adapter maps from its live resolution.
# ---------------------------------------------------------------------------
WORKSPACE_RESOLVED = "resolved"
WORKSPACE_UNRESOLVED = "unresolved"

AUTHORITY_RESOLVED = "resolved"
AUTHORITY_MISSING = "missing"  # no durable role bound to the default lane
AUTHORITY_BLOCKED = "blocked"  # invalid / ambiguous / provider-mismatch declaration

PROVIDER_RESOLVED = "resolved"
PROVIDER_UNRESOLVED = "unresolved"

TARGET_OK = "ok"  # exactly one live default-lane agent with a usable locator AND a matched attestation
TARGET_MISSING = "missing"  # zero live agents for this (workspace, provider, default lane)
TARGET_AMBIGUOUS = "ambiguous"  # 2+ (duplicate identity) — never guess one
TARGET_LOCATOR_MISSING = "locator_missing"  # one agent, no usable live locator
#: The single live agent has no generation-matched startup self-attestation (review j#89878 F2).
#: An assigned name decodes an *intent*; only the store's generation-bound record attests that
#: THIS process booted with that identity, so a name-only match is not "attested".
TARGET_UNATTESTED = "unattested"

ANCHOR_VERIFIED = "verified"  # the exact journal carries this action's decision token, and is current
ANCHOR_UNVERIFIED = "unverified"  # not a workflow-event marker on this issue / live read unavailable
ANCHOR_SUPERSEDED = "superseded"  # a NEWER decision of the SAME kind supersedes this one
#: The journal carries a workflow-event marker, but not one that authorizes the requested action
#: (review j#89878 F1). Distinct from ``unverified`` on purpose: the anchor is real, the *pairing*
#: is not, and an operator needs to be told which of the two is wrong.
ANCHOR_ACTION_MISMATCH = "action_mismatch"
#: The decision marker names this action's token but omits the lane / lane_generation the
#: canonical producer writes (review j#89918 finding 2). A decision that does not name the lane and
#: generation it authorizes cannot be exact-matched against anything, so it authorizes nothing.
#: This is also what a marker-shaped *quotation* in prose degrades to — a quoted gate token carries
#: no lane or generation, so it can never pass as a decision.
ANCHOR_DECISION_INCOMPLETE = "decision_incomplete"
#: The decision names a lane generation that is no longer the current one for that lane: the lane
#: was re-created / advanced, so this authorization belongs to a dead generation.
ANCHOR_GENERATION_STALE = "generation_stale"
#: The lane the decision names has no readable live lifecycle facts, so nothing can be matched
#: against it (review j#89969 finding 2). A decision naming a lane the runtime does not know is
#: not a decision this rail can act on.
ANCHOR_LANE_UNRESOLVED = "lane_unresolved"
#: The decision's declared scope does not match the live lane it names.
ANCHOR_SCOPE_MISMATCH = "scope_mismatch"
#: The issue carries MORE THAN ONE decision of this action's token (review j#90250 finding 2). The
#: marker scanner recognises a token anywhere in a note and cannot tell a real decision from one
#: QUOTED in prose or backticks, so a quotation appears as a second decision. An issue's bootstrap
#: is authorized once; two candidates is ambiguity, and this rail never picks between them.
ANCHOR_DECISION_AMBIGUOUS = "decision_ambiguous"
#: The named journal CLAIMS this action's decision token in a marker body the canonical producer
#: could not have rendered — a repeated key, a whitespace-contaminated field, a malformed component,
#: or a body naming a second gate alongside it (Redmine #14667). Distinct from
#: :data:`ANCHOR_UNVERIFIED` ("this journal carries no decision") and from
#: :data:`ANCHOR_DECISION_AMBIGUOUS` ("it carries two"): here there IS a same-kind claim and it is
#: uncountable, so the remedy is different — the coordinator re-records the decision with a
#: producer-rendered marker. The claim is never dropped in favour of a clean sibling.
ANCHOR_DECISION_UNREADABLE = "decision_unreadable"

FENCE_OPEN = "open"  # the fence reserved this delegation (the single caller cleared to deliver)
FENCE_DUPLICATE = "duplicate"  # in flight, or this exact decision was already delegated
FENCE_STALE = "stale"  # this decision is not strictly newer than the one delegated on this route
FENCE_RECONCILE = "reconcile"  # a prior reserve never resolved; an operator reconcile precedes
FENCE_UNAVAILABLE = "unavailable"  # the store could not be consulted -> do-not-send

# ---------------------------------------------------------------------------
# Fixed zero-send reasons. Every refusal names exactly one, so a caller never has to read prose to
# find out what stopped it — and a durable record of a refusal stays machine-comparable.
# ---------------------------------------------------------------------------
REASON_ACTION_UNKNOWN = "proxy_action_unknown"
REASON_WORKSPACE_UNRESOLVED = "proxy_workspace_unresolved"
REASON_AUTHORITY_MISSING = "proxy_coordinator_authority_missing"
REASON_AUTHORITY_BLOCKED = "proxy_coordinator_authority_blocked"
REASON_PROVIDER_UNRESOLVED = "proxy_provider_unresolved"
REASON_TARGET_MISSING = "proxy_target_missing"
REASON_TARGET_AMBIGUOUS = "proxy_target_ambiguous"
REASON_TARGET_LOCATOR_MISSING = "proxy_target_locator_missing"
REASON_TARGET_UNATTESTED = "proxy_target_unattested"
REASON_ANCHOR_UNVERIFIED = "proxy_anchor_unverified"
REASON_ANCHOR_SUPERSEDED = "proxy_anchor_superseded"
REASON_ANCHOR_ACTION_MISMATCH = "proxy_anchor_action_mismatch"
REASON_ANCHOR_DECISION_INCOMPLETE = "proxy_anchor_decision_incomplete"
REASON_ANCHOR_GENERATION_STALE = "proxy_anchor_generation_stale"
REASON_ANCHOR_LANE_UNRESOLVED = "proxy_anchor_lane_unresolved"
REASON_ANCHOR_SCOPE_MISMATCH = "proxy_anchor_scope_mismatch"
REASON_ANCHOR_DECISION_AMBIGUOUS = "proxy_anchor_decision_ambiguous"
REASON_ANCHOR_DECISION_UNREADABLE = "proxy_anchor_decision_unreadable"
#: The single send fired but did not positively land; the fence holds an ``uncertain`` generation
#: awaiting an operator reconcile (review j#89878 finding 3). NOT a success.
REASON_DELIVERY_UNCERTAIN = "proxy_delivery_uncertain"
REASON_DUPLICATE = "proxy_duplicate"
REASON_STALE = "proxy_stale"
REASON_FENCE_RECONCILE = "proxy_fence_reconcile_required"
REASON_FENCE_UNAVAILABLE = "proxy_fence_unavailable"

DELIVER = "deliver"
ZERO_SEND = "zero_send"

_TARGET_REASON = {
    TARGET_MISSING: REASON_TARGET_MISSING,
    TARGET_AMBIGUOUS: REASON_TARGET_AMBIGUOUS,
    TARGET_LOCATOR_MISSING: REASON_TARGET_LOCATOR_MISSING,
    TARGET_UNATTESTED: REASON_TARGET_UNATTESTED,
}

_ANCHOR_REASON = {
    ANCHOR_UNVERIFIED: REASON_ANCHOR_UNVERIFIED,
    ANCHOR_SUPERSEDED: REASON_ANCHOR_SUPERSEDED,
    ANCHOR_ACTION_MISMATCH: REASON_ANCHOR_ACTION_MISMATCH,
    ANCHOR_DECISION_INCOMPLETE: REASON_ANCHOR_DECISION_INCOMPLETE,
    ANCHOR_GENERATION_STALE: REASON_ANCHOR_GENERATION_STALE,
    ANCHOR_LANE_UNRESOLVED: REASON_ANCHOR_LANE_UNRESOLVED,
    ANCHOR_SCOPE_MISMATCH: REASON_ANCHOR_SCOPE_MISMATCH,
    ANCHOR_DECISION_AMBIGUOUS: REASON_ANCHOR_DECISION_AMBIGUOUS,
    ANCHOR_DECISION_UNREADABLE: REASON_ANCHOR_DECISION_UNREADABLE,
}

_FENCE_REASON = {
    FENCE_DUPLICATE: REASON_DUPLICATE,
    FENCE_STALE: REASON_STALE,
    FENCE_RECONCILE: REASON_FENCE_RECONCILE,
    FENCE_UNAVAILABLE: REASON_FENCE_UNAVAILABLE,
}


def normalize_action(action: object) -> str:
    """Normalize a raw action token to a member of :data:`PROXY_ACTIONS`, or ``""`` (pure)."""
    token = str(action or "").strip()
    return token if token in PROXY_ACTIONS else ""


@dataclass(frozen=True)
class ProxyLinks:
    """The action-time status of each authority link the adapter resolved (value object).

    Every field is a status token, never the underlying object: the matrix decides from *what the
    adapter found*, so it stays pure and exhaustively testable without a live herdr / Redmine.
    """

    action: str
    workspace: str
    authority: str
    provider: str
    target: str
    anchor: str
    fence: str


@dataclass(frozen=True)
class ProxyDecision:
    """Whether the single delegation may be delivered, or a fixed zero-send reason."""

    decision: str
    reason: str = ""
    detail: str = ""

    @property
    def delivers(self) -> bool:
        return self.decision == DELIVER


def decide_proxy_delegation(links: ProxyLinks) -> ProxyDecision:
    """Decide deliver / zero-send from the action-time link statuses (pure, fail-closed).

    Evaluated in **authority order**, cheapest-and-most-fundamental first, so the reported reason is
    the *first* broken link rather than an incidental later one. The ordering also matters for
    effect: the fence is consulted last, so a delegation that was going to be refused for a bad
    target or an unverified anchor never consumes a generation.

    Only an in-vocabulary action, a resolved workspace, a resolved authority, a resolved provider, a
    single addressable target, a verified non-superseded anchor, AND an open fence deliver. Every
    other combination is a :data:`ZERO_SEND` naming exactly one fixed reason.
    """
    if not normalize_action(links.action):
        return ProxyDecision(
            decision=ZERO_SEND,
            reason=REASON_ACTION_UNKNOWN,
            detail=(
                f"action {str(links.action or '')!r} is not a delegable coordinator action; "
                f"expected one of {list(PROXY_ACTIONS)}"
            ),
        )
    if links.workspace != WORKSPACE_RESOLVED:
        return ProxyDecision(
            decision=ZERO_SEND,
            reason=REASON_WORKSPACE_UNRESOLVED,
            detail=(
                "the workspace anchor could not be derived from the repo checkout; the proxy "
                "never falls back to a caller-supplied workspace id"
            ),
        )
    if links.authority == AUTHORITY_MISSING:
        return ProxyDecision(
            decision=ZERO_SEND,
            reason=REASON_AUTHORITY_MISSING,
            detail=(
                "no durable workflow-role authority binds this workspace's default lane; declare "
                "it with `workflow role-authority --mint-coordinator` before delegating"
            ),
        )
    if links.authority != AUTHORITY_RESOLVED:
        return ProxyDecision(
            decision=ZERO_SEND,
            reason=REASON_AUTHORITY_BLOCKED,
            detail=(
                "the durable workflow-role authority is present but unusable (malformed / "
                "ambiguous / provider mismatch); fail closed rather than delegate on a guess"
            ),
        )
    if links.provider != PROVIDER_RESOLVED:
        return ProxyDecision(
            decision=ZERO_SEND,
            reason=REASON_PROVIDER_UNRESOLVED,
            detail=(
                "provider_binding resolves no provider for the bound default-lane role; bind it "
                "(or fix the config) before delegating"
            ),
        )
    if links.target != TARGET_OK:
        return ProxyDecision(
            decision=ZERO_SEND,
            reason=_TARGET_REASON.get(links.target, REASON_TARGET_MISSING),
            detail=(
                f"live default-lane coordinator target status {links.target!r}; the proxy "
                "addresses exactly one attested agent or none at all"
            ),
        )
    if links.anchor != ANCHOR_VERIFIED:
        return ProxyDecision(
            decision=ZERO_SEND,
            reason=_ANCHOR_REASON.get(links.anchor, REASON_ANCHOR_UNVERIFIED),
            detail=(
                f"durable anchor status {links.anchor!r}; the proxy delegates only a decision the "
                "source-of-truth record currently carries"
            ),
        )
    if links.fence != FENCE_OPEN:
        return ProxyDecision(
            decision=ZERO_SEND,
            reason=_FENCE_REASON.get(links.fence, REASON_FENCE_UNAVAILABLE),
            detail=f"delegation fence state {links.fence!r}; zero-send rather than risk a repeat",
        )
    return ProxyDecision(
        decision=DELIVER,
        detail="every authority link re-derived at action time; the fence reserved this delegation",
    )


def target_status_from_cardinality(
    live: int, with_locator: int, *, attested: Optional[bool] = None
) -> str:
    """Map a live-target cardinality + attestation join onto a status (pure, fail-closed).

    A duplicate identity is **ambiguity**, never a silently-picked target: 2+ live agents matching
    the same (workspace, provider, default lane) means the inventory cannot name one coordinator,
    and delivering to either would be a guess.

    ``attested`` is the generation-bound startup self-attestation join for the single candidate
    (review j#89878 finding 2). It is a **required** step, not a refinement: an mzb1 assigned name
    decodes what a slot was *launched to be*, which the store's generation-pinned record is what
    actually attests. ``None`` (the caller could not perform the join) is treated as
    :data:`TARGET_UNATTESTED`, so an unreadable attestation store fails closed rather than decaying
    to a name-only match.
    """
    if live <= 0:
        return TARGET_MISSING
    if live >= 2:
        return TARGET_AMBIGUOUS
    if with_locator < 1:
        return TARGET_LOCATOR_MISSING
    return TARGET_OK if attested else TARGET_UNATTESTED


@dataclass(frozen=True)
class DecisionRecord:
    """One durable decision the issue's journals carry (value object).

    ``journal`` is the **owning entry's own** id, ``token`` the workflow-event marker's
    ``gate`` / ``kind``, and ``lane`` / ``lane_generation`` the fields the canonical dispatch
    producer writes. The last two are what make a decision *exact-matchable*; a decision that omits
    them names no scope and therefore authorizes nothing (review j#89918 finding 2).
    """

    journal: str
    token: str
    lane: str = ""
    lane_generation: str = ""


def _generation_ordinal(value: object) -> Optional[int]:
    """The numeric lane generation, or ``None`` when absent / non-canonical (pure, fail-closed).

    "Numeric" is :func:`.marker_value_contract.is_canonical_positive_decimal` — the feature's
    single declared shape for ``lane_generation``, bounded by the lifecycle store's own
    ``lane_generation INTEGER`` column — and NOT ``str.isdigit()``, which was the guard here.
    ``isdigit()`` is not "a number ``int()`` can read": measured on this surface (Redmine
    #14753), a decision carrying ``lane_generation="²"`` raised a raw ``ValueError`` out of
    :func:`_lane_scoped_status`, a function whose whole job is to return a fixed refusal token.
    ``None`` is that token's precondition — the decision reads as
    :data:`ANCHOR_DECISION_INCOMPLETE` and authorizes nothing.

    Refusing ``"0"`` / ``"01"`` is the same fail-closed direction, not a new rule: a lane
    generation is a positive count, and a padded token is not the value the lifecycle row holds,
    so neither can exact-match a live generation.
    """
    token = str(value or "").strip()
    return int(token) if is_canonical_positive_decimal(token) else None


@dataclass(frozen=True)
class IssueExpectation:
    """The live lifecycle facts for the ISSUE a bootstrap decision names (value object).

    ``owns_active_lane`` is the precondition the bootstrap acts on, inverted: a bootstrap
    authorizes materializing the first lane, so an issue that already owns one has moved past it
    (the caller wants ``dispatch_next``). ``latest_decision_journal`` is the newest accepted-token
    decision on the issue, so an older one reads as superseded.
    """

    issue: str
    owns_active_lane: bool
    latest_decision_journal: str


@dataclass(frozen=True)
class LaneExpectation:
    """The live lifecycle facts for the lane a decision names (value object).

    Resolved at action time from the lane lifecycle authority — never from the decision itself and
    never from the caller. ``decision_journal`` is the journal the lane's lifecycle currently
    records as its decision anchor; it is the field that makes a *quotation* of a real decision
    distinguishable from the decision (review j#89969 finding 2).
    """

    lane: str
    generation: int
    decision_journal: str


#: Canonical-read refusals the adapter reports, mapped onto anchor statuses. The reader looks at
#: exactly ONE journal — the one the invocation names — so each refusal is about that journal alone
#: and can never be caused by, or poisoned by, anything else on the issue (Design Answer j#90329
#: contract 5). Within that one journal the refusals ARE same-note: a claim of this action's token
#: that the canonical producer could not have rendered refuses the journal rather than being
#: dropped in favour of a clean sibling marker beside it (Redmine #14667).
DECISION_REFUSAL_STATUS: "dict[str, str]" = {
    "no_canonical_decision": ANCHOR_UNVERIFIED,
    "duplicate_canonical_decision": ANCHOR_DECISION_AMBIGUOUS,
    "unreadable_canonical_decision": ANCHOR_DECISION_UNREADABLE,
    "action_not_declared": ANCHOR_ACTION_MISMATCH,
}


def _lane_scoped_status(record: "DecisionRecord", expected) -> str:
    """Classify a lane-scoped decision against the lane's live lifecycle facts (pure)."""
    generation = _generation_ordinal(record.lane_generation)
    lane = record.lane.strip()
    if not lane or generation is None:
        return ANCHOR_DECISION_INCOMPLETE
    if expected is None:
        return ANCHOR_LANE_UNRESOLVED
    if not isinstance(expected, LaneExpectation):
        return ANCHOR_SCOPE_MISMATCH
    if lane != expected.lane.strip():
        return ANCHOR_SCOPE_MISMATCH
    if generation != expected.generation:
        return ANCHOR_GENERATION_STALE
    return ANCHOR_VERIFIED


def _issue_scoped_status(record: "DecisionRecord", expected) -> str:
    """Classify an issue-scoped (bootstrap) decision against the issue's live facts (pure).

    A bootstrap decision must not name a lane: it authorizes creating the first one. And the issue
    must not already own an active lane — that precondition is what the bootstrap acts on.
    """
    if record.lane.strip() or record.lane_generation.strip():
        return ANCHOR_SCOPE_MISMATCH
    if expected is None:
        return ANCHOR_LANE_UNRESOLVED
    if not isinstance(expected, IssueExpectation):
        return ANCHOR_SCOPE_MISMATCH
    if expected.owns_active_lane:
        return ANCHOR_SCOPE_MISMATCH
    return ANCHOR_VERIFIED


def anchor_status_for(
    *,
    action: str,
    decision: "Optional[DecisionRecord]",
    decision_refusal: str = "",
    expected,
) -> str:
    """Classify the NAMED journal's canonical decision against live facts (pure, fail-closed).

    The reader hands in the single canonical decision it found in the journal the invocation named,
    or the fixed refusal reason for why that journal carries none (Design Answer j#90329 contract 5).
    Scanning the issue's history is gone: it is what let a quotation elsewhere become authority, and
    then what let the anti-quotation rule make an issue permanently unusable. A named journal is a
    **durable work intent**, not a proof of who wrote it — the proxy no longer claims the latter.

    - a refusal from the canonical read -> its mapped status
      (:data:`DECISION_REFUSAL_STATUS`);
    - **lane-scoped**: the decision names a lane and numeric generation
      (:data:`ANCHOR_DECISION_INCOMPLETE` otherwise) which must match that lane's live lifecycle
      facts (:data:`ANCHOR_LANE_UNRESOLVED` / :data:`ANCHOR_SCOPE_MISMATCH` /
      :data:`ANCHOR_GENERATION_STALE`);
    - **issue-scoped**: the decision must NOT name a lane, and the issue must not already own an
      active lane;
    - otherwise -> :data:`ANCHOR_VERIFIED`.
    """
    if decision is None:
        return DECISION_REFUSAL_STATUS.get(decision_refusal, ANCHOR_UNVERIFIED)
    normalized = normalize_action(action)
    if not normalized:
        return ANCHOR_ACTION_MISMATCH
    if ACTION_SCOPES.get(normalized) == SCOPE_ISSUE:
        return _issue_scoped_status(decision, expected)
    return _lane_scoped_status(decision, expected)


__all__ = (
    "ACTION_DISPATCH_NEXT",
    "PROXY_ACTIONS",
    "ACTION_BOOTSTRAP_LANE",
    "ACTION_DECISION_TOKENS",
    "ACTION_SCOPES",
    "SCOPE_LANE",
    "SCOPE_ISSUE",
    "WORKSPACE_RESOLVED",
    "WORKSPACE_UNRESOLVED",
    "AUTHORITY_RESOLVED",
    "AUTHORITY_MISSING",
    "AUTHORITY_BLOCKED",
    "PROVIDER_RESOLVED",
    "PROVIDER_UNRESOLVED",
    "TARGET_OK",
    "TARGET_MISSING",
    "TARGET_AMBIGUOUS",
    "TARGET_LOCATOR_MISSING",
    "TARGET_UNATTESTED",
    "ANCHOR_VERIFIED",
    "ANCHOR_UNVERIFIED",
    "ANCHOR_SUPERSEDED",
    "ANCHOR_ACTION_MISMATCH",
    "ANCHOR_DECISION_INCOMPLETE",
    "ANCHOR_GENERATION_STALE",
    "ANCHOR_LANE_UNRESOLVED",
    "ANCHOR_SCOPE_MISMATCH",
    "ANCHOR_DECISION_AMBIGUOUS",
    "ANCHOR_DECISION_UNREADABLE",
    "FENCE_OPEN",
    "FENCE_DUPLICATE",
    "FENCE_STALE",
    "FENCE_RECONCILE",
    "FENCE_UNAVAILABLE",
    "REASON_ACTION_UNKNOWN",
    "REASON_WORKSPACE_UNRESOLVED",
    "REASON_AUTHORITY_MISSING",
    "REASON_AUTHORITY_BLOCKED",
    "REASON_PROVIDER_UNRESOLVED",
    "REASON_TARGET_MISSING",
    "REASON_TARGET_AMBIGUOUS",
    "REASON_TARGET_LOCATOR_MISSING",
    "REASON_TARGET_UNATTESTED",
    "REASON_ANCHOR_UNVERIFIED",
    "REASON_ANCHOR_SUPERSEDED",
    "REASON_ANCHOR_ACTION_MISMATCH",
    "REASON_ANCHOR_DECISION_INCOMPLETE",
    "REASON_ANCHOR_GENERATION_STALE",
    "REASON_ANCHOR_LANE_UNRESOLVED",
    "REASON_ANCHOR_SCOPE_MISMATCH",
    "REASON_ANCHOR_DECISION_AMBIGUOUS",
    "REASON_ANCHOR_DECISION_UNREADABLE",
    "REASON_DELIVERY_UNCERTAIN",
    "REASON_DUPLICATE",
    "REASON_STALE",
    "REASON_FENCE_RECONCILE",
    "REASON_FENCE_UNAVAILABLE",
    "DELIVER",
    "ZERO_SEND",
    "normalize_action",
    "DecisionRecord",
    "DECISION_REFUSAL_STATUS",
    "IssueExpectation",
    "LaneExpectation",
    "ProxyLinks",
    "ProxyDecision",
    "decide_proxy_delegation",
    "target_status_from_cardinality",
    "anchor_status_for",
)
