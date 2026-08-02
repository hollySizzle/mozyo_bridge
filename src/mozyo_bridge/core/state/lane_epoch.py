"""Lane epoch — the clock-free, locator-free generation proof (Redmine #14756).

``sublane resume`` must prove that a relaunched pair is a GENUINE post-hibernate process
generation. Four authorities have now been tried for that proof, and the first three each
failed because they asked a question the row could not actually answer:

1. the **timestamp** boundary (``hibernated_at``, schema v8) — defeated by a backdated CAS
   stamp, a regressed host clock, or an ``observed_at`` the attesting process writes about
   itself (Redmine #14477 review j#94531 R2-F1, disposition j#94544 A.3);
2. the **caller-supplied release pins** — defeated because ``request_release`` accepted any
   pin list, so a caller could record locators that were never live (review j#94570 R3-F1);
3. the **released-locator fence** over a driver-derived observation (schema v9,
   :mod:`...lane_released_locator_fence`) — sound, but it proves the wrong thing when the
   evidence is missing and it refuses in the safe direction on tmux pane-id REUSE, so
   ``release_evidence_absent`` / ``released_locator_reuse`` are permanent fail-closed states
   for a lane that did nothing wrong;
4. **this** surface: a monotonic integer the STORE mints, injected into a launch's process
   environment, and self-attested by the process that received it.

Why an epoch closes what the other three cannot. The defect shape common to (1) and (2) is
that a *caller* supplied the value the proof rested on, so no amount of cross-checking made
it authority. Here nothing is supplied: the lifecycle store advances ``lane_epoch`` with
``lane_epoch = lane_epoch + 1`` evaluated by SQLite against the row's OWN stored value,
inside the same guarded transaction as the hibernate disposition CAS. There is no parameter
to backdate, no clock to roll back, and no second value to reconcile.

Why a survivor cannot hold a fresh epoch. The epoch reaches a process exactly once — as an
environment variable injected at ``herdr agent start``. A live process's environment is
immutable to every other process (POSIX), which is the same fact that forced #13637's
self-attestation design: nothing outside the process can write it, and the process itself
cannot be handed a new one without being relaunched. So a pane that **survived** hibernate's
release still carries the epoch that was current when it booted — necessarily an epoch minted
BEFORE the hibernate transition advanced the counter.

**The admission rule, and why it is stated two ways.** Redmine #14756 acceptance 3 requires
the attested epoch to be *strictly newer than the hibernate epoch*. This module stores ONE
counter and derives both quantities from it, rather than storing a second column that is
always ``lane_epoch - 1`` and can only drift out of agreement with the first (the #14477
R7 lesson: one stored fact classified by two surfaces is eventually classified two ways):

- :func:`hibernate_boundary_epoch` — the epoch the RELEASED generation held, ``lane_epoch - 1``;
- :func:`required_resume_epoch` — the epoch a fresh generation must carry, ``lane_epoch``.

so ``attested > hibernate_boundary_epoch`` and ``attested >= required_resume_epoch`` are the
same predicate, and :func:`lane_epoch_verdict` computes it once. Both spellings are exported
so a caller states whichever half it means without re-deriving the arithmetic.

**Zero is absence, never a boundary.** A row migrated from a pre-v10 build, or one that has
never hibernated under a v10 build, carries ``0``. That is *no epoch has ever been minted*,
which is not the same as "an epoch of zero" and must never be treated as a threshold every
positive epoch clears — that would admit any attested pair at all, the exact inverse of a
generation proof. It resolves to :data:`EPOCH_AUTHORITY_UNAVAILABLE` and the caller fails
closed, exactly as the v8 anchor does for a missing ``hibernated_at`` (review j#94515 /
verdict j#94520: there is no safe substitute, and a generation proof may not guess). The
operational cost is stated plainly: a lane hibernated by a pre-#14756 build resumes only
after it passes through a v10 hibernate transition.

**The attested token is classified by its exact bytes.** The epoch arrives back from the
agent as a string it read out of its own environment, so it is parsed, never normalised: the
canonical form is precisely ``str(n)`` for an integer ``n >= 1`` — no sign, no whitespace, no
leading zero, no underlying-type coercion. ``" 7"``, ``"+7"``, ``"07"`` and ``"7.0"`` are
*not* the epoch 7; they are tokens no canonical producer could have written, and folding them
into 7 would launder a value the store never minted into the authority position (#14477 R6-F1
/ R8-F2: normalising a stored authority value fabricates a canonical fact the storage does not
hold). They classify as :data:`EPOCH_MALFORMED` and fail closed.

Pure: no IO, no clock, no environment. Every function is total and returns a closed token.
"""

from __future__ import annotations

from typing import Optional

#: The lifecycle column this module owns the semantics of (schema v10). Named here so the
#: schema, the single CAS writer and the resume reader all spell one authority the same way.
LANE_EPOCH_COLUMN = "lane_epoch"

#: The environment variable a managed launch injects the minted epoch through, and the one
#: the #13637 startup self-check reads out of its OWN process env. Kept beside the semantics
#: rather than in the launch adapter so the producer and the consumer cannot drift apart.
MOZYO_LANE_EPOCH_ENV = "MOZYO_LANE_EPOCH"

#: The stored value meaning **no epoch has ever been minted for this lane**. Deliberately not
#: a usable threshold: see this module's docstring.
LANE_EPOCH_UNMINTED = 0

# --- Verdict vocabulary (closed; a consumer branches on these). ------------------------
#: Both halves resolved and the attested epoch is at least the required one — i.e. strictly
#: newer than the epoch the released generation held. The ONLY passing token.
EPOCH_OK = "lane_epoch_ok"
#: The LIFECYCLE row cannot state a required epoch: there is no row, or its ``lane_epoch``
#: is unminted (a pre-v10 hibernation, or a lane hibernated by an older build). The proof
#: cannot run at all; it is never treated as "nothing to clear, therefore fresh". The
#: operator's next rail is a v10 hibernate transition, not a substitute value.
EPOCH_AUTHORITY_UNAVAILABLE = "lane_epoch_authority_unavailable"
#: The ATTESTATION carries no epoch: a legacy (v1/v2) attestation row, or a launch by a
#: runtime that predates the epoch injection. Absence of evidence is not freshness.
EPOCH_ATTESTATION_ABSENT = "lane_epoch_attestation_absent"
#: The attested token is present but is not a canonical epoch (signed, padded, spaced,
#: non-decimal, zero, negative, or otherwise a token no producer could have written). It is
#: NOT coerced to the nearest integer — see this module's docstring.
EPOCH_MALFORMED = "lane_epoch_malformed"
#: Both halves resolved and the attested epoch is NOT newer than the boundary — the defining
#: signature of a pre-hibernate process, i.e. a survivor of the release.
EPOCH_NOT_NEWER = "lane_epoch_not_newer"


#: A stored ``lane_epoch`` of exactly int ``0`` — the counter has never been minted.
EPOCH_STORED_UNMINTED = "stored_unminted"
#: A stored ``lane_epoch`` that is an exact positive int — a real generation counter.
EPOCH_STORED_MINTED = "stored_minted"
#: A stored ``lane_epoch`` that is NOT a value this store's writer can have produced: TEXT,
#: REAL, ``bool``, ``NULL`` or a negative int. It is not zero, and must never be treated as
#: zero by anything that WRITES (Redmine #14756 j#96881 F2).
EPOCH_STORED_MALFORMED = "stored_malformed"


def classify_stored_epoch(value: object) -> tuple[int, str]:
    """``(epoch, state)`` for a row's raw ``lane_epoch`` — malformed is its own answer.

    The distinction this exists to preserve: **"never minted" and "unreadable" are different
    facts**, and only one of them may be advanced from. Folding them together is safe on the
    read side (both mean "cannot prove a generation") and unsafe on the write side, which is
    exactly the asymmetry j#96881 F2 measured: because the old helper answered ``0`` for
    ``'corrupt'``, ``-7``, ``2.5``, ``True`` and ``NULL`` alike, a hibernate CAS minted ``1``
    from every one of them. That is a counter ROLLBACK — it re-issues an epoch that some
    already-released generation may still hold in its environment, resurrecting precisely the
    survivor admission this module exists to close. A negative value is malformed for the same
    reason rather than merely "small": the counter only ever increments from zero, so a
    negative one is evidence the row is corrupt, not evidence of an early generation.

    ``bool`` is excluded and a REAL is rejected rather than truncated, for the reason
    ``lane_lifecycle_schema._recorded_version`` documents: ``int(2.5) == 2`` would walk a
    value the store never wrote straight through a threshold comparison.

    The returned integer is meaningful ONLY for :data:`EPOCH_STORED_MINTED`; for the other
    two it is :data:`LANE_EPOCH_UNMINTED` and must not be compared against.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return LANE_EPOCH_UNMINTED, EPOCH_STORED_MALFORMED
    if value < LANE_EPOCH_UNMINTED:
        return LANE_EPOCH_UNMINTED, EPOCH_STORED_MALFORMED
    if value == LANE_EPOCH_UNMINTED:
        return LANE_EPOCH_UNMINTED, EPOCH_STORED_UNMINTED
    return value, EPOCH_STORED_MINTED


def _stored_epoch(value: object) -> int:
    """The READ-side projection: a minted epoch, else :data:`LANE_EPOCH_UNMINTED`.

    Deliberately collapses malformed onto unminted, because a reader's only question is "can
    this row prove a generation?" and the answer for both is no — a corrupt row fails closed
    through :data:`EPOCH_AUTHORITY_UNAVAILABLE` exactly as an unminted one does.

    **Writers must not use this.** They need :func:`classify_stored_epoch`, whose whole point
    is that advancing from a value you could not read is not the same as advancing from zero.
    """
    epoch, _state = classify_stored_epoch(value)
    return epoch


def required_resume_epoch(record: Optional[object]) -> tuple[int, str]:
    """``(epoch, authority)`` — the epoch a fresh generation must carry to resume ``record``.

    ``authority`` is :data:`EPOCH_OK` when the row minted one, else
    :data:`EPOCH_AUTHORITY_UNAVAILABLE` with a zero epoch the caller must NOT compare
    against. Reading the returned integer without checking the authority token would treat
    an unminted row as a threshold every epoch clears.
    """
    if record is None:
        return LANE_EPOCH_UNMINTED, EPOCH_AUTHORITY_UNAVAILABLE
    stored = _stored_epoch(getattr(record, LANE_EPOCH_COLUMN, LANE_EPOCH_UNMINTED))
    if stored == LANE_EPOCH_UNMINTED:
        return LANE_EPOCH_UNMINTED, EPOCH_AUTHORITY_UNAVAILABLE
    return stored, EPOCH_OK


def hibernate_boundary_epoch(record: Optional[object]) -> tuple[int, str]:
    """``(epoch, authority)`` — the epoch the RELEASED generation held (``lane_epoch - 1``).

    The literal reading of acceptance 3's "strictly newer than the hibernate epoch". It is
    derived from the same single stored counter as :func:`required_resume_epoch`, so the two
    can never disagree about one lane. A lane whose first-ever hibernate minted epoch 1 has a
    boundary of 0: its pre-hibernate panes were launched while the counter was unminted and
    carry no epoch at all, which :data:`EPOCH_ATTESTATION_ABSENT` refuses on its own.
    """
    required, authority = required_resume_epoch(record)
    if authority != EPOCH_OK:
        return LANE_EPOCH_UNMINTED, authority
    return required - 1, EPOCH_OK


def parse_attested_epoch(raw: object) -> tuple[int, str]:
    """``(epoch, reason)`` for a token an agent attested out of its own environment.

    ``reason`` is :data:`EPOCH_OK` (with the parsed epoch), :data:`EPOCH_ATTESTATION_ABSENT`
    (nothing recorded), or :data:`EPOCH_MALFORMED`. Canonical is exactly ``str(n)`` for
    ``n >= 1``: the round-trip check ``str(int(token)) == token`` rejects every non-canonical
    spelling of the same number — leading zeros, a ``+`` sign, surrounding whitespace,
    underscore separators — in one predicate that cannot drift from the producer's own
    ``str(epoch)`` rendering. Nothing is trimmed first: a padded token is a token no producer
    wrote, and trimming it would manufacture agreement the record does not carry.
    """
    if raw is None:
        return LANE_EPOCH_UNMINTED, EPOCH_ATTESTATION_ABSENT
    if isinstance(raw, bool) or not isinstance(raw, str):
        # Only a string is ever stored (the agent attests a raw env token). Anything else is
        # a decode error, not an epoch — and `True` must never read as 1.
        return LANE_EPOCH_UNMINTED, EPOCH_MALFORMED
    if raw == "":
        return LANE_EPOCH_UNMINTED, EPOCH_ATTESTATION_ABSENT
    # `str.isdigit()` accepts non-ASCII decimal digits (e.g. Arabic-Indic), which `int()`
    # then happily parses — so the ASCII check is explicit rather than delegated. Redmine
    # #14753 closed the same class of hole on a different surface.
    if not all("0" <= character <= "9" for character in raw):
        return LANE_EPOCH_UNMINTED, EPOCH_MALFORMED
    parsed = int(raw)
    if str(parsed) != raw or parsed < 1:
        # Non-canonical spelling ("07"), or a non-positive epoch the store never mints.
        return LANE_EPOCH_UNMINTED, EPOCH_MALFORMED
    return parsed, EPOCH_OK


def lane_epoch_verdict(
    record: Optional[object], attested_raw: object
) -> tuple[bool, str]:
    """``(ok, reason)`` — may ``attested_raw`` be a post-hibernate generation of ``record``?

    ``ok`` is True only for :data:`EPOCH_OK`. Precedence is fail-closed and names the half
    that failed, so an operator can tell "this lane cannot prove anything" (upgrade the
    lane through a v10 hibernate) apart from "this process cannot prove anything" (relaunch
    it) apart from "this process IS the survivor" (close it):

    1. the row cannot state a requirement -> :data:`EPOCH_AUTHORITY_UNAVAILABLE`;
    2. the attestation carries no / a malformed epoch -> its own token;
    3. the attested epoch is not newer than the boundary -> :data:`EPOCH_NOT_NEWER`;
    4. otherwise :data:`EPOCH_OK`.

    The authority half is checked FIRST and deliberately: reporting a *process* problem for a
    lane that could never have stated a requirement would send an operator to relaunch panes
    that are already correct.
    """
    required, authority = required_resume_epoch(record)
    if authority != EPOCH_OK:
        return False, authority
    attested, reason = parse_attested_epoch(attested_raw)
    if reason != EPOCH_OK:
        return False, reason
    if attested < required:
        # Equivalently `attested <= hibernate_boundary_epoch(record)`: the process was handed
        # its epoch before the hibernate transition advanced the counter.
        return False, EPOCH_NOT_NEWER
    return True, EPOCH_OK


def lane_epoch_on_transition(
    current: object, *, target: str, hibernated: str
) -> Optional[int]:
    """This row's ``lane_epoch`` after a disposition CAS to ``target``, or ``None`` to refuse.

    ``None`` means **the stored value is malformed and this CAS must not write at all**
    (Redmine #14756 j#96881 F2). It is returned for both branches, and the non-hibernate one
    matters just as much as the minting one: this function's contract below says a non-minting
    target preserves the stored epoch "byte for byte", and the previous implementation did not
    — it wrote ``0`` back over ``'corrupt'``, silently converting an unreadable row into one
    that reads as a legitimate never-minted lane, which the adoption rail would then happily
    mint to ``1``. Laundering by way of an unrelated transition is still laundering.

    Advancing INTO ``hibernated`` mints the next epoch; every other target preserves the
    stored one **byte for byte**. There is no reset case, and that asymmetry against the
    sibling ``hibernated_at`` (which CLEARS on the way back to ``active``) is the whole
    correctness property, not an oversight:

    - ``hibernated_at`` is a *boundary in force*, meaningless for an awake lane, and an
      un-cleared stale one would be a threshold in the past — i.e. a LOOSER gate;
    - ``lane_epoch`` is a *counter*, and resetting it would re-mint epochs a previous
      generation's processes already hold. A pane released at epoch 3 would match a lane
      whose counter restarted and climbed back to 3 — resurrecting the survivor-admission
      this module exists to close.

    So the callers that clear the release axis on the way to ``active``
    (``supersede_and_activate``, ``open_next_generation``) deliberately omit this column from
    their UPDATE column lists rather than writing a cleared value through it (#14477 R4-F1
    enumerated the writers that must RESET a new field; this is the inverse obligation — the
    writers that must NOT).
    """
    stored, state = classify_stored_epoch(current)
    if state == EPOCH_STORED_MALFORMED:
        return None
    if target == hibernated:
        return stored + 1
    return stored


__all__ = (
    "EPOCH_ATTESTATION_ABSENT",
    "EPOCH_AUTHORITY_UNAVAILABLE",
    "EPOCH_MALFORMED",
    "EPOCH_NOT_NEWER",
    "EPOCH_OK",
    "EPOCH_STORED_MALFORMED",
    "EPOCH_STORED_MINTED",
    "EPOCH_STORED_UNMINTED",
    "LANE_EPOCH_COLUMN",
    "LANE_EPOCH_UNMINTED",
    "MOZYO_LANE_EPOCH_ENV",
    "classify_stored_epoch",
    "hibernate_boundary_epoch",
    "lane_epoch_on_transition",
    "lane_epoch_verdict",
    "parse_attested_epoch",
    "required_resume_epoch",
)
