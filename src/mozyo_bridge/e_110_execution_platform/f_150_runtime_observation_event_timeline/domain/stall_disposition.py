"""Stall classification vocabulary and its prescription map (Redmine #15843).

:mod:`...domain.pane_stall_sensor` answers "did the screen move". That is a trigger, not
a diagnosis, and wiring it straight to a remedy is the failure this module exists to
prevent: the remedies for the known stall shapes are mutually destructive. ``/clear`` +
re-injection recovers a context-refusal stall (#15816) and *throws away a working
session* if the lane was merely reasoning. A single Enter recovers a swallowed-Enter
stall (#15842) and *double-submits* if the body was already sent. A relaunch recovers a
dead runtime and *destroys hours of work* if the provider's server was simply down and
about to come back. So classification is mandatory before any prescription, and the
prescription is a recommendation this layer never applies.

Two rules shape the whole map, both from the issue's owner intent (#15843 description):

**Patience is the fail-safe.** Detection cannot separate "the provider's server is not
answering" from "the runtime is wedged" — both render a frozen screen and emit nothing.
The costs of guessing wrong are wildly asymmetric: waiting on a wedged lane costs time,
relaunching a server-down lane costs the work. So the undifferentiated case is
:data:`CLASS_UNRESPONSIVE_INDETERMINATE` and it prescribes patience. Relaunch is never
this layer's first answer and never an automatic one; it becomes a *presented candidate*
only via :data:`RX_OWNER_ESCALATION`, and only once the caller states from the durable
record that the patient window has already been spent.

**Unknown is a real outcome.** Every path that cannot be positively classified lands on
:data:`CLASS_UNKNOWN` → :data:`RX_NO_ACTION`. A classifier that must produce a remedy will
invent one; this one is allowed to say nothing, which is what makes it safe to run
unattended.

Posture: every prescription in this map is :data:`APPLY_PRESENT_ONLY`. The watcher reports;
an actor with authority decides. That keeps the module clear of the zero-wait / raw-pane
boundaries (a presented prescription is not a send) and means a misclassification cannot
by itself change any state.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

# --------------------------------------------------------------------------------------
# Classification vocabulary
# --------------------------------------------------------------------------------------

#: The screen advanced — the unit is progressing and is not a stall candidate.
CLASS_SCREEN_PROGRESSING = "screen_progressing"
#: Chrome moved but content did not: the render loop is alive. Reasoning, a tool call and
#: a long test run all look exactly like this, so it is explicitly NOT a stall verdict.
CLASS_BUSY_LIKELY = "busy_likely"
#: A declared pre-composer startup screen is up (trust dialog, theme picker, login, update
#: prompt). Classified by the existing send-time admission authority, not re-derived here.
CLASS_STARTUP_INTERACTION = "startup_interaction"
#: The provider answered the request with a content-policy refusal instead of working
#: (#15789 / #15816). The session is alive; its accumulated context is the suspect.
CLASS_CONTENT_REFUSAL = "content_refusal"
#: The dispatched body is still sitting in the composer, unsent (#15842 swallowed Enter).
CLASS_UNSENT_COMPOSER = "unsent_composer"
#: A declared provider signature for a transient upstream failure is on screen.
#: "Suspected" is load-bearing: see :data:`EVIDENCE_BINARY_READ_UNRENDERED`.
CLASS_PROVIDER_UNRESPONSIVE_SUSPECTED = "provider_unresponsive_suspected"
#: Frozen screen, no declared signature matched: server-down and wedged-runtime merged,
#: because nothing observable separates them. Prescribes patience by design.
CLASS_UNRESPONSIVE_INDETERMINATE = "unresponsive_indeterminate"
#: The screen could not be read. Evidence in neither direction.
CLASS_SCREEN_UNREADABLE = "screen_unreadable"
#: Fail-safe terminal class. Nothing positively matched.
CLASS_UNKNOWN = "unknown"

STALL_CLASSES: frozenset[str] = frozenset(
    {
        CLASS_SCREEN_PROGRESSING,
        CLASS_BUSY_LIKELY,
        CLASS_STARTUP_INTERACTION,
        CLASS_CONTENT_REFUSAL,
        CLASS_UNSENT_COMPOSER,
        CLASS_PROVIDER_UNRESPONSIVE_SUSPECTED,
        CLASS_UNRESPONSIVE_INDETERMINATE,
        CLASS_SCREEN_UNREADABLE,
        CLASS_UNKNOWN,
    }
)

# --------------------------------------------------------------------------------------
# Prescription vocabulary
# --------------------------------------------------------------------------------------

#: Do nothing. The right answer for progress, for legitimate busy, and for every
#: unclassified case.
RX_NO_ACTION = "no_action"
#: Wait, then re-drive the same anchor later. Never relaunch, never reset. The prescription
#: for anything that might be an upstream outage: the model comes back on its own.
RX_PATIENT_WAIT_RETRY = "patient_wait_then_retry"
#: Press Enter, nothing else — the body is already typed (ADR-0002 bounded Enter-only
#: budget; #15842 established that a retained body is the swallowed-Enter signature).
RX_ENTER_ONLY_RETRY = "enter_only_retry"
#: One context reset plus re-injection from the durable anchor, as the single diagnostic
#: split between state-cause and content-cause (skill workflow `## 停滞・拒否からの
#: context reset 回復`). Explicitly one attempt, not a loop.
RX_CONTEXT_RESET_REINJECT = "context_reset_reinjection"
#: Hand the screen to the operator. Declaring a startup screen has never authorised
#: answering it (#13760 / #14741 境界), and that boundary is unchanged here.
RX_OPERATOR_RESOLVES_SCREEN = "operator_resolves_startup_screen"
#: The patient window is spent and the unit is still frozen. Escalate to the owner window
#: with relaunch named as a *candidate* for a human to weigh, never as an action taken.
RX_OWNER_ESCALATION = "owner_escalation"

STALL_PRESCRIPTIONS: frozenset[str] = frozenset(
    {
        RX_NO_ACTION,
        RX_PATIENT_WAIT_RETRY,
        RX_ENTER_ONLY_RETRY,
        RX_CONTEXT_RESET_REINJECT,
        RX_OPERATOR_RESOLVES_SCREEN,
        RX_OWNER_ESCALATION,
    }
)

#: The only posture this layer emits. Kept as a named token rather than an implicit
#: property so that a future increment adding an applying posture has to change a
#: reviewable constant instead of quietly widening a boolean.
APPLY_PRESENT_ONLY = "present_only"

# --------------------------------------------------------------------------------------
# Evidence tiers for provider signature data
# --------------------------------------------------------------------------------------

#: The literal has been **seen on a real rendered screen**, and that observation is
#: recorded against a durable anchor. That single property is what the tier gates on.
#:
#: #14741 phrased its bar as "read from the shipped binary AND confirmed by rendering",
#: and both halves were needed *there* because the strings were being proposed out of a
#: binary and had to be shown to actually render. Reading the binary is a way to arrive at
#: a candidate, not the thing that makes the candidate trustworthy — a verbatim capture of
#: the live screen satisfies the same requirement directly, and more strongly. #15843
#: j#109938 records the correction; the per-entry comment in the data file states which
#: route each signature took.
EVIDENCE_RENDERED_CONFIRMED = "rendered_confirmed"
#: The literals were read from the shipped binary but the screen was never rendered,
#: because reproducing it requires an upstream outage that cannot be induced. A signature
#: at this tier may only produce a *suspected* class, and every suspected class in the map
#: below routes to the same non-destructive prescription its indeterminate sibling gets —
#: so the weaker evidence changes what is REPORTED and never what is RECOMMENDED.
EVIDENCE_BINARY_READ_UNRENDERED = "binary_read_unrendered"

EVIDENCE_TIERS: frozenset[str] = frozenset(
    {EVIDENCE_RENDERED_CONFIRMED, EVIDENCE_BINARY_READ_UNRENDERED}
)

#: Classes a :data:`EVIDENCE_BINARY_READ_UNRENDERED` signature is permitted to assert.
#: Enforced at load time so an unrendered literal can never be promoted into a class whose
#: prescription is destructive by adding a line of data.
UNRENDERED_ADMISSIBLE_CLASSES: frozenset[str] = frozenset(
    {CLASS_PROVIDER_UNRESPONSIVE_SUSPECTED}
)


class StallDispositionError(ValueError):
    """Raised when a class / prescription pairing is outside the fixed map."""


@dataclass(frozen=True)
class Prescription:
    """What a classification recommends, and under what conditions.

    ``relaunch_is_a_candidate`` exists so the one place relaunch may be *mentioned* is
    explicit and greppable. It is never paired with an applying posture, and it is false
    on every class that could be an upstream outage.
    """

    action: str
    posture: str = APPLY_PRESENT_ONLY
    relaunch_is_a_candidate: bool = False
    rationale_anchor: str = ""

    def __post_init__(self) -> None:
        if self.action not in STALL_PRESCRIPTIONS:
            raise StallDispositionError(f"unknown prescription {self.action!r}")
        if self.posture != APPLY_PRESENT_ONLY:
            raise StallDispositionError(
                f"prescription posture {self.posture!r} is not emitted by this layer"
            )

    def telemetry(self) -> dict[str, object]:
        return {
            "prescription": self.action,
            "posture": self.posture,
            "relaunch_is_a_candidate": self.relaunch_is_a_candidate,
            "rationale_anchor": self.rationale_anchor,
        }


_PRESCRIPTIONS: Mapping[str, Prescription] = MappingProxyType(
    {
        CLASS_SCREEN_PROGRESSING: Prescription(
            RX_NO_ACTION, rationale_anchor="screen advanced between samples"
        ),
        CLASS_BUSY_LIKELY: Prescription(
            RX_NO_ACTION,
            rationale_anchor="ack-completion-receiver-state.md: silence is not a stall",
        ),
        CLASS_STARTUP_INTERACTION: Prescription(
            RX_OPERATOR_RESOLVES_SCREEN,
            rationale_anchor="#13760 / #14741: declaring a screen never authorises answering it",
        ),
        CLASS_CONTENT_REFUSAL: Prescription(
            RX_CONTEXT_RESET_REINJECT,
            rationale_anchor="#15816: one reset + durable-anchor re-injection, not a loop",
        ),
        CLASS_UNSENT_COMPOSER: Prescription(
            RX_ENTER_ONLY_RETRY,
            rationale_anchor="ADR-0002 / #15842: body typed once, Enter-only budget",
        ),
        CLASS_PROVIDER_UNRESPONSIVE_SUSPECTED: Prescription(
            RX_PATIENT_WAIT_RETRY,
            rationale_anchor="#15843 owner intent: server-down is patient, never relaunch",
        ),
        CLASS_UNRESPONSIVE_INDETERMINATE: Prescription(
            RX_PATIENT_WAIT_RETRY,
            rationale_anchor="#15843 owner intent: outage and wedge are merged, patience is safe",
        ),
        CLASS_SCREEN_UNREADABLE: Prescription(
            RX_NO_ACTION,
            rationale_anchor="unreadable is evidence in neither direction",
        ),
        CLASS_UNKNOWN: Prescription(
            RX_NO_ACTION, rationale_anchor="fail-safe: no positive classification"
        ),
    }
)

#: The escalation a caller may reach ONLY by asserting, from the durable record, that the
#: patient window has already been spent on a still-frozen unit. It is not reachable from
#: any classification alone — which is the mechanical form of "patience first".
ESCALATION_AFTER_PATIENCE = Prescription(
    RX_OWNER_ESCALATION,
    relaunch_is_a_candidate=True,
    rationale_anchor="#15843: relaunch is a candidate for a human only after patience",
)

#: Classes for which a spent patient window may escalate. A unit that is merely busy or
#: whose screen could not be read never escalates on a timer.
ESCALATABLE_CLASSES: frozenset[str] = frozenset(
    {CLASS_PROVIDER_UNRESPONSIVE_SUSPECTED, CLASS_UNRESPONSIVE_INDETERMINATE}
)


def prescribe(stall_class: str, *, patient_window_exhausted: bool = False) -> Prescription:
    """Map a classification onto its prescription.

    ``patient_window_exhausted`` is a durable-record fact supplied by the caller — this
    module keeps no history and cannot time anything itself. When it is true and the class
    is one that patience was meant to cover, the prescription escalates to a human with
    relaunch named as a candidate. It never escalates a busy, progressing, unreadable or
    unknown unit, so a long-running test cannot be aged into a relaunch suggestion.
    """
    if stall_class not in STALL_CLASSES:
        raise StallDispositionError(f"unknown stall class {stall_class!r}")
    if patient_window_exhausted and stall_class in ESCALATABLE_CLASSES:
        return ESCALATION_AFTER_PATIENCE
    return _PRESCRIPTIONS[stall_class]
