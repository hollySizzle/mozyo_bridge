"""Per-role startup health of one session-start slot (Redmine #13948, Answer j#80989).

``session-start`` used to report a slot ``launched`` the moment ``herdr agent start``
returned a well-formed locator in the requested workspace/tab. Every one of those checks
reads the **launcher's own claim**; none of them observes the process the launcher
spawned. A provider that execs and exits ~immediately therefore left a live-looking
``launched`` + exit 0 while the pane decayed to shell residue (#13882 j#80951 / j#80968:
Claude residue, Codex live — the same installed runtime, twice, on a clean repo).

This module is the pure classifier for the missing observation. It answers one question
per slot — *did the thing we started come up, and is it the thing we started?* — and it
answers it on **three independent axes** rather than one collapsed token (Answer j#80989
Q4 / j#80990). Collapsing them is what produced the defect: `launched` meant "the start
command was accepted", and there was nowhere to say "…but nothing is running there".

- :data:`DISPOSITION_*` — what this run *did* with the slot (plan / adopt / launch /
  surface). A disposition is not a health claim.
- :data:`HEALTH_*` — what is *actually there now*, observed after the fact.
- :data:`COMPENSATION_*` — what is *owed* for the side effects this run already caused.

Fail-closed precedence (the order in :func:`classify_startup_health` is the contract):
process-level facts outrank screen facts, which outrank attestation facts. An unreadable
inventory is never "healthy" — absence of a liveness proof is not proof of liveness
(#13845 discipline). Only :data:`HEALTH_HEALTHY` is a positive success verdict; every
other token is a distinct, named, non-success cause, because "distinguish trust
interaction / provider exit / shell residue / attestation failure" is the whole point
(#13948 Acceptance 3).

The inputs are **neutral fact tokens**, not the caller's vocabulary: the application layer
maps its admission outcomes (#13760 :mod:`herdr_startup_admission`) and attestation join
states (#13637 :mod:`herdr_identity_attestation`) onto :data:`SCREEN_*` /
:data:`ATTESTATION_*`. That keeps this domain module free of application imports and keeps
the mapping explicit at one visible seam instead of implied by a shared enum.

No pane text, no paths, no env values reach any field here: a health record is put on a
structured outcome and a pasteable durable record (the #13760 j#77947 invariant 3
discipline, inherited deliberately).
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Axis 1: what this run did with the slot (never a health claim) -------------------

#: Dry-run only: the slot was classified, nothing was started.
DISPOSITION_PLANNED = "planned"
#: A pre-existing live slot this run joined (its attestation matched at classify time).
DISPOSITION_ADOPTED = "adopted"
#: This run started the process. The only disposition a rollback may ever compensate.
DISPOSITION_FRESH_LAUNCHED = "fresh_launched"
#: A pre-existing slot surfaced read-only (stale residue / unattested). Not ours to close.
DISPOSITION_SURFACED = "surfaced"

DISPOSITIONS: frozenset[str] = frozenset(
    {
        DISPOSITION_PLANNED,
        DISPOSITION_ADOPTED,
        DISPOSITION_FRESH_LAUNCHED,
        DISPOSITION_SURFACED,
    }
)

# --- Axis 2: what is actually there now ----------------------------------------------

#: The slot is live, is the process we started, its screen is clear, and it attested.
HEALTH_HEALTHY = "healthy"
#: A declared provider startup screen (trust / login / theme) is up. Never answered here.
HEALTH_STARTUP_INTERACTION = "startup_interaction_required"
#: The visible pane could not be read. A transport failure — never "startup clear".
HEALTH_RECEIVER_UNREADABLE = "receiver_unreadable"
#: The locator the launcher returned is gone from the live inventory: it started and left.
HEALTH_PROVIDER_EXITED = "provider_exited"
#: The name resolves to a positively dead shell (agent absent) — #13518 residue.
HEALTH_SHELL_RESIDUE = "shell_residue"
#: Live and screen-clear, but no self-attestation record appeared within the deadline.
HEALTH_ATTESTATION_TIMEOUT = "attestation_timeout"
#: A record exists but does not bind to this live generation / this slot's identity.
HEALTH_ATTESTATION_MISMATCH = "attestation_mismatch"
#: The durable name resolves to a locator that is not the one we launched.
HEALTH_LOCATOR_DRIFT = "locator_drift"
#: The live inventory could not be read. Fail-closed: it is not evidence of anything.
HEALTH_INVENTORY_UNREADABLE = "inventory_unreadable"
#: The provider has no profile, so its startup screens cannot be described. Never guessed.
HEALTH_UNPROFILED_PROVIDER = "unprofiled_provider"
#: This launch was not wrapped by the #13637 self-check, so no self-attestation can ever
#: exist for it. Distinct from `attestation_timeout` (which means "a record should be
#: coming and did not"): nothing is coming, and waiting the deadline would be a lie about
#: what is being waited for. An unwrapped launch is a supported fallback, so this is a
#: named non-success with a fix, never a hard failure.
HEALTH_ATTESTATION_UNAVAILABLE = "attestation_unavailable"
#: No probe was performed (dry-run, or a read-only surfacing). Not a success verdict.
HEALTH_NOT_PROBED = "not_probed"

HEALTH_OUTCOMES: frozenset[str] = frozenset(
    {
        HEALTH_HEALTHY,
        HEALTH_STARTUP_INTERACTION,
        HEALTH_RECEIVER_UNREADABLE,
        HEALTH_PROVIDER_EXITED,
        HEALTH_SHELL_RESIDUE,
        HEALTH_ATTESTATION_TIMEOUT,
        HEALTH_ATTESTATION_MISMATCH,
        HEALTH_ATTESTATION_UNAVAILABLE,
        HEALTH_LOCATOR_DRIFT,
        HEALTH_INVENTORY_UNREADABLE,
        HEALTH_UNPROFILED_PROVIDER,
        HEALTH_NOT_PROBED,
    }
)

# --- Axis 3: what this run owes for the effects it already caused ---------------------

#: Nothing was started, or everything came up healthy. No compensation exists.
COMPENSATION_NOT_NEEDED = "not_needed"
#: This run started the slot and the run did not fully succeed: an explicit public
#: rollback/replay may compensate it. session-start never closes it itself (j#80991).
COMPENSATION_ROLLBACK_OWED = "rollback_owed"
#: An explicit rollback proved this participant absent and recorded completion.
COMPENSATION_ROLLED_BACK = "rolled_back"
#: An explicit rollback refused this participant (a fence failed). Zero-close, zero-write.
COMPENSATION_ROLLBACK_BLOCKED = "rollback_blocked"
#: A rollback acted but could not prove the end state (close failed / residue / unreadable).
COMPENSATION_ROLLBACK_INCOMPLETE = "rollback_incomplete"

COMPENSATIONS: frozenset[str] = frozenset(
    {
        COMPENSATION_NOT_NEEDED,
        COMPENSATION_ROLLBACK_OWED,
        COMPENSATION_ROLLED_BACK,
        COMPENSATION_ROLLBACK_BLOCKED,
        COMPENSATION_ROLLBACK_INCOMPLETE,
    }
)

# --- Neutral input facts (the caller maps its own vocabulary onto these) --------------

#: The visible pane was not read (dry-run / the slot is already known dead).
SCREEN_NOT_PROBED = "not_probed"
#: Read, and no declared startup blocker matched.
SCREEN_CLEAR = "clear"
#: Read, and a declared startup blocker matched.
SCREEN_BLOCKED = "blocked"
#: The read failed. Distinct from clear, always.
SCREEN_UNREADABLE = "unreadable"
#: The provider has no profile to match blockers against.
SCREEN_UNPROFILED = "unprofiled"

#: No record can exist: the launch was not wrapped by the #13637 self-check.
ATTESTATION_NOT_PROBED = "not_probed"
#: A record is present and generation-bound to this live locator and identity.
ATTESTATION_OK = "ok"
#: No record at all. The only state a bounded wait may retry — the wrapper writes once,
#: before exec, so every other state is already terminal.
ATTESTATION_ABSENT = "absent"
#: A record exists but is stale / foreign / missing-env / conflicting. Terminal.
ATTESTATION_INVALID = "invalid"


class StartupHealthError(ValueError):
    """A startup-health record violates the closed contract (fail-closed)."""


@dataclass(frozen=True)
class SlotHealth:
    """One slot's three-axis startup verdict (never raises on read; validated on build).

    ``blocker_id`` is the fixed provider-profile token and is non-empty only for
    :data:`HEALTH_STARTUP_INTERACTION` — it is the only thing about a startup screen that
    may leave the pane. ``detail`` is a fixed operator sentence, never observed content.
    """

    provider: str
    assigned_name: str
    disposition: str
    health: str
    locator: str = ""
    blocker_id: str = ""
    compensation: str = COMPENSATION_NOT_NEEDED
    detail: str = ""

    def __post_init__(self) -> None:
        if self.disposition not in DISPOSITIONS:
            raise StartupHealthError(
                f"launch disposition {self.disposition!r} is not recognised; "
                f"allowed: {sorted(DISPOSITIONS)}"
            )
        if self.health not in HEALTH_OUTCOMES:
            raise StartupHealthError(
                f"startup health {self.health!r} is not recognised; "
                f"allowed: {sorted(HEALTH_OUTCOMES)}"
            )
        if self.compensation not in COMPENSATIONS:
            raise StartupHealthError(
                f"compensation {self.compensation!r} is not recognised; "
                f"allowed: {sorted(COMPENSATIONS)}"
            )
        if self.health == HEALTH_STARTUP_INTERACTION and not self.blocker_id:
            raise StartupHealthError(
                "a startup-interaction health must name the matched blocker id: it is the "
                "only thing about the screen a structured outcome may report"
            )
        if self.health != HEALTH_STARTUP_INTERACTION and self.blocker_id:
            raise StartupHealthError(
                f"startup health {self.health!r} must not carry a blocker id "
                f"(got {self.blocker_id!r})"
            )
        if self.compensation != COMPENSATION_NOT_NEEDED and (
            self.disposition != DISPOSITION_FRESH_LAUNCHED
        ):
            # Only what this run started can ever be compensated (Answer j#80989 Q1.2).
            raise StartupHealthError(
                f"compensation {self.compensation!r} is only meaningful for a slot this "
                f"run launched; {self.assigned_name!r} is {self.disposition!r}"
            )

    @property
    def healthy(self) -> bool:
        """True only for a positively observed good slot. Every other token is a cause."""
        return self.health == HEALTH_HEALTHY

    def as_payload(self) -> dict:
        return {
            "provider": self.provider,
            "assigned_name": self.assigned_name,
            "disposition": self.disposition,
            "health": self.health,
            "locator": self.locator,
            "blocker_id": self.blocker_id,
            "compensation": self.compensation,
            "detail": self.detail,
        }


#: Fixed operator sentences. Keyed by health token so the text can never disagree with
#: the verdict it explains (and so no observed content can leak into a detail).
HEALTH_DETAIL: dict[str, str] = {
    HEALTH_HEALTHY: (
        "live at the launched locator, startup screen clear, and startup "
        "self-attestation present and generation-matched"
    ),
    HEALTH_STARTUP_INTERACTION: (
        "a declared provider startup screen is waiting for an operator; mozyo never "
        "answers it (accepting a trust / login prompt is an action in the provider's UI)"
    ),
    HEALTH_RECEIVER_UNREADABLE: (
        "the visible pane could not be read, so the startup state is unknown; an "
        "unreadable pane is never read as startup-clear"
    ),
    HEALTH_PROVIDER_EXITED: (
        "the locator returned by `agent start` is no longer in the live inventory: the "
        "provider started and left"
    ),
    HEALTH_SHELL_RESIDUE: (
        "the durable name resolves to a positively dead shell (no agent detected): the "
        "provider exited and left its pane behind"
    ),
    HEALTH_ATTESTATION_TIMEOUT: (
        "live and screen-clear, but no startup self-attestation record appeared within "
        "the bounded deadline; the slot's boot identity is unverified"
    ),
    HEALTH_ATTESTATION_MISMATCH: (
        "a startup self-attestation record exists but does not bind to this live process "
        "generation / this slot's identity; a stale or foreign record is never re-used"
    ),
    HEALTH_ATTESTATION_UNAVAILABLE: (
        "this launch was not wrapped by the startup self-check, so its boot identity "
        "cannot be verified; put a `mozyo-bridge` on the launch env PATH to wrap it"
    ),
    HEALTH_LOCATOR_DRIFT: (
        "the durable name resolves to a locator other than the one `agent start` "
        "returned; refusing to treat another process as this run's launch"
    ),
    HEALTH_INVENTORY_UNREADABLE: (
        "the live inventory could not be read, so this slot cannot be observed; an "
        "unreadable inventory is never an empty or a healthy one"
    ),
    HEALTH_UNPROFILED_PROVIDER: (
        "the provider has no profile, so its startup screens cannot be described; the "
        "gate never guesses that an unknown provider has no startup screen"
    ),
    HEALTH_NOT_PROBED: "no startup probe was performed for this slot",
}


def classify_startup_health(
    *,
    inventory_readable: bool,
    row_present: bool,
    row_stale: bool,
    live_locator: str,
    launched_locator: str,
    screen: str,
    attestation: str,
) -> str:
    """Classify one fresh-launched slot from observed facts (pure, total, fail-closed).

    Precedence is deliberate and is the contract:

    1. an unreadable inventory yields :data:`HEALTH_INVENTORY_UNREADABLE` — it can prove
       nothing, and must never decay into a success;
    2. process-level facts (gone / residue / drift) outrank everything below: they say
       the thing we started is not there, which makes any screen or attestation reading
       moot — and they are the live #13882 shape;
    3. screen facts outrank attestation facts. The wrapper writes its attestation
       *before* exec (#13637), so a trust-screened agent still has a valid record;
       reporting ``attestation_*`` there would name the wrong cause;
    4. attestation last: absent (nothing arrived in the deadline) is distinct from
       invalid (something arrived and did not bind).

    Only an all-positive path reaches :data:`HEALTH_HEALTHY`.
    """
    if not inventory_readable:
        return HEALTH_INVENTORY_UNREADABLE
    if not row_present:
        return HEALTH_PROVIDER_EXITED
    if row_stale:
        return HEALTH_SHELL_RESIDUE
    if not live_locator or not launched_locator or live_locator != launched_locator:
        return HEALTH_LOCATOR_DRIFT
    if screen == SCREEN_BLOCKED:
        return HEALTH_STARTUP_INTERACTION
    if screen == SCREEN_UNREADABLE:
        return HEALTH_RECEIVER_UNREADABLE
    if screen == SCREEN_UNPROFILED:
        return HEALTH_UNPROFILED_PROVIDER
    if screen != SCREEN_CLEAR:
        # `not_probed` / anything unrecognised: an unclassified visible state is never
        # admitted (Answer j#80989 Q1.6).
        return HEALTH_RECEIVER_UNREADABLE
    if attestation == ATTESTATION_OK:
        return HEALTH_HEALTHY
    if attestation == ATTESTATION_ABSENT:
        return HEALTH_ATTESTATION_TIMEOUT
    if attestation == ATTESTATION_INVALID:
        return HEALTH_ATTESTATION_MISMATCH
    # `not_probed` (an unwrapped launch) and anything unrecognised: no record is coming.
    # Never a success — an unverifiable boot identity is the #13637 gap, not a green.
    return HEALTH_ATTESTATION_UNAVAILABLE


__all__ = (
    "ATTESTATION_ABSENT",
    "ATTESTATION_INVALID",
    "ATTESTATION_NOT_PROBED",
    "ATTESTATION_OK",
    "COMPENSATIONS",
    "COMPENSATION_NOT_NEEDED",
    "COMPENSATION_ROLLBACK_BLOCKED",
    "COMPENSATION_ROLLBACK_INCOMPLETE",
    "COMPENSATION_ROLLBACK_OWED",
    "COMPENSATION_ROLLED_BACK",
    "DISPOSITIONS",
    "DISPOSITION_ADOPTED",
    "DISPOSITION_FRESH_LAUNCHED",
    "DISPOSITION_PLANNED",
    "DISPOSITION_SURFACED",
    "HEALTH_ATTESTATION_MISMATCH",
    "HEALTH_ATTESTATION_TIMEOUT",
    "HEALTH_ATTESTATION_UNAVAILABLE",
    "HEALTH_DETAIL",
    "HEALTH_HEALTHY",
    "HEALTH_INVENTORY_UNREADABLE",
    "HEALTH_LOCATOR_DRIFT",
    "HEALTH_NOT_PROBED",
    "HEALTH_OUTCOMES",
    "HEALTH_PROVIDER_EXITED",
    "HEALTH_RECEIVER_UNREADABLE",
    "HEALTH_SHELL_RESIDUE",
    "HEALTH_STARTUP_INTERACTION",
    "HEALTH_UNPROFILED_PROVIDER",
    "SCREEN_BLOCKED",
    "SCREEN_CLEAR",
    "SCREEN_NOT_PROBED",
    "SCREEN_UNPROFILED",
    "SCREEN_UNREADABLE",
    "SlotHealth",
    "StartupHealthError",
    "classify_startup_health",
)
