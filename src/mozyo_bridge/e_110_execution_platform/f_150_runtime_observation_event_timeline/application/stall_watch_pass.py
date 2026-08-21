"""One read-only stall-watch pass over a set of targets (Redmine #15843).

The composition root for the stall watcher: it takes two screen samples per target
through the caller's already-bound read primitive, hands them to the pure sensor
(:mod:`...domain.pane_stall_sensor`), classifies whatever did not advance, and maps the
classification onto a prescription (:mod:`...domain.stall_disposition`). It returns
observations. It never types, never presses a key, never resets a session, never
relaunches, and never writes a durable record — those are the caller's authority and the
whole reason a misclassification here cannot cost anything.

Placement (issue #15843 acceptance 4). This is a watcher-layer pass, not an LLM turn.
``skills/mozyo-bridge-agent/references/workflow.md`` `## Wait / polling 効率標準` requires
an agent turn to end zero-wait after a dispatch and moves bounded waiting into background
watchers and operator debug — so the one sleep in this module belongs to a watcher process
calling it, and an agent turn must not call it to poll its own dispatch. That is a
placement rule, not a lock: the module is a plain function, and the discipline it protects
lives in the caller.

Reuse (issue #15843 IR step 2). Nothing here re-derives an existing authority:

- the screen read is the caller's :class:`...TerminalTransportPort.read_pane`, the same
  read-only primitive the send path already uses;
- startup screens are classified by :func:`evaluate_startup_admission` — the #13760
  authority that already knows each provider's declared blockers, already refuses to guess
  for an unprofiled provider, and already never returns pane text;
- a retained dispatched body is evaluated by :func:`current_composer_retains_body` — the
  same predicate the queue-enter retry gate uses — against the marker the caller supplies
  from the durable delivery record. Reusing it is not a convenience: the first cut of this
  module substring-matched the marker against the whole visible pane, which
  ``ack-completion-receiver-state.md`` forbids ("scrollback 全体の substring は使わない")
  for a reason that bites hardest here — a *successfully submitted* body stays in the
  transcript as a user message, so a whole-pane match reports ``unsent_composer`` most
  eagerly on exactly the panes that did submit. That predicate instead finds the last
  rendered composer prompt, fails closed when unindented output below it proves the prompt
  is historical, and removes whitespace so a hard-wrapped marker still matches;
- everything provider-specific is data (``agent_provider_stall_signatures.yaml``).

Classification order, and why (first-match with the intersections named)
-----------------------------------------------------------------------
The rules below are ordered, and three of them genuinely intersect. Leaving an
intersection to fall out of the ordering is how a first-match tree silently hides a
co-applicable case, so each is named with its precedence basis:

1. **incomparable** → ``screen_unreadable``. Precondition of everything else.
2. **advancing** → ``screen_progressing``. Progress outranks every static-screen
   inference: a signature glimpsed while content is moving is transcript, not state.
3. **blank** → ``unknown``. A readable-but-empty screen is uninterpretable, and calling
   an empty pane "frozen" would report a stall for a pane that has nothing on it.
4. **startup blocker matched** → ``startup_interaction``. *Intersects rule 5*: #13760's
   live defect is precisely a dispatched body typed into a startup screen. Rule 4 wins on
   ``least_effect_first`` — an Enter pressed at a startup screen answers the dialog's
   default, which is the #13760 / #14741 defect itself, so the Enter prescription must
   never be reachable while a startup screen is up. *Intersects rule 6* (an update prompt
   can coexist with a retry line) and wins on ``role_precedence``: rendered-confirmed
   evidence with an operator-owned remedy outranks a lower-tier suspicion.
5. **dispatched body retained in the CURRENT composer** → ``unsent_composer``. *Intersects
   rule 6*: a retained body under a retry banner is possible. Rule 5 wins on
   ``direct_evidence_over_suspicion`` — a body still sitting in the live composer is an
   observation about *this* dispatch's submit, the banner is an inference about the
   provider, and the remedy (one Enter, body never re-typed) is the bounded budget
   ADR-0002 authorises regardless. The precedence rests on the evidence being
   current-composer; it would not survive a whole-pane match, which observes only that the
   dispatch left a trace somewhere.
6. **declared stall signature matched** → the class it asserts.
7. **chrome moved** → ``busy_likely``. The render loop is alive and nothing positive
   matched; reasoning, a tool call and a long test run all land here, and the prescription
   is to do nothing.
8. **byte-identical** → ``unresponsive_indeterminate``. Frozen with nothing matched. The
   deliberately merged class: server-down and a wedged runtime are indistinguishable from
   outside, so they share the patient prescription rather than being guessed apart.

Rules 7 and 8 exhaust the non-advancing states, so there is no unreachable tail: every
input lands on exactly one rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.pane_stall_sensor import (  # noqa: E501
    DEFAULT_CHROME_SIMILARITY,
    DEFAULT_SAMPLE_INTERVAL_SECONDS,
    DIFF_CHROME_ONLY,
    DIFF_INCOMPARABLE,
    READ_OK,
    READ_UNREADABLE,
    ScreenDiff,
    ScreenSample,
    compare_samples,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_disposition import (  # noqa: E501
    CLASS_BUSY_LIKELY,
    CLASS_SCREEN_PROGRESSING,
    CLASS_SCREEN_UNREADABLE,
    CLASS_STARTUP_INTERACTION,
    CLASS_UNKNOWN,
    CLASS_UNRESPONSIVE_INDETERMINATE,
    CLASS_UNSENT_COMPOSER,
    Prescription,
    prescribe,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_admission import (  # noqa: E501
    ADMISSION_BLOCKED,
    evaluate_startup_admission,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_resend_gate import (  # noqa: E501
    current_composer_retains_body,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_stall_signature import (  # noqa: E501
    StallSignatureRegistry,
    first_match,
)


class StallWatchError(ValueError):
    """Raised on a structurally invalid watch request."""


@dataclass(frozen=True)
class StallWatchTarget:
    """One unit to observe.

    ``pending_body_marker`` is the exact marker of the last body dispatched to this
    target, taken by the caller from the durable delivery record. It is optional and its
    absence is not a degraded mode — it simply means rule 5 has no evidence to evaluate,
    so ``unsent_composer`` is never asserted on a guess. Joining the delivery ledger to
    supply it automatically is a separate increment; this is the seam it plugs into.

    ``patient_window_exhausted`` is likewise a durable-record fact: this layer keeps no
    history and cannot time anything, so the escalation that may name relaunch as a
    candidate is only reachable by a caller stating that patience has already been spent.
    """

    target: str
    provider_id: str = ""
    pending_body_marker: str = ""
    patient_window_exhausted: bool = False

    def __post_init__(self) -> None:
        if not self.target:
            raise StallWatchError("stall watch target requires a target identity")


@dataclass(frozen=True)
class StallObservation:
    """What one pass concluded about one target.

    Carries no pane content, by the same rule :class:`StartupAdmission` follows: the whole
    record must be safe to paste into a durable journal. What the screen *matched* is a
    fixed token; what the screen *said* never leaves the classifier.
    """

    target: str
    provider_id: str
    diff: ScreenDiff
    stall_class: str
    prescription: Prescription
    matched_id: str = ""
    evidence: str = ""

    def telemetry(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "provider_id": self.provider_id,
            "stall_class": self.stall_class,
        }
        payload.update(self.diff.telemetry())
        payload.update(self.prescription.telemetry())
        if self.matched_id:
            payload["matched_id"] = self.matched_id
        if self.evidence:
            payload["evidence"] = self.evidence
        return payload


def classify_static_screen(
    *,
    provider_id: str,
    screen: str,
    diff_state: str,
    pending_body_marker: str,
    signatures: StallSignatureRegistry,
    profile_registry: object = None,
) -> tuple[str, str, str]:
    """Classify a screen that did not advance. Returns ``(class, matched_id, evidence)``.

    Pure with respect to I/O: the screen is already captured, and the startup authority is
    handed a closure over that text rather than a live read, so no second capture happens
    and no send boundary is touched.
    """
    if not screen.strip():
        return CLASS_UNKNOWN, "", ""

    admission = evaluate_startup_admission(
        provider_id=provider_id,
        read_visible=lambda: screen,
        registry=profile_registry,
    )
    if admission.outcome == ADMISSION_BLOCKED:
        return CLASS_STARTUP_INTERACTION, admission.blocker_id, ""

    if current_composer_retains_body(screen, pending_body_marker):
        return CLASS_UNSENT_COMPOSER, "", ""

    signature = first_match(signatures.for_provider(provider_id), screen)
    if signature is not None:
        return signature.asserts, signature.signature_id, signature.evidence

    if diff_state == DIFF_CHROME_ONLY:
        return CLASS_BUSY_LIKELY, "", ""
    return CLASS_UNRESPONSIVE_INDETERMINATE, "", ""


def observe_target(
    target: StallWatchTarget,
    earlier: ScreenSample,
    later: ScreenSample,
    *,
    signatures: StallSignatureRegistry,
    chrome_similarity: float = DEFAULT_CHROME_SIMILARITY,
    profile_registry: object = None,
) -> StallObservation:
    """Turn one target's two samples into an observation with a prescription."""
    diff = compare_samples(earlier, later, chrome_similarity=chrome_similarity)

    if diff.state == DIFF_INCOMPARABLE:
        stall_class, matched_id, evidence = CLASS_SCREEN_UNREADABLE, "", ""
    elif diff.advancing:
        stall_class, matched_id, evidence = CLASS_SCREEN_PROGRESSING, "", ""
    else:
        stall_class, matched_id, evidence = classify_static_screen(
            provider_id=target.provider_id,
            screen=later.normalized,
            diff_state=diff.state,
            pending_body_marker=target.pending_body_marker,
            signatures=signatures,
            profile_registry=profile_registry,
        )

    return StallObservation(
        target=target.target,
        provider_id=target.provider_id,
        diff=diff,
        stall_class=stall_class,
        prescription=prescribe(
            stall_class,
            patient_window_exhausted=target.patient_window_exhausted,
        ),
        matched_id=matched_id,
        evidence=evidence,
    )


def run_stall_watch_pass(
    targets: Sequence[StallWatchTarget],
    *,
    read_screen: Callable[[str], tuple[bool, str]],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    signatures: StallSignatureRegistry,
    interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
    chrome_similarity: float = DEFAULT_CHROME_SIMILARITY,
    profile_registry: object = None,
) -> tuple[StallObservation, ...]:
    """Sample every target twice around one interval and classify each.

    All first samples are taken, then the interval is slept **once**, then all second
    samples are taken — so N targets cost one interval, not N. That matters: a per-target
    sleep would make the watcher's cadence degrade as the cockpit grows, which is the
    property that made hand-polling unworkable in the first place.

    A read that raises is recorded as an unreadable sample rather than aborting the pass.
    One wedged target must not blind the watcher to the rest of the cockpit — and an
    unreadable sample already fails safe to ``screen_unreadable`` / no action.
    """
    if interval_seconds < 0:
        raise StallWatchError(
            f"interval_seconds must not be negative; got {interval_seconds!r}"
        )

    firsts = [_sample(target.target, read_screen, clock) for target in targets]
    if targets:
        sleep(interval_seconds)
    seconds = [_sample(target.target, read_screen, clock) for target in targets]

    return tuple(
        observe_target(
            target,
            earlier,
            later,
            signatures=signatures,
            chrome_similarity=chrome_similarity,
            profile_registry=profile_registry,
        )
        for target, earlier, later in zip(targets, firsts, seconds)
    )


def _sample(
    target: str,
    read_screen: Callable[[str], tuple[bool, str]],
    clock: Callable[[], float],
) -> ScreenSample:
    captured_at = clock()
    try:
        readable, content = read_screen(target)
    except (Exception, SystemExit):
        return ScreenSample(
            target=target, captured_at=captured_at, read_state=READ_UNREADABLE
        )
    if not readable:
        return ScreenSample(
            target=target, captured_at=captured_at, read_state=READ_UNREADABLE
        )
    return ScreenSample(
        target=target,
        captured_at=captured_at,
        content=content or "",
        read_state=READ_OK,
    )


def load_default_signatures() -> StallSignatureRegistry:
    """Lazy accessor for the packaged registry (kept out of import time for testability)."""
    from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_stall_signature import (  # noqa: E501
        load_stall_signature_registry,
    )

    return load_stall_signature_registry()


__all__ = [
    "StallObservation",
    "StallWatchError",
    "StallWatchTarget",
    "classify_static_screen",
    "load_default_signatures",
    "observe_target",
    "run_stall_watch_pass",
]
