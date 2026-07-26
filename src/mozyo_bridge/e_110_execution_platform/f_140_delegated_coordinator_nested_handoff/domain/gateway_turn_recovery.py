"""Gateway provider-turn failure classification + guarded refresh decision (Redmine #14203).

A managed sublane's same-lane implementation_gateway can end a provider turn immediately —
seconds after a confirmed callback delivery — leaving NO expected durable gate, while the live
runtime keeps reporting a settled ``turn_ended`` (dogfood #14203: five lanes, all ``sent`` /
``started``, zero durable response). No public surface could (a) name that state without
conflating it with workflow truth, or (b) refresh exactly that gateway process without raw
backend operations. This module is the pure half of both:

* **Part A — turn classification**: a closed vocabulary over an all-positive-fact observation
  of ONE delivered callback's provider turn. The durable journal is the authority (a landed
  gate is ``turn_productive`` no matter what the runtime looked like); an unconfirmed delivery
  or turn start is NEVER promoted to a failure (live evidence: two consecutive
  ``delivered_not_started`` reports in #14219 both turned out to be successful landings); an
  unreadable observation fails closed to ``turn_unobservable``.
* **Part B — refresh decision**: the ordered fail-closed preflight a guarded gateway refresh
  makes over a positive-fact observation of the exact pinned gateway slot — the mirror of
  :mod:`.stale_worker_recovery` (which protects the gateway and recovers only workers; this
  module protects the worker / default coordinator / foreign slot and refreshes only the
  exact same-lane implementation_gateway).

Terminology is the #14203 j#86040 closed vocabulary: the durable gate (e.g. ``review_request``)
is carried verbatim; the transport facts are ``callback delivery`` / ``callback outcome``; the
resume is ``callback recovery``. No cause is named ``publication`` / ``wake`` / ``outbox`` —
the classification describes observations, never an unproven failing subsystem.

Every observation field is a **positive** fact defaulting to the unsafe side, so a missing /
unreadable observation blocks rather than actuates. This module never opens a store, reads a
live inventory, or mutates a process — callers pin every branch with process-free tests.
"""

from __future__ import annotations

from mozyo_bridge.core.state.replacement_transaction_model import norm

# -- Part A: provider-turn classification (a closed set) ------------------------

#: The expected durable gate LANDED after the resume anchor — the turn produced workflow
#: truth. No recovery is needed; runtime appearances (however brief the turn looked) are
#: irrelevant, because the durable journal is the authority.
TURN_CLASS_PRODUCTIVE = "turn_productive"

#: The failure #14203 names: the callback delivery was confirmed, the provider turn START was
#: confirmed, the runtime has settled back to ``turn_ended`` — and a FRESH durable re-read
#: confirms NO expected gate landed after the anchor. Only this class justifies a refresh.
TURN_CLASS_FAILED = "turn_failed_no_durable_gate"

#: The callback delivery or the turn start could NOT be positively confirmed. This is NOT a
#: provider-turn failure: a bounded turn-start wait routinely times out on a landing that
#: succeeds moments later (#14219 dogfood: two ``delivered_not_started`` reports, both real
#: landings). Fail-closed: re-observe / re-read the durable record; never refresh on this.
TURN_CLASS_UNCONFIRMED = "turn_unconfirmed"

#: Delivery + turn start confirmed and no gate landed YET, but the runtime has not settled —
#: the turn may still be running. Not a failure; re-observe after it settles.
TURN_CLASS_NOT_SETTLED = "turn_not_settled"

#: The durable source could not be freshly read (or the observation contradicts itself) — the
#: classification that would justify a destructive refresh cannot be established. Fail-closed.
TURN_CLASS_UNOBSERVABLE = "turn_unobservable"

TURN_CLASSES = frozenset(
    {
        TURN_CLASS_PRODUCTIVE,
        TURN_CLASS_FAILED,
        TURN_CLASS_UNCONFIRMED,
        TURN_CLASS_NOT_SETTLED,
        TURN_CLASS_UNOBSERVABLE,
    }
)

# -- Part A: secret-safe turn-failure reason (a closed set, unknown fail-closed) ---

#: The provider refused/ended the turn on a rate limit (evidence-backed only).
TURN_REASON_RATE_LIMIT = "rate_limit"
#: The provider session lost authentication (evidence-backed only).
TURN_REASON_AUTH = "auth"
#: The provider session itself is stale/invalid (evidence-backed only).
TURN_REASON_SESSION_STALE = "session_stale"
#: No structured reason evidence — the fail-closed default. NO herdr surface exposes a
#: turn-end reason / exit code / error string (#14203 dogfood j#84337: the cause could not be
#: asserted), so this is the normal value; a reason is only ever a normalized injected
#: evidence token, never inferred from runtime state or pane text.
TURN_REASON_UNKNOWN = "unknown"

TURN_FAILURE_REASONS = frozenset(
    {
        TURN_REASON_RATE_LIMIT,
        TURN_REASON_AUTH,
        TURN_REASON_SESSION_STALE,
        TURN_REASON_UNKNOWN,
    }
)


def normalize_turn_failure_reason(token: str) -> str:
    """Normalize an injected reason-evidence token to the closed secret-safe set. (pure)

    Only an exact member of :data:`TURN_FAILURE_REASONS` passes through; anything else —
    empty, free text, a raw provider error string, an unrecognized code — collapses to
    :data:`TURN_REASON_UNKNOWN` (fail-closed; the raw token is never carried onward, so a
    secret-bearing evidence string can never leak into a durable record through this path).
    """
    value = norm(token)
    return value if value in TURN_FAILURE_REASONS else TURN_REASON_UNKNOWN


class GatewayTurnObservation:
    """The positive facts observed about ONE delivered callback's provider turn.

    Every field defaults to the unsafe side (``False``), so a missing / unreadable
    observation fails closed at :func:`classify_gateway_turn`:

    - ``delivery_confirmed`` — the callback delivery's callback outcome was positively
      ``sent`` (never inferred from an unconfirmed wait).
    - ``turn_started`` — the provider turn START was positively observed (the turn-start
      rail's ``started`` outcome / an observed working transition).
    - ``settled_turn_ended`` — the runtime has settled back to ``turn_ended`` /
      ``awaiting_input`` at a FRESH read taken after the turn start.
    - ``expected_gate_landed`` — a fresh durable re-read found a qualifying gate journal
      strictly AFTER the resume anchor (ordered durable journal-id comparison, never
      wall-clock).
    - ``expected_gate_absent`` — the fresh durable re-read COMPLETED and positively confirmed
      no qualifying gate after the anchor. Absence is a fact only a readable source can
      assert; an unreadable source leaves this ``False`` (which classifies unobservable,
      never "absent").
    - ``durable_source_fresh`` — the durable read above was a FRESH read (a source declaring
      freshness), not a frozen snapshot re-read (#13889: a snapshot re-read is a no-op guard).
    - ``reason_token`` — optional structured reason evidence (normalized by
      :func:`normalize_turn_failure_reason`; free text collapses to ``unknown``).
    """

    __slots__ = (
        "delivery_confirmed",
        "turn_started",
        "settled_turn_ended",
        "expected_gate_landed",
        "expected_gate_absent",
        "durable_source_fresh",
        "reason_token",
    )

    def __init__(
        self,
        *,
        delivery_confirmed: bool = False,
        turn_started: bool = False,
        settled_turn_ended: bool = False,
        expected_gate_landed: bool = False,
        expected_gate_absent: bool = False,
        durable_source_fresh: bool = False,
        reason_token: str = "",
    ) -> None:
        self.delivery_confirmed = bool(delivery_confirmed)
        self.turn_started = bool(turn_started)
        self.settled_turn_ended = bool(settled_turn_ended)
        self.expected_gate_landed = bool(expected_gate_landed)
        self.expected_gate_absent = bool(expected_gate_absent)
        self.durable_source_fresh = bool(durable_source_fresh)
        self.reason_token = norm(reason_token)

    def as_payload(self) -> dict[str, object]:
        return {
            "delivery_confirmed": self.delivery_confirmed,
            "turn_started": self.turn_started,
            "settled_turn_ended": self.settled_turn_ended,
            "expected_gate_landed": self.expected_gate_landed,
            "expected_gate_absent": self.expected_gate_absent,
            "durable_source_fresh": self.durable_source_fresh,
            "reason": normalize_turn_failure_reason(self.reason_token),
        }


def classify_gateway_turn(observation: GatewayTurnObservation) -> str:
    """Classify one delivered callback's provider turn. (pure, fail-closed, ordered)

    The order encodes the authority hierarchy:

    1. a self-contradictory durable observation (landed AND absent) is unobservable —
       never trust either side of a contradiction;
    2. a LANDED gate is productive — the durable journal is workflow truth and wins over
       every runtime appearance;
    3. absence must be POSITIVELY confirmed from a FRESH source — an unreadable / snapshot
       source is unobservable (a destructive decision is never keyed off an unproven
       absence);
    4. an unconfirmed delivery or turn start is ``turn_unconfirmed`` — NOT a failure (the
       #14219 dogfood false-negatives made structural);
    5. an unsettled runtime is ``turn_not_settled`` — the turn may still be running;
    6. only the fully-confirmed remainder is ``turn_failed_no_durable_gate``.
    """
    if observation.expected_gate_landed and observation.expected_gate_absent:
        return TURN_CLASS_UNOBSERVABLE
    if observation.expected_gate_landed:
        return TURN_CLASS_PRODUCTIVE
    if not (observation.expected_gate_absent and observation.durable_source_fresh):
        return TURN_CLASS_UNOBSERVABLE
    if not (observation.delivery_confirmed and observation.turn_started):
        return TURN_CLASS_UNCONFIRMED
    if not observation.settled_turn_ended:
        return TURN_CLASS_NOT_SETTLED
    return TURN_CLASS_FAILED


# -- Part B: guarded gateway refresh decision (a closed set) --------------------

#: Every gate holds: the target is the exact same-lane implementation_gateway the approval
#: names, its provider turn is classified failed, and an ``--execute`` may proceed to the
#: owner-approval + guarded actuation.
REFRESH_ACTIONABLE = "actionable"

#: The live inventory cannot uniquely resolve the pinned gateway identity — unreadable or
#: ambiguous. Never degraded to "absent" and relaunched blind.
REFRESH_BLOCK_UNKNOWN = "identity_unknown"
#: The pinned slot is NOT the lane's implementation_gateway — it is the lane worker, the
#: default coordinator / companion, or a foreign slot. The refresh closes only the exact
#: gateway (the mirror of ``recover-stale``'s ``gateway_or_foreign_protected``): everything
#: else is protected, zero actuation.
REFRESH_BLOCK_NON_GATEWAY = "non_gateway_protected"
#: The lane's durable issue owner does not match the approval's issue-lane. Zero actuation.
REFRESH_BLOCK_WRONG_ISSUE_LANE = "wrong_issue_lane"
#: The live slot's revision / generation no longer matches the approved generation — a newer
#: generation superseded this approval (or the slot was recycled). Zero actuation.
REFRESH_BLOCK_STALE_GENERATION = "stale_generation"
#: The provider turn is NOT classified ``turn_failed_no_durable_gate`` — productive,
#: unconfirmed, unsettled, or unobservable. A refresh without a classified failure is blind
#: process churn; zero actuation.
REFRESH_BLOCK_TURN_NOT_FAILED = "turn_not_classified_failed"
#: The gateway is not settled (``working`` / busy / unknown) at the fresh action-time read —
#: never close a possibly-working turn. Zero actuation.
REFRESH_BLOCK_NOT_SETTLED = "gateway_not_settled"
#: The gateway's composer holds real unsent input (a NORMAL-intensity composer, not an idle
#: ghost placeholder). Closing would destroy it; zero actuation.
REFRESH_BLOCK_PENDING_COMPOSER = "pending_composer_input"
#: No durable resume anchor exists for this lane — there is nothing for a fresh gateway to
#: resume, so the refresh would be process churn without a recovery purpose. Zero actuation.
REFRESH_BLOCK_NO_RESUME_ANCHOR = "no_resume_anchor"
#: The lane's worker slot could not be positively distinguished from the close target — a
#: refresh must byte-preserve the worker, so an indistinguishable pair blocks. Zero actuation.
REFRESH_BLOCK_WORKER_NOT_DISTINGUISHED = "worker_not_distinguished"
#: Another replacement authority (a different approved generation / in-flight transaction) is
#: already acting on this slot — never race two authorities. Zero actuation.
REFRESH_BLOCK_AUTHORITY_CONFLICT = "authority_conflict"

REFRESH_VERDICTS = frozenset(
    {
        REFRESH_ACTIONABLE,
        REFRESH_BLOCK_UNKNOWN,
        REFRESH_BLOCK_NON_GATEWAY,
        REFRESH_BLOCK_WRONG_ISSUE_LANE,
        REFRESH_BLOCK_STALE_GENERATION,
        REFRESH_BLOCK_TURN_NOT_FAILED,
        REFRESH_BLOCK_NOT_SETTLED,
        REFRESH_BLOCK_PENDING_COMPOSER,
        REFRESH_BLOCK_NO_RESUME_ANCHOR,
        REFRESH_BLOCK_WORKER_NOT_DISTINGUISHED,
        REFRESH_BLOCK_AUTHORITY_CONFLICT,
    }
)

#: The verdicts that forbid any actuation (everything but :data:`REFRESH_ACTIONABLE`).
REFRESH_BLOCKERS = frozenset(REFRESH_VERDICTS - {REFRESH_ACTIONABLE})


class GatewayRefreshObservation:
    """The action-time facts a preflight observes about the pinned gateway slot.

    Every field is a **positive** fact defaulting to the unsafe side (``False``):

    - ``identity_resolved`` — the live inventory resolves EXACTLY one slot at the pinned
      ``(workspace, lane, issue, provider, assigned_name, locator)``.
    - ``is_lane_implementation_gateway`` — that slot is the lane's same-lane
      implementation_gateway (not the worker, not the default coordinator / companion, not
      foreign).
    - ``issue_lane_matches`` — the lane's durable issue owner matches the approval's issue.
    - ``generation_matches`` — the live slot's revision / generation matches the approved one.
    - ``settled_idle`` — the fresh action-time runtime state is settled
      (``turn_ended`` / ``awaiting_input``), never working / unknown.
    - ``composer_clear`` — the composer holds NO real unsent input (empty or an idle ghost
      placeholder; an unreadable composer observation leaves this ``False``).
    - ``resume_anchor_present`` — a durable resume anchor (the existing gate journal to
      re-deliver) exists for this lane.
    - ``worker_distinct_preserved`` — the lane's worker slot is positively identified as a
      DIFFERENT slot than the close target (so the close cannot touch it).
    - ``no_authority_conflict`` — no other approved generation / in-flight replacement
      transaction is already acting on this slot.
    """

    __slots__ = (
        "identity_resolved",
        "is_lane_implementation_gateway",
        "issue_lane_matches",
        "generation_matches",
        "settled_idle",
        "composer_clear",
        "resume_anchor_present",
        "worker_distinct_preserved",
        "no_authority_conflict",
    )

    def __init__(
        self,
        *,
        identity_resolved: bool = False,
        is_lane_implementation_gateway: bool = False,
        issue_lane_matches: bool = False,
        generation_matches: bool = False,
        settled_idle: bool = False,
        composer_clear: bool = False,
        resume_anchor_present: bool = False,
        worker_distinct_preserved: bool = False,
        no_authority_conflict: bool = False,
    ) -> None:
        self.identity_resolved = bool(identity_resolved)
        self.is_lane_implementation_gateway = bool(is_lane_implementation_gateway)
        self.issue_lane_matches = bool(issue_lane_matches)
        self.generation_matches = bool(generation_matches)
        self.settled_idle = bool(settled_idle)
        self.composer_clear = bool(composer_clear)
        self.resume_anchor_present = bool(resume_anchor_present)
        self.worker_distinct_preserved = bool(worker_distinct_preserved)
        self.no_authority_conflict = bool(no_authority_conflict)

    def as_payload(self) -> dict[str, bool]:
        return {
            "identity_resolved": self.identity_resolved,
            "is_lane_implementation_gateway": self.is_lane_implementation_gateway,
            "issue_lane_matches": self.issue_lane_matches,
            "generation_matches": self.generation_matches,
            "settled_idle": self.settled_idle,
            "composer_clear": self.composer_clear,
            "resume_anchor_present": self.resume_anchor_present,
            "worker_distinct_preserved": self.worker_distinct_preserved,
            "no_authority_conflict": self.no_authority_conflict,
        }


def decide_gateway_refresh(
    observation: GatewayRefreshObservation, turn_class: str
) -> str:
    """Classify the refresh target. (pure, fail-closed, ordered)

    Returns :data:`REFRESH_ACTIONABLE` only when EVERY gate holds AND the provider turn is
    classified :data:`TURN_CLASS_FAILED`; otherwise the first failing gate's closed blocker
    (most-fundamental first) so the durable record names exactly which fence stopped it.

    The order is deliberate (the :func:`.stale_worker_recovery.decide_recovery` discipline):

    1. identity must resolve at all;
    2. the slot must be the lane implementation_gateway (protect the worker / coordinator /
       foreign slot before inspecting anything else);
    3. the issue-lane owner must match;
    4. the generation must match;
    5. the provider turn must be CLASSIFIED failed (a productive / unconfirmed / unsettled /
       unobservable turn never justifies a close — checked before the runtime gates so a
       blind refresh is named for what it is, not for an incidental runtime state);
    6. the gateway must be settled at the fresh action-time read;
    7. the composer must hold no real unsent input;
    8. a durable resume anchor must exist (a refresh exists to resume work, not to churn);
    9. the worker must be positively distinguished from the close target;
    10. no competing authority may already be acting on the slot.
    """
    if not observation.identity_resolved:
        return REFRESH_BLOCK_UNKNOWN
    if not observation.is_lane_implementation_gateway:
        return REFRESH_BLOCK_NON_GATEWAY
    if not observation.issue_lane_matches:
        return REFRESH_BLOCK_WRONG_ISSUE_LANE
    if not observation.generation_matches:
        return REFRESH_BLOCK_STALE_GENERATION
    if norm(turn_class) != TURN_CLASS_FAILED:
        return REFRESH_BLOCK_TURN_NOT_FAILED
    if not observation.settled_idle:
        return REFRESH_BLOCK_NOT_SETTLED
    if not observation.composer_clear:
        return REFRESH_BLOCK_PENDING_COMPOSER
    if not observation.resume_anchor_present:
        return REFRESH_BLOCK_NO_RESUME_ANCHOR
    if not observation.worker_distinct_preserved:
        return REFRESH_BLOCK_WORKER_NOT_DISTINGUISHED
    if not observation.no_authority_conflict:
        return REFRESH_BLOCK_AUTHORITY_CONFLICT
    return REFRESH_ACTIONABLE


def is_refresh_actionable(verdict: str) -> bool:
    """Does this verdict permit the guarded actuation? (pure)"""
    return norm(verdict) == REFRESH_ACTIONABLE


def gateway_refresh_action_id(
    *, lane_id: str, role: str, provider: str, assigned_name: str, locator: str,
    revision: str,
) -> str:
    """The deterministic action id that names ONE exact gateway generation. (pure)

    The transaction key's ``action_id`` for a gateway refresh:
    ``refresh-gateway:<lane>:<role>:<provider>:<assigned_name>:<locator>:r<revision>`` pinned
    to the exact live inventory row generation the approval names (review j#87364 F5: the row
    ``revision`` is a REQUIRED authority component — a same-name/-locator slot recycled at a new
    process generation derives a DIFFERENT key, so an old approval can never close it). The distinct ``refresh-gateway:`` prefix keeps a
    gateway refresh and a worker recovery of the same slot-shape from ever sharing a
    transaction key. Every component must be present — an under-specified target could never
    identify one exact receiver, so it raises (the ``stale_worker_recovery_action_id``
    precedent).
    """
    parts = {
        "lane_id": norm(lane_id),
        "role": norm(role),
        "provider": norm(provider),
        "assigned_name": norm(assigned_name),
        "locator": norm(locator),
        "revision": norm(revision),
    }
    missing = [name for name, value in parts.items() if not value]
    if missing:
        raise ValueError(
            "a gateway refresh action id requires a non-empty lane_id / role / provider / "
            f"assigned_name / locator / revision (missing: {', '.join(missing)})"
        )
    return "refresh-gateway:" + ":".join(
        parts[name] for name in ("lane_id", "role", "provider", "assigned_name", "locator")
    ) + ":r" + parts["revision"]


# -- Part C: the resume continuation (item 4 — reuse, never regenerate) ---------

#: The ONLY continuation semantic action a gateway refresh may name: resume the EXISTING
#: durable anchor via the callback recovery surface (``sublane callback-recovery``), which
#: already enforces at-most-one notification per dispatch anchor, historical / superseded /
#: landed-gate zero-sends, and never regenerates an Implementation Request / Review Request.
RESUME_VIA_CALLBACK_RECOVERY = "callback_recovery_once"

#: The closed set of durable gate kinds a resume anchor may carry (the governed handoff kind
#: vocabulary). A continuation naming any other token is a zero-actuation typed blocker.
RESUMABLE_GATES = frozenset(
    {
        "custom",
        "design_consultation",
        "implementation_done",
        "implementation_request",
        "reply",
        "review_request",
        "review_result",
    }
)


__all__ = (
    "TURN_CLASS_PRODUCTIVE",
    "TURN_CLASS_FAILED",
    "TURN_CLASS_UNCONFIRMED",
    "TURN_CLASS_NOT_SETTLED",
    "TURN_CLASS_UNOBSERVABLE",
    "TURN_CLASSES",
    "TURN_REASON_RATE_LIMIT",
    "TURN_REASON_AUTH",
    "TURN_REASON_SESSION_STALE",
    "TURN_REASON_UNKNOWN",
    "TURN_FAILURE_REASONS",
    "normalize_turn_failure_reason",
    "GatewayTurnObservation",
    "classify_gateway_turn",
    "REFRESH_ACTIONABLE",
    "REFRESH_BLOCK_UNKNOWN",
    "REFRESH_BLOCK_NON_GATEWAY",
    "REFRESH_BLOCK_WRONG_ISSUE_LANE",
    "REFRESH_BLOCK_STALE_GENERATION",
    "REFRESH_BLOCK_TURN_NOT_FAILED",
    "REFRESH_BLOCK_NOT_SETTLED",
    "REFRESH_BLOCK_PENDING_COMPOSER",
    "REFRESH_BLOCK_NO_RESUME_ANCHOR",
    "REFRESH_BLOCK_WORKER_NOT_DISTINGUISHED",
    "REFRESH_BLOCK_AUTHORITY_CONFLICT",
    "REFRESH_VERDICTS",
    "REFRESH_BLOCKERS",
    "GatewayRefreshObservation",
    "decide_gateway_refresh",
    "is_refresh_actionable",
    "gateway_refresh_action_id",
    "RESUME_VIA_CALLBACK_RECOVERY",
    "RESUMABLE_GATES",
)
