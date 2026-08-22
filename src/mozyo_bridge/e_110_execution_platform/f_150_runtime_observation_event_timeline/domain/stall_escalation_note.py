"""The body of a stall-escalation ``blocked`` gate journal (Redmine #15855).

#15855 j#110121-5 fixed the durable escalation record as a Redmine ``## Gate: blocked``
journal with ``reason: stall_watch_escalation``, written through the existing canonical
gate writer. This module renders **only the prose body** of that note; the structured
``[mozyo:workflow-event:gate=blocked:...]`` marker is appended by the one canonical
renderer (``redmine_journal_source.render_gate_note``), which every gate journal in this
repo already goes through. No new gate token, no new marker channel, no second producer.

Three properties this renderer is responsible for:

**It carries no pane text.** Everything below is a fixed classification token, an identity,
a count, or a timestamp — the same hygiene rule ``StallObservation`` already follows
(``stall-watcher-screen-diff.md`` `## 出力の hygiene`). This is what makes it safe for an
unattended writer to post the body verbatim: there is no path by which a screen's contents
can reach a durable record.

**It claims an observation, never a conclusion.** The note says what was classified, how
many consecutive passes agreed, and what the classifier's prescription was. It does not
say the unit is dead, that the work is complete, or that any remedy was applied — ADR-0014
draws that line, and a watcher writing into the coordinator's own record is exactly where
it would be crossed. The prescription is reproduced as a *recommendation to a human*,
matching the ``present_only`` posture the classifier already emits.

**It states the policy that produced it.** Cadence and threshold are operator runtime
policy (``stall-watcher-screen-diff.md`` `## 既存正本との境界`), so "N consecutive" means
nothing to a later reader without the N. ``policy_id`` is rendered so the record is
self-describing rather than requiring the reader to guess which configuration was live.
"""

from __future__ import annotations

from typing import Optional, Sequence

#: The fixed reason token this rail writes into every escalation note. Fixed rather than
#: free prose because a consumer filtering "which blocked journals came from the stall
#: watcher" must not have to match on wording.
STALL_ESCALATION_REASON = "stall_watch_escalation"

#: The gate this rail writes under. ``blocked`` is already a callback-required kind
#: (``redmine_journal_source.GATE_BEARING_KINDS``), and its documented meaning is exactly
#: what is wanted here: it "only *wakes the coordinator to read the journal*, it authorizes
#: nothing". Introducing a stall-specific gate would add a token every consumer would have
#: to learn for no additional meaning.
STALL_ESCALATION_GATE = "blocked"


def render_policy_id(*, cadence_seconds: int, threshold: int, source: str) -> str:
    """A compact, stable description of the policy that produced an escalation.

    ``source`` names where the values came from (a config surface, or the portable
    default), so a reader can tell a deliberately-configured cadence from a shipped one
    without consulting the host.
    """
    return f"cadence={int(cadence_seconds)}s;threshold={int(threshold)};source={source}"


def render_escalation_body(
    *,
    issue: str,
    slot_label: str,
    generation: str,
    target: str,
    provider_id: str,
    stall_class: str,
    prescription: str,
    consecutive: int,
    first_observed_at: str,
    last_observed_at: str,
    policy_id: str,
    idempotency_key: str,
    matched_id: str = "",
    evidence_tier: str = "",
    extra_notes: Optional[Sequence[str]] = None,
) -> str:
    """Render the prose body of one stall-escalation ``blocked`` gate journal.

    The caller passes this to the canonical gate writer, which appends the structured
    marker. Every argument is a token, an identity, a count or a timestamp; there is no
    parameter through which pane content could enter.
    """
    lines = [
        f"## Gate: {STALL_ESCALATION_GATE}",
        "",
        f"- reason: {STALL_ESCALATION_REASON}",
        f"- issue: {issue}",
        f"- slot: {slot_label}",
    ]
    if generation:
        lines.append(f"- generation: {generation}")
    if target:
        lines.append(f"- last_seen_target: {target} (transient locator; evidence only)")
    if provider_id:
        lines.append(f"- provider_id: {provider_id}")
    lines.extend(
        [
            f"- stall_class: {stall_class}",
            f"- prescription: {prescription} (posture: present_only — recommended to a "
            "human, not applied)",
            f"- consecutive_detections: {consecutive}",
            f"- first_observed_at: {first_observed_at}",
            f"- last_observed_at: {last_observed_at}",
        ]
    )
    if matched_id:
        lines.append(f"- matched_id: {matched_id}")
    if evidence_tier:
        lines.append(f"- evidence_tier: {evidence_tier}")
    lines.extend(
        [
            f"- policy: {policy_id}",
            f"- idempotency_key: {idempotency_key}",
            "",
            "The screen-diff stall watcher classified this unit as the same stall class on "
            f"{consecutive} consecutive passes. This journal records that observation so a "
            "coordinator reads the durable record; it asserts nothing about whether the "
            "unit is dead, whether its work is complete, or whether any remedy was "
            "applied. The watcher took no action: it does not type, press Enter, reset a "
            "session, or relaunch anything.",
            "",
            "No pane content is carried in this record by construction.",
        ]
    )
    if extra_notes:
        lines.append("")
        lines.extend(f"- {note}" for note in extra_notes if note)
    return "\n".join(lines)


#: The field name the note carries so one firing can be found again, and the EXACT line
#: shape :func:`render_escalation_body` writes it in.
#:
#: Parser and renderer live in the same module deliberately. The readback is the rail's only
#: external authority — the one thing that can say a journal really exists — and it was a
#: substring search while its docstring claimed an exact line match (review j#110281
#: finding_exactmatch). A parser kept next to the renderer can be held to the property that
#: actually matters and is asserted by test: **whatever the renderer emits, the parser finds;
#: nothing else is accepted.**
IDEMPOTENCY_FIELD = "idempotency_key"
_KEY_LINE_PREFIX = f"- {IDEMPOTENCY_FIELD}: "


def note_declares_key(notes: object) -> str:
    """The ONE idempotency key a note declares as a canonical field, or ``""``.

    ``""`` for a note that declares none — and equally for one that declares SEVERAL
    different keys. A note claiming two firings is malformed, and picking either one would
    let a crafted journal be accepted as proof for a firing it does not carry. Ambiguity is
    refused rather than resolved.

    Only the renderer's own line shape counts. Everything the substring search used to
    accept is refused here, and each of these was reproduced as accepted before the fix:

    - ``- idempotency_key: <key>-suffix`` — a longer key that merely starts with this one;
    - ``the string idempotency_key: <key> appears here`` — a prose mention;
    - ``> "idempotency_key: <key>"`` — a quotation of another journal.

    The value is returned verbatim rather than validated here. The caller compares it with
    the firing's own key, which is canonical by construction, so a malformed declaration can
    never match one — and the key's grammar stays in the single place that owns it instead
    of acquiring a second implementation in this module.
    """
    declared = set()
    for raw in str(notes or "").splitlines():
        line = raw.strip()
        if not line.startswith(_KEY_LINE_PREFIX):
            continue
        declared.add(line[len(_KEY_LINE_PREFIX):].strip())
    if len(declared) != 1:
        return ""
    return declared.pop()


__all__ = (
    "IDEMPOTENCY_FIELD",
    "STALL_ESCALATION_GATE",
    "STALL_ESCALATION_REASON",
    "note_declares_key",
    "render_escalation_body",
    "render_policy_id",
)
