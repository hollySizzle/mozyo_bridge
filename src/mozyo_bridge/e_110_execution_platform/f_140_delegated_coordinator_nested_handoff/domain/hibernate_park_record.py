"""The governed parked-state record, and the callback outcome inside it (Redmine #14219 T2b).

Split out of :mod:`.hibernate_basis_producer` when that module reached the module-health ceiling.
It is the one place that reads a park declaration's PROSE-adjacent structure — the governed
``- <field>: <value>`` lines the skill's fixed field shape defines — while the producer proper
stays with markers and conjuncts.

The park marker asserts that a lane is parked. This module checks the record that assertion has to
sit in, and every rule here exists because a weaker version of it shipped and was caught:

* the COMPLETE governed field set, not a convenient subset (j#86443 R2-F4);
* the field VALUES, not merely their presence (j#86503 R3-F3);
* the anchor naming THIS declaration, not merely this issue (j#86525 R4-F1);
* each governed field declared exactly once, since a first-write-wins duplicate has no order
  authority behind it (j#86525 R4-F3);
* the callback outcome as a RECORD for all three outcomes, ``sent`` included (j#86548 R5-F1);
* the record read through the canonical template's own field names rather than invented aliases,
  which had inverted the check — refusing canonical records while accepting unrelated prose
  (j#86548 R5-F2);
* each field and each part carrying its OWN authority, rather than one cross-record search that
  let a pane id inside a retry command stand in for the candidate rows (j#86558 R6-F1/F2).
"""

from __future__ import annotations

import re
from typing import Optional

import argparse
import shlex

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (  # noqa: E501
    KIND_LABELS,
    AnchorError,
    build_marker,
    normalize_anchor,
)

#: The park marker is not accompanied by the governed fixed-field park journal.
GAP_PARK_JOURNAL_FIELDS_ABSENT = "park_journal_fields_absent"
#: journal — so it is some other record's anchor, not this park declaration's own.
GAP_PARK_ANCHOR_NOT_THIS_DECLARATION = "park_anchor_not_this_declaration"
#: alongside it (a reason, and for ``blocked`` a replayable retry command).
GAP_PARK_CALLBACK_DETAIL_ABSENT = "park_callback_detail_absent"
#: operator's next move differs.
GAP_PARK_JOURNAL_FIELDS_INVALID = "park_journal_fields_invalid"

#: Returned when one governed field is declared more than once with DIFFERING values.
_FIELD_CONFLICT = object()


def _field_pattern(*names: str) -> "re.Pattern[str]":
    """A line-anchored governed ``- <name>: <value>`` matcher accepting any of ``names`` (pure).

    The same shape the glance reads its disposition fields from — a list marker, emphasis,
    backticks and an ASCII or fullwidth colon are tolerated — and, like the glance, several
    spellings of the same field are accepted rather than one being imposed.
    """
    alternation = "|".join(re.escape(name) for name in names)
    return re.compile(
        r"^\s*[-*]?\s*\**\s*(?:" + alternation + r")\**\s*[:：]\s*(?P<value>.+?)\s*$",
        re.MULTILINE | re.IGNORECASE,
    )


def governed_field(notes: str, *names: str):
    """The governed field's single value, ``""`` if absent, or :data:`_FIELD_CONFLICT` (pure).

    Reading only the FIRST match made a note self-contradictory-but-passing: appending
    ``callback_result: invented`` after ``callback_result: sent`` left the first one authoritative
    with nothing to justify that order (checkpoint j#86525 R4-F3). Every other layer of this
    surface folds duplicates the same way — identical repeats collapse, differing ones are a typed
    conflict — so the governed fields do too.
    """
    values = {
        match.group("value").strip().strip("`")
        for match in _field_pattern(*names).finditer(notes or "")
    }
    if not values:
        return ""
    if len(values) > 1:
        return _FIELD_CONFLICT
    return values.pop()


#: The governed fixed fields a parked-state journal records, per the skill's own fixed field shape
#: (``references/workflow.md`` ``## Sublane 完了 guardrail``): the parked state is a handoff-worthy
#: ``blocked`` state, so besides the dependency fields it carries the ``durable_anchor`` it is filed
#: against and the ``callback_result`` that makes the state complete.
#:
#: The COMPLETE set is required, not a convenient subset (checkpoint j#86443 R2-F4). Checking four
#: of the six let a note that never called back — the exact failure the guardrail was written for
#: (`progress_without_callback`) — read as an affirmative park basis. "The lane is parked" and "the
#: park was handed off" are one durable state in that contract, so the producer requires the whole
#: record rather than leaving half of it to an action-time obligation.
#: Fields whose presence is what the record needs (their content is free text by contract).
_PARK_FREE_FIELDS = (("blocked_by",), ("resume_condition",))
_PARK_STATE_BLOCKED = "blocked"
#: ``callback_result: sent | blocked | not-attempted`` — the skill fixes the vocabulary, and
#: "silence is not allowed" is the whole point of the field. An invented value is silence wearing a
#: token, so it is refused rather than counted as a callback.
_CALLBACK_SENT = "sent"
_CALLBACK_BLOCKED = "blocked"
_CALLBACK_NOT_ATTEMPTED = "not-attempted"
_PARK_CALLBACK_RESULTS = frozenset({_CALLBACK_SENT, _CALLBACK_BLOCKED, _CALLBACK_NOT_ATTEMPTED})
#: ``resume_owner: coordinator`` — the guardrail assigns re-dispatch to the coordinator by name; a
#: park that nominates anyone else has not handed resume ownership to who actually owns it.
_PARK_RESUME_OWNER = "coordinator"
#: ``durable_anchor: #<issue_id> j#<gate_journal_id>``.
_DURABLE_ANCHOR_RE = re.compile(r"^#(?P<issue>\d+)\s+j#(?P<journal>\d+)$")

#: The canonical callback-outcome record, verbatim from the skill's own template
#: (``references/workflow.md`` ``### Callback outcome journal テンプレート``):
#:
#:     - target: coordinator (`--target coordinator`) | <coordinator_codex_%pane>
#:     - result: sent | blocked | not-attempted
#:     - on sent: command + observed landing marker
#:     - on blocked: reason / candidates (`agents targets` rows) / retry command (`--target %pane
#:       --target-repo auto`)
#:     - on not-attempted: explicit reason
#:
#: R4 invented its own spellings for these because the fixed-field block does not list them — but
#: the template does, and not finding it is not the same as it not existing (checkpoint j#86548
#: R5-F2). The invented aliases REJECTED canonical records while ACCEPTING values that had nothing
#: to do with a callback, which is the meaning of the check inverted. The template is the contract;
#: only the parked-state fold-in spelling ``callback_result`` is accepted alongside ``result``
#: because the fixed-field shape itself uses it.
_CALLBACK_RESULT_FIELDS = ("result", "callback_result")
_CALLBACK_TARGET_FIELD = "target"
_CALLBACK_DETAIL_FIELDS = {
    _CALLBACK_SENT: ("on sent",),
    _CALLBACK_BLOCKED: ("on blocked",),
    _CALLBACK_NOT_ATTEMPTED: ("on not-attempted",),
}

#: A pane token as ``agents targets`` prints one: ``%14`` or ``w3F:p4``, matched WHOLE. A target is
#: one of these or the natural coordinator token — never a phrase that merely mentions one.
_PANE_TOKEN_RE = re.compile(r"(?:%\d+|\w+:p\w+)\Z")
#: The coordinator's natural target (`--target coordinator`), the normal route.
_COORDINATOR_TARGET = "coordinator"
#: The delivery command and the receiver a coordinator callback must name.
_HANDOFF_SEND_RE = re.compile(r"\bhandoff\s+send\b", re.IGNORECASE)
_RECEIVER_CODEX = "codex"
#: The canonical handoff marker's mandatory fields (``handoff.build_marker``): the source system,
#: the anchor, the kind, and the receiver. A token missing any of them was never produced by the
#: sender, so it is not the landing observation — each is checked individually where its value is.
_MARKER_REQUIRED_FIELDS = ("source", "issue", "journal", "kind", "to")


def canonical_target(value: str) -> str:
    """The canonical coordinator target a ``target:`` field names, or ``""`` (pure).

    The template writes the two permitted forms as ``coordinator (`--target coordinator`)`` and
    ``<coordinator_codex_%pane>`` — both of which SAY they are the coordinator's. So the field must
    name the coordinator, and the effective target is then either that natural token or the one
    pane it resolves to.

    Two ways this was wrong before. It asked whether ``"coordinator" in value.lower()``, so
    ``noncoordinator`` and ``the-coordinator-ish`` read as the coordinator; and once whole-token
    matching landed, a bare pane still passed on shape alone — ``same-lane worker w3F:p3`` is a
    well-formed pane, and nothing in it claims to be the coordinator (checkpoint j#86562 R7-F1).
    A pane is a target only when the record says whose it is, and only when there is exactly one:
    two panes name no single place the callback went.
    """
    tokens = [token.strip("`(),") for token in str(value).split()]
    if not any(token == _COORDINATOR_TARGET for token in tokens):
        return ""
    panes = {token for token in tokens if _PANE_TOKEN_RE.match(token)}
    if len(panes) > 1:
        return ""
    return panes.pop() if panes else _COORDINATOR_TARGET


#: A record's command is one invocation that WAS run (``sent``) or WILL
#: be replayed (``blocked``); anything that composes, conditions, redirects or substitutes commands
#: means the token sequence is not that invocation. Control detection lives in
#: :func:`_lex_command` (punctuation tokens outside quotes + inline substitution characters).
#: The CLI's own entry points (``pyproject`` console scripts).
_CLI_ENTRYPOINTS = ("mozyo-bridge", "mozyo")

#: The canonical ``handoff send`` grammar, mirrored as DATA: ``(flag, required, choices, value)``
#: where ``value`` is ``str`` / ``float`` / ``int`` for a value option, ``"flag"`` for a bare
#: switch and ``"append"`` for a repeatable option. The domain must not import the
#: application-layer parser builder, so this table exists — and a drift-guard test builds the REAL
#: parser (``configure_handoff_parser`` + ``add_handoff_select_args``) and asserts this table
#: matches it action for action, the same pattern ``callback_delivery`` uses for its tokens.
#: An invocation this grammar rejects is one the CLI would refuse to run, and a command that never
#: ran is not delivery evidence (checkpoint j#86626 R10-F1 — the previous check looked at token 0
#: and ``handoff send`` only, and its own positive fixture was missing the required ``--source`` /
#: ``--kind``).
_SEND_OPTIONS: tuple = (
    ("--to", True, ("claude", "codex"), str),
    ("--source", True, ("asana", "redmine"), str),
    ("--kind", True, tuple(sorted(KIND_LABELS)), str),
    ("--task-id", False, None, str),
    ("--comment-id", False, None, str),
    ("--anchor-url", False, None, str),
    ("--issue", False, None, str),
    ("--journal", False, None, str),
    ("--target", False, None, str),
    ("--target-repo", False, None, str),
    ("--target-lane", False, None, str),
    ("--target-project", False, None, str),
    ("--allow-direct-worker", False, None, "flag"),
    ("--workdir", False, None, str),
    (
        "--role-profile",
        False,
        ("coordinator", "delegated_coordinator", "implementation_gateway", "implementation_worker"),
        str,
    ),
    ("--profile-field", False, None, "append"),
    ("--main-lane-exception", False, None, str),
    ("--mode", False, ("pending", "queue-enter", "standard"), str),
    ("--summary", False, None, str),
    ("--force", False, None, "flag"),
    ("--landing-timeout", False, None, float),
    ("--submit-delay", False, None, float),
    ("--read-lines", False, None, int),
    ("--queue-enter-retry-window", False, None, float),
    ("--queue-enter-retry-interval", False, None, float),
    ("--no-target-activation", False, None, "flag"),
    ("--restore-previous-active", False, None, "flag"),
    ("--record-format", False, ("both", "json", "text"), str),
    ("--record-command", False, None, str),
    ("--persist-delivery", False, None, "flag"),
    ("--select", False, None, "flag"),
    ("--target-session", False, None, str),
)

#: Options that legitimately repeat (argparse append) — exempt from the conflict rule.
_APPEND_FLAGS = frozenset(
    flag for flag, _required, _choices, value in _SEND_OPTIONS if value == "append"
)
_VALUE_FLAGS = frozenset(
    flag for flag, _required, _choices, value in _SEND_OPTIONS if value not in ("flag",)
)


class _GrammarRefusal(Exception):
    """Raised instead of argparse's stderr-print-and-exit — the check is side-effect free."""


class _RefusingParser(argparse.ArgumentParser):
    def error(self, message):  # pragma: no cover - trivial override
        raise _GrammarRefusal(message)


def _build_send_parser() -> argparse.ArgumentParser:
    # ``allow_abbrev=False``: the canonical CLI accepts long-option abbreviation, but an
    # abbreviated flag in a durable record reintroduces the very ambiguity the conflict rule
    # exists to refuse — ``--ki review_request --kind reply`` declares two values for one
    # logical option while wearing two names (checkpoint j#86645 R11-F3). Evidence is written
    # unabbreviated.
    parser = _RefusingParser(prog="handoff-send-evidence", add_help=False, allow_abbrev=False)
    for flag, required, choices, value in _SEND_OPTIONS:
        dest = flag[2:].replace("-", "_")
        if value == "flag":
            parser.add_argument(flag, dest=dest, required=required, action="store_true")
        elif value == "append":
            parser.add_argument(flag, dest=dest, required=required, action="append")
        else:
            parser.add_argument(
                flag,
                dest=dest,
                required=required,
                choices=list(choices) if choices else None,
                type=None if value is str else value,
            )
    return parser


_SEND_PARSER = _build_send_parser()


def _conflicting_store_flags(tokens: "list[str]") -> bool:
    """Whether any single-valued flag repeats with DIFFERING values (pure).

    Argparse itself is last-write-wins, but a durable record whose command says two things has no
    single meaning to trust (the R8 rule). Append options (``--profile-field``) repeat by design
    and are exempt; identical repeats collapse.
    """
    seen: dict = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--"):
            index += 1
            continue
        flag, eq, inline = token.partition("=")
        if flag in _APPEND_FLAGS:
            index += 1 if eq else 2
            continue
        if flag in _VALUE_FLAGS:
            if eq:
                value = inline
                index += 1
            elif index + 1 < len(tokens) and not tokens[index + 1].startswith("--"):
                value = tokens[index + 1]
                index += 2
            else:
                index += 1
                continue  # missing value: the grammar parse refuses it
            if flag in seen and seen[flag] != value:
                return True
            seen[flag] = value
        else:
            index += 1
    return False


def _lex_command(text: str) -> "list[str] | None":
    """``text`` as the CLI would receive it after shell lexing, or ``None`` (pure).

    ``text.split()`` broke every quoted argument — ``--summary \'park callback delivered\'`` is one
    value to the shell and three tokens to a naive split, so a correctly-recorded executed command
    stopped satisfying the basis (checkpoint j#86645 R11-F4). ``shlex`` in posix mode is the
    side-effect-free equivalent of that lexing; an unclosed quote is fail-closed.

    Shell CONTROL is detected as punctuation tokens outside quotes (``;`` ``|`` ``&`` ``<`` ``>``
    ``(`` ``)``), so a quoted separator inside a summary is content, not composition. Command
    substitution (``$(`` and backtick) stays refused wherever it appears: inside double quotes it
    still executes, and refusing the single-quoted false positive is the fail-closed direction.
    """
    if "`" in text or "$(" in text or "\n" in text:
        return None
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";|&<>()")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    # With ``punctuation_chars`` set, operators outside quotes surface as their own tokens made
    # entirely of those characters.
    if any(token and all(char in ";|&<>()" for char in token) for token in tokens):
        return None
    return tokens


def _send_invocation(text: str) -> "argparse.Namespace | None":
    """The executable ``mozyo-bridge handoff send ...`` invocation ``text`` IS, or ``None`` (pure).

    ``text`` must BE the invocation: the caller has already cut the command component out of the
    record, so token 0 is the CLI entry point or this is not a command. The arguments are then
    validated against the mirrored canonical grammar — required options, choices, unknown
    arguments, value types, both ``--flag value`` and ``--flag=value`` spellings (abbreviations
    refused, see the parser) — the anchor against the canonical ``normalize_anchor``, and the
    POST-PARSE semantics the CLI itself enforces after argparse: ``--kind custom`` without
    ``--summary`` is refused by ``build_notification_body`` before anything is sent, so it is not
    an executable delivery either (checkpoint j#86645 R11-F2).

    Still refused outright: shell control outside quotes, an unclosed quote, a second entry point,
    and a single-valued flag repeated with differing values.
    """
    tokens = _lex_command(text)
    if tokens is None:
        return None
    if not tokens or tokens[0] not in _CLI_ENTRYPOINTS:
        return None
    if any(token in _CLI_ENTRYPOINTS for token in tokens[1:]):
        return None
    if tokens[1:3] != ["handoff", "send"]:
        return None
    arguments = tokens[3:]
    if _conflicting_store_flags(arguments):
        return None
    try:
        namespace = _SEND_PARSER.parse_args(arguments)
    except _GrammarRefusal:
        return None
    try:
        normalize_anchor(
            namespace.source,
            task_id=namespace.task_id,
            comment_id=namespace.comment_id,
            anchor_url=namespace.anchor_url,
            issue=namespace.issue,
            journal=namespace.journal,
        )
    except AnchorError:
        return None
    # Canonical post-parse rule (``build_notification_body``): a custom kind carries its summary.
    if namespace.kind == "custom" and not namespace.summary:
        return None
    return namespace


#: The exact labels the template puts before a command. Anything else preceding the invocation is
#: a wrapper, not a label (checkpoint j#86577 R9-F1). The label is stripped by the CALLER, so the
#: parser has no guessing to do.
_COMMAND_LABEL_RE = re.compile(r"^(?:retry\s+command|retry|command)\s*:\s*", re.IGNORECASE)


def _strip_command_label(text: str) -> str:
    """Drop one template label from the FRONT of ``text`` (pure). Nothing else is removed."""
    return _COMMAND_LABEL_RE.sub("", text.strip(), count=1)


#: ``[mozyo:handoff:<body>]`` read RAW — the shared scanner folds duplicate keys last-write-wins,
#: which is exactly what has to be refused here.
_HANDOFF_MARKER_RE = re.compile(r"\[mozyo:handoff:(?P<body>[^\]]*)\]")
#: The durable source this evidence surface is anchored in.
_MARKER_SOURCE = "redmine"


def _raw_marker_fields(body: str) -> "dict | None":
    """One marker body as ``{key: value}``, or ``None`` when a key is declared twice with
    differing values (pure).

    The shared scanner's last-write-wins is right for a lenient reader and wrong for evidence:
    ``journal=1:journal=85500`` and ``to=claude:to=codex`` both resolved to the acceptable value
    and passed (checkpoint j#86569 R8-F2). A token that says two things proves neither.
    """
    fields: dict = {}
    for token in body.split(":"):
        key, sep, value = token.strip().partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key in fields and fields[key] != value:
            return None
        fields[key] = value
    return fields


def _observed_marker_fields(detail: str) -> "dict | None":
    """THE landing marker's fields in ``detail``, or ``None`` (pure).

    Every marker in the detail is read, not just a matching one; identical repeats collapse and
    differing ones conflict — the same rule the governed fields follow (checkpoint j#86577 R9-F3).
    """
    markers = []
    for match in _HANDOFF_MARKER_RE.finditer(detail):
        fields = _raw_marker_fields(match.group("body"))
        if fields is None:
            return None
        markers.append(fields)
    distinct = {tuple(sorted(fields.items())) for fields in markers}
    if len(distinct) != 1:
        return None
    return dict(distinct.pop())


def _split_outside_quotes(text: str, separators: str) -> "tuple[str, str] | None":
    """Split ``text`` at the FIRST separator that sits outside any quote, or ``None`` (pure).

    The command/observation boundary and the blocked three-part boundary are template structure,
    so a ``/`` inside a quoted summary is content, not a boundary (checkpoint j#86645 R11-F4).
    """
    quote = ""
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
            continue
        if char in separators:
            return text[:index], text[index + 1:]
    return None


def _parts_outside_quotes(text: str, separator: str) -> "list[str]":
    """Every ``separator``-delimited part of ``text``, quote-aware (pure)."""
    parts = []
    remainder = text
    while True:
        split = _split_outside_quotes(remainder, separator)
        if split is None:
            parts.append(remainder)
            return parts
        head, remainder = split
        parts.append(head)


def _sent_detail_gap(detail: str, *, target: str, source_issue: str, journal: str) -> Optional[str]:
    """Whether a ``sent`` record is THIS callback's delivery evidence (pure).

    Three authorities, each with its own comparison:

    * the COMMAND must be an invocation the CLI would actually run
      (:func:`_send_invocation`), delivering ``--to codex`` at the target the record declares;
    * the MARKER must be about this park declaration — this durable source, a known kind, the
      coordinator's Codex, this issue and this journal;
    * the two must be the SAME delivery: the marker the command composes — via the canonical
      ``normalize_anchor`` + ``build_marker``, not a re-implementation — must field-for-field
      equal the marker observed (checkpoint j#86626 R10-F2; an executable command for a DIFFERENT
      anchor or kind can otherwise borrow another handoff's landing observation).
    """
    split = _split_outside_quotes(detail, "/+")
    if split is None:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    command, observation = split
    namespace = _send_invocation(_strip_command_label(command))
    if namespace is None:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    if namespace.to != _RECEIVER_CODEX:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    if namespace.target != target:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    # ``--mode pending`` places the body in the composer WITHOUT pressing Enter — the canonical
    # rail returns ``pending_input``, not a delivery. A pending command sent nothing (checkpoint
    # j#86645 R11-F2).
    if namespace.mode == "pending":
        return GAP_PARK_CALLBACK_DETAIL_ABSENT

    observed = _observed_marker_fields(observation)
    if observed is None:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    if observed.get("source") != _MARKER_SOURCE:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    if observed.get("kind") not in KIND_LABELS:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    if observed.get("to") != _RECEIVER_CODEX:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    if (
        observed.get("issue") != str(source_issue).strip()
        or observed.get("journal") != str(journal).strip()
    ):
        return GAP_PARK_CALLBACK_DETAIL_ABSENT

    anchor = normalize_anchor(
        namespace.source,
        task_id=namespace.task_id,
        comment_id=namespace.comment_id,
        anchor_url=namespace.anchor_url,
        issue=namespace.issue,
        journal=namespace.journal,
    )
    built_body_match = _HANDOFF_MARKER_RE.fullmatch(
        build_marker(anchor, namespace.kind, namespace.to)
    )
    if built_body_match is None:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    built = _raw_marker_fields(built_body_match.group("body"))
    if built != observed:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    return None


def _blocked_detail_gap(detail: str, *, source_issue: str, journal: str) -> Optional[str]:
    """Whether a ``blocked`` record carries reason / candidates / retry command (pure).

    Read as the template writes it — EXACTLY three ``/``-separated parts (quote-aware), each with
    its own job:

    * part 1 — the reason, which must be text and not evidence standing in for one;
    * part 2 — the candidate rows, which must actually name at least one pane;
    * part 3 — a retry the CLI would actually run (same invocation rules as ``sent``), delivering
      ``--to codex``, pinned at ONE of those candidates and at ``--target-repo auto`` — and
      anchored at THIS park declaration: a replay of this callback carries this callback's anchor,
      and an executable retry for another issue or journal is a handoff to a different ticket
      (checkpoint j#86645 R11-F1). A ``--mode pending`` retry is likewise refused: replaying it
      would place the body without submitting, which is not the delivery the record promises.
    """
    parts = [part.strip() for part in _parts_outside_quotes(detail, "/")]
    parts = [part for part in parts if part]
    if len(parts) != 3:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    reason, candidates, retry = parts

    if any(_PANE_TOKEN_RE.match(token.strip("`(),")) for token in reason.split()):
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    if _HANDOFF_SEND_RE.search(reason):
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    candidate_panes = {
        token.strip("`(),")
        for token in candidates.split()
        if _PANE_TOKEN_RE.match(token.strip("`(),"))
    }
    if not candidate_panes:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT

    namespace = _send_invocation(_strip_command_label(retry))
    if namespace is None:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    if namespace.to != _RECEIVER_CODEX:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    if namespace.target not in candidate_panes:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    if namespace.target_repo != "auto":
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    if namespace.mode == "pending":
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    if (
        namespace.source != _MARKER_SOURCE
        or str(namespace.issue or "").strip() != str(source_issue).strip()
        or str(namespace.journal or "").strip() != str(journal).strip()
    ):
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    return None


def park_journal_gap(
    notes: str, *, source_issue: str = "", declaration_journal: str = ""
) -> Optional[str]:
    """The typed reason the note is not a valid governed parked-state journal, else ``None`` (pure).

    Presence is not the contract (checkpoint j#86503 R3-F3): the skill's fixed field shape pins the
    VALUES too, and a record whose ``callback_result`` is an invented word, whose ``resume_owner``
    is not the coordinator, or whose ``durable_anchor`` points somewhere else is not the record the
    park basis rests on. Reading those as satisfied is the same class of defect as reading a
    marker's self-declared authority: the shape looked right, so the content went unchecked.

    Two things the value alone still does not settle (checkpoint j#86525):

    * **which record the anchor names** (R4-F1). ``#<issue> j#<journal>`` has to point at THIS park
      declaration, not merely at some journal of this issue — otherwise any older callback journal
      on the same issue can stand in for this park's own handoff.
    * **whether the outcome was actually recorded** (R4-F2). ``blocked`` and ``not-attempted`` are
      legitimate outcomes, but the guardrail's point is that they are legitimate WHEN RECORDED: a
      blocked callback carries its reason and a replayable retry command, a not-attempted one
      carries its reason. Accepting the bare token re-admits exactly the "parked and nobody was
      told" state the completion rule exists to prevent — the token would be the silence, not the
      record of it.

    ``source_issue`` / ``declaration_journal`` are the issue and journal these notes came from —
    the SCOPE of the read, not the candidate's lane or head, so the producer's no-target-binding
    invariant is untouched. Either being empty relaxes only that comparison.
    """
    state = governed_field(notes, "state")
    callback_result = governed_field(notes, *_CALLBACK_RESULT_FIELDS)
    resume_owner = governed_field(notes, "resume_owner")
    anchor_raw = governed_field(notes, "durable_anchor")
    free = [governed_field(notes, *names) for names in _PARK_FREE_FIELDS]

    fields = [state, callback_result, resume_owner, anchor_raw, *free]
    # A field declared twice with differing values has no order authority (R4-F3): refuse before
    # any of them is read as the value.
    if any(value is _FIELD_CONFLICT for value in fields):
        return GAP_PARK_JOURNAL_FIELDS_INVALID
    if not all(fields):
        return GAP_PARK_JOURNAL_FIELDS_ABSENT

    if state.lower() != _PARK_STATE_BLOCKED:
        return GAP_PARK_JOURNAL_FIELDS_INVALID
    outcome = callback_result.lower()
    if outcome not in _PARK_CALLBACK_RESULTS:
        return GAP_PARK_JOURNAL_FIELDS_INVALID
    if resume_owner.lower() != _PARK_RESUME_OWNER:
        return GAP_PARK_JOURNAL_FIELDS_INVALID

    detail_gap = _callback_outcome_gap(
        notes, outcome, source_issue=source_issue, journal=declaration_journal
    )
    if detail_gap is not None:
        return detail_gap

    anchor = _DURABLE_ANCHOR_RE.match(anchor_raw)
    if anchor is None:
        return GAP_PARK_JOURNAL_FIELDS_INVALID
    if source_issue and anchor.group("issue") != str(source_issue).strip():
        return GAP_PARK_JOURNAL_FIELDS_INVALID
    if declaration_journal and anchor.group("journal") != str(declaration_journal).strip():
        return GAP_PARK_ANCHOR_NOT_THIS_DECLARATION
    return None


def _callback_outcome_gap(
    notes: str, outcome: str, *, source_issue: str = "", journal: str = ""
) -> Optional[str]:
    """The reason the callback outcome is not a complete record, else ``None`` (pure).

    Every outcome — ``sent`` included — has to be a RECORD, not a token, and the record has to be
    about THIS callback. Each requirement is carried by one named place:

    ======================  ==================================================================
    field / part            what it must prove
    ======================  ==================================================================
    ``target``              the callback was routed to the coordinator (natural target or pane)
    ``on sent`` command     it was delivered, to the coordinator's Codex, at that same target
    ``on sent`` marker      the landing marker observed is THIS issue + THIS declaration, to codex
    ``on blocked`` part 1   the reason
    ``on blocked`` part 2   the candidate panes (``agents targets`` rows)
    ``on blocked`` part 3   a retry pinned to one of those panes, replayable
    ``on not-attempted``    the explicit reason
    ======================  ==================================================================
    """
    declared = governed_field(notes, _CALLBACK_TARGET_FIELD)
    if declared is _FIELD_CONFLICT or not declared:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    # The contract's first line is "EVERY outcome: the target is the coordinator's natural target
    # or a resolved pane". R7 wrote that in the contract but wired the check inside the ``sent``
    # branch only, so a blocked / not-attempted record could name any target at all (checkpoint
    # j#86562 R7-F1). It is a common rule, so it is applied once, here, before the branch.
    target = canonical_target(declared)
    if not target:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT

    detail = governed_field(notes, *_CALLBACK_DETAIL_FIELDS[outcome])
    if detail is _FIELD_CONFLICT or not detail:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT

    if outcome == _CALLBACK_SENT:
        return _sent_detail_gap(
            detail, target=target, source_issue=source_issue, journal=journal
        )
    if outcome == _CALLBACK_BLOCKED:
        return _blocked_detail_gap(detail, source_issue=source_issue, journal=journal)
    # not-attempted: an explicit reason, which is the whole field.
    return None


__all__ = [
    "GAP_PARK_ANCHOR_NOT_THIS_DECLARATION",
    "GAP_PARK_CALLBACK_DETAIL_ABSENT",
    "GAP_PARK_JOURNAL_FIELDS_ABSENT",
    "GAP_PARK_JOURNAL_FIELDS_INVALID",
    "governed_field",
    "park_journal_gap",
]
