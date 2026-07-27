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
#: Delegate the coordinator's already-resolved "dispatch the next managed sublane" decision.
ACTION_DISPATCH_NEXT = "dispatch_next"

PROXY_ACTIONS: tuple[str, ...] = (ACTION_DISPATCH_NEXT,)

#: The CLOSED action -> accepted decision-token map. A journal authorizes an action only when it
#: carries a workflow-event marker naming one of that action's tokens. ``implementation_request``
#: is the coordinator's dispatch decision; it is deliberately NOT in the callback-required
#: ``GATE_BEARING_KINDS`` vocabulary (a dispatch wakes nobody), which is why the adapter reads the
#: generic workflow-event token rather than the callback-gate reader.
ACTION_DECISION_TOKENS: "dict[str, tuple[str, ...]]" = {
    ACTION_DISPATCH_NEXT: ("implementation_request",),
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

FENCE_OPEN = "open"  # the fence reserved this delegation (the single caller cleared to deliver)
FENCE_DUPLICATE = "duplicate"  # in flight, or this exact decision was already delegated
FENCE_STALE = "stale"  # a newer decision was already delegated on this route
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


def anchor_status_for(
    journal: str,
    *,
    action: str,
    decision_journals: "dict[str, tuple[str, ...]]",
) -> str:
    """Classify a requested journal as this action's current durable decision (pure, fail-closed).

    ``decision_journals`` maps a decision token (the workflow-event marker's ``gate`` / ``kind``)
    to the issue's journal ids carrying it, in note order, as read from source-of-truth Redmine
    (prose is never a source). The classification is the **pair** (action, journal), because
    verifying them separately verifies neither:

    - the journal carries none of this issue's workflow-event markers -> :data:`ANCHOR_UNVERIFIED`.
      This is also what an unreachable Redmine produces (no markers at all), so a failed live read
      can never look like a verified anchor;
    - the journal carries a marker, but not one in this action's
      :data:`ACTION_DECISION_TOKENS` -> :data:`ANCHOR_ACTION_MISMATCH`. The anchor is real; it just
      does not authorize *this* action;
    - the journal carries an accepted token but a LATER journal carries the same token ->
      :data:`ANCHOR_SUPERSEDED`. Supersession is judged **within the action's own decision series**:
      an unrelated later gate must not make the correct authorization look stale, and an unrelated
      later gate must not stand in for it either;
    - otherwise -> :data:`ANCHOR_VERIFIED`.
    """
    want = (journal or "").strip()
    token_map = {str(k): tuple(v or ()) for k, v in (decision_journals or {}).items()}
    if not want or not any(want in journals for journals in token_map.values()):
        return ANCHOR_UNVERIFIED

    accepted = ACTION_DECISION_TOKENS.get(normalize_action(action), ())
    accepted_journals = [j for token in accepted for j in token_map.get(token, ())]
    if want not in accepted_journals:
        return ANCHOR_ACTION_MISMATCH
    if accepted_journals[-1] != want:
        return ANCHOR_SUPERSEDED
    return ANCHOR_VERIFIED


__all__ = (
    "ACTION_DISPATCH_NEXT",
    "PROXY_ACTIONS",
    "ACTION_DECISION_TOKENS",
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
    "REASON_DELIVERY_UNCERTAIN",
    "REASON_DUPLICATE",
    "REASON_STALE",
    "REASON_FENCE_RECONCILE",
    "REASON_FENCE_UNAVAILABLE",
    "DELIVER",
    "ZERO_SEND",
    "normalize_action",
    "ProxyLinks",
    "ProxyDecision",
    "decide_proxy_delegation",
    "target_status_from_cardinality",
    "anchor_status_for",
)
