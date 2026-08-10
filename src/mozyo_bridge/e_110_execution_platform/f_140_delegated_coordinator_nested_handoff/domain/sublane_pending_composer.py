"""Pure pending-composer classifier for receiver quarantine (#13763).

Redmine #15193 — **the classification label is a verdict, not the whole observation.**

The precedence in :func:`classify_pending_composer` is a safety boundary and is unchanged:
an unreadable inventory, a mismatched generation, an unattested identity or a working agent
must all win over "there is pending text here", because none of those states may authorize
replacing a receiver. But the *winning* label used to be the ONLY thing that left this
module, which silently destroyed the facts the losing branches had already observed.

That loss is exactly the #15193 deadlock. A stopped lane whose receiver carries BOTH a
generation mismatch AND a real pending composer classifies as ``generation_mismatch``, whose
``quarantine_candidate`` is ``False`` — so ``sublane quarantine-inspect`` reports
``not_quarantine_candidate`` and mints no approval. Meanwhile ``sublane hibernate``'s
action-time boundary probes the composer directly, sees the pending input, blocks with
``composer_pending_real``, and names ``owner_approved_quarantine`` as the next action. Each
surface is individually correct and together they are a closed loop: hibernate sends the
operator to quarantine, quarantine says the receiver is not a candidate, and the canonical
rail cannot dispose of the input at all (#15110 j#102068, #15140 j#102064, #15195 j#102193).

The fix here is deliberately narrow: **no precedence changes, no new candidacy**. The
classification now carries the *co-observed* facts alongside the winning label —
:attr:`PendingComposerClassification.pending_observed` and
:attr:`~PendingComposerClassification.generation_axes` — so a downstream rail can tell the
difference between "generation mismatch, composer empty" and "generation mismatch, composer
holds a real unsent input", which the collapsed label cannot express. Deciding what may be
done about that combination is NOT this module's job; it belongs to
:mod:`...domain.generation_mismatch_disposition`, which consumes these facts.

Both added fields keep the contract-8 body fence: an ``Optional[bool]`` and a tuple drawn
from the closed :data:`GENERATION_AXES` vocabulary carry no body, hash, length or excerpt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Why the generation did not match (Redmine #15193; closed vocabulary).
#
# ``generation_matches`` is a single boolean folded from FOUR independent checks by the
# quarantine inspector (identity decode, revision equality, worktree scope, and the
# gateway/worker pair). Collapsing them lost which one actually failed, so an operator facing
# `generation_mismatch` could not tell a recycled pane apart from a half-live lane pair — and
# an owner approval could not name the exact condition it was approving over.
#
# Every token below is a bare axis name: no path, no locator, no revision value, no pane text.
# ---------------------------------------------------------------------------

#: The row's decoded assigned name does not name the exact (workspace, lane, role) slot.
GEN_AXIS_IDENTITY = "identity"
#: The row's live revision differs from the revision the caller asserted.
GEN_AXIS_REVISION = "revision"
#: The row's working directory does not resolve to the lane's repo scope.
GEN_AXIS_WORKSPACE_CWD = "workspace_cwd"
#: The lane's gateway/worker pair is not both-live at a single shared placement.
GEN_AXIS_PAIR = "pair"
#: The inventory carried zero or several rows for the exact pinned (name, locator).
GEN_AXIS_ROW_AMBIGUOUS = "row_ambiguous"

GENERATION_AXES = frozenset(
    {
        GEN_AXIS_IDENTITY,
        GEN_AXIS_REVISION,
        GEN_AXIS_WORKSPACE_CWD,
        GEN_AXIS_PAIR,
        GEN_AXIS_ROW_AMBIGUOUS,
    }
)

#: Stable report order, so two observations of the same axis set render identically and an
#: approval token minted from them compares byte-equal at execute time.
GENERATION_AXIS_ORDER: tuple[str, ...] = (
    GEN_AXIS_IDENTITY,
    GEN_AXIS_REVISION,
    GEN_AXIS_WORKSPACE_CWD,
    GEN_AXIS_PAIR,
    GEN_AXIS_ROW_AMBIGUOUS,
)


def ordered_generation_axes(axes: "tuple[str, ...]") -> tuple[str, ...]:
    """Normalise an axis tuple to the canonical order, dropping unknowns (pure).

    Deduplicates and filters to :data:`GENERATION_AXES` so an axis list that reaches an
    approval token is always canonical — an approval minted from ``("pair", "identity")``
    must re-verify equal against an observation of ``("identity", "pair")``, and a caller
    cannot smuggle an unrecognised token through the comparison.
    """
    present = {axis for axis in axes if axis in GENERATION_AXES}
    return tuple(axis for axis in GENERATION_AXIS_ORDER if axis in present)


#: Runtime states that mean a worker is mid-turn. A single definition on purpose (Redmine
#: #15193): the classifier's precedence puts ``generation_mismatch`` ABOVE ``agent_working``,
#: so a mismatched receiver whose agent is BUSY still classifies as ``generation_mismatch``.
#: Any caller deciding whether work is in flight must therefore read the raw agent state
#: through this predicate rather than infer it from the label — inferring it from the label
#: silently reports a running worker as idle.
_WORKING_STATES = ("busy", "working")


def agent_state_is_working(agent_state: str) -> bool:
    """Is this raw runtime state a worker mid-turn? (pure)"""
    return str(agent_state or "").strip().lower() in _WORKING_STATES


NO_PENDING_COMPOSER = "no_pending_composer"
CORRELATED_KNOWN_MARKER = "correlated_known_marker"
UNCORRELATED = "uncorrelated"
AMBIGUOUS = "ambiguous"
AGENT_WORKING = "agent_working"
IDENTITY_UNATTESTED = "identity_unattested"
GENERATION_MISMATCH = "generation_mismatch"
INVENTORY_UNREADABLE = "inventory_unreadable"

PENDING_COMPOSER_CLASSIFICATIONS = frozenset(
    {
        NO_PENDING_COMPOSER,
        CORRELATED_KNOWN_MARKER,
        UNCORRELATED,
        AMBIGUOUS,
        AGENT_WORKING,
        IDENTITY_UNATTESTED,
        GENERATION_MISMATCH,
        INVENTORY_UNREADABLE,
    }
)


@dataclass(frozen=True)
class PendingComposerSignal:
    """Content-free facts supplied by the transient live adapter.

    The composer body never crosses this boundary.  ``correlated_marker_ids``
    carries only delivery-ledger marker identities that the adapter positively
    found in the current composer and in the ledger.
    """

    inventory_readable: bool
    has_pending: Optional[bool]
    agent_state: str
    identity_attested: bool
    generation_matches: bool
    correlated_marker_ids: tuple[str, ...] = ()
    correlation_ambiguous: bool = False
    #: Which exact axes made ``generation_matches`` false (Redmine #15193). Drawn from the
    #: closed :data:`GENERATION_AXES` vocabulary; empty when the generation matched, or when
    #: the observing adapter could not attribute the mismatch to a specific axis. An empty
    #: tuple on a mismatched generation therefore means "unattributed", never "no mismatch" —
    #: the disposition rail refuses to mint an approval it cannot name the condition for.
    generation_axes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PendingComposerClassification:
    """The winning verdict PLUS the facts the losing precedence branches observed.

    ``label`` and the three derived predicates are unchanged (#13763). Redmine #15193 adds the
    co-observed facts so a caller can distinguish states the single label folds together —
    specifically ``generation_mismatch`` with an empty composer (nothing at stake) from
    ``generation_mismatch`` with a real unsent input (an operator decision is owed). Neither
    added field widens candidacy: :attr:`quarantine_candidate` still reads ``label`` alone.
    """

    label: str
    correlated_marker_id: str = ""
    #: The raw pending fact as observed, carried through REGARDLESS of which precedence
    #: branch won. ``None`` means the composer could not be read (never "empty"), matching
    #: :attr:`PendingComposerSignal.has_pending`. This is the fact the collapsed label
    #: destroyed and the whole reason #15193's two surfaces could not agree.
    pending_observed: Optional[bool] = None
    #: The exact axes behind a ``generation_mismatch`` label, canonically ordered.
    generation_axes: tuple[str, ...] = ()

    @property
    def q_enter_recommended(self) -> bool:
        return self.label == CORRELATED_KNOWN_MARKER

    @property
    def quarantine_candidate(self) -> bool:
        return self.label in (UNCORRELATED, AMBIGUOUS)

    @property
    def blocked(self) -> bool:
        return not (self.q_enter_recommended or self.quarantine_candidate)

    @property
    def generation_mismatch_with_pending(self) -> bool:
        """The exact #15193 shape: generation mismatched AND a real pending input observed.

        This is a recognition predicate only — it grants nothing. It says the receiver is in
        the state where the canonical rails deadlock, so a caller may route to the disposition
        preflight instead of repeating ``not_quarantine_candidate`` at the operator. An
        unreadable composer (``pending_observed is None``) is deliberately excluded: an
        unprovable pending fact must never open a disposition path.
        """
        return self.label == GENERATION_MISMATCH and self.pending_observed is True

    def as_payload(self) -> dict[str, object]:
        return {
            "classification": self.label,
            "correlated_marker_id": self.correlated_marker_id or None,
            "q_enter_recommended": self.q_enter_recommended,
            "quarantine_candidate": self.quarantine_candidate,
            "blocked": self.blocked,
            "pending_observed": self.pending_observed,
            "generation_axes": list(self.generation_axes),
            "generation_mismatch_with_pending": self.generation_mismatch_with_pending,
        }


def classify_pending_composer(
    signal: PendingComposerSignal,
) -> PendingComposerClassification:
    """Classify the exact current receiver, fail-closed by precedence.

    Precedence is UNCHANGED from #13763 — the label a given signal produces is identical.
    Redmine #15193 only attaches the co-observed facts (:attr:`~PendingComposerClassification.
    pending_observed` / :attr:`~PendingComposerClassification.generation_axes`) to every
    verdict, so the branches that lose the precedence race stop discarding what they saw.

    ``pending_observed`` is taken from the signal verbatim rather than re-derived from the
    label: only the raw observation distinguishes "composer empty" from "composer unreadable",
    and the ``inventory_unreadable`` label is reachable from both.
    """
    axes = ordered_generation_axes(signal.generation_axes)

    def verdict(label: str, **changes: object) -> PendingComposerClassification:
        # One construction point, so no branch can forget to carry the co-observed facts.
        # ``generation_axes`` is attached only to the mismatch verdict it explains; on any
        # other label the axes describe a condition that did not decide the outcome, and
        # reporting them there would invite reading a spent observation as live evidence.
        return PendingComposerClassification(
            label,
            pending_observed=signal.has_pending,
            generation_axes=axes if label == GENERATION_MISMATCH else (),
            **changes,  # type: ignore[arg-type]
        )

    if not signal.inventory_readable:
        return verdict(INVENTORY_UNREADABLE)
    if not signal.generation_matches:
        return verdict(GENERATION_MISMATCH)
    if not signal.identity_attested:
        return verdict(IDENTITY_UNATTESTED)
    if agent_state_is_working(signal.agent_state):
        return verdict(AGENT_WORKING)
    if signal.has_pending is None:
        return verdict(INVENTORY_UNREADABLE)
    if not signal.has_pending:
        return verdict(NO_PENDING_COMPOSER)
    markers = tuple(dict.fromkeys(m for m in signal.correlated_marker_ids if m))
    if signal.correlation_ambiguous or len(markers) > 1:
        return verdict(AMBIGUOUS)
    if len(markers) == 1:
        return verdict(CORRELATED_KNOWN_MARKER, correlated_marker_id=markers[0])
    return verdict(UNCORRELATED)


__all__ = (
    "AGENT_WORKING",
    "AMBIGUOUS",
    "CORRELATED_KNOWN_MARKER",
    "GENERATION_AXES",
    "GENERATION_AXIS_ORDER",
    "GENERATION_MISMATCH",
    "GEN_AXIS_IDENTITY",
    "GEN_AXIS_PAIR",
    "GEN_AXIS_REVISION",
    "GEN_AXIS_ROW_AMBIGUOUS",
    "GEN_AXIS_WORKSPACE_CWD",
    "IDENTITY_UNATTESTED",
    "INVENTORY_UNREADABLE",
    "NO_PENDING_COMPOSER",
    "PENDING_COMPOSER_CLASSIFICATIONS",
    "UNCORRELATED",
    "PendingComposerClassification",
    "PendingComposerSignal",
    "agent_state_is_working",
    "classify_pending_composer",
    "ordered_generation_axes",
)
