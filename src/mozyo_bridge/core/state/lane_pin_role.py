"""The canonical declared-pin role vocabulary and its fail-closed pair resolution (#13920).

``ProcessGenerationPin.role`` names WHICH SLOT of a lane's gateway+worker pair a pin
describes. Nothing in :mod:`...lane_lifecycle_model` constrains it: the pin model requires
``role`` to be non-empty and rejects a duplicate ``(role, provider, assigned_name)``, but it
never checks the *vocabulary*. So a writer and a reader that disagree on the spelling do not
error — the reader's ``by_role.get("gateway")`` simply misses a pin written as ``"codex"``
and reads the row as **pin-less** (Redmine #13920: an adopted lane hibernates, and
``sublane recover-pair`` blocks on ``hibernated_record_missing_pins`` although the row's
pins are right there).

This module is the ONE boundary that owns that vocabulary, so the spelling is not a fact
each call site re-derives from whichever constant it happened to import:

- :data:`PIN_ROLE_GATEWAY` / :data:`PIN_ROLE_WORKER` are the **canonical** spellings every
  new write uses.
- :func:`canonical_pin_role` additionally **read-accepts** the legacy ``codex`` / ``claude``
  spelling the #13809 adopt/backfill writer shipped, so an existing exact legacy row stays
  recoverable without a migrating write (the #13844 / #13882 read-compat discipline: reads
  widen, writes stay canonical, and no shared-home row is rewritten as a side effect of
  being read). ``sublane repair-pins`` (#13879) remains the explicit re-declaration rail.

**Why the legacy spelling is not simply a provider name.** ``codex`` / ``claude`` are also
valid *provider* ids, and that coincidence is what hid this bug. They are not read as
providers here. The #13809 writer pins ``role=GATEWAY_ROLE`` for the gateway slot whatever
provider fills it — under a swapped binding it writes ``(role="codex", provider="claude")``
— so in a *pin role* position ``codex`` means "the gateway slot", never "the codex process".
The pin's own ``provider`` field carries the provider. Nothing here consults a binding.

**Why non-empty pins are not proof** (Redmine #13920 正規化した意図). A row can be
non-empty and still not name an unambiguous pair, so :func:`resolve_declared_pin_pair`
returns a fail-closed reason rather than a best guess for every such shape: a role from
neither vocabulary (:data:`PIN_PAIR_FOREIGN`), both vocabularies in one row — evidence the
row was written by two disagreeing paths, so its provenance is not established
(:data:`PIN_PAIR_MIXED`), two pins collapsing onto one canonical slot
(:data:`PIN_PAIR_DUPLICATE`, which the model's ``stable_identity`` dedupe does NOT catch:
``codex`` and ``gateway`` are distinct identities there), and a half pair
(:data:`PIN_PAIR_INCOMPLETE`). The caller closes / sends nothing on any of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from mozyo_bridge.core.state.lane_lifecycle_model import ProcessPinError, norm

# -- the canonical vocabulary (what every new write spells) -------------------------------

#: The pair's gateway slot — the pane governed kinds are routed to.
PIN_ROLE_GATEWAY = "gateway"
#: The pair's worker slot — the same-lane implementer.
PIN_ROLE_WORKER = "worker"

#: Both canonical slots, gateway first (the stable pair order).
CANONICAL_PIN_ROLES = (PIN_ROLE_GATEWAY, PIN_ROLE_WORKER)

#: The pre-#13920 spelling the #13809 adopt/backfill writer shipped, mapped onto the slot it
#: named. READ-ONLY compatibility: no new write emits these, and reading one never rewrites
#: the row (see the module docstring on why these are slot names here, not provider ids).
LEGACY_PIN_ROLES = {
    "codex": PIN_ROLE_GATEWAY,
    "claude": PIN_ROLE_WORKER,
}

# -- pair-resolution outcome vocabulary (a reason token, never an exception) ---------------

#: The row names an unambiguous gateway+worker pair.
PIN_PAIR_OK = ""
#: The row declares no pins at all (a create-time issue lane, or the v4->v5 pins-only gap).
PIN_PAIR_ABSENT = "declared_pins_absent"
#: The declared-slots snapshot did not decode (corrupt / newer envelope): fail closed.
PIN_PAIR_UNREADABLE = "declared_pins_unreadable"
#: A pin carries a role from neither vocabulary — a foreign / unknown slot.
PIN_PAIR_FOREIGN = "foreign_pin_role"
#: One row mixes canonical and legacy spellings: two disagreeing writers touched it.
PIN_PAIR_MIXED = "mixed_pin_role_vocabulary"
#: Two pins resolve onto the SAME canonical slot (e.g. ``codex`` + ``gateway``).
PIN_PAIR_DUPLICATE = "duplicate_pin_role"
#: The row names only one half of the pair.
PIN_PAIR_INCOMPLETE = "incomplete_pin_pair"

#: The vocabulary a resolved row was written in (reported, never acted on).
PIN_VOCABULARY_CANONICAL = "canonical"
PIN_VOCABULARY_LEGACY = "legacy"


def canonical_pin_role(raw: object) -> str:
    """The canonical slot ``raw`` names, or ``""`` when it names neither slot.

    Accepts a canonical spelling as itself and a legacy one by :data:`LEGACY_PIN_ROLES`.
    An empty / foreign / unknown role reads ``""`` so the caller fails closed rather than
    guessing which slot an unrecognized pin belongs to.
    """
    role = norm(raw)
    if role in CANONICAL_PIN_ROLES:
        return role
    return LEGACY_PIN_ROLES.get(role, "")


@dataclass(frozen=True)
class DeclaredPinPair:
    """The resolved gateway+worker pins, or the fail-closed reason there is no pair.

    ``gateway`` / ``worker`` are the caller's own pin objects (unmodified — this boundary
    reads roles, it never rewrites a pin). ``vocabulary`` reports which spelling the row was
    written in, for the operator record; it is evidence, not a branch condition.
    """

    gateway: Optional[Any] = None
    worker: Optional[Any] = None
    reason: str = PIN_PAIR_ABSENT
    vocabulary: str = ""

    @property
    def ok(self) -> bool:
        """Did the row name an unambiguous pair? (both slots present, no fail-closed reason)"""
        return (
            self.reason == PIN_PAIR_OK
            and self.gateway is not None
            and self.worker is not None
        )

    @property
    def is_legacy(self) -> bool:
        """Was this pair read through the legacy spelling? (a repair-pins candidate)"""
        return self.ok and self.vocabulary == PIN_VOCABULARY_LEGACY


def resolve_declared_pin_pair(pins: Sequence[Any]) -> DeclaredPinPair:
    """Resolve a declared-pin set into an unambiguous pair, or a fail-closed reason (pure).

    The single place a consumer of the pair asks "which pin is the gateway, which is the
    worker?". Every shape that does not name exactly one of each — including a NON-EMPTY one
    — returns a reason and no pins, so "the row has pins" is never itself the proof (Redmine
    #13920 items 2/3). See the module docstring for why each shape fails closed.
    """
    declared = tuple(pins or ())
    if not declared:
        return DeclaredPinPair(reason=PIN_PAIR_ABSENT)

    # Pass 1 — vocabulary. A foreign role is unresolvable; a row mixing both spellings has no
    # single writer to attribute it to, so neither is read as authoritative (checked BEFORE
    # the slot dedupe below, so a `codex` + `gateway` row reports the more precise `mixed`).
    vocabularies: set[str] = set()
    for pin in declared:
        raw = norm(getattr(pin, "role", ""))
        if not canonical_pin_role(raw):
            return DeclaredPinPair(reason=PIN_PAIR_FOREIGN)
        vocabularies.add(
            PIN_VOCABULARY_LEGACY if raw in LEGACY_PIN_ROLES else PIN_VOCABULARY_CANONICAL
        )
    if len(vocabularies) != 1:
        return DeclaredPinPair(reason=PIN_PAIR_MIXED)

    # Pass 2 — slots. Two pins on one canonical slot are ambiguous: the pin model's
    # `stable_identity` dedupe cannot see it (it compares the RAW role, so `codex` and
    # `gateway` are two identities), and picking either would be a guess.
    by_role: dict[str, Any] = {}
    for pin in declared:
        role = canonical_pin_role(getattr(pin, "role", ""))
        if role in by_role:
            return DeclaredPinPair(reason=PIN_PAIR_DUPLICATE)
        by_role[role] = pin

    gateway = by_role.get(PIN_ROLE_GATEWAY)
    worker = by_role.get(PIN_ROLE_WORKER)
    if gateway is None or worker is None:
        return DeclaredPinPair(reason=PIN_PAIR_INCOMPLETE)
    return DeclaredPinPair(
        gateway=gateway,
        worker=worker,
        reason=PIN_PAIR_OK,
        vocabulary=vocabularies.pop(),
    )


def read_declared_pin_pair(record: Any) -> DeclaredPinPair:
    """:func:`resolve_declared_pin_pair` over a lifecycle record's ``declared_pins``.

    ``declared_pins`` decodes the stored snapshot and **raises** on a corrupt / newer-version
    envelope (the :func:`...decode_declared_slots` fail-closed contract). That raise is an
    unreadable row, not a crash for the caller to handle ad hoc, so it is reported as
    :data:`PIN_PAIR_UNREADABLE` — the same "no pair" verdict shape as every other reason.
    """
    try:
        pins = getattr(record, "declared_pins", ())
    except (ProcessPinError, ValueError):
        return DeclaredPinPair(reason=PIN_PAIR_UNREADABLE)
    return resolve_declared_pin_pair(pins)


__all__ = (
    "CANONICAL_PIN_ROLES",
    "DeclaredPinPair",
    "LEGACY_PIN_ROLES",
    "PIN_PAIR_ABSENT",
    "PIN_PAIR_DUPLICATE",
    "PIN_PAIR_FOREIGN",
    "PIN_PAIR_INCOMPLETE",
    "PIN_PAIR_MIXED",
    "PIN_PAIR_OK",
    "PIN_PAIR_UNREADABLE",
    "PIN_ROLE_GATEWAY",
    "PIN_ROLE_WORKER",
    "PIN_VOCABULARY_CANONICAL",
    "PIN_VOCABULARY_LEGACY",
    "canonical_pin_role",
    "read_declared_pin_pair",
    "resolve_declared_pin_pair",
)
