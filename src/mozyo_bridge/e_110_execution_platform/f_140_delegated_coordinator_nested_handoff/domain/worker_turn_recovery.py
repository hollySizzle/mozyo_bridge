"""Live turn-ended sublane WORKER classification + guarded refresh decision (Redmine #14661).

A managed sublane's standard implementation **worker** can settle back to a live ``turn_ended``
after a confirmed resume delivery and produce NO durable progress — while holding in-scope
dirty worktree files it must not lose (live evidence: the #14658 lane, j#92366). No public
surface could name or recover that state:

* ``sublane recover-stale`` refuses it as ``not_stale`` — its ``is_stale`` gate demands a
  positive shell-residue signal, and this worker's process is genuinely LIVE. That fence is
  correct and is NOT loosened here: a vanished worker and a live-but-unproductive worker are
  different facts and get different admissions (#14661 j#92369 design constraint).
* ``sublane recover-gateway`` is the exact mirror of this need but protects the WORKER by
  design (:data:`...gateway_turn_recovery.REFRESH_BLOCK_NON_GATEWAY`) — it can never close one.
* ``sublane callback-recovery`` reports ``no_progress_after_handoff`` and ``sublane start
  --execute`` re-adopts the pair, but neither closes the exact stuck participant, and a
  repeated re-dispatch after the same failure is not exactly-once.

This module is the pure half of the missing surface, and it deliberately **shares** the two
existing recovery dialects rather than forking a third:

* **Part A — turn classification** reuses the #14203 closed ``TURN_CLASS_*`` vocabulary and its
  ordered classifier verbatim (:func:`...gateway_turn_recovery.classify_gateway_turn`; the
  axes it reads describe ONE delivered callback's provider turn and carry no gateway
  semantics). #14661 adds only what its acceptance requires: the classification is admissible
  only while it is **bound** to the exact durable anchor, the pinned lane generation, and the
  pinned participant revision. An unbound observation is ``turn_unobservable`` — never
  promoted, never guessed.
* **Part B — refresh decision** mirrors :func:`...gateway_turn_recovery.decide_gateway_refresh`
  with the protected set inverted (the lane gateway, the default coordinator, and every
  foreign slot are protected; only the exact standard sublane worker is closable) and the
  #13806 ``dirty_state_unreadable`` worktree fence re-used verbatim, because a worker refresh's
  whole point is to preserve a dirty worktree byte-for-byte across the close.

Every blocker token is either an existing token imported from the surface that first defined it
or one of the two genuinely new mirrors (``worker_not_settled`` / ``gateway_not_distinguished``).
Every observation field is a **positive** fact defaulting to the unsafe side, so a missing /
unreadable observation blocks rather than actuates. This module never opens a store, reads a
live inventory, or mutates a process — callers pin every branch with process-free tests.
"""

from __future__ import annotations

from mozyo_bridge.core.state.replacement_transaction_model import norm
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E501
    GatewayTurnObservation,
    REFRESH_ACTIONABLE,
    REFRESH_BLOCK_AUTHORITY_CONFLICT,
    REFRESH_BLOCK_LAUNCH_AUTHORITY,
    REFRESH_BLOCK_NO_RESUME_ANCHOR,
    REFRESH_BLOCK_PENDING_COMPOSER,
    REFRESH_BLOCK_STALE_GENERATION,
    REFRESH_BLOCK_TURN_NOT_FAILED,
    REFRESH_BLOCK_UNKNOWN,
    REFRESH_BLOCK_WRONG_ISSUE_LANE,
    TURN_CLASS_FAILED,
    TURN_CLASS_UNOBSERVABLE,
    classify_gateway_turn,
    normalize_turn_failure_reason,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    GATE_BEARING_KINDS,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.stale_worker_recovery import (  # noqa: E501
    RECOVER_BLOCK_DIRTY_UNREADABLE,
    RECOVER_BLOCK_GATEWAY_OR_FOREIGN,
)

# -- Part A: what counts as a WORKER's durable progress -------------------------

#: The gate-bearing kinds a coordinator / auditor authors and a worker never does. A
#: ``review_result`` landing after the anchor is the *reviewer's* output — the very thing that
#: gets delivered TO a worker — so counting it as the worker's own progress would let an
#: incoming review suppress the recovery of the worker that never answered it.
_NON_WORKER_GATES = frozenset({"review_result"})

#: The closed causal-response vocabulary for a WORKER anchor: the durable gate kinds whose
#: landing (strictly after the anchor journal, in the anchor issue) proves the delivered turn
#: produced workflow truth. Unlike #14203's gateway contract — where a ``review_request``
#: anchor's response carries an explicit ``req=<anchor>`` back-pointer — no worker gate marker
#: carries a causal back-pointer to the request it answers
#: (:func:`...redmine_journal_source.render_workflow_event_marker` emits ``req`` on
#: ``review_result`` only). So the causal link here is *ordering + lane binding*, and the
#: asymmetry is resolved in the SAFE direction: a progress gate of unknown lane provenance
#: still counts as landed (classifying ``turn_productive``, which REFUSES the refresh). Never
#: the reverse — this set may only ever grow, so a new gate-bearing kind is automatically
#: "progress" and can only ever reduce the number of admitted refreshes.
WORKER_PROGRESS_GATES = frozenset(GATE_BEARING_KINDS - _NON_WORKER_GATES)


class WorkerTurnObservation:
    """The positive facts observed about ONE delivered anchor's WORKER provider turn.

    The first seven axes are the #14203 shared axes, carried with identical meaning (see
    :class:`...gateway_turn_recovery.GatewayTurnObservation`); ``expected_gate_landed`` /
    ``expected_gate_absent`` are resolved against :data:`WORKER_PROGRESS_GATES`.

    The last three are the #14661 **binding** axes its acceptance names. Each is a positive
    fact that the observation an actuation would key off is about the exact thing the approval
    pinned:

    - ``anchor_bound`` — the delivery / turn facts above were resolved from the record for the
      EXACT durable Redmine anchor (issue + journal + gate kind + worker receiver + the pinned
      locator), never a global timeline and never a neighbouring handoff.
    - ``lane_generation_bound`` — the durable progress re-read was evaluated against the pinned
      lane generation, so a previous generation's landed gate cannot read as this one's
      progress (and vice versa).
    - ``participant_revision_bound`` — the live row whose settled runtime state was read is the
      pinned participant revision, so a same-name slot recycled at a new process generation is
      never classified in the old generation's name.

    Every field defaults to the unsafe side (``False``), so a missing / unreadable observation
    fails closed at :func:`classify_worker_turn`.
    """

    __slots__ = (
        "delivery_confirmed",
        "turn_started",
        "settled_turn_ended",
        "expected_gate_landed",
        "expected_gate_absent",
        "durable_source_fresh",
        "reason_token",
        "anchor_bound",
        "lane_generation_bound",
        "participant_revision_bound",
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
        anchor_bound: bool = False,
        lane_generation_bound: bool = False,
        participant_revision_bound: bool = False,
    ) -> None:
        self.delivery_confirmed = bool(delivery_confirmed)
        self.turn_started = bool(turn_started)
        self.settled_turn_ended = bool(settled_turn_ended)
        self.expected_gate_landed = bool(expected_gate_landed)
        self.expected_gate_absent = bool(expected_gate_absent)
        self.durable_source_fresh = bool(durable_source_fresh)
        self.reason_token = norm(reason_token)
        self.anchor_bound = bool(anchor_bound)
        self.lane_generation_bound = bool(lane_generation_bound)
        self.participant_revision_bound = bool(participant_revision_bound)

    @property
    def identity_bound(self) -> bool:
        """Are ALL THREE #14661 identity bindings positively established? (pure)"""
        return (
            self.anchor_bound
            and self.lane_generation_bound
            and self.participant_revision_bound
        )

    def shared_axes(self) -> GatewayTurnObservation:
        """This observation's #14203 shared axes, for the shared classifier. (pure)

        The binding axes are deliberately NOT folded in here: they gate whether the shared
        classifier may be consulted at all (:func:`classify_worker_turn`), rather than being
        mixed into its ordered reasoning — which would change the meaning of the shared
        vocabulary for one caller.
        """
        return GatewayTurnObservation(
            delivery_confirmed=self.delivery_confirmed,
            turn_started=self.turn_started,
            settled_turn_ended=self.settled_turn_ended,
            expected_gate_landed=self.expected_gate_landed,
            expected_gate_absent=self.expected_gate_absent,
            durable_source_fresh=self.durable_source_fresh,
            reason_token=self.reason_token,
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "delivery_confirmed": self.delivery_confirmed,
            "turn_started": self.turn_started,
            "settled_turn_ended": self.settled_turn_ended,
            "expected_gate_landed": self.expected_gate_landed,
            "expected_gate_absent": self.expected_gate_absent,
            "durable_source_fresh": self.durable_source_fresh,
            "anchor_bound": self.anchor_bound,
            "lane_generation_bound": self.lane_generation_bound,
            "participant_revision_bound": self.participant_revision_bound,
            "reason": normalize_turn_failure_reason(self.reason_token),
        }


def classify_worker_turn(observation: WorkerTurnObservation) -> str:
    """Classify one delivered anchor's WORKER provider turn. (pure, fail-closed, ordered)

    Returns a member of the SHARED :data:`...gateway_turn_recovery.TURN_CLASSES` vocabulary —
    #14661 adds no class token, so every downstream consumer of a turn classification reads one
    dialect.

    The identity bindings are checked FIRST and collapse to
    :data:`...gateway_turn_recovery.TURN_CLASS_UNOBSERVABLE`: an observation that is not
    provably about the pinned anchor / lane generation / participant revision cannot establish
    the classification a destructive refresh would key off, which is exactly what
    ``turn_unobservable`` already means. Only a fully bound observation reaches the shared
    ordered classifier, which then applies the #14203 authority hierarchy unchanged (a landed
    durable gate wins over every runtime appearance; an unconfirmed delivery / turn start is
    never a failure; an unsettled runtime is never a failure).
    """
    if not observation.identity_bound:
        return TURN_CLASS_UNOBSERVABLE
    return classify_gateway_turn(observation.shared_axes())


# -- Part B: guarded worker refresh decision (a closed set) --------------------

#: Every gate holds: the target is the exact standard sublane worker the approval names, its
#: provider turn is classified failed, and an ``--execute`` may proceed to the owner-approval +
#: guarded actuation.
WORKER_REFRESH_ACTIONABLE = REFRESH_ACTIONABLE

#: The live inventory cannot uniquely resolve the pinned worker identity — unreadable or
#: ambiguous. Never degraded to "absent" and relaunched blind.
WORKER_REFRESH_BLOCK_UNKNOWN = REFRESH_BLOCK_UNKNOWN
#: The pinned slot is NOT a standard sublane worker — it is the lane gateway, the default
#: coordinator / companion, or a foreign slot. Shares the ``recover-stale`` token verbatim
#: because it is the identical protected set and the identical refusal.
WORKER_REFRESH_BLOCK_GATEWAY_OR_FOREIGN = RECOVER_BLOCK_GATEWAY_OR_FOREIGN
#: The lane's durable issue owner does not match the approval's issue-lane. Zero actuation.
WORKER_REFRESH_BLOCK_WRONG_ISSUE_LANE = REFRESH_BLOCK_WRONG_ISSUE_LANE
#: The live slot's revision / generation no longer matches the approved generation — a newer
#: generation superseded this approval (or the slot was recycled). Zero actuation.
WORKER_REFRESH_BLOCK_STALE_GENERATION = REFRESH_BLOCK_STALE_GENERATION
#: The provider turn is NOT classified ``turn_failed_no_durable_gate`` — productive,
#: unconfirmed, unsettled, or unobservable (which includes an unbound observation). This is the
#: gate that makes "durable progress already landed", "delivery uncertain", and "still working"
#: all zero-close / zero-send.
WORKER_REFRESH_BLOCK_TURN_NOT_FAILED = REFRESH_BLOCK_TURN_NOT_FAILED
#: The lane's ambient LAUNCH authority is not exact + current: the lifecycle
#: ``(revision, generation)`` moved, the canonical ``worktree_identity`` token is unbound or
#: mismatched, the worktree is unreadable, or the branch drifted (Redmine #14475). The refresh's
#: own launch leg re-joins exactly this authority AFTER the destructive close, so a refresh
#: admitted without it closes a worker it can never relaunch. Checked BEFORE any close.
WORKER_REFRESH_BLOCK_LAUNCH_AUTHORITY = REFRESH_BLOCK_LAUNCH_AUTHORITY
#: The worker is not settled (``working`` / busy / unknown) at the fresh action-time read —
#: never close a possibly-working turn. The mirror of ``gateway_not_settled``.
WORKER_REFRESH_BLOCK_NOT_SETTLED = "worker_not_settled"
#: The worker's composer holds real unsent input (a NORMAL-intensity composer, not an idle
#: ghost placeholder). Closing would destroy it; zero actuation.
WORKER_REFRESH_BLOCK_PENDING_COMPOSER = REFRESH_BLOCK_PENDING_COMPOSER
#: No durable resume anchor exists for this lane — there is nothing for a fresh worker to
#: resume, so the refresh would be process churn without a recovery purpose. Zero actuation.
WORKER_REFRESH_BLOCK_NO_RESUME_ANCHOR = REFRESH_BLOCK_NO_RESUME_ANCHOR
#: The worker's worktree state cannot be read. Byte preservation of the in-scope dirty files is
#: the entire reason this surface exists, and it needs a readable worktree — a *dirty* worktree
#: is fine and is exactly what gets preserved; an UNREADABLE one is not. Shares the #13806
#: token verbatim (same fact, same refusal). Zero actuation.
WORKER_REFRESH_BLOCK_DIRTY_UNREADABLE = RECOVER_BLOCK_DIRTY_UNREADABLE
#: The lane's GATEWAY slot could not be positively distinguished from the close target — a
#: worker refresh must leave the same-lane gateway running, so an indistinguishable pair
#: blocks. The mirror of ``worker_not_distinguished``. Zero actuation.
WORKER_REFRESH_BLOCK_GATEWAY_NOT_DISTINGUISHED = "gateway_not_distinguished"
#: Another replacement authority (a different approved generation / in-flight transaction) is
#: already acting on this slot — never race two authorities. Zero actuation.
WORKER_REFRESH_BLOCK_AUTHORITY_CONFLICT = REFRESH_BLOCK_AUTHORITY_CONFLICT

WORKER_REFRESH_VERDICTS = frozenset(
    {
        WORKER_REFRESH_ACTIONABLE,
        WORKER_REFRESH_BLOCK_UNKNOWN,
        WORKER_REFRESH_BLOCK_GATEWAY_OR_FOREIGN,
        WORKER_REFRESH_BLOCK_WRONG_ISSUE_LANE,
        WORKER_REFRESH_BLOCK_STALE_GENERATION,
        WORKER_REFRESH_BLOCK_TURN_NOT_FAILED,
        WORKER_REFRESH_BLOCK_LAUNCH_AUTHORITY,
        WORKER_REFRESH_BLOCK_NOT_SETTLED,
        WORKER_REFRESH_BLOCK_PENDING_COMPOSER,
        WORKER_REFRESH_BLOCK_NO_RESUME_ANCHOR,
        WORKER_REFRESH_BLOCK_DIRTY_UNREADABLE,
        WORKER_REFRESH_BLOCK_GATEWAY_NOT_DISTINGUISHED,
        WORKER_REFRESH_BLOCK_AUTHORITY_CONFLICT,
    }
)

#: The verdicts that forbid any actuation (everything but :data:`WORKER_REFRESH_ACTIONABLE`).
WORKER_REFRESH_BLOCKERS = frozenset(WORKER_REFRESH_VERDICTS - {WORKER_REFRESH_ACTIONABLE})


class WorkerRefreshObservation:
    """The action-time facts a preflight observes about the pinned worker slot.

    Every field is a **positive** fact defaulting to the unsafe side (``False``):

    - ``identity_resolved`` — the live inventory resolves EXACTLY one slot at the pinned
      ``(workspace, lane, issue, provider, assigned_name, locator)``.
    - ``is_standard_sublane_worker`` — that slot is a standard sublane *worker* (not the lane
      gateway, not the default coordinator / companion, not foreign).
    - ``issue_lane_matches`` — the lane's durable issue owner matches the approval's issue.
    - ``generation_matches`` — the live slot's revision / generation matches the approved one.
    - ``settled_idle`` — the fresh action-time runtime state is settled
      (``turn_ended`` / ``awaiting_input``), never working / unknown.
    - ``composer_clear`` — the composer holds NO real unsent input (empty or an idle ghost
      placeholder; an unreadable composer observation leaves this ``False``).
    - ``resume_anchor_present`` — a durable resume anchor (the existing gate journal to
      re-deliver) exists for this lane.
    - ``worktree_readable`` — the worker's worktree state can be read (dirty or clean; only an
      *unreadable* one blocks — a dirty one is preserved byte-for-byte across the close).
    - ``gateway_distinct_preserved`` — the lane's gateway slot is positively identified as a
      LIVE, DIFFERENT slot than the close target (so the close cannot touch it).
    - ``no_authority_conflict`` — no other approved generation / in-flight replacement
      transaction is already acting on this slot.
    - ``launch_authority_current`` — the lane's ambient LAUNCH authority is exact + current
      RIGHT NOW (Redmine #14475). Joined by the use case from the ONE authority evaluator, not
      by the target observer: it is a fact about the LANE, while every other axis is about the
      SLOT.
    """

    __slots__ = (
        "identity_resolved",
        "is_standard_sublane_worker",
        "issue_lane_matches",
        "generation_matches",
        "settled_idle",
        "composer_clear",
        "resume_anchor_present",
        "worktree_readable",
        "gateway_distinct_preserved",
        "no_authority_conflict",
        "launch_authority_current",
    )

    def __init__(
        self,
        *,
        identity_resolved: bool = False,
        is_standard_sublane_worker: bool = False,
        issue_lane_matches: bool = False,
        generation_matches: bool = False,
        settled_idle: bool = False,
        composer_clear: bool = False,
        resume_anchor_present: bool = False,
        worktree_readable: bool = False,
        gateway_distinct_preserved: bool = False,
        no_authority_conflict: bool = False,
        launch_authority_current: bool = False,
    ) -> None:
        self.identity_resolved = bool(identity_resolved)
        self.is_standard_sublane_worker = bool(is_standard_sublane_worker)
        self.issue_lane_matches = bool(issue_lane_matches)
        self.generation_matches = bool(generation_matches)
        self.settled_idle = bool(settled_idle)
        self.composer_clear = bool(composer_clear)
        self.resume_anchor_present = bool(resume_anchor_present)
        self.worktree_readable = bool(worktree_readable)
        self.gateway_distinct_preserved = bool(gateway_distinct_preserved)
        self.no_authority_conflict = bool(no_authority_conflict)
        self.launch_authority_current = bool(launch_authority_current)

    def with_launch_authority(self, current: bool) -> "WorkerRefreshObservation":
        """This observation with its launch-authority axis replaced. (pure)

        The axis is joined by the use case from the ONE authority evaluator (so the preflight
        verdict and the action-time launch fence read the same source exactly once), rather
        than by the target observer — which observes the SLOT, while this axis is about the
        LANE. Every other axis is carried through unchanged.
        """
        return WorkerRefreshObservation(
            identity_resolved=self.identity_resolved,
            is_standard_sublane_worker=self.is_standard_sublane_worker,
            issue_lane_matches=self.issue_lane_matches,
            generation_matches=self.generation_matches,
            settled_idle=self.settled_idle,
            composer_clear=self.composer_clear,
            resume_anchor_present=self.resume_anchor_present,
            worktree_readable=self.worktree_readable,
            gateway_distinct_preserved=self.gateway_distinct_preserved,
            no_authority_conflict=self.no_authority_conflict,
            launch_authority_current=bool(current),
        )

    def as_payload(self) -> dict[str, bool]:
        return {
            "identity_resolved": self.identity_resolved,
            "is_standard_sublane_worker": self.is_standard_sublane_worker,
            "issue_lane_matches": self.issue_lane_matches,
            "generation_matches": self.generation_matches,
            "settled_idle": self.settled_idle,
            "composer_clear": self.composer_clear,
            "resume_anchor_present": self.resume_anchor_present,
            "worktree_readable": self.worktree_readable,
            "gateway_distinct_preserved": self.gateway_distinct_preserved,
            "no_authority_conflict": self.no_authority_conflict,
            "launch_authority_current": self.launch_authority_current,
        }


def decide_worker_refresh(observation: WorkerRefreshObservation, turn_class: str) -> str:
    """Classify the worker refresh target. (pure, fail-closed, ordered)

    Returns :data:`WORKER_REFRESH_ACTIONABLE` only when EVERY gate holds AND the provider turn
    is classified :data:`...gateway_turn_recovery.TURN_CLASS_FAILED`; otherwise the first
    failing gate's closed blocker (most-fundamental first) so the durable record names exactly
    which fence stopped it.

    The order is the :func:`...gateway_turn_recovery.decide_gateway_refresh` order with the
    protected set inverted and the #13806 worktree fence spliced in:

    1. identity must resolve at all;
    2. the slot must be a standard sublane WORKER (protect the lane gateway / default
       coordinator / foreign slot before inspecting anything else about it);
    3. the issue-lane owner must match (a stale approval on a re-owned lane);
    4. the generation must match (a superseded / recycled generation);
    5. the provider turn must be CLASSIFIED failed — a productive / unconfirmed / unsettled /
       unobservable (including identity-unbound) turn never justifies a close, checked before
       the runtime gates so a blind refresh is named for what it is;
    6. the lane's LAUNCH authority must be exact + current — the actuation-feasibility gate
       whose absence makes the refresh *irrecoverable* rather than merely refused, ordered
       after the turn classification so a *productive* turn on an unbound lane still reports
       ``turn_not_classified_failed`` (Redmine #14475);
    7. the worker must be settled at the fresh action-time read;
    8. the composer must hold no real unsent input;
    9. a durable resume anchor must exist (a refresh exists to resume work, not to churn);
    10. the worktree must be READABLE — the dirty in-scope bytes this surface exists to
        preserve cannot be preserved across a close that cannot even read them (#13806);
    11. the lane gateway must be positively distinguished from the close target;
    12. no competing authority may already be acting on the slot.
    """
    if not observation.identity_resolved:
        return WORKER_REFRESH_BLOCK_UNKNOWN
    if not observation.is_standard_sublane_worker:
        return WORKER_REFRESH_BLOCK_GATEWAY_OR_FOREIGN
    if not observation.issue_lane_matches:
        return WORKER_REFRESH_BLOCK_WRONG_ISSUE_LANE
    if not observation.generation_matches:
        return WORKER_REFRESH_BLOCK_STALE_GENERATION
    if norm(turn_class) != TURN_CLASS_FAILED:
        return WORKER_REFRESH_BLOCK_TURN_NOT_FAILED
    if not observation.launch_authority_current:
        return WORKER_REFRESH_BLOCK_LAUNCH_AUTHORITY
    if not observation.settled_idle:
        return WORKER_REFRESH_BLOCK_NOT_SETTLED
    if not observation.composer_clear:
        return WORKER_REFRESH_BLOCK_PENDING_COMPOSER
    if not observation.resume_anchor_present:
        return WORKER_REFRESH_BLOCK_NO_RESUME_ANCHOR
    if not observation.worktree_readable:
        return WORKER_REFRESH_BLOCK_DIRTY_UNREADABLE
    if not observation.gateway_distinct_preserved:
        return WORKER_REFRESH_BLOCK_GATEWAY_NOT_DISTINGUISHED
    if not observation.no_authority_conflict:
        return WORKER_REFRESH_BLOCK_AUTHORITY_CONFLICT
    return WORKER_REFRESH_ACTIONABLE


def is_worker_refresh_actionable(verdict: str) -> bool:
    """Does this verdict permit the guarded actuation? (pure)"""
    return norm(verdict) == WORKER_REFRESH_ACTIONABLE


def worker_refresh_action_id(
    *, lane_id: str, role: str, provider: str, assigned_name: str, locator: str,
    revision: str,
) -> str:
    """The deterministic action id that names ONE exact live worker generation. (pure)

    The transaction key's ``action_id`` for a live-turn-ended worker refresh:
    ``refresh-worker:<lane>:<role>:<provider>:<assigned_name>:<locator>:r<revision>``.

    The distinct ``refresh-worker:`` prefix keeps this surface's transaction key disjoint from
    both the #13806 stale-worker recovery (``recover:``) of the SAME slot shape and the #14203
    gateway refresh (``refresh-gateway:``) — the two recoveries of one worker are different
    admissions and must never share a replay fence. The row ``revision`` is a REQUIRED
    authority component (the #14203 j#87364 F5 lesson): a same-name / same-locator slot
    recycled at a new process generation derives a DIFFERENT key, so an old approval can never
    close it. Every component must be present — an under-specified target could never identify
    one exact receiver, so it raises rather than emit an ambiguous id.
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
            "a worker refresh action id requires a non-empty lane_id / role / provider / "
            f"assigned_name / locator / revision (missing: {', '.join(missing)})"
        )
    return "refresh-worker:" + ":".join(
        parts[name] for name in ("lane_id", "role", "provider", "assigned_name", "locator")
    ) + ":r" + parts["revision"]


__all__ = (
    "WORKER_PROGRESS_GATES",
    "WorkerTurnObservation",
    "classify_worker_turn",
    "WORKER_REFRESH_ACTIONABLE",
    "WORKER_REFRESH_BLOCK_UNKNOWN",
    "WORKER_REFRESH_BLOCK_GATEWAY_OR_FOREIGN",
    "WORKER_REFRESH_BLOCK_WRONG_ISSUE_LANE",
    "WORKER_REFRESH_BLOCK_STALE_GENERATION",
    "WORKER_REFRESH_BLOCK_TURN_NOT_FAILED",
    "WORKER_REFRESH_BLOCK_LAUNCH_AUTHORITY",
    "WORKER_REFRESH_BLOCK_NOT_SETTLED",
    "WORKER_REFRESH_BLOCK_PENDING_COMPOSER",
    "WORKER_REFRESH_BLOCK_NO_RESUME_ANCHOR",
    "WORKER_REFRESH_BLOCK_DIRTY_UNREADABLE",
    "WORKER_REFRESH_BLOCK_GATEWAY_NOT_DISTINGUISHED",
    "WORKER_REFRESH_BLOCK_AUTHORITY_CONFLICT",
    "WORKER_REFRESH_VERDICTS",
    "WORKER_REFRESH_BLOCKERS",
    "WorkerRefreshObservation",
    "decide_worker_refresh",
    "is_worker_refresh_actionable",
    "worker_refresh_action_id",
)
