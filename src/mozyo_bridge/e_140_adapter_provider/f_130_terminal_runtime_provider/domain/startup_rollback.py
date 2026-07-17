"""Whether one startup participant may be closed by a rollback (Redmine #13948, j#80989).

The pure eligibility decision, kept apart from the I/O that gathers the facts so it can be
driven exhaustively. This is the module that says **no**: it is the only thing standing
between "the run that started this pane wants it back" and "a command closed an agent
somebody was working in".

The authority it exercises is narrow by construction (Answer j#80989 Q1, narrowed by the
j#80991 reconciliation). ``action ownership is not a generic pending-composer discard
permission``: what a rollback may throw away is the startup UI/process state *its own
transaction created*, never a body an LLM or an operator put in a composer. So:

- only a **participant of this exact action** is even a candidate — never an adopted slot,
  never a pane whose durable name merely matches;
- the live world must still **agree** with the record: same name, same locator, unique, no
  foreign or duplicate or newer generation;
- a **durable obligation** outranks everything (an unreadable ledger included): idle is a
  receiver state, not proof that nothing is owed to the slot (#13892 j#80506 F4);
- composer state is **three-valued** and stays that way. ``pending`` and ``unreadable``
  are different facts with the same answer here (preserve), but collapsing them would make
  the *reason* unreportable — the #13892 R8 defect this issue's consultation re-found;
- a recognised **startup blocker** is action-owned UI: this action put that screen there by
  starting the provider, and no one has typed into it. It is closeable — and it is never
  *answered* (accepting a trust prompt is an action in the provider's own UI, #13760).

Every predicate is a positive fact. Nothing here progresses on the absence of evidence.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Close it: this action started it, the world still agrees, and nothing is owed to it.
#: This verdict — and ONLY this one — names a live pane to close.
ROLLBACK_ELIGIBLE = "eligible"
#: Positively absent: the durable name is not in the live inventory at all. Settled, so a
#: partial rollback resumes past it — but NOT a close target (review j#81070 R1-F2).
#: Collapsing this into `eligible` made the rail hand the RECORDED locator to close, and a
#: foreign agent that had since taken that pane id was closed instead. Absence is a reason
#: to do nothing, never a reason to close an address.
ROLLBACK_ABSENT = "absent"
#: The record and the live world disagree about identity: not ours, or not ours any more.
ROLLBACK_IDENTITY_DRIFT = "identity_drift"
#: More than one live agent answers to the durable name. Never resolved by guessing.
ROLLBACK_AMBIGUOUS = "ambiguous"
#: Work is owed to this slot by a durable ledger. Closing it would drop that work.
ROLLBACK_WORK_OBLIGATION = "work_obligation_present"
#: The obligation ledger could not be read. Absence of a read is not absence of work.
ROLLBACK_OBLIGATION_UNREADABLE = "obligation_unreadable"
#: Someone's unsent input is in the composer. Preserved regardless of any approval.
ROLLBACK_PENDING_INPUT = "pending_input_present"
#: The composer could not be read, so pending input cannot be ruled out.
ROLLBACK_COMPOSER_UNREADABLE = "composer_unreadable"
#: The slot is busy: a turn is running. Never interrupted by a rollback.
ROLLBACK_AGENT_BUSY = "agent_busy"
#: The live inventory could not be read. Fail closed; close nothing.
ROLLBACK_INVENTORY_UNREADABLE = "inventory_unreadable"
#: A live-state port (runtime state / composer) raised while reading this pane, so its
#: idle/settle facts are unknown. Fail closed — an unreadable live state is not a settled one.
ROLLBACK_LIVE_STATE_UNREADABLE = "live_state_unreadable"
#: Already proven closed by this same action. Replay is answered from the record.
ROLLBACK_ALREADY_CLOSED = "already_closed"

#: Verdicts that name a live pane this rollback may close. Exactly one, on purpose: a
#: caller asking "is this a target?" must not be able to answer it from "is this settled?".
ROLLBACK_CLOSE_TARGETS: frozenset[str] = frozenset({ROLLBACK_ELIGIBLE})

#: Verdicts that need no further action from this rollback (closed, or never there). A
#: settled participant does NOT block the run — blocking on one is how an interrupted
#: rollback becomes permanently stuck (#13847 R1-F1).
ROLLBACK_SETTLED: frozenset[str] = frozenset(
    {ROLLBACK_ELIGIBLE, ROLLBACK_ABSENT, ROLLBACK_ALREADY_CLOSED}
)

ROLLBACK_VERDICTS: frozenset[str] = frozenset(
    {
        ROLLBACK_ELIGIBLE,
        ROLLBACK_ABSENT,
        ROLLBACK_IDENTITY_DRIFT,
        ROLLBACK_AMBIGUOUS,
        ROLLBACK_WORK_OBLIGATION,
        ROLLBACK_OBLIGATION_UNREADABLE,
        ROLLBACK_PENDING_INPUT,
        ROLLBACK_COMPOSER_UNREADABLE,
        ROLLBACK_AGENT_BUSY,
        ROLLBACK_INVENTORY_UNREADABLE,
        ROLLBACK_LIVE_STATE_UNREADABLE,
        ROLLBACK_ALREADY_CLOSED,
    }
)

#: Composer facts, three-valued (never a bool — Answer j#80990 / #13892 R8).
COMPOSER_EMPTY = "empty"
COMPOSER_PENDING = "pending"
COMPOSER_UNREADABLE = "unreadable"
#: The provider's own recognised startup screen. Not composer input: this action's launch
#: is what put it there, and it holds nothing anyone typed.
COMPOSER_STARTUP_BLOCKER = "startup_blocker"

#: Fixed operator sentences, keyed by verdict so the text can never contradict it.
ROLLBACK_DETAIL: dict[str, str] = {
    ROLLBACK_ELIGIBLE: (
        "this action started this exact pane, the live inventory still agrees, and "
        "nothing is owed to it"
    ),
    ROLLBACK_ABSENT: (
        "this participant's durable name is not in the live inventory: it is already "
        "gone, so there is nothing to close (the recorded locator is an address, not a "
        "claim on whoever holds it now)"
    ),
    ROLLBACK_IDENTITY_DRIFT: (
        "the live slot is not the one this action started (name / locator / role no "
        "longer match the participant record); refusing to close another process"
    ),
    ROLLBACK_AMBIGUOUS: (
        "more than one live agent answers to this durable name; refusing to resolve a "
        "duplicate by guessing which one is ours"
    ),
    ROLLBACK_WORK_OBLIGATION: (
        "a durable ledger still owes work to this slot; closing it would drop that work "
        "(an idle agent is not an agent with nothing owed to it)"
    ),
    ROLLBACK_OBLIGATION_UNREADABLE: (
        "the obligation ledger could not be read, so it cannot be shown that nothing is "
        "owed to this slot; an unreadable ledger is never an empty one"
    ),
    ROLLBACK_PENDING_INPUT: (
        "unsent input is waiting in this slot's composer; a startup rollback discards "
        "only the startup state it created, never a body someone put there"
    ),
    ROLLBACK_COMPOSER_UNREADABLE: (
        "this slot's composer could not be read, so pending input cannot be ruled out; "
        "an unreadable composer is never an empty one"
    ),
    ROLLBACK_AGENT_BUSY: (
        "this slot is running a turn; a rollback never interrupts work in flight"
    ),
    ROLLBACK_INVENTORY_UNREADABLE: (
        "the live inventory could not be read, so this participant cannot be identified; "
        "fail closed and close nothing"
    ),
    ROLLBACK_LIVE_STATE_UNREADABLE: (
        "a live-state read (runtime state / composer) failed for this pane, so it cannot "
        "be shown idle with no pending input; fail closed and close nothing"
    ),
    ROLLBACK_ALREADY_CLOSED: (
        "this action already proved this participant closed; replay is answered from the "
        "record rather than by closing something again"
    ),
}


@dataclass(frozen=True)
class ParticipantFacts:
    """Everything observed about one participant at action time. All positive facts."""

    #: The record already proves this one closed (a resumed / replayed rollback).
    recorded_closed: bool = False
    #: The live inventory was readable at all. False => nothing below can be trusted.
    inventory_readable: bool = True
    #: How many live agents carry this participant's exact durable name.
    name_matches: int = 0
    #: The locator the live row reports for that name.
    live_locator: str = ""
    #: The locator this action recorded when it started the pane.
    recorded_locator: str = ""
    #: The live row is positively dead shell residue (#13518): no agent, hence no turn and
    #: no composer to lose. Its settle-facts are true by construction.
    shell_residue: bool = False
    #: The agent is positively idle (a settled runtime state).
    agent_idle: bool = False
    #: Three-valued composer fact (see COMPOSER_*).
    composer: str = COMPOSER_UNREADABLE
    #: A durable ledger owes work to this slot.
    obligation_present: bool = False
    #: The obligation ledger could not be read.
    obligation_unreadable: bool = False
    #: A live-state port (runtime state / composer read) raised while observing this pane.
    live_state_unreadable: bool = False


def classify_rollback(facts: ParticipantFacts) -> str:
    """Decide one participant's fate (pure, total, fail-closed).

    Precedence is the contract:

    1. an already-proven close answers from the record — a replay must never close twice;
    2. an unreadable inventory can prove nothing, so nothing proceeds;
    3. identity comes next: if this is not the pane we started, no later fact matters —
       and asking about *its* composer would already be a trespass;
    4. obligations outrank runtime state: idle is a receiver state, not a proof that no
       work is owed (#13892 j#80506 F4);
    5. positive shell residue short-circuits the liveness questions: there is no agent, so
       there is no turn and no composer — asking would only produce an unreadable answer
       and preserve a dead pane forever (the #13845 over-block defect);
    6. busy before composer: a running turn is a reason on its own;
    7. composer last, three-valued, and only `empty` or `startup_blocker` may pass.
    """
    if facts.recorded_closed:
        return ROLLBACK_ALREADY_CLOSED
    if not facts.inventory_readable:
        return ROLLBACK_INVENTORY_UNREADABLE
    if facts.name_matches > 1:
        return ROLLBACK_AMBIGUOUS
    if facts.name_matches < 1:
        # Positively absent: a prior run of this same rollback closed it, or it never came
        # up. Either way the participant is settled, which is what makes an interrupted
        # rollback resumable rather than permanently stuck (#13847 R1-F1 / #13892's
        # partial-close discipline) — but it is NOT a close target. The recorded locator
        # is an address we once launched at, not a claim on whoever holds it now
        # (review j#81070 R1-F2: a foreign agent on that pane id was closed).
        return ROLLBACK_ABSENT
    recorded = (facts.recorded_locator or "").strip()
    live = (facts.live_locator or "").strip()
    if not recorded or not live or recorded != live:
        return ROLLBACK_IDENTITY_DRIFT
    if facts.obligation_unreadable:
        return ROLLBACK_OBLIGATION_UNREADABLE
    if facts.obligation_present:
        return ROLLBACK_WORK_OBLIGATION
    if facts.shell_residue:
        return ROLLBACK_ELIGIBLE
    if facts.live_state_unreadable:
        # A runtime/composer port raised while reading this live, ours pane (review j#81224
        # R7-F4): we cannot show it is idle with no pending input, so it is not eligible —
        # an unreadable live state is never a settled one.
        return ROLLBACK_LIVE_STATE_UNREADABLE
    if not facts.agent_idle:
        return ROLLBACK_AGENT_BUSY
    if facts.composer == COMPOSER_PENDING:
        return ROLLBACK_PENDING_INPUT
    if facts.composer == COMPOSER_STARTUP_BLOCKER:
        # Action-owned startup UI: this action's launch put the screen there and nobody
        # typed into it. Closing it discards no one's work. It is NEVER answered.
        return ROLLBACK_ELIGIBLE
    if facts.composer == COMPOSER_EMPTY:
        return ROLLBACK_ELIGIBLE
    # `unreadable` and anything unrecognised: never assume an empty composer.
    return ROLLBACK_COMPOSER_UNREADABLE


__all__ = (
    "COMPOSER_EMPTY",
    "ROLLBACK_ABSENT",
    "ROLLBACK_CLOSE_TARGETS",
    "ROLLBACK_SETTLED",
    "COMPOSER_PENDING",
    "COMPOSER_STARTUP_BLOCKER",
    "COMPOSER_UNREADABLE",
    "ROLLBACK_AGENT_BUSY",
    "ROLLBACK_ALREADY_CLOSED",
    "ROLLBACK_AMBIGUOUS",
    "ROLLBACK_COMPOSER_UNREADABLE",
    "ROLLBACK_DETAIL",
    "ROLLBACK_ELIGIBLE",
    "ROLLBACK_IDENTITY_DRIFT",
    "ROLLBACK_INVENTORY_UNREADABLE",
    "ROLLBACK_OBLIGATION_UNREADABLE",
    "ROLLBACK_PENDING_INPUT",
    "ROLLBACK_VERDICTS",
    "ROLLBACK_WORK_OBLIGATION",
    "ParticipantFacts",
    "classify_rollback",
)
