"""The whole-plan launch preflight (Redmine #13647 Tranche 2, Design Answer j#85645).

Tranche 1 fixed a lane's pair *geometry* (which way the panes split, by lane-role kind).
This is the other half of the design's authority model: the **per-slot** plan — which
workflow role each slot carries, which named profile that role selects, which provider runs
it, and with which argv — resolved and validated ONCE, before the launch takes its first
irreversible step.

Why a whole-plan type rather than per-slot checks inside the launch loop: the launch
prepares a *pair*. A per-slot validation that fires while the second slot is being launched
leaves the first one already live — a partial lane, which is exactly the failure mode the
existing session-start preflights exist to prevent. Cross-slot defects (two slots claiming
the same workflow role, two entries for one physical slot, the same slot asked for two
different profiles) are only visible when the whole plan is in hand, so the plan is the unit
of validation and every defect below is a **typed zero-start**: no workspace, no tab, no
agent, no startup action.

Authority boundary (Design Answer j#85645, j#84266 — kept enforced by what this module does
NOT do):

- ``workflow_role`` / ``profile_id`` are **plan-only**. They never become an mzb1 assigned
  name, ``MOZYO_AGENT_ROLE`` (a provider token), or a route / attestation / retire identity —
  those stay provider-token-bound, and nothing here writes or reads them.
- roles are never *inferred*. A role is supplied by a caller that resolved it from durable
  governance, or the plan is not built at all (the pre-#13647 launch, unchanged). A supplied
  role that is not in the known vocabulary is a refusal, never a fallback.
- the plan's provenance is a durable :class:`DecisionPointer` — the SAME anchor vocabulary
  the lifecycle authority record stores, not a parallel one. Zero anchors (unresolved) or
  more than one distinct anchor (ambiguous / contradicting governance) both refuse: the
  design's "矛盾/複数 anchor で ambiguous なら guess せず zero-start".

Pure: frozen dataclasses, a typed error, and total functions over injected vocabularies
(known providers / known roles are passed in, never imported from an adapter). No I/O, no
config read, no registry lookup — the composition root supplies those, which is what lets
the whole contract be pinned without a live herdr, a store, or a network.
"""

from __future__ import annotations

from collections import abc
from dataclasses import dataclass, field
from typing import Collection, Optional, Sequence

from mozyo_bridge.core.state.lane_kind import LaneKindError, optional_lane_kind
from mozyo_bridge.core.state.lane_lifecycle_model import DecisionPointer


class LaneLaunchPlanError(ValueError):
    """A lane launch plan is unresolvable / contradictory; fail closed (zero-start).

    The ONE error this module raises. Everything it refuses — a structural type violation at
    construction, a vocabulary / uniqueness / reconciliation defect in the resolver, an
    unusable governance anchor — surfaces as this type, so the launch's own fail-closed
    boundary catches all of them with a single ``except`` and turns them into a typed
    zero-start (review j#85870: a refusal that escapes as some other exception type is not
    part of the advertised contract).
    """


def _owned_str(value: str) -> str:
    """An exact, inert ``str`` with the same characters — no subclass hook runs (j#86068).

    ``"" + value`` looked like a conversion that bypasses the caller's class and was the
    opposite: Python gives the RIGHT operand priority when its type is a subclass of the
    left one's, so a ``__radd__`` on the caller's ``str`` subclass ran and escaped as a raw
    exception. Every "obviously safe" conversion has that same shape — ``str(value)``
    dispatches to ``__str__``, ``value[:]`` to ``__getitem__`` — because they all dispatch on
    the VALUE's type, which is the caller's.

    The base method invoked explicitly does not dispatch, so nothing a subclass defines can
    run. Measured against a subclass that raises from ``__radd__``, ``__add__``, ``__str__``,
    ``__repr__``, ``__format__``, ``__getitem__``, ``__iter__``, ``__hash__``, ``__eq__``,
    ``__lt__``, ``__bool__``, ``__len__``, ``__mod__``, ``encode``, ``__reduce__`` and
    ``__copy__``: this returns an exact ``str`` with identical characters, and none of them
    is called.
    """
    return str.__str__(value)


def _kind(value: object) -> str:
    """The type name of a caller's value, for a message — never a way to fail (j#86049)."""
    try:
        return type(value).__name__
    except Exception:  # pragma: no cover - a type whose own name cannot be read
        return "<unnameable type>"


def _shown(value: object) -> str:
    """``repr(value)`` for a refusal message, with the caller's code unable to change the outcome.

    Interpolating a caller-supplied value into a refusal RE-ENTERS that caller's code:
    ``repr()`` runs ``__repr__`` and ``f"{exc}"`` runs ``__str__``. When that code raises, the
    refusal being built is replaced by a raw exception — the plan fails, but not in the way it
    promised to, and precisely at the moment it was trying to fail closed (review j#86049).

    A ``str`` subclass is the case that shows this is not exotic: it satisfies every
    ``isinstance`` check the module makes, so a value can be fully "type-validated" and still
    carry arbitrary code on its ``__repr__`` (measured on 5 such surfaces).

    Diagnostic text must never decide the OUTCOME. Nothing is swallowed: the value's failure
    is reported in place of the value, and the original exception of a failed READ is kept as
    ``__cause__`` by :func:`_read_once`.
    """
    try:
        return _owned_str(repr(value))
    except Exception:
        return f"<unprintable {_kind(value)}>"


def _text(value: object) -> str:
    """``str(value)`` for a message, unable to change the outcome (review j#86049)."""
    try:
        return _owned_str(str(value))
    except Exception:
        return f"<unprintable {_kind(value)}>"


def _read_once(value: object, factory, *, field: str):
    """Materialize a caller's collection EXACTLY once, as THIS module's error either way.

    The shape guards answer "is this the right kind of container"; they cannot answer "can
    it actually be read". A value that satisfies :class:`abc.Sequence` / :class:`abc.Collection`
    perfectly well may still raise from its own ``__iter__`` / ``__getitem__`` — a lazily
    backed sequence, a property that reaches for something no longer there — and that raw
    exception used to travel straight out of the plan (review j#86008: measured on all 15
    public collection surfaces). The launch turns a refusal into a typed zero-start with a
    single ``except LaneLaunchPlanError``, so anything escaping as another type is not the
    contract this module advertises, however correct the eventual failure looks.

    ``BaseException`` is deliberately NOT caught: a ``KeyboardInterrupt`` during a read is
    the operator stopping the process, not the plan refusing an input.
    """
    try:
        return factory(value)
    except Exception as exc:
        raise LaneLaunchPlanError(
            f"{field} could not be read ({_kind(exc)}); the plan is not built from an "
            "input it cannot even materialise"
        ) from exc


def _ordered_tokens(value: object, *, field: str) -> tuple[str, ...]:
    """``value`` as an OWNED tuple of strings, from an ORDERED sequence (review j#85875 F3).

    Rejecting only ``str`` / ``bytes`` and calling ``tuple()`` on whatever is left admitted
    sets and other unordered containers, whose iteration order is not a property of the
    value: the same plan would then fix a different argv / launch order in a different
    process. These fields are launch ORDER, so only an ordered sequence can express them.
    A bare string is refused separately because it iterates into characters — a silent
    "argv" of single letters is worse than a refusal.
    """
    if isinstance(value, (str, bytes)):
        raise LaneLaunchPlanError(
            f"{field} must be a sequence of tokens, got {_kind(value)} {_shown(value)}: "
            "a single string is a command line, not an ordered token sequence"
        )
    if not isinstance(value, abc.Sequence):
        raise LaneLaunchPlanError(
            f"{field} must be an ordered sequence (list / tuple), got "
            f"{_kind(value)}: an unordered container cannot express launch order "
            "— its iteration order is not part of the value"
        )
    tokens = _read_once(value, tuple, field=field)
    return tuple(_checked_str(token, field=f"{field} entry") for token in tokens)


def _ordered_container(value: object, *, field: str, expected: str) -> tuple:
    """``value`` as an owned tuple, from an ORDERED sequence — the OUTER shell check.

    Separate from the element checks on purpose (review j#85943): every previous round
    validated what was *inside* a container while the container itself stayed unexamined,
    and the shells kept letting things through — a mapping destructured its KEYS into a
    ``(split, order)`` geometry, and a set satisfied a declared ``Sequence``.

    Deliberately NOT merged with :func:`_ordered_tokens`, which applies the same shape rule
    to token sequences: they are separate boundaries, and folding them into one helper would
    mean a single guard's removal broke every case at once — which is exactly the coupling
    that lets one boundary's probe mask another's loss.
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, abc.Sequence):
        raise LaneLaunchPlanError(
            f"{field} must be an ordered sequence of {expected}, got "
            f"{_kind(value)}: an unordered / mapping container is not a "
            f"positional sequence"
        )
    return _read_once(value, tuple, field=field)


def _checked_vocabulary(value: object, *, field: str) -> frozenset[str]:
    """An injected vocabulary as an OWNED exact-token set (review j#85875 F1).

    A bare string is the dangerous case: ``"ximplementerx"`` passes ``role in vocabulary``
    by SUBSTRING, so the fail-closed vocabulary check silently stops being one. A non-string
    element is the other: it survives until the refusal message tries to join it and the
    caller gets a raw ``TypeError`` instead of this module's single typed error.
    """
    if isinstance(value, (str, bytes)):
        raise LaneLaunchPlanError(
            f"{field} must be a collection of tokens, got {_kind(value)} {_shown(value)}: "
            "membership in a bare string is a substring test, not a vocabulary check"
        )
    # Covers BOTH "not iterable at all" and "iterable that fails while being read":
    # each is a vocabulary the plan was not actually given, and neither may leave as a
    # raw exception (review j#86008).
    tokens = _read_once(value, frozenset, field=field)
    return frozenset(
        _checked_str(token, field=f"{field} entry") for token in tokens
    )


def _checked_str(value: object, *, field: str) -> str:
    """``value`` as a ``str``, or a typed refusal (review j#85870).

    A type annotation is documentation, not a runtime check, and ``frozen=True`` only stops
    re-binding — neither stops a caller handing this boundary an ``int`` provider or a
    ``None`` role. The value objects here are the launch's fail-closed authority, so they
    verify their own fields on EVERY construction path rather than trusting the annotation.
    """
    if not isinstance(value, str):
        raise LaneLaunchPlanError(
            f"{field} must be a string, got {_kind(value)} {_shown(value)}"
        )
    # OWN the string, not just its characters (review j#86049). `isinstance` admits a `str`
    # SUBCLASS, which satisfies every check this module makes while carrying arbitrary code
    # on `__repr__` / `__format__` / `__hash__` / `__eq__` / `__bool__` / `__lt__`. Those run
    # later — in a message, a vocabulary lookup, a truthiness test — and a raising one
    # replaces a typed refusal with a raw exception, or turns a decision into a failure.
    # `_owned_str` yields an exact, inert `str` with the same characters, so nothing
    # downstream can re-enter the caller. Concatenation was the FIRST attempt at this and was
    # wrong in the most direct way — `"" + value` gives a subclass right operand priority and
    # calls its `__radd__` (review j#86068) — so see that helper for why the conversion is a
    # base method invoked explicitly. This is the same discipline the sequences already
    # follow: validate, then own what you validated.
    return _owned_str(value)


@dataclass(frozen=True)
class SlotLaunchSpec:
    """One slot's caller-resolved launch intent (pure value, pre-launch).

    ``workflow_role`` is the slot's governed responsibility (the vocabulary the caller's
    registry defines); ``profile_id`` is the named profile that role selects; ``provider``
    is the runtime that executes it; ``launch_argv`` is the exact resolved argv tail.
    ``physical_slot`` names the pair position the slot occupies (a caller-supplied label
    such as ``"first"`` / ``"second"``) — a *plan* coordinate, never a live ``%pane`` id. It
    is REQUIRED in a plan: a blank position is not "unpinned", it is an unstated one, and it
    used to slip past the position-collision check (review j#85859 F2).

    Every field is caller-supplied on purpose: this type asserts nothing about where the
    values came from, and the plan below refuses whatever it cannot fully resolve.
    """

    workflow_role: str = ""
    profile_id: str = ""
    provider: str = ""
    launch_argv: tuple[str, ...] = ()
    physical_slot: str = ""

    def __post_init__(self) -> None:
        # Structural validation, on every construction path (review j#85870): the four
        # scalar fields are exactly what downstream code interpolates into a launch, an
        # error message or a dict key, so a non-string here is refused rather than carried.
        # This is deliberately TYPE-only — whether a role exists in the vocabulary, or a
        # position collides with a peer's, is contextual and belongs to the resolver.
        for field_name in ("workflow_role", "profile_id", "provider", "physical_slot"):
            object.__setattr__(
                self,
                field_name,
                _checked_str(
                    getattr(self, field_name), field=f"SlotLaunchSpec.{field_name}"
                ),
            )
        # Copy the argv into a tuple at CONSTRUCTION (review j#85859 F3). ``frozen=True``
        # only stops the attribute being re-bound; a caller that passed a list kept a live
        # handle to the validated plan's command and could empty it afterwards (measured:
        # ['--model','x'] -> [] after the caller's clear()). A plan that can change after
        # it is validated is not "fixed before the first write", which is this type's whole
        # reason to exist — so the value owns its own copy.
        object.__setattr__(
            self,
            "launch_argv",
            _ordered_tokens(self.launch_argv, field="SlotLaunchSpec.launch_argv"),
        )


def _owned_slots(slots: object) -> tuple:
    """The plan's own immutable slot sequence, or a typed refusal (review j#85863)."""
    if isinstance(slots, (str, bytes)) or not isinstance(slots, abc.Sequence):
        raise LaneLaunchPlanError(
            "plan slots must be an ordered sequence of SlotLaunchSpec, got "
            f"{_kind(slots)}: launch order is part of the plan"
        )
    owned = _read_once(slots, tuple, field="plan slots")
    for slot in owned:
        if not isinstance(slot, SlotLaunchSpec):
            raise LaneLaunchPlanError(
                f"plan slot {_shown(slot)} is {_kind(slot)}, not SlotLaunchSpec; the plan "
                "does not carry a slot it cannot validate"
            )
    return owned


def _owned_placement(placement: object) -> tuple:
    """The plan's own immutable ``(split, order)`` geometry, or a typed refusal.

    The order sequence is the launch geometry — WHICH provider occupies the container — so a
    caller-owned list here means the resolved geometry could change between validation and
    the launch that acts on it.
    """
    if placement is None:
        return (None, None)
    # The OUTER carrier first (review j#85943): a mapping unpacks its KEYS into
    # `(split, order)` and silently drops its values, so `{"right": 0, None: 0}` was
    # landing as the geometry `("right", None)`. Only a positional two-element ordered
    # sequence is a `(split, order)` pair.
    pair = _ordered_container(
        placement,
        field="plan placement",
        expected="two positional elements (split, order)",
    )
    if len(pair) != 2:
        raise LaneLaunchPlanError(
            f"plan placement must be a (split, order) pair, got {len(pair)} element(s): "
            f"{_shown(placement)}"
        )
    split, order = pair
    if split is not None:
        if not isinstance(split, str):
            raise LaneLaunchPlanError(
                f"plan placement split must be a string or None, got {_kind(split)}"
            )
        split = _checked_str(split, field="plan placement split")
    if order is None:
        return (split, None)
    return (split, _ordered_tokens(order, field="plan placement order"))


@dataclass(frozen=True)
class ResolvedLaneLaunchPlan:
    """A fully resolved, validated pair plan — the last thing fixed before the first write.

    ``lane_kind`` / ``lane_class`` / ``placement`` are the Tranche 1 pair geometry;
    ``source_anchor`` is the durable governance record the whole plan was resolved from;
    ``slots`` are the validated per-slot plans in launch order.

    Normally constructed by :func:`resolve_lane_launch_plan`, so an instance has passed every
    whole-plan validation — but the constructor is public, so it OWNS its data either way:
    every sequence it is handed is copied into a tuple here (review j#85863). ``frozen=True``
    only stops the attributes being re-bound; without the copy a caller that passed a list
    kept a live handle into the validated plan and could empty its slots or its launch order
    afterwards (both measured). A plan that can change after it is validated cannot be "the
    last thing fixed before the first write", which is this type's entire purpose.
    """

    lane_class: str
    lane_kind: Optional[str] = None
    placement: tuple[Optional[str], Optional[Sequence[str]]] = (None, None)
    source_anchor: Optional[DecisionPointer] = None
    slots: tuple[SlotLaunchSpec, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "lane_class",
            _checked_str(self.lane_class, field="ResolvedLaneLaunchPlan.lane_class"),
        )
        if self.lane_kind is not None:
            object.__setattr__(
                self,
                "lane_kind",
                _checked_str(self.lane_kind, field="ResolvedLaneLaunchPlan.lane_kind"),
            )
        if self.source_anchor is not None and not isinstance(
            self.source_anchor, DecisionPointer
        ):
            # A plan's provenance is the durable decision record itself, not a token that
            # merely looks like one: a string here would read back as governance the store
            # never issued (review j#85870 measured `source_anchor='not a pointer'`).
            raise LaneLaunchPlanError(
                "ResolvedLaneLaunchPlan.source_anchor must be a DecisionPointer, got "
                f"{_kind(self.source_anchor)} {_shown(self.source_anchor)}"
            )
        object.__setattr__(self, "slots", _owned_slots(self.slots))
        object.__setattr__(self, "placement", _owned_placement(self.placement))

    @property
    def workflow_roles(self) -> tuple[str, ...]:
        """The plan's workflow roles in launch order (plan-only; never a route identity)."""
        return tuple(slot.workflow_role for slot in self.slots)

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(slot.provider for slot in self.slots)


def resolve_source_anchor(
    anchors: Sequence[DecisionPointer],
    *,
    required: bool = False,
) -> Optional[DecisionPointer]:
    """The ONE durable anchor this plan is resolved from, or fail closed.

    ``()`` yields ``None`` when the caller builds no role-bearing plan (the pre-#13647
    launch). With ``required=True`` — which is what a NON-EMPTY plan asks for — zero anchors
    is itself a refusal (review j#85859 F1): a plan that assigns governed responsibilities
    without naming the durable decision that assigned them is exactly the un-anchored role
    the design refuses to launch from ("durable governance から一意解決した caller の plan
    だけ", j#85645). One anchor, or several that name the exact same durable record, resolves
    to that anchor. Two *different* anchors mean the governance
    inputs contradict each other about which decision authorizes this launch, and picking
    one would be a guess about whose decision is live — so it refuses (Design Answer j#85645:
    "矛盾/複数 anchor で ambiguous なら guess せず zero-start").
    """
    distinct: list = []
    for anchor in _ordered_container(
        anchors, field="governance anchors", expected="DecisionPointer"
    ):
        if not isinstance(anchor, DecisionPointer):
            # `None` is refused like any other non-pointer (review j#85943): silently
            # dropping it would turn "an anchor was supposed to be here and could not be
            # resolved" into "no anchor was supplied", which is precisely the distinction
            # the exact-one-anchor rule exists to keep.
            raise LaneLaunchPlanError(
                f"a governance anchor must be a DecisionPointer, got "
                f"{_kind(anchor)} {_shown(anchor)}"
            )
        if anchor not in distinct:
            distinct.append(anchor)
    if not distinct:
        if required:
            raise LaneLaunchPlanError(
                "a role-bearing launch plan requires the durable governance record it was "
                "resolved from; none was supplied. A slot plan without provenance cannot be "
                "distinguished from a guessed one, so it is not launched"
            )
        return None
    if len(distinct) > 1:
        raise LaneLaunchPlanError(
            "the launch plan's governance anchor is ambiguous: "
            f"{len(distinct)} different durable records claim this launch "
            f"({', '.join(sorted(f'{a.source}#{a.issue_id}@{a.journal_id}' for a in distinct))}). "
            "Resolve which decision authorizes it before launching; the plan is not built "
            "from a guess."
        )
    return distinct[0]


def resolve_lane_launch_plan(
    *,
    lane_class: str,
    slot_specs: Sequence[SlotLaunchSpec],
    known_providers: Collection[str],
    known_roles: Collection[str],
    request_providers: Sequence[str],
    known_lane_classes: Collection[str],
    known_splits: Collection[str],
    lane_kind: Optional[str] = None,
    placement: tuple[Optional[str], Optional[Sequence[str]]] = (None, None),
    anchors: Sequence[DecisionPointer] = (),
) -> ResolvedLaneLaunchPlan:
    """Validate a whole pair plan, or fail closed before anything is launched.

    Every refusal below is a :class:`LaneLaunchPlanError` raised while the plan is still
    just data — the caller has created no workspace, tab, agent or startup action — which is
    what makes "reject the pair" a safe outcome rather than a half-built lane.

    The validations (Design Answer j#85645 "whole-plan preflight"):

    1. **unresolved slot** — a slot missing its role, profile, provider or argv is not a
       plan, it is a hole. Launching it would either start the wrong thing or start nothing
       and leave its peer live;
    2. **unknown workflow role / unregistered provider** — a token outside the caller's
       vocabulary cannot select a profile or an adapter. It is refused rather than degraded
       to a default, because a silent default is how a slot ends up running someone else's
       responsibility;
    3. **duplicate workflow role** — two slots claiming the same governed responsibility is
       a contradiction about who does the work (and, downstream, about who a role-addressed
       handoff belongs to);
    4. **duplicate physical slot** — two entries for one pair position; whichever launched
       second would silently win;
    5. **same (physical slot, provider) with a different profile or argv** — the same slot
       asked to be two different things. Distinct providers legitimately carry distinct
       profiles, so only a *same-slot* conflict refuses;
    6. **missing / ambiguous governance anchor** — a role-bearing plan must name exactly one
       durable decision (see :func:`resolve_source_anchor`);
    7. **a plan that does not describe THIS launch** (review j#85859 F2). ``request_providers``
       is the launch's actual slot set, and the plan must account for it **exactly**: same
       number of slots, same provider multiset, and every slot pinned to a distinct non-empty
       physical position. This is what makes it a *whole-plan* preflight rather than a
       structural check on unrelated data — a plan that describes one slot of a two-slot
       launch leaves the other slot to start with nobody having declared what it is, which is
       precisely the partial lane the gate exists to prevent.

       Launch ORDER is deliberately not pinned here: the placement policy reorders providers
       AFTER this preflight (``resolve_launch_order``), so requiring the plan's order to
       match the request's would reject correct plans. Cardinality + multiset + unique
       positions is what "the plan accounts for exactly this pair" needs.

    ``slot_specs=()`` returns an empty plan (no roles, no anchor requirement, no
    reconciliation): the launch is byte-for-byte the pre-#13647 one, so an unconfigured /
    legacy caller is unaffected.

    Division of labour (review j#85870): **structural** validation — is each field the type
    it claims to be, does the value own its sequences — happens in the value objects'
    constructors and therefore on EVERY path, including a direct
    ``ResolvedLaneLaunchPlan(...)``. **Contextual** validation — vocabulary membership,
    cross-slot uniqueness, reconciliation with the launch, anchor exactness — needs inputs
    only this function is given, so it happens here. A directly constructed plan is
    consequently *type-valid* but NOT "validated against a launch": only a plan this
    function returned has been checked against the pair it belongs to.
    """
    try:
        # Owned BEFORE it crosses into the vocabulary module (review j#86049): a hostile
        # `str` subclass would otherwise run its own code inside THAT module's checks and
        # message building, where this boundary's retype cannot reach it.
        kind = optional_lane_kind(
            lane_kind if lane_kind is None else _checked_str(
                lane_kind, field="ResolvedLaneLaunchPlan.lane_kind"
            ),
            source="ResolvedLaneLaunchPlan.lane_kind",
        )
    except LaneKindError as exc:
        # Re-typed at the plan boundary (review j#85870): the launch catches this module's
        # error to produce its typed zero-start, so a vocabulary refusal that escaped as a
        # different type would leave the caller with an untyped failure instead. The cause
        # chain keeps the original vocabulary error visible.
        raise LaneLaunchPlanError(_text(exc)) from exc
    slots = _owned_slots(slot_specs)
    request = _checked_request_providers(request_providers)
    providers_vocabulary = _checked_vocabulary(known_providers, field="known providers")
    roles_vocabulary = _checked_vocabulary(known_roles, field="known workflow roles")
    checked_placement = _checked_geometry(
        lane_class=lane_class,
        placement=placement,
        known_lane_classes=_checked_vocabulary(
            known_lane_classes, field="known lane classes"
        ),
        known_splits=_checked_vocabulary(known_splits, field="known splits"),
        known_providers=providers_vocabulary,
    )
    anchor = resolve_source_anchor(anchors, required=bool(slots))
    if not slots:
        return ResolvedLaneLaunchPlan(
            lane_class=lane_class,
            lane_kind=kind,
            placement=checked_placement,
            source_anchor=anchor,
            slots=(),
        )
    _reconcile_with_request(slots, request)
    seen_roles: dict = {}
    by_position: dict = {}
    for index, slot in enumerate(slots):
        where = f"slot {index} ({slot.provider or 'no provider'})"
        for label, value in (
            ("workflow role", slot.workflow_role),
            ("profile id", slot.profile_id),
            ("provider", slot.provider),
        ):
            if not value:
                raise LaneLaunchPlanError(
                    f"{where} has no {label}: a launch plan is not built from a partially "
                    "resolved slot (nothing was launched)"
                )
        if not slot.launch_argv:
            raise LaneLaunchPlanError(
                f"{where} resolved no launch argv for profile {_shown(slot.profile_id)}; "
                "refusing to launch a slot whose command is unresolved"
            )
        if slot.workflow_role not in roles_vocabulary:
            raise LaneLaunchPlanError(
                f"{where} carries unknown workflow role {_shown(slot.workflow_role)}; known "
                f"roles: {', '.join(sorted(roles_vocabulary))}. A supplied-but-unregistered "
                "role fails closed rather than falling back to a default responsibility"
            )
        if slot.provider not in providers_vocabulary:
            raise LaneLaunchPlanError(
                f"{where} names unregistered provider {_shown(slot.provider)}; known providers: "
                f"{', '.join(sorted(providers_vocabulary))}"
            )
        if slot.workflow_role in seen_roles:
            raise LaneLaunchPlanError(
                f"workflow role {_shown(slot.workflow_role)} is claimed by two slots "
                f"({seen_roles[slot.workflow_role]} and {where}); one responsibility "
                "cannot be carried by two slots of the same pair"
            )
        seen_roles[slot.workflow_role] = where
        position = slot.physical_slot  # already type-checked at slot construction
        if not position:
            # A blank position is not "unpinned", it is an unstated one — and a blank was an
            # escape hatch out of the collision check below (review j#85859 F2). A plan that
            # claims to describe the pair states where each slot goes.
            raise LaneLaunchPlanError(
                f"{where} pins no physical slot; a plan that describes this launch names "
                "the pair position each slot occupies"
            )
        previous = by_position.get(position)
        if previous is None:
            by_position[position] = slot
        elif previous.provider == slot.provider:
            if (
                previous.profile_id != slot.profile_id
                or previous.launch_argv != slot.launch_argv
            ):
                raise LaneLaunchPlanError(
                    f"physical slot {_shown(position)} is planned twice for provider "
                    f"{_shown(slot.provider)} with different profile / argv "
                    f"({_shown(previous.profile_id)} vs {_shown(slot.profile_id)}); the plan does "
                    "not choose between them"
                )
            raise LaneLaunchPlanError(
                f"physical slot {_shown(position)} is planned twice for provider "
                f"{_shown(slot.provider)}; one pair position holds one slot"
            )
        else:
            raise LaneLaunchPlanError(
                f"physical slot {_shown(position)} is claimed by two providers "
                f"({_shown(previous.provider)} and {_shown(slot.provider)}); one pair position "
                "holds one slot"
            )
    return ResolvedLaneLaunchPlan(
        lane_class=lane_class,
        lane_kind=kind,
        # The value the geometry check itself validated — never a second read of the
        # caller's object (review j#85885).
        placement=checked_placement,
        source_anchor=anchor,
        slots=slots,
    )


def _checked_geometry(
    *,
    lane_class: object,
    placement: object,
    known_lane_classes: frozenset,
    known_splits: frozenset,
    known_providers: frozenset,
) -> tuple:
    """The pair geometry this plan fixes must itself be RESOLVED (review j#85875 F4).

    Returns the OWNED, validated geometry — and the caller must store exactly that value
    (review j#85885). Checking one read of the caller's object and then storing a second
    read of it is a time-of-check/time-of-use gap: a placement whose value changes between
    reads was measured landing an unchecked ``("diagonal", ("foreign",))`` inside a plan the
    resolver had just "validated". Check what you use; use what you checked.

    The whole-plan contract is that ``lane_kind?, lane_class, resolved placement`` are fixed
    before the first write (Design Answer j#85645). A plan carrying ``lane_class="foreign"``
    or ``split="diagonal"`` is not a resolved geometry, so calling it "validated against this
    launch" would be false. The vocabularies are INJECTED (like roles / providers) so this
    leaf stays pure and does not reach into the config context that owns them.

    Launch ORDER is still not compared with the request (that boundary was set in j#85859
    F2 and is unchanged): this checks only that the geometry VALUES are ones the system
    recognises — each order entry a known provider, named at most once.
    """
    lane_class = _checked_str(lane_class, field="ResolvedLaneLaunchPlan.lane_class")
    if lane_class not in known_lane_classes:
        raise LaneLaunchPlanError(
            f"unknown lane class {_shown(lane_class)}; known classes: "
            f"{', '.join(sorted(known_lane_classes))}"
        )
    owned = _owned_placement(placement)
    split, order = owned
    if split is not None and split not in known_splits:
        raise LaneLaunchPlanError(
            f"unknown placement split {_shown(split)}; known splits: "
            f"{', '.join(sorted(known_splits))}"
        )
    if order is None:
        return owned
    seen: set = set()
    for provider in order:
        if provider not in known_providers:
            raise LaneLaunchPlanError(
                f"placement order names unregistered provider {_shown(provider)}; known "
                f"providers: {', '.join(sorted(known_providers))}"
            )
        if provider in seen:
            raise LaneLaunchPlanError(
                f"placement order names provider {_shown(provider)} twice; an order is a "
                "permutation, not a multiset"
            )
        seen.add(provider)
    return owned


def _checked_request_providers(request_providers: object) -> tuple[str, ...]:
    """The launch's actual provider list as strings, or a typed refusal (j#85870)."""
    return _ordered_tokens(request_providers, field="the launch's providers")


def _reconcile_with_request(
    slots: Sequence[SlotLaunchSpec], request_providers: Sequence[str]
) -> None:
    """The plan must account for EXACTLY the slots this launch will start (j#85859 F2)."""
    from collections import Counter

    requested = Counter(request_providers)
    planned = Counter(slot.provider for slot in slots)
    if sum(planned.values()) != sum(requested.values()):
        raise LaneLaunchPlanError(
            f"the launch plan describes {sum(planned.values())} slot(s) but this launch "
            f"starts {sum(requested.values())} ({', '.join(request_providers)}); an "
            "unexplained slot would start with nothing having declared what it is"
        )
    if planned != requested:
        missing = sorted((requested - planned).elements())
        extra = sorted((planned - requested).elements())
        raise LaneLaunchPlanError(
            "the launch plan does not describe this launch's providers"
            + (f"; unplanned: {', '.join(missing)}" if missing else "")
            + (f"; planned but not launched: {', '.join(extra)}" if extra else "")
        )


__all__ = (
    "LaneLaunchPlanError",
    "ResolvedLaneLaunchPlan",
    "SlotLaunchSpec",
    "resolve_lane_launch_plan",
    "resolve_source_anchor",
)
