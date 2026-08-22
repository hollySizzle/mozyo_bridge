"""Version-scoped sublane continuous tracking (Redmine #15844).

ADR-0011 makes the project coordinator the owner of a Redmine Version and makes *drain*
— taking every dispatched sublane to a terminal state — its most important duty. The
runtime had no surface that could answer the drain question, and the reason is
structural: **every existing enumeration starts from the lane set.**

- ``sublane reboot-audit`` (#14499) walks the lifecycle rows, so an issue with no lane is
  outside its domain entirely;
- ``workflow drain-queue`` (#13967) and ``workflow glance`` start from the *active* lane
  roster;
- ``workflow dispatch-plan`` (#12920) does start from a Version — but it projects onto
  ``open_leaf_issues`` because it answers "what do I dispatch next?". A left-behind lane
  is by definition on the *closed* side of that projection, so it can never appear.

So the #15789 shape — the issue is closed, the work is integrated, and the lane is still
sitting at ``active`` — falls through every surface at once. A coordinator handing work
over one item at a time was substituting for the missing machine, which is exactly the
"the project coordinator layer does not exist as a real thing" the owner named.

This module is the pure classifier for the Version-side join. Given a Version's issue set
and the lane lifecycle rows this workspace owns, it answers **what is owed**, per issue,
in one total and deterministic pass.

What it deliberately does NOT do
--------------------------------

It never decides *which recovery rail* a left-behind lane should take. That judgement
already exists — ``reboot-audit`` makes it on a four-authority join, and
``recovery-rail-taxonomy.md`` (#15841) maps the 25 rails and their intersections.
Re-deriving it here would mint a second vocabulary for the same shape, which is the very
overlap #15846 (binding Phase 2) exists to remove. Tracking therefore *names the lane* and
hands off to the existing rail entry point.

It also emits no composite ``integration_ready`` verdict. Under ADR-0011 a Version's
integration disposition is a decision the project coordinator owns, and Version close
additionally needs owner approval. A read-only aggregation that says "ready" in one word
is a count wearing the costume of a judgement. The roll-up is a count, not a button — the
same line ``reboot-audit`` draws.

Design spec: ``vibes/docs/specs/version-owning-project-coordinator.md``.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from mozyo_bridge.core.state.lane_lifecycle_model import (
    DISPOSITION_RETIRED,
    DISPOSITION_SUPERSEDED,
)

#: A lane id this surface is willing to put into a command or a durable line.
#:
#: The same conservative posture as ``herdr_identity._NAME_CHAR_RE``, whose docstring
#: states it "rejects anything that could smuggle a shell/argv token before the structured
#: decode even runs". ``LaneLifecycleKey`` itself only rejects the empty string, so a row
#: carrying ``issue_1; printf X`` is a storable lane id — the guard has to live wherever
#: that id is re-emitted.
#:
#: **The alphabet is the canonical producers', not a sample's** (Redmine #15844 review
#: j#110012 finding_1; verdict j#110013). The first form here was ``^[A-Za-z0-9_]+$``,
#: justified by all 121 rows of the operator store satisfying it — but that store happened
#: to contain no project-gateway lane, and
#: :func:`...workflow_role_authority.project_gateway_lane_id` builds ``pgwv1_<slug>-<digest>``,
#: so **every** canonically derived gateway lane carries a hyphen and was refused a command.
#: ``issue-15844-feature`` — which ``parse_issue_from_lane_label`` officially parses and
#: ``SublaneCreateRequest`` accepts — was refused for the same reason. A sample bounds a
#: measurement, not a vocabulary.
#:
#: The leading character stays non-hyphen deliberately: relaxing to a plain
#: ``[A-Za-z0-9_-]+`` would admit ``-x`` / ``--execute`` and hand the argv-flag spoofing
#: closed in R2 straight back through the hyphen. ``project_gateway_lane_id`` strips
#: leading hyphens and prefixes ``pgwv1_``, so the constraint costs its output nothing
#: (verified over its slug edge cases, including an all-hyphen and a non-ASCII scope).
#:
#: Deliberately still NARROWER than the create ingress: ``SublaneCreateRequest.missing_fields``
#: only checks non-emptiness, so "accept whatever ingress accepts" would empty the guard.
#: What this aligns with is the canonical producers' output and the documented naming
#: conventions.
_LANE_ID_SAFE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")

#: Characters neutralized wherever a lane id reaches rendered text or a payload.
#:
#: Covers C0, DEL and C1 — and **U+2028 / U+2029**, which ``str.splitlines()`` also treats
#: as boundaries. The first version stopped at C1 and let a LINE SEPARATOR through, which
#: still forged the record's line structure (measured: +3 lines, one per echo site;
#: Redmine #15844 review j#110012 finding_2).
#:
#: The set was not guessed twice: sweeping the whole code-point space shows ``splitlines()``
#: splits on exactly ten code points (0x0a 0x0b 0x0c 0x0d 0x1c 0x1d 0x1e 0x85 0x2028
#: 0x2029), and the ranges below cover all ten. A regression re-derives that sweep so the
#: set cannot silently drift from the runtime's own behaviour.
#:
#: A raw newline in an id forges the OUTPUT'S OWN line structure — a fabricated ``$`` step
#: line and a split ``lane <id>:`` line were both reproduced. ``shlex.quote`` does not help
#: here: it single-quotes the value and leaves the boundary inside, so the line still
#: breaks. Quoting and line-safety are two different defects with two different fixes.
_CONTROL_CHARS_RE = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\u2028\\u2029]")

#: The lane dispositions that mean "this lane is finished with"; the terminal set is
#: ``managed-state-model.md``'s, not a second definition. ``hibernated`` is deliberately
#: absent: a hibernated lane has released its process but still owns its issue, so it is
#: still owed a drain.
TERMINAL_LANE_DISPOSITIONS = (DISPOSITION_RETIRED, DISPOSITION_SUPERSEDED)

#: The issue is closed but a non-terminal lane still owns it. THE #15789 shape.
DISPOSITION_DRAIN_OWED = "drain_owed"
#: An open issue with a live lane — the ordinary in-progress shape.
DISPOSITION_IN_FLIGHT = "in_flight"
#: The issue is closed and nothing non-terminal is left holding it.
DISPOSITION_SETTLED = "settled"
#: Every lane reached a terminal state, but the issue is still open. The spine calls a
#: close-ready issue left at 着手中 a durable-state inconsistency, not harmless bookkeeping.
DISPOSITION_LANE_TERMINAL_ISSUE_OPEN = "lane_terminal_issue_open"
#: An open non-leaf issue with no lane: an umbrella is not a dispatch candidate, so its
#: having no lane is not a finding.
DISPOSITION_UMBRELLA_OPEN = "umbrella_open"
#: An open leaf issue with no lane at all.
DISPOSITION_UNDISPATCHED = "undispatched"
#: The issue's open/closed state could not be read. Fail-safe: an unread issue is never
#: reported as settled, because that would pass off a finding about the READ as a finding
#: about the Version.
DISPOSITION_UNKNOWN_ISSUE_STATE = "unknown_issue_state"

#: Every disposition, in the decision-table order of the design spec `## 3.1`.
VERSION_ISSUE_DISPOSITIONS = (
    DISPOSITION_UNKNOWN_ISSUE_STATE,
    DISPOSITION_DRAIN_OWED,
    DISPOSITION_IN_FLIGHT,
    DISPOSITION_SETTLED,
    DISPOSITION_LANE_TERMINAL_ISSUE_OPEN,
    DISPOSITION_UMBRELLA_OPEN,
    DISPOSITION_UNDISPATCHED,
)

#: The dispositions the coordinator is asked to look at. ``in_flight`` is NOT one of them
#: (work in progress is not a finding), and neither is ``undispatched`` — an undispatched
#: leaf is the *dispatch* question, which ``workflow dispatch-plan`` already owns.
ATTENTION_DISPOSITIONS = (
    DISPOSITION_DRAIN_OWED,
    DISPOSITION_LANE_TERMINAL_ISSUE_OPEN,
    DISPOSITION_UNKNOWN_ISSUE_STATE,
)


def is_terminal_lane_disposition(disposition: str) -> bool:
    return (disposition or "").strip() in TERMINAL_LANE_DISPOSITIONS


def is_renderable_lane_id(lane_id: str) -> bool:
    """May this lane id be put into a command or a durable line? (fail-closed)"""
    return bool(_LANE_ID_SAFE_RE.match(lane_id or ""))


def display_lane_id(lane_id: str) -> str:
    """A lane id that cannot forge the line structure of the text it is printed into."""
    return _CONTROL_CHARS_RE.sub(_escape_char, lane_id or "")


def _escape_char(match: "re.Match[str]") -> str:
    r"""``\xNN`` below U+0100, ``\uXXXX`` above — never the raw boundary character."""
    code = ord(match.group())
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def reboot_audit_command(lane_id: str) -> Optional[str]:
    """The diagnosis invocation for ``lane_id``, or ``None`` when it cannot be vouched for.

    Built as structured argv and rendered through ``shlex.quote`` per token — the pattern
    this repo already settled on for copy-pasteable commands in durable records
    (``f_130_handoff_routing/domain/handoff.py``, ``delegation_launch_adopt``,
    ``grandchild_dispatch``), where the same class of finding was adjudicated in
    Redmine #12162 review j#60107.

    Quoting alone is not the whole fix. Two further vectors were reproduced for #15844
    review j#109990 (verdicts in j#109994):

    - an id needs no shell metacharacter to be dangerous — ``issue_1 --execute`` forges an
      extra **argv flag**, which only quoting closes;
    - an id with a newline forges the surrounding record's line structure, which quoting
      does **not** close.

    So an id that fails :func:`is_renderable_lane_id` yields ``None`` and no command is
    offered at all. Sanitizing it into something plausible would hand an operator an
    executable string derived from an identity this surface could not verify — the same
    move as reporting an unread authority as an empty one, which this module refuses
    everywhere else.
    """
    if not is_renderable_lane_id(lane_id):
        return None
    return shell_safe_command(
        ("mozyo-bridge", "sublane", "reboot-audit", "--lane-label", lane_id)
    )


def shell_safe_command(parts: Sequence[str]) -> str:
    """Render structured argv as one copy-pasteable command (per-token ``shlex.quote``).

    The repo's established form for a command that lands in a durable record
    (``handoff.py`` / ``delegation_launch_adopt`` / ``grandchild_dispatch``, adjudicated in
    Redmine #12162 review j#60107).

    Kept as its own function, and tested on inputs :func:`is_renderable_lane_id` would
    reject, **because today the identity guard already excludes every value that quoting
    would change** — measured: mutating the quoting away left the whole suite green. Two
    honest options existed: delete it as subsumed, or keep it as the independent second
    layer the review asked for and make it measurable. Deleting it would mean a future
    loosening of the identity alphabet silently reopens the injection, with no test to
    notice. So it stays, and it is exercised directly rather than only through a caller
    that can never reach its interesting case.
    """
    return " ".join(shlex.quote(part) for part in parts)


@dataclass(frozen=True)
class TrackedLane:
    """One lane lifecycle row, reduced to the two fields tracking reads.

    Nothing else from the row travels: tracking answers "is anything owed", and the fields
    that decide *how to recover* a lane (binding, generation, revision, worktree identity)
    belong to the rails that do the recovering.
    """

    lane_id: str
    lane_disposition: str

    @property
    def is_terminal(self) -> bool:
        return is_terminal_lane_disposition(self.lane_disposition)

    @property
    def identity_renderable(self) -> bool:
        return is_renderable_lane_id(self.lane_id)

    def as_payload(self) -> dict[str, object]:
        return {
            # Display form: a structured consumer escapes this itself, but the same value
            # is also read by eye out of a pasted journal, and a raw control character
            # there forges the record's line structure.
            "lane_id": display_lane_id(self.lane_id),
            "lane_disposition": self.lane_disposition,
            "terminal": self.is_terminal,
            "identity_renderable": self.identity_renderable,
        }


@dataclass(frozen=True)
class VersionIssueFacts:
    """One Version issue joined with the lanes this workspace owns for it.

    ``status_name`` is the readability evidence, not decoration. The normalizer
    (:func:`..._lane_bucket_issue_from_mapping`) reads ``status.is_closed`` with a
    ``False`` default, so an issue whose status object could not be read arrives
    indistinguishable from a genuinely open one — *unless* one also looks at
    ``status.name``, which a well-formed Redmine issue always carries. Treating the
    defaulted ``False`` as "open" would let an unread issue silently join the in-flight
    population.

    Subjects are absent by construction (``LaneBucketIssue`` omits them), so a snapshot of
    this type can be pasted into a durable journal without carrying confidential text.
    """

    issue_id: str
    is_closed: bool
    is_leaf: bool
    tracker: Optional[str] = None
    status_name: Optional[str] = None
    lanes: tuple[TrackedLane, ...] = ()

    @property
    def issue_state_readable(self) -> bool:
        """Was the issue's status actually read, as opposed to defaulted?"""
        return bool((self.status_name or "").strip())

    @property
    def nonterminal_lanes(self) -> tuple[TrackedLane, ...]:
        return tuple(lane for lane in self.lanes if not lane.is_terminal)

    @property
    def terminal_lanes(self) -> tuple[TrackedLane, ...]:
        return tuple(lane for lane in self.lanes if lane.is_terminal)

    def as_payload(self) -> dict[str, object]:
        return {
            "issue_id": self.issue_id,
            "tracker": self.tracker,
            "status_name": self.status_name,
            "is_closed": self.is_closed,
            "is_leaf": self.is_leaf,
            "issue_state_readable": self.issue_state_readable,
            "lanes": [lane.as_payload() for lane in self.lanes],
        }


@dataclass(frozen=True)
class VersionIssueTracking:
    """One issue's disposition, its reason token, and the diagnosis entry point (if any).

    ``diagnosis_steps`` is named for what it is. It was ``next_steps``, which read as "run
    these and the lane is drained" — a claim measured to be false for the very lanes this
    surface finds. For #15842 / #15843, ``reboot-audit`` reads Redmine and prescribes
    ``guarded_close``, while ``sublane retire`` reads the local durable record and refuses
    with ``issue_not_closed`` / ``durable_record_missing``: two authorities disagreeing
    about one fact, so the chain stops one rail short of terminal (measured; recorded in
    j#109995, class named by j#109972). What this surface can honestly offer is the entry
    point to the *diagnosis*, not a drain that completes.

    ``unrenderable_lane_ids`` keeps a lane whose identity failed the guard visible. Owed
    work does not stop being owed because its id could not be vouched for; suppressing the
    command must not suppress the finding.
    """

    facts: VersionIssueFacts
    disposition: str
    reason: str
    diagnosis_steps: tuple[str, ...] = ()
    unrenderable_lane_ids: tuple[str, ...] = ()

    @property
    def issue_id(self) -> str:
        return self.facts.issue_id

    @property
    def needs_attention(self) -> bool:
        return self.disposition in ATTENTION_DISPOSITIONS

    def as_payload(self) -> dict[str, object]:
        return {
            "issue_id": self.facts.issue_id,
            "disposition": self.disposition,
            "reason": self.reason,
            "diagnosis_steps": list(self.diagnosis_steps),
            "unrenderable_lane_ids": [
                display_lane_id(lane) for lane in self.unrenderable_lane_ids
            ],
            "facts": self.facts.as_payload(),
            "needs_attention": self.needs_attention,
        }


def classify_version_issue(facts: VersionIssueFacts) -> VersionIssueTracking:
    """Classify one joined issue (pure, total, first-match).

    The table is the design spec `## 3.1`, in order:

    1. the issue's state could not be read -> :data:`DISPOSITION_UNKNOWN_ISSUE_STATE`;
    2. a non-terminal lane and a closed issue -> :data:`DISPOSITION_DRAIN_OWED`;
    3. a non-terminal lane and an open issue -> :data:`DISPOSITION_IN_FLIGHT`;
    4. no non-terminal lane and a closed issue -> :data:`DISPOSITION_SETTLED`;
    5. no non-terminal lane, open, but a terminal lane exists ->
       :data:`DISPOSITION_LANE_TERMINAL_ISSUE_OPEN`;
    6. no lane at all, open, not a leaf -> :data:`DISPOSITION_UMBRELLA_OPEN`;
    7. no lane at all, open, a leaf -> :data:`DISPOSITION_UNDISPATCHED`.

    **Totality**: after rule 1 fixes readability, rules 2-3 split "some non-terminal lane"
    on closed-ness, and rules 4-7 split "no non-terminal lane" on closed-ness, then on
    whether any lane exists, then on leaf-ness. The last two branches exhaust a boolean,
    so there is no unreachable rule and no catch-all to write.

    **The one intersection** (spec `## 3.1`, ``role_precedence``): being an umbrella and
    holding a lane are not mutually exclusive — measured on 2026-08-22, #15631 is a
    non-leaf of Version #329 *and* owns the lane ``issue_15631_trial``. Leaf-ness is only
    ever the discriminant for "should an open issue with no lane count as undispatched?",
    so rules 2/3 read the lanes and never consult it. Collapsing an umbrella that owns a
    lane into ``umbrella_open`` would make a roll-up lane's left-behind state permanently
    invisible — the very failure this module exists to catch.
    """
    if not facts.issue_state_readable:
        return VersionIssueTracking(
            facts=facts,
            disposition=DISPOSITION_UNKNOWN_ISSUE_STATE,
            reason="issue_status_unreadable",
        )

    nonterminal = facts.nonterminal_lanes
    if nonterminal:
        if facts.is_closed:
            commands = tuple(
                (lane.lane_id, reboot_audit_command(lane.lane_id))
                for lane in nonterminal
            )
            return VersionIssueTracking(
                facts=facts,
                disposition=DISPOSITION_DRAIN_OWED,
                reason="issue_closed_lane_not_terminal",
                diagnosis_steps=tuple(cmd for _, cmd in commands if cmd is not None),
                unrenderable_lane_ids=tuple(
                    lane_id for lane_id, cmd in commands if cmd is None
                ),
            )
        return VersionIssueTracking(
            facts=facts,
            disposition=DISPOSITION_IN_FLIGHT,
            reason="issue_open_lane_active",
        )

    if facts.is_closed:
        return VersionIssueTracking(
            facts=facts,
            disposition=DISPOSITION_SETTLED,
            reason="issue_closed_no_nonterminal_lane",
        )
    if facts.terminal_lanes:
        return VersionIssueTracking(
            facts=facts,
            disposition=DISPOSITION_LANE_TERMINAL_ISSUE_OPEN,
            reason="lane_terminal_issue_still_open",
        )
    if not facts.is_leaf:
        return VersionIssueTracking(
            facts=facts,
            disposition=DISPOSITION_UMBRELLA_OPEN,
            reason="umbrella_not_a_dispatch_candidate",
        )
    return VersionIssueTracking(
        facts=facts,
        disposition=DISPOSITION_UNDISPATCHED,
        reason="open_leaf_without_lane",
    )


@dataclass(frozen=True)
class UnscopedLane:
    """A non-terminal lane of this workspace whose issue is NOT in the tracked Version.

    Reported, never classified. Deciding whether such a lane is a legitimate member of a
    different Version or another left-behind one requires reading *that* Version, which is
    a different snapshot; claiming an answer from this one would be the same overreach
    this module refuses elsewhere.

    It exists because scoping tracking to a Version creates a fresh blind spot, and the
    #15789 lesson is precisely that a blind spot nobody enumerates is where work goes to
    die. Emitting this section unconditionally is what keeps "I ran version-track" from
    reading as "I looked at every lane on this host".
    """

    lane_id: str
    issue_id: str
    lane_disposition: str

    def as_payload(self) -> dict[str, object]:
        return {
            "lane_id": display_lane_id(self.lane_id),
            "issue_id": self.issue_id,
            "lane_disposition": self.lane_disposition,
            "identity_renderable": is_renderable_lane_id(self.lane_id),
        }


@dataclass(frozen=True)
class VersionTrackingSnapshot:
    """One version tracking snapshot: per-issue dispositions + the count-only roll-up."""

    version_id: str
    version_name: str
    issues: tuple[VersionIssueTracking, ...]
    unscoped_lanes: tuple[UnscopedLane, ...]

    @property
    def counts(self) -> dict[str, int]:
        """Every disposition, including the zeroes.

        Absent keys would make a reader infer a zero from silence, which is the same
        mistake as reading an unread authority as an empty one.
        """
        tally = {name: 0 for name in VERSION_ISSUE_DISPOSITIONS}
        for issue in self.issues:
            tally[issue.disposition] = tally.get(issue.disposition, 0) + 1
        return tally

    @property
    def attention(self) -> tuple[VersionIssueTracking, ...]:
        return tuple(issue for issue in self.issues if issue.needs_attention)

    def as_payload(self) -> dict[str, object]:
        return {
            "state": "tracked",
            "version_id": self.version_id,
            "version_name": self.version_name,
            "issue_count": len(self.issues),
            "counts": self.counts,
            "issues": [issue.as_payload() for issue in self.issues],
            "attention": [issue.as_payload() for issue in self.attention],
            "unscoped_lanes": [lane.as_payload() for lane in self.unscoped_lanes],
            "unscoped_lane_count": len(self.unscoped_lanes),
        }


def build_version_tracking(
    *,
    version_id: str,
    version_name: str,
    issues: Sequence[VersionIssueFacts],
    unscoped_lanes: Sequence[UnscopedLane] = (),
) -> VersionTrackingSnapshot:
    """Classify every joined issue into one snapshot (pure)."""
    return VersionTrackingSnapshot(
        version_id=version_id,
        version_name=version_name,
        issues=tuple(classify_version_issue(facts) for facts in issues),
        unscoped_lanes=tuple(unscoped_lanes),
    )


def join_version_issues(
    bucket_issues: Sequence[object],
    lanes_by_issue: Mapping[str, Sequence[TrackedLane]],
) -> tuple[VersionIssueFacts, ...]:
    """Join ``LaneBucketIssue`` records with this workspace's lanes (pure).

    Typed against the published record's *attributes* rather than importing the Redmine
    adapter's class: ``LaneBucketIssue`` is the neutral, provider-agnostic record
    (``f_110_ticket_adapter_common``) that #12919 created so consumers could read a bucket
    without reaching into a provider, and the delegated-coordinator context reads it as a
    published type — the same way ``lane_set_dispatch_plan`` does.
    """
    facts: list[VersionIssueFacts] = []
    for issue in bucket_issues:
        issue_id = str(getattr(issue, "issue_id", "") or "").strip()
        if not issue_id:
            continue
        facts.append(
            VersionIssueFacts(
                issue_id=issue_id,
                is_closed=bool(getattr(issue, "is_closed", False)),
                is_leaf=bool(getattr(issue, "is_leaf", False)),
                tracker=getattr(issue, "tracker", None),
                status_name=getattr(issue, "status_name", None),
                lanes=tuple(lanes_by_issue.get(issue_id, ())),
            )
        )
    return tuple(facts)


def render_version_tracking_text(snapshot: VersionTrackingSnapshot) -> str:
    """The operator rendering. Carries tokens and identifiers only, never issue text."""
    counts = snapshot.counts
    lines = [
        f"workflow version-track: version #{snapshot.version_id} "
        f"({snapshot.version_name or '-'}) — {len(snapshot.issues)} issue(s)"
    ]
    lines.append(
        "  counts: "
        + ", ".join(f"{name}={counts[name]}" for name in VERSION_ISSUE_DISPOSITIONS)
    )
    attention = snapshot.attention
    if attention:
        lines.append(f"  attention: {len(attention)}")
        for issue in attention:
            lines.append(
                f"    #{issue.issue_id} -> {issue.disposition} ({issue.reason})"
                + (
                    f" status={issue.facts.status_name}"
                    if issue.facts.status_name
                    else ""
                )
            )
            for lane in issue.facts.lanes:
                lines.append(
                    f"      lane {display_lane_id(lane.lane_id)}: "
                    f"{lane.lane_disposition}"
                    + (" [terminal]" if lane.is_terminal else "")
                )
            for step in issue.diagnosis_steps:
                lines.append(f"      $ {step}")
            for lane_id in issue.unrenderable_lane_ids:
                lines.append(
                    f"      ! lane {display_lane_id(lane_id)}: identity is not a "
                    "renderable lane id; no command is offered for it"
                )
    else:
        lines.append("  attention: none")
    # Always rendered, including the empty case: this section is what keeps a
    # Version-scoped pass from reading as a host-wide one (spec `## 3.2`).
    lines.append(f"  unscoped_lanes: {len(snapshot.unscoped_lanes)}")
    for lane in snapshot.unscoped_lanes:
        lines.append(
            f"    {display_lane_id(lane.lane_id)} issue={lane.issue_id or '-'} "
            f"({lane.lane_disposition})"
        )
    lines.append(
        "  note: read-only. This names lanes; it does not choose a recovery rail — "
        "`sublane reboot-audit` owns that judgement. The roll-up is a count, not a "
        "Version integration verdict."
    )
    lines.append(
        "  note: a listed command is the entry point to a DIAGNOSIS, not a drain that "
        "completes — measured, `reboot-audit` and `sublane retire` can disagree about "
        "whether the issue is closed, and the chain then stops one rail short."
    )
    return "\n".join(lines)


__all__ = (
    "ATTENTION_DISPOSITIONS",
    "DISPOSITION_DRAIN_OWED",
    "DISPOSITION_IN_FLIGHT",
    "DISPOSITION_LANE_TERMINAL_ISSUE_OPEN",
    "DISPOSITION_SETTLED",
    "DISPOSITION_UMBRELLA_OPEN",
    "DISPOSITION_UNDISPATCHED",
    "DISPOSITION_UNKNOWN_ISSUE_STATE",
    "TERMINAL_LANE_DISPOSITIONS",
    "TrackedLane",
    "UnscopedLane",
    "VERSION_ISSUE_DISPOSITIONS",
    "VersionIssueFacts",
    "VersionIssueTracking",
    "VersionTrackingSnapshot",
    "build_version_tracking",
    "classify_version_issue",
    "display_lane_id",
    "is_renderable_lane_id",
    "is_terminal_lane_disposition",
    "join_version_issues",
    "reboot_audit_command",
    "render_version_tracking_text",
    "shell_safe_command",
)
