"""Screen-difference primary sensor for stall detection (Redmine #15843).

The class of failure this closes has no ACK and no callback. When a provider's server
stops answering, the model never returns a turn, so nothing is emitted: no delivery
outcome changes, no durable journal appears, no runtime event fires. Every detector this
repo owns is anchored on a signal that a *working* provider produces, so all of them stay
silent forever. On 2026-08-21 the three stalls of that night (#15841 silent stall, #15789
cyber-block residue, #15842 update prompt) were each found by the owner **looking at the
pane**, and nothing else found them. The owner's reading of that evidence (issue #15843
description) is the design premise here: for this class, "is the screen changing" is the
only ground truth available, and a crude screen diff is more robust than a clever
provider-specific health parse.

What this module is, and what it deliberately is not
---------------------------------------------------
It is a **primary trigger only**. ``vibes/docs/logics/ack-completion-receiver-state.md``
(`## なぜ pane text / stdout silence を completion truth に昇格させてはいけないか`) forbids
promoting pane silence into a completion / liveness verdict, and the reason applies in
full here: a silent screen is also what reasoning, a permission wait, and a twenty-minute
test run look like. So this module answers exactly one question — *did the rendered screen
move between two samples* — and hands the answer to a classifier. It never concludes
"stalled", never names a remedy, and never sends anything.

The three-way discriminator (why not a boolean)
-----------------------------------------------
A boolean "changed / unchanged" is vacuous against a real provider TUI, in both directions:

- an idle-but-alive TUI animates (spinner frame, elapsed timer, token counter), so
  byte-equality almost never holds and a naive "unchanged" trigger would never fire;
- a similarity threshold alone collapses "the animation is still running" together with
  "the whole render loop has stopped", which are the two cases the classifier most needs
  separated.

So the sensor reports three states, keyed off the fact that *animated chrome is itself a
liveness signal*:

- :data:`DIFF_CHANGED` — similarity below the threshold. Content advanced; the unit is
  making observable progress and is not a stall candidate at all.
- :data:`DIFF_CHROME_ONLY` — similarity at or above the threshold but not exact. Something
  moved and it was small: the render loop is alive while content stands still. This is what
  legitimate busy looks like, and it is *not* by itself evidence of a stall.
- :data:`DIFF_IDENTICAL` — byte-identical after normalisation. Not even the animation
  advanced. This is the strong signal, and it is what makes a short sampling interval
  sufficient: a swallowed Enter, an unanswered startup screen, and a dead render loop are
  all fully static within seconds, whereas a working provider is not.

That distinction is why the default interval is one bounded-cadence tick rather than the
"1 分間" of the original proposal: duration is not what separates a stalled screen from a
busy one — *whether the chrome moves* is. Duration still matters for escalation, but that
is a durable-record question owned by the caller, not a property of two samples.

Known calibration limit, and which way it fails
-----------------------------------------------
Similarity is computed over the whole screen, so the same counter tick is a large relative
change on a nearly-empty pane and a negligible one on a full pane. A short screen whose
only movement is its own chrome can therefore land on :data:`DIFF_CHANGED` instead of
:data:`DIFF_CHROME_ONLY`. That direction is deliberate and is the safe one: the sensor
under-triggers on a small screen (reporting progress, prescribing nothing) rather than
over-triggering. The opposite error — a busy pane read as frozen — is bounded by the fact
that every prescription downstream is present-only, but it is still the one worth avoiding,
and this is why the threshold is an argument rather than a constant.

Purity: no clock, no transport, no provider data. Samples are handed in already captured
(the caller owns the read primitive and the cadence), and every threshold is an argument.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

#: The rendered screen advanced: similarity fell below the threshold.
DIFF_CHANGED = "changed"
#: Small movement only (animation / counters) while content stood still.
DIFF_CHROME_ONLY = "chrome_only"
#: Byte-identical after normalisation — not even the animation advanced.
DIFF_IDENTICAL = "identical"
#: One or both samples could not be read, so no comparison exists.
DIFF_INCOMPARABLE = "incomparable"

#: Closed vocabulary of :attr:`ScreenDiff.state`.
DIFF_STATES: frozenset[str] = frozenset(
    {DIFF_CHANGED, DIFF_CHROME_ONLY, DIFF_IDENTICAL, DIFF_INCOMPARABLE}
)

#: States in which the screen did not advance. Membership here is a *trigger*, never a
#: verdict — :mod:`...domain.stall_disposition` decides what (if anything) it means.
NON_ADVANCING_STATES: frozenset[str] = frozenset({DIFF_CHROME_ONLY, DIFF_IDENTICAL})

#: Similarity at or above which the movement counts as chrome rather than content.
#: 0.98 admits a spinner frame, an elapsed-seconds counter and a token counter on a
#: full-height pane (a few characters out of a couple of thousand) while a single new
#: line of output lands well below it. Tunable per call; this is only the default.
DEFAULT_CHROME_SIMILARITY = 0.98

#: Seconds between the two samples of one pass. Matches the operator's bounded-wait
#: watcher cadence rather than introducing a second, competing rhythm.
DEFAULT_SAMPLE_INTERVAL_SECONDS = 50.0

#: Why a sample carries no content. ``READ_OK`` is the only readable value.
READ_OK = "ok"
READ_UNREADABLE = "unreadable"


class PaneStallSensorError(ValueError):
    """Raised when a sample or threshold is structurally invalid."""


def normalize_screen(content: str) -> str:
    """Collapse render noise that carries no information about progress.

    Trailing whitespace and blank trailing lines are terminal padding: a redraw can add
    or drop them without anything happening. Nothing else is touched — in particular no
    provider-specific region is stripped, because the moment this function needs to know
    which columns hold a spinner it has become the fragile provider-specific parse the
    owner's premise rejects.
    """
    lines = [line.rstrip() for line in content.replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


@dataclass(frozen=True)
class ScreenSample:
    """One read-only capture of a target's rendered screen.

    ``content`` is never re-exported by this module's outputs. It exists so the diff can
    be computed and so the classifier can be handed the *later* sample; the sensor's own
    results carry ratios and state tokens only.
    """

    target: str
    captured_at: float
    content: str = ""
    read_state: str = READ_OK

    def __post_init__(self) -> None:
        if not self.target:
            raise PaneStallSensorError("screen sample requires a target")
        if self.read_state not in (READ_OK, READ_UNREADABLE):
            raise PaneStallSensorError(
                f"screen sample read_state {self.read_state!r} is not a known value"
            )
        if self.read_state == READ_UNREADABLE and self.content:
            raise PaneStallSensorError(
                "an unreadable screen sample must not carry content"
            )

    @property
    def readable(self) -> bool:
        return self.read_state == READ_OK

    @property
    def normalized(self) -> str:
        return normalize_screen(self.content)


@dataclass(frozen=True)
class ScreenDiff:
    """The comparison of two samples of the same target.

    ``elapsed_seconds`` is reported, not judged. It lets a caller record how wide the
    observation window actually was without this module deciding that some width means
    "stalled" — the duration threshold is a durable-record question (see the module
    docstring), and encoding one here would smuggle a verdict into the sensor.
    """

    target: str
    state: str
    similarity: float
    elapsed_seconds: float

    def __post_init__(self) -> None:
        if self.state not in DIFF_STATES:
            raise PaneStallSensorError(f"unknown screen diff state {self.state!r}")

    @property
    def advancing(self) -> bool:
        return self.state == DIFF_CHANGED

    @property
    def triggers_classification(self) -> bool:
        """True when the screen did not advance, so a classifier pass is warranted."""
        return self.state in NON_ADVANCING_STATES

    def telemetry(self) -> dict[str, object]:
        return {
            "target": self.target,
            "screen_diff": self.state,
            "similarity": round(self.similarity, 4),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


def compare_samples(
    earlier: ScreenSample,
    later: ScreenSample,
    *,
    chrome_similarity: float = DEFAULT_CHROME_SIMILARITY,
) -> ScreenDiff:
    """Compare two samples of one target into a :class:`ScreenDiff`.

    Fail-safe direction: an unreadable sample yields :data:`DIFF_INCOMPARABLE`, never
    :data:`DIFF_IDENTICAL`. "Could not read the screen" is evidence in neither direction
    — the same rule the send-time admission gate applies to an unreadable pane, and the
    same one ``ack-completion-receiver-state.md`` states for missing evidence.
    """
    if not 0.0 < chrome_similarity <= 1.0:
        raise PaneStallSensorError(
            f"chrome_similarity must be in (0, 1]; got {chrome_similarity!r}"
        )
    if earlier.target != later.target:
        raise PaneStallSensorError(
            f"cannot diff different targets: {earlier.target!r} vs {later.target!r}"
        )
    elapsed = later.captured_at - earlier.captured_at
    if elapsed < 0:
        raise PaneStallSensorError("the later sample must not precede the earlier one")

    if not (earlier.readable and later.readable):
        return ScreenDiff(
            target=later.target,
            state=DIFF_INCOMPARABLE,
            similarity=0.0,
            elapsed_seconds=elapsed,
        )

    before = earlier.normalized
    after = later.normalized
    if before == after:
        return ScreenDiff(
            target=later.target,
            state=DIFF_IDENTICAL,
            similarity=1.0,
            elapsed_seconds=elapsed,
        )

    similarity = SequenceMatcher(None, before, after).ratio()
    state = DIFF_CHROME_ONLY if similarity >= chrome_similarity else DIFF_CHANGED
    return ScreenDiff(
        target=later.target,
        state=state,
        similarity=similarity,
        elapsed_seconds=elapsed,
    )
