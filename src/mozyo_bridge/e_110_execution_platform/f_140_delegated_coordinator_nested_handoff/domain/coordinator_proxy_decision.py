"""The proxy decision's marker grammar: its producer, its shapes, and its reader (Redmine #14667).

Split out of the ``coordinator_proxy_send`` rail, which had grown to hold two different things: the
**impure choreography** (live probes, the exactly-once fence, the send port) and this — a **pure**
grammar over a journal note's text. Nothing here reads env, opens a file, or performs a send, so it
belongs on the domain side of the boundary, and the split is what the module-health gate was
pointing at when the combined module crossed its line threshold.

The three pieces are deliberately together, because they are one contract read from three
directions:

- :func:`render_bootstrap_decision_marker` is the **only** producer of this marker. That is a
  measured fact, not a convention: :func:`...redmine_journal_source.render_workflow_event_marker`
  refuses any gate outside :data:`...redmine_journal_source.GATE_BEARING_KINDS`, and
  ``implementation_request`` is deliberately not in it, so no gate-note producer can emit one;
- :func:`canonical_decision_shapes` derives, *from that producer*, the field-key sets a decision may
  have. Deriving rather than listing is the point — a hand-written list is a second statement of the
  producer's grammar, and this module's history is a catalogue of what happens when one rule has two
  definitions;
- :func:`canonical_decision_in_journal` reads a named journal against both, plus the SHARED strict
  component rules (:func:`...redmine_journal_source.strict_marker_fields` /
  :func:`...redmine_journal_source.marker_logical_gates`) that every other authority consumer calls.

The design record for all of it is `vibes/docs/specs/external-client-coordinator-proxy.md` §3.
"""

from __future__ import annotations

from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.canonical_note_scan import (  # noqa: E501
    MARKER_CHANNEL_WORKFLOW_EVENT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.coordinator_proxy import (  # noqa: E501
    ACTION_DECISION_TOKENS,
    DecisionRecord,
    normalize_action,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.marker_value_contract import (  # noqa: E501
    MarkerValueError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    marker_components_in_note,
    marker_declares_gate,
    marker_logical_gates,
    strict_marker_fields,
    validate_marker_field_value,
)


#: The workflow-event marker channel whose ``gate`` / ``kind`` field names a durable decision. Bound
#: to the shared channel constant rather than re-spelled: the channel set is the scan authority's,
#: and a second literal here is a token this rail could drift on alone.
_WORKFLOW_EVENT_CHANNEL = MARKER_CHANNEL_WORKFLOW_EVENT

#: The marker field that binds a decision to the proxy action it authorizes (Design Answer j#90329
#: contract 5). Without it the same ``implementation_request`` token had to serve every purpose, so
#: "which action does this decision authorize" was never expressed and had to be guessed from the
#: issue's history — which is what let a quotation elsewhere on the issue become authority, and then
#: what let the anti-quotation rule poison the issue permanently.
DECISION_ACTION_FIELD = "proxy_action"

def canonical_decision_in_journal(
    notes: str, *, action: str
) -> "tuple[Optional[DecisionRecord], str]":
    """The single canonical decision a NAMED journal carries for ``action``, or a refusal reason.

    Reads exactly one journal — the one the invocation named — instead of scanning the issue's
    history (Design Answer j#90329 contract 5). The history scan was the root of both failures: a
    quotation anywhere on the issue became a candidate, and the rule that refused two candidates
    then made the issue permanently unusable. Neither can happen when the only text considered is
    the named journal's, with quotations stripped first.

    Canonicality is exact **twice over**, and the second half was missing (Redmine #14667). It is
    not enough that the note carry exactly one accepted marker: that marker's BODY must be one the
    canonical producer (:func:`render_bootstrap_decision_marker`) could have rendered. The reader
    used to fold each body to a dict with last-write-wins, which erases the evidence that it could
    not — so three bodies measured on ``origin/main-next@4f0d765b`` each decided a proxy SEND:

        gate=some_other:gate=implementation_request      (repeated key, last-write-wins)
        proxy_action=dispatch_next:proxy_action=…        (the same, on the action field)
        gate = implementation_request:proxy_action = …   (whitespace-contaminated fields)

    So the body is judged from its **uncollapsed components** by the shared strict reader every
    authority consumer uses (:func:`...redmine_journal_source.strict_marker_fields`), and which
    gate a readable body declares comes from :func:`...redmine_journal_source.marker_logical_gates`
    — both aliases read as a SET, never first-non-empty, because a second gate spelled in the other
    alias is a second authority claim rather than a fallback. Those are the shared authority's rules
    about a body's SYNTAX, and this module adds none of its own to them, or the two would drift the
    way the two notions of "quoted" once did.

    Syntax is not the whole criterion, and the first version of this reader stopped there (review
    j#92839 finding 1). Well-formed components say nothing about WHICH fields the body carries, so
    ``…:proxy_action=bootstrap_lane:extra=value`` — a body the producer cannot emit — passed every
    check and delivered a send. The field set is therefore matched against
    :func:`canonical_decision_shapes`, which is **derived from the producer** rather than listed:
    the vocabulary belongs to the producer, and a copy of it here is the second definition this
    module keeps being bitten by.

    An unreadable marker is **not dropped**. A marker that claims one of this action's tokens and
    is not countable as exactly that token refuses the whole journal
    (``unreadable_canonical_decision``), so a clean sibling written beside a forged one can never
    make the journal read like a clean one — which is how "parse strictly" turns a duplicate
    refusal into an acceptance if the unreadable marker is simply skipped. The claim itself is
    asked of the RAW components (:func:`...redmine_journal_source.marker_declares_gate`), because
    "does this marker claim this gate" and "is its body readable" are different questions and only
    the second one had been asked.

    Zero, two-or-more, an unreadable claim, or a marker that names a different action are all
    refusals with a fixed reason; the caller turns those into zero-send statuses.

    What counts as a quotation is **not** decided here (Redmine #14585), and neither is where a
    marker may be scanned from. Both live in the shared :mod:`...domain.canonical_note_scan`
    authority, which :func:`...redmine_journal_source.marker_components_in_note` scans **per
    canonical line** over :func:`...canonical_note_scan.canonical_note_lines`'s output. The
    per-line property is load-bearing, not decorative: the marker grammar's body is ``[^\\]]*``,
    which spans newlines, so scanning the blanked note as one string would let an unclosed
    ``[mozyo:`` on a quoted line close on a ``]`` further down and read as a marker that no single
    line contains. That property now comes from the shared scan rather than from a loop here plus a
    promise about an injected parser — one authority for both which text is the writer's own voice
    and where a marker may be read from.
    """
    declared_action = normalize_action(action)
    accepted = ACTION_DECISION_TOKENS.get(declared_action, ())
    shapes = canonical_decision_shapes()
    found: list = []
    unreadable = False
    for channel, components in marker_components_in_note(notes):
        if channel != _WORKFLOW_EVENT_CHANNEL:
            continue
        fields = strict_marker_fields(components)
        gates = marker_logical_gates(fields)
        if (
            len(gates) == 1
            and next(iter(gates)) in accepted
            # ...AND the body carries the field set a producer renders. Well-formed components are
            # not enough: an extra field is a body no producer can emit (review j#92839 finding 1).
            and frozenset(fields) in shapes
        ):
            found.append((next(iter(gates)), fields))
            continue
        # Not countable as one of this action's decisions. If it CLAIMS one anyway, it is a
        # same-kind claim this rail cannot honour, and the journal is fail-closed.
        if any(marker_declares_gate(components, token) for token in accepted):
            unreadable = True
    if unreadable:
        return None, "unreadable_canonical_decision"
    if not found:
        return None, "no_canonical_decision"
    if len(found) >= 2:
        return None, "duplicate_canonical_decision"
    token, fields = found[0]
    # No ``.strip()`` on any field read below: the strict reader has already refused every body
    # carrying whitespace around a key or a value, so stripping here would only hide that
    # guarantee — and a reader that re-normalizes what its producer is required to render exactly
    # is how the lenient fold looked correct in the first place.
    if fields.get(DECISION_ACTION_FIELD, "") != declared_action:
        return None, "action_not_declared"
    return (
        DecisionRecord(
            journal="",  # filled by the caller with the OWNING entry id, never self-reported
            token=token,
            lane=fields.get("lane", ""),
            lane_generation=fields.get("lane_generation", ""),
        ),
        "",
    )


def _require_str(field: str, value: object) -> None:
    """Refuse a non-string argument before it can reach a branch or a marker (pure).

    This is a TYPE precondition on this producer's own signature, not a second grammar. Every rule
    about what a value may CONTAIN — delimiters, whitespace, emptiness — stays with the shared
    :func:`...redmine_journal_source.validate_marker_field_value`, and nothing here duplicates it.

    It has to live at this boundary rather than in that shared contract, and the reason was
    measured rather than assumed (review j#93162): the shared validator coerces with ``str(value)``,
    which turns ``False`` into ``'False'`` and ``0`` into ``'0'`` — so routing a non-string through
    it produces a clean-looking field instead of a refusal. Adding a string-only rule there instead
    would break a real caller: the recovery-admission producer passes ``lane_generation=1`` as an
    int, and that coercion is load-bearing for it (22 errors when probed). A rule that is right for
    one producer and wrong for another does not belong in the shared contract.
    """
    if not isinstance(value, str):
        raise MarkerValueError(
            f"marker field {field!r} must be a string, got {type(value).__name__} {value!r}. "
            "A non-string cannot be judged by the marker value contract without being coerced "
            "into one, and a coerced value is not the value the caller passed"
        )


def render_bootstrap_decision_marker(lane: str = "", lane_generation: str = "") -> str:
    """The canonical decision marker a coordinator writes to authorize a proxy action (producer).

    ``proxy_action`` is what makes the decision unambiguous about *what it authorizes*; the reader
    refuses a marker that omits it. A lane-scoped action additionally names its lane and generation.

    Every emitted value goes through the shared marker value contract
    (:func:`...redmine_journal_source.validate_marker_field_value`) rather than being interpolated
    (Redmine #14667 review j#92839 finding 2). Interpolating them let a value carrying a marker
    separator render a marker that *reads back as a different well-formed body*: a
    ``lane_generation`` of ``2]junk`` closed the token early, and the scan read a clean-looking
    decision for generation ``2`` that then delivered a send. The central `### Hibernate Evidence
    Marker Contract` states the rule this violated — "renderer は parser が拒否するものを書かない" —
    and the shared validator is where it already lives, so this calls it instead of restating which
    characters are dangerous. It refuses ``[``, ``=``, ``:``, ``]``, whitespace, and the empty
    value; the last is stricter than this rail's own reader needs (a blank generation would merely
    classify ``decision_incomplete``) but a marker that can never authorize anything is not
    something a producer should be able to write either.

    Raises :class:`...marker_value_contract.MarkerValueError` rather than emitting such a marker —
    a producer error is recoverable, a durable marker that means something other than its arguments
    is not.
    """
    marker = f"[mozyo:workflow-event:gate=implementation_request:{DECISION_ACTION_FIELD}="
    # The type of the argument that DECIDES the branch is settled before the branch. Testing
    # ``lane`` for truthiness let every falsy non-string — ``None`` / ``False`` / ``0`` — fall into
    # the bootstrap branch and render a valid marker that delivered (review j#93162). That was not
    # a pre-existing hole being preserved: ``lane.strip()`` raised ``AttributeError`` on those
    # values and stopped before any marker existed, so the truthiness test converted a zero-send
    # into a positive authority send.
    _require_str("lane", lane)
    # Exactly one spelling selects a bootstrap: the empty string. "Anything falsy" is not a
    # sentinel, it is a coincidence of Python's truth table.
    if lane == "":
        return marker + "bootstrap_lane]"
    _require_str("lane_generation", lane_generation)
    # ...and the VALUE rules stay the shared authority's (review j#93063): the raw value, judged
    # as given, by the contract every other producer calls.
    lane_value = validate_marker_field_value("lane", lane)
    generation_value = validate_marker_field_value("lane_generation", lane_generation)
    return marker + f"dispatch_next:lane={lane_value}:lane_generation={generation_value}]"


def canonical_decision_shapes() -> "frozenset[frozenset[str]]":
    """The marker field-key sets a decision may have, DERIVED FROM THE PRODUCER (pure).

    Review j#92839 finding 1: the reader checked that every *component* was well-formed and never
    checked WHICH fields the body carried, so ``…:proxy_action=bootstrap_lane:extra=value`` — a body
    :func:`render_bootstrap_decision_marker` cannot emit — resolved ``verified`` and delivered a
    send. The docstring above :func:`canonical_decision_in_journal` already claimed the criterion
    ("a body the canonical producer could have rendered"); only the implementation fell short of it.

    The shapes are **derived by rendering, not listed here**. A hand-written list is a second
    statement of the producer's grammar, and this module's whole history is about what happens when
    one rule has two definitions. Deriving also settles the question the first implementation got
    wrong: this token's producer set really is closed —
    :func:`...redmine_journal_source.render_workflow_event_marker` refuses any gate outside
    :data:`...redmine_journal_source.GATE_BEARING_KINDS`, and ``implementation_request`` is
    deliberately not in it, so no gate-note producer can emit this marker and
    :func:`render_bootstrap_decision_marker` is the only one that can.

    Each producer shape is admitted **with and without** :data:`DECISION_ACTION_FIELD`. The producer
    always writes it, but its ABSENCE has its own documented classification — ``action_not_declared``
    / :data:`ANCHOR_ACTION_MISMATCH` (spec §3) — and folding that into "unreadable" would replace a
    precise reason with a vaguer one. Both are zero-send; the operator is told different things.

    **Known limit, stated rather than papered over:** this samples the producer's two branches with
    two calls. A producer that grew a THIRD branch would not be sampled here, and its output would
    read as producer-impossible. That fails closed (a refusal, never a delivery) and surfaces
    immediately as a refused clean decision, but it is a real coupling: a new branch must be added
    to this derivation in the same change.
    """
    shapes: set = set()
    for marker in (
        render_bootstrap_decision_marker(),
        render_bootstrap_decision_marker(lane="derivation", lane_generation="1"),
    ):
        for _channel, components in marker_components_in_note(marker):
            keys = frozenset(key for key, _value in components)
            shapes.add(keys)
            shapes.add(keys - {DECISION_ACTION_FIELD})
    return frozenset(shapes)

__all__ = (
    "DECISION_ACTION_FIELD",
    "canonical_decision_in_journal",
    "canonical_decision_shapes",
    "render_bootstrap_decision_marker",
)
