"""Who may be treated as a project-coordinator pane (Redmine #14996 R2).

The authority half of :mod:`herdr_project_column_reflow`. The geometry half
decides where a pane goes; this one decides whether a pane may be reasoned about
at all — and four review rounds landed here rather than on the geometry, which is
why it is its own surface, and why that surface is a named policy with injected
ports rather than a bag of functions over stores (review j#99931 finding_4,
against ``logic-object-oriented-architecture-policy``).

The question is harder than it looks because a herdr assigned name decodes to a
PROVIDER token (``codex`` / ``claude``), not a workflow role. Decoding proves a
slot is one of ours; it does not say whose, nor that the pane is alive, nor that
the process behind it is the one that attested, nor that its lane is still meant
to exist. Every conjunct below is here because a review reproduced a pane being
moved without it — or, once, refused with it:

- an implementation lane mis-placed into this workspace was chosen as the anchor
  and bounced (j#99885 finding_2);
- the TOP pair, which belongs in its own dedicated workspace, was grouped as a
  project pair and six panes moved (j#99904 finding_1);
- a stale sibling FILTERED OUT of the set made its pair look healthy, and four
  panes moved before the closing verdict caught it (j#99904 finding_2);
- a ``present`` self-attestation from a previous process generation was re-used
  as this generation's authority (j#99913 finding_1);
- a pane whose live provider contradicted its assigned name passed a
  liveness-only check (j#99913 finding_2);
- a legitimate ``delegated_coordinator`` running from its linked worktree was
  refused by a registry-root containment test the identity model never had
  (j#99913 finding_3);
- this run's OWN pair was exempted from stale / provider / cwd checks it could
  have answered immediately, on the strength of an exemption argued only for the
  two facts it cannot (j#99931 finding_1);
- an undecodable row inside the target tab was skipped rather than refused, so
  six panes moved before the tiling check failed (j#99931 finding_2);
- a HIBERNATED lane's surviving panes were treated as an active coordinator
  because the lane-kind projection dropped ``lane_disposition`` (j#99931
  finding_3).

Three rules follow, and every part of this module obeys them:

1. **Delegate to the canonical authority; never re-derive an equivalent.** Two of
   the findings above were hand-written equivalents that had quietly dropped one
   of the original's conjuncts, and a third dropped a field off a canonical
   record. :func:`evaluate_attestation` owns the generation pin;
   :func:`herdr_workspace_segment` owns what directory belongs to what workspace;
   a lifecycle record owns BOTH a lane's kind and its disposition.
2. **Unresolved evidence refuses; it never disappears.** Excluding a row you
   cannot explain changes the shape of the set the next check reasons about.
3. **An exemption is only as wide as the reason for it.** This run's own panes
   skip exactly the two facts a just-launched slot cannot yet answer — its
   durable lane kind and its startup attestation — and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Protocol, Sequence

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    evaluate_attestation,
)
from mozyo_bridge.core.state.lane_kind import LANE_KIND_DELEGATED_COORDINATOR
from mozyo_bridge.core.state.lane_lifecycle_model import DISPOSITION_ACTIVE
from mozyo_bridge.core.state.lane_lifecycle_readonly import (
    load_lane_lifecycle_readonly,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.lane_placement import (  # noqa: E501
    LANE_PLACEMENT_PROVIDERS,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    _workspace_prefix,
    herdr_workspace_segment,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_LOCATOR,
    AGENT_KEY_LOCATOR_ALIAS,
    AGENT_KEY_LOCATOR_ALIAS_2,
    AGENT_KEY_NAME,
    DEFAULT_LANE,
    _agent_locator,
    _norm,
    decode_assigned_name,
    terminal_identity_of_live_slot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E501
    SLOT_STALE,
    classify_named_slot,
)

#: The ``agent list`` key carrying the provider herdr detected in the pane. Live rows
#: hold the canonical token verbatim (measured on the operator's running herdr:
#: ``codex`` / ``claude``), which is what lets it be matched against a decoded role
#: rather than merely tested for emptiness.
_DETECTED_AGENT_KEY = "agent"

#: The ``agent list`` key naming the herdr workspace a row lives in. Live rows carry
#: it alongside the locator (measured on the operator's running herdr), so a row can
#: claim this workspace even when its locator is unusable — which is exactly the row
#: a locator-only scope test walked past (review j#99938 finding_1).
AGENT_KEY_WORKSPACE = "workspace_id"


@dataclass(frozen=True)
class CoordinatorPane:
    """One identity-decoded pane in the shared workspace, plus its raw evidence.

    The evidence fields are carried rather than pre-judged so the authority can
    REFUSE on what it cannot resolve instead of silently excluding it — the
    distinction review j#99904 finding_2 turned on.
    """

    locator: str
    assigned_name: str
    workspace_id: str
    lane_id: str
    role: str
    #: Herdr's stable pane/workspace cwd (``cwd``; ``""`` if absent).
    cwd: str = ""
    #: The cwd of the current foreground process.  This may legitimately be an
    #: agent helper/plugin directory and is not the pane's workspace authority.
    foreground_cwd: str = ""
    #: The canonical provider herdr detected (``""`` when absent or unrecognised).
    detected_provider: str = ""
    #: Internal-only server-owned generation identity; never rendered publicly.
    terminal_id: str = field(default="", repr=False)
    #: herdr reported the pane as shell residue (identity outlived its agent).
    stale: bool = False

    @property
    def pair_key(self) -> "tuple[str, str]":
        """The project pair this pane belongs to: its mozyo workspace + lane."""
        return (self.workspace_id, self.lane_id or DEFAULT_LANE)


@dataclass(frozen=True)
class OwnSlot:
    """One slot THIS run launched, as the run itself knows it.

    An exact triple rather than a lane key, because the exemption is bound to the
    panes this run created and not to everything that happens to share their pair
    key (review j#99931 finding_1).
    """

    locator: str
    assigned_name: str
    provider: str


@dataclass(frozen=True)
class LaneFact:
    """A lane's durable geometry facts — BOTH of them.

    ``disposition`` rides alongside ``kind`` because a projection that kept only
    the kind let a hibernated lane's survivors act as an active coordinator
    (review j#99931 finding_3).
    """

    kind: str
    disposition: str


@dataclass(frozen=True)
class ProjectGroupDecision:
    """The authority's verdict: the project pairs, or the reason there are none.

    A typed result rather than a ``(dict, str)`` tuple: this is an
    authority-bearing decision, and the repo's architecture policy keeps those off
    raw payloads (review j#99931 finding_4).
    """

    groups: "Mapping[tuple[str, str], tuple[CoordinatorPane, ...]]"
    refusal: str = ""
    #: The pair key this run's own launched panes decode to (``None`` when the run
    #: launched none). The single source for "which pair is ours" — deriving a
    #: second one beside it is what let a run claim a project the workspace does
    #: not hold (review j#99938 finding_2).
    own_key: "Optional[tuple[str, str]]" = None
    #: ``True`` only when the cwd on one of THIS run's freshly launched panes
    #: resolves to no workspace.  The caller may re-read that row for a bounded
    #: interval, but must never retry a missing/mismatched cwd, a foreign row, or
    #: structurally invalid evidence.
    retryable_own_cwd_unresolved: bool = False

    @property
    def ok(self) -> bool:
        return not self.refusal

    @classmethod
    def refused(
        cls, reason: str, *, retryable_own_cwd_unresolved: bool = False
    ) -> "ProjectGroupDecision":
        return cls(
            groups={},
            refusal=reason,
            retryable_own_cwd_unresolved=retryable_own_cwd_unresolved,
        )


class AttestationPort(Protocol):
    """Reads a slot's startup self-attestation and judges it for a live pane."""

    def attested(self, pane: "CoordinatorPane") -> "tuple[bool, str]":
        """``(ok, state)`` — ``state`` names the refusal when ``ok`` is False."""


class LaneFactsPort(Protocol):
    """Reads every lane's durable geometry facts, or reports them unreadable."""

    def lane_facts(self) -> "Optional[Mapping[tuple[str, str], LaneFact]]":
        """``None`` when the authority cannot be read (the caller fails closed)."""


class WorkspaceResolverPort(Protocol):
    """Maps a working directory to the mozyo workspace it belongs to."""

    def workspace_of(self, cwd: str) -> str:
        """``""`` when no ancestor of ``cwd`` resolves a workspace identity."""


class StoreAttestationPort:
    """Live port over the #13637 attestation store.

    The judgment itself is :func:`evaluate_attestation` — the join adopt and
    doctor already share — so the generation pin cannot drift from theirs.
    """

    def __init__(self, home: Path) -> None:
        self._store = HerdrIdentityAttestationStore(home=home)

    def attested(self, pane: "CoordinatorPane") -> "tuple[bool, str]":
        join = evaluate_attestation(
            self._store.read(pane.assigned_name),
            live_locator=pane.locator,
            live_terminal_id=pane.terminal_id,
            expected_workspace_id=pane.workspace_id,
            expected_role=pane.role,
            expected_lane=pane.lane_id,
        )
        return join.ok, join.state


class StoreLaneFactsPort:
    """Live port over the generation-bound lane lifecycle store."""

    def __init__(self, home: Path) -> None:
        self._home = home

    def lane_facts(self) -> "Optional[Mapping[tuple[str, str], LaneFact]]":
        records = load_lane_lifecycle_readonly(home=self._home)
        if records is None:
            return None
        return {
            (
                record.repo_workspace_id,
                _norm(record.lane_id) or DEFAULT_LANE,
            ): LaneFact(
                kind=_norm(record.lane_kind),
                disposition=_norm(record.lane_disposition),
            )
            for record in records
        }


class IdentityWorkspaceResolver:
    """Live port over the identity model's own workspace resolver.

    Walking up to the nearest ancestor that resolves an identity covers a main
    checkout, a subdirectory of one, a linked worktree and a subdirectory of that
    with one rule — and it cannot drift from #13152 / #13377 because it IS that
    rule. A containment test against the registry root refused every legitimate
    managed ``delegated_coordinator`` (review j#99913 finding_3).
    """

    def __init__(self, home: Path) -> None:
        self._home = home

    def workspace_of(self, cwd: str) -> str:
        try:
            start = Path(cwd).expanduser().resolve()
        except (OSError, ValueError, RuntimeError):
            return ""
        for candidate in (start, *start.parents):
            try:
                resolved = herdr_workspace_segment(candidate, home=self._home)
            except (OSError, ValueError):
                return ""
            if resolved:
                return resolved
        return ""


def _detected_provider(row: Mapping[str, object]) -> str:
    """The canonical provider herdr detected in the pane, or ``""``.

    :func:`classify_named_slot` is deliberately conservative in the other
    direction — it returns ``live`` for a legacy / minimal row that carries no
    liveness field at all, because its job is never to clobber a real agent. That
    makes "not stale" the wrong predicate for deciding whether a pane may be moved
    (review j#99904 finding_2). Returning the VALUE rather than a boolean is
    review j#99913 finding_2: a non-blank marker is not a role proof. Anything
    outside the canonical vocabulary is ``""`` — unknown, not "some provider".
    """
    detected = _norm(row.get(_DETECTED_AGENT_KEY))
    return detected if detected in LANE_PLACEMENT_PROVIDERS else ""


#: The row belongs to this workspace and may be decoded into a pane.
ROW_IN_SCOPE = "in_scope"
#: The row resolves to a DIFFERENT workspace. Out of scope is a boundary, not an
#: exclusion: nothing about it is unresolved, it simply is not ours.
ROW_OUT_OF_SCOPE = "out_of_scope"
#: The row's location cannot be established, or it claims this workspace in a way
#: this module cannot address. Unresolved evidence refuses.
ROW_REFUSED = "refused"

#: The closed disposition vocabulary. Every inventory row lands in exactly one —
#: which is the property two reviews found missing. j#99938 finding_1 collapsed
#: "claims us but unaddressable" into out-of-scope; j#99950 finding_1 then collapsed
#: "resolves nowhere" into it too. Both had been written as the two-valued question
#: "is this row ours?", and both cost six pane moves.
ROW_DISPOSITIONS: "tuple[str, ...]" = (ROW_IN_SCOPE, ROW_OUT_OF_SCOPE, ROW_REFUSED)


#: The row keys that can carry a pane locator, in the order the canonical reader
#: consults them.
_LOCATOR_KEYS: "tuple[str, ...]" = (
    AGENT_KEY_LOCATOR,
    AGENT_KEY_LOCATOR_ALIAS,
    AGENT_KEY_LOCATOR_ALIAS_2,
)

#: The text fields that decide WHERE a row lives. Shape-checked before scope,
#: because scope is what they answer.
_LOCATION_TEXT_FIELDS: "tuple[str, ...]" = (AGENT_KEY_WORKSPACE, *_LOCATOR_KEYS)

#: The text fields this module reads only AFTER a row is in scope, to turn it into
#: pane evidence. Shape-checked then, and not before: a row that has already proved
#: it lives in another workspace is out of scope, and refusing the whole read over
#: the shape of a field nobody was going to read narrows that boundary instead of
#: widening the guard (review j#99978 finding_1).
#:
#: Both tuples exist because the defect they answer — ``_norm`` stringifying
#: anything non-``None``, so a list becomes the workspace id ``"[]"`` — belongs to
#: every field that passes through it. The generalisation is over WHICH fields are
#: checked; it says nothing about WHEN, and applying it to both at once broke the
#: out-of-scope boundary.
_EVIDENCE_TEXT_FIELDS: "tuple[str, ...]" = (
    AGENT_KEY_NAME,
    _DETECTED_AGENT_KEY,
    "foreground_cwd",
    "cwd",
)


@dataclass(frozen=True)
class RowVerdict:
    """Where one inventory row stands, on the closed disposition vocabulary."""

    disposition: str
    locator: str = ""
    refusal: str = ""


def _text_field_refusal(row: Mapping[str, object], key: str) -> str:
    """``""`` unless ``key`` is present holding something that is not text.

    The canonical ``_norm`` is ``str(value).strip()`` for anything non-``None``, so
    a malformed payload does not stay malformed — it becomes a *string*. A list
    normalises to ``"[]"``, a dict to ``"{}"``, an int to ``"17"``, ``True`` to
    ``"True"``, and every one of those reads as a perfectly good foreign workspace
    id. Four such rows classified as out-of-scope and six panes moved past them
    (review j#99971 finding_1).

    So the raw shape is judged BEFORE normalisation. Absent and ``null`` stay
    absent — the row simply does not state the field — and a whitespace-only
    string still folds to absent, which is what ``_norm`` already means by it.
    """
    if key not in row:
        return ""
    value = row[key]
    if value is None or isinstance(value, str):
        return ""
    return (
        f"the herdr inventory row states {key!r} as "
        f"{type(value).__name__}; refusing to read a non-text field as identity "
        "evidence"
    )


def classify_inventory_row(row: object, target_workspace: str) -> RowVerdict:
    """Place one ``agent list`` row on :data:`ROW_DISPOSITIONS` (pure, total).

    A row states its workspace twice — explicitly in ``workspace_id``, and inside
    its locator. The decision is taken over the FULL product of what those two can
    say — including what SHAPE they say it in — in this order:

    0. **a LOCATION field is present but is not text** -> refused. ``_norm`` turns
       anything non-``None`` into a string, so a list, dict, int or bool would
       otherwise be promoted into a perfectly good workspace id
       (:func:`_text_field_refusal`). Only the fields that answer *where* are asked
       here; the evidence fields are asked at step 5, once the row is known to be
       ours to read at all.
    1. **both say something, and they disagree** -> refused. Self-consistency is
       asked before "is it ours?", because such a row has not established that it
       lives anywhere — including elsewhere (review j#99960 finding_1).
    2. **neither names this workspace** -> out of scope if either resolved
       somewhere (a boundary, not an exclusion); refused if neither did, since its
       location cannot be established at all.
    3. it names this workspace, so it must be addressable: **no locator** ->
       refused; **a locator :func:`_workspace_prefix` cannot parse** -> refused
       (``""`` is that function's contract for a malformed handle, precisely so the
       caller fails closed rather than guessing).
    4. **two locator keys naming different panes** -> refused.
    5. now that the row IS ours, its EVIDENCE fields must be text too; otherwise
       **in scope**.

    ``tests/unit/.../test_herdr_project_column_reflow`` enumerates the declared x
    locator grid and asserts the table covers every cell — a table is a claim, and
    the claim is what is checked.
    """
    if not isinstance(row, Mapping):
        return RowVerdict(
            ROW_REFUSED,
            refusal="the herdr inventory contains a row this module cannot read",
        )
    for key in _LOCATION_TEXT_FIELDS:
        shape = _text_field_refusal(row, key)
        if shape:
            return RowVerdict(ROW_REFUSED, refusal=shape)
    stated = {
        _norm(row.get(key)) for key in _LOCATOR_KEYS if _norm(row.get(key))
    }
    if len(stated) > 1:
        # The canonical reader takes the first key that answers, so a row naming
        # two different panes would be read as whichever key came first. A row that
        # cannot agree with itself about which pane it is has not identified one.
        return RowVerdict(
            ROW_REFUSED,
            refusal=(
                f"the herdr inventory row states {len(stated)} different pane "
                f"locators {sorted(stated)!r}; refusing to pick one"
            ),
        )
    locator = _agent_locator(row)
    prefix = _workspace_prefix(locator)
    declared = _norm(row.get(AGENT_KEY_WORKSPACE))
    if declared and prefix and declared != prefix:
        # Self-consistency is asked BEFORE "is it ours?", because a row whose two
        # statements disagree has not established that it lives anywhere — including
        # elsewhere. Asking the target question first let a row declaring one foreign
        # workspace while addressing another pass as out-of-scope, and six panes moved
        # around an occupant nobody had accounted for (review j#99960 finding_1).
        return RowVerdict(
            ROW_REFUSED,
            refusal=(
                f"pane {locator!r} reports workspace {declared!r} while its locator "
                f"says {prefix!r}; refusing to reason about a contradictory row"
            ),
        )
    if declared != target_workspace and prefix != target_workspace:
        if declared or prefix:
            return RowVerdict(ROW_OUT_OF_SCOPE)
        return RowVerdict(
            ROW_REFUSED,
            refusal=(
                "the herdr inventory holds a row with neither a workspace nor a "
                "resolvable pane locator; refusing to reshape a workspace whose "
                "occupants this plan cannot enumerate"
            ),
        )
    if not locator:
        return RowVerdict(
            ROW_REFUSED,
            refusal=(
                f"a row claiming workspace {target_workspace!r} carries no pane "
                "locator; refusing to reshape a workspace holding a pane this plan "
                "cannot address"
            ),
        )
    if not prefix:
        return RowVerdict(
            ROW_REFUSED,
            refusal=(
                f"a row claiming workspace {target_workspace!r} carries the "
                f"unparseable pane handle {locator!r}; refusing to address it"
            ),
        )
    for key in _EVIDENCE_TEXT_FIELDS:
        shape = _text_field_refusal(row, key)
        if shape:
            return RowVerdict(ROW_REFUSED, refusal=shape)
    return RowVerdict(ROW_IN_SCOPE, locator=locator)


def coordinator_panes_in(
    rows: Sequence[Mapping[str, object]], target_workspace: str
) -> "tuple[tuple[CoordinatorPane, ...], str]":
    """``(panes, refusal)`` for every inventory row inside ``target_workspace``.

    Rows located in ANOTHER herdr workspace are out of scope and contribute
    nothing — that is a scope boundary, not an exclusion. Inside the target
    workspace nothing is skipped: a row whose assigned name will not decode, or a
    locator that appears twice, refuses the whole set. Review j#99931 finding_2
    measured what skipping cost — an unexplained row rode along until the closing
    tiling check failed, six pane moves later — and the rule it broke is this
    module's own second one.

    A non-mapping element means the inventory payload is not the shape this module
    reasons about at all, so it refuses rather than being stepped over.
    """
    panes: list = []
    seen: set = set()
    for row in rows:
        verdict = classify_inventory_row(row, target_workspace)
        if verdict.disposition == ROW_REFUSED:
            return (), verdict.refusal
        if verdict.disposition == ROW_OUT_OF_SCOPE:
            continue
        locator = verdict.locator
        if locator in seen:
            return (), (
                f"pane {locator!r} appears twice in the herdr inventory; refusing to "
                "reason about a workspace whose panes are not uniquely identified"
            )
        seen.add(locator)
        decoded = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decoded.ok or decoded.identity is None:
            return (), (
                f"pane {locator!r} in the shared project-coordinator workspace carries "
                "no decodable mozyo identity; refusing to reshape a workspace holding "
                "a pane this plan cannot account for"
            )
        panes.append(
            CoordinatorPane(
                locator=locator,
                assigned_name=_norm(row.get(AGENT_KEY_NAME)),
                workspace_id=decoded.identity.workspace_id,
                lane_id=decoded.identity.lane_id or DEFAULT_LANE,
                role=decoded.identity.role,
                cwd=_norm(row.get("cwd")),
                foreground_cwd=_norm(row.get("foreground_cwd")),
                detected_provider=_detected_provider(row),
                terminal_id=terminal_identity_of_live_slot(
                    row.get(AGENT_KEY_NAME), locator, rows
                ) or "",
                stale=classify_named_slot(row) == SLOT_STALE,
            )
        )
    return tuple(panes), ""


def group_by_pair(
    panes: Sequence[CoordinatorPane],
) -> "dict[tuple[str, str], tuple[CoordinatorPane, ...]]":
    """Panes grouped by ``(workspace_id, lane_id)`` — grouping only, no authority."""
    groups: dict = {}
    for pane in panes:
        groups.setdefault(pane.pair_key, []).append(pane)
    return {key: tuple(members) for key, members in groups.items()}


class ProjectColumnAuthority:
    """Decides which panes in the shared workspace are project-coordinator pairs.

    One named policy over three injected ports, so a test states the specification
    with fakes rather than by monkeypatching module-level reads, and so what it
    publishes is a value object rather than a payload.

    The phases run cheapest-first — inventory shape, provider shape, the top
    exclusion, the facts every row already carries, then the two store-backed
    joins — so an inventory that is malformed on its face is refused without
    opening any store. Every phase completes before a plan exists, which is the
    property reviews j#99904 and j#99931 both turn on: a refusal costs zero pane
    moves.
    """

    def __init__(
        self,
        *,
        attestation: AttestationPort,
        lanes: LaneFactsPort,
        workspaces: WorkspaceResolverPort,
    ) -> None:
        self._attestation = attestation
        self._lanes = lanes
        self._workspaces = workspaces

    def resolve(
        self,
        rows: Sequence[Mapping[str, object]],
        *,
        target_workspace: str,
        own_slots: Sequence[OwnSlot] = (),
        expected_own_key: "Optional[tuple[str, str]]" = None,
        top_workspace_id: str = "",
    ) -> ProjectGroupDecision:
        panes, refusal = coordinator_panes_in(rows, target_workspace)
        if refusal:
            return ProjectGroupDecision.refused(refusal)
        groups = group_by_pair(panes)
        own_index, refusal = self._own_index(own_slots)
        if refusal:
            return ProjectGroupDecision.refused(refusal)

        for key, members in sorted(groups.items()):
            refusal = self._provider_shape_refusal(key, members) or self._top_refusal(
                key, top_workspace_id
            )
            if refusal:
                return ProjectGroupDecision.refused(refusal)

        own_key, refusal = self._own_key(groups, own_index, expected_own_key)
        if refusal:
            return ProjectGroupDecision.refused(refusal)

        retryable_own_refusal = ""
        for pane in panes:
            refusal, cwd_unresolved = self._observable_refusal(pane)
            if not refusal:
                continue
            if cwd_unresolved and pane.locator in own_index:
                # Do not return yet: a later foreign/conflicting row is a
                # stronger, non-retryable refusal and must not be hidden by
                # inventory ordering.  Retry is allowed only if this full pass
                # finds nothing except fresh-own unresolved stable cwd rows.
                if not retryable_own_refusal:
                    retryable_own_refusal = refusal
                continue
            return ProjectGroupDecision.refused(refusal)
        refusal = self._named_lane_refusal(groups, own_key)
        if refusal:
            return ProjectGroupDecision.refused(refusal)

        for pane in panes:
            if pane.locator in own_index:
                continue
            ok, state = self._attestation.attested(pane)
            if not ok:
                return ProjectGroupDecision.refused(
                    f"pane {pane.locator!r} has no usable startup self-attestation "
                    f"({state})"
                )
        if retryable_own_refusal:
            return ProjectGroupDecision.refused(
                retryable_own_refusal,
                retryable_own_cwd_unresolved=True,
            )
        return ProjectGroupDecision(groups=groups, own_key=own_key)

    # -- phases ------------------------------------------------------------
    def _provider_shape_refusal(
        self, key: "tuple[str, str]", members: Sequence[CoordinatorPane]
    ) -> str:
        """``""`` iff this group's providers are a shape a coordinator pair can have.

        A distinct, non-empty subset of the canonical providers. Two rows carrying
        the SAME assigned name were once grouped as a pair, so an identity conflict
        was reshaped as if it were a healthy one (review j#99885 finding_3).

        A group of ONE live provider is deliberately allowed (Design Consultation
        Answer j#99900): the layout proves a full-height column regardless of how
        many panes stack in it, so a project currently short a slot still owns a
        real column. That exception covers a GENUINE one — nothing filtered away,
        every authority resolved — which is why nothing above this line skips a row.
        """
        providers = [pane.role for pane in members]
        unknown = sorted({p for p in providers if p not in LANE_PLACEMENT_PROVIDERS})
        if unknown:
            return (
                f"project pair {key!r} carries unrecognised provider(s) {unknown!r}; "
                "refusing to reshape a group this plan cannot identify"
            )
        if len(set(providers)) != len(providers):
            return (
                f"project pair {key!r} carries duplicate provider(s) "
                f"{sorted(providers)!r} — an identity conflict, not a coordinator pair"
            )
        if len(providers) > len(LANE_PLACEMENT_PROVIDERS):
            return (
                f"project pair {key!r} holds {len(providers)} live panes, more than a "
                "coordinator pair can have"
            )
        return ""

    def _top_refusal(self, key: "tuple[str, str]", top_workspace_id: str) -> str:
        """Both halves of the mode's default-lane invariant, not just the first.

        Under ``role_grouped_space`` a default lane is a *project* coordinator
        exactly when its workspace is not the configured top — the same predicate
        ``is_role_grouped_project_coordinator`` enforces. Copying only the first
        half grouped the top pair as a project pair and moved six panes (review
        j#99904 finding_1).
        """
        top = _norm(top_workspace_id)
        if top and key[0] == top and key[1] == DEFAULT_LANE:
            return (
                f"the configured top coordinator {key!r} occupies this shared "
                "project-coordinator workspace; it belongs in its own dedicated one, "
                "and this plan will not reshape it"
            )
        return ""

    def _own_index(
        self, own_slots: Sequence[OwnSlot]
    ) -> "tuple[dict[str, OwnSlot], str]":
        """``{locator: slot}`` for this run's launched slots — or a refusal.

        Folding the slots into a mapping is itself a filter: an earlier cut let a
        duplicate locator overwrite its predecessor, so a slot whose identity
        contradicted the inventory simply vanished and the survivor alone carried
        the exact join (review j#99950 finding_2). Two launches reporting one pane
        is a backend contradiction, not a set to deduplicate, and a launched slot
        with no locator cannot be joined at all.
        """
        index: dict = {}
        for slot in own_slots:
            if not slot.locator:
                return {}, (
                    f"this run reports a launched {slot.provider or 'slot'!r} with no "
                    "pane locator; refusing to reason about a slot it cannot join"
                )
            if slot.locator in index:
                return {}, (
                    f"this run reports two launched slots on pane {slot.locator!r}; "
                    "refusing to reason about a run whose own slots contradict "
                    "each other"
                )
            index[slot.locator] = slot
        return index, ""

    def _own_key(
        self,
        groups: "Mapping[tuple[str, str], tuple[CoordinatorPane, ...]]",
        own_index: "Mapping[str, OwnSlot]",
        expected_own_key: "Optional[tuple[str, str]]",
    ) -> "tuple[Optional[tuple[str, str]], str]":
        """``(own pair key, refusal)`` from the panes this run actually launched.

        The exemption is bound to an exact join — locator, assigned name and
        provider — rather than to a pair key, so a pane that merely shares the key
        is not exempt (review j#99931 finding_1). A locator this run claims that is
        absent from the workspace, or present under a different identity, is a
        contradiction rather than something to fall back from.

        That join proves the slots are self-consistent WITHIN the inventory, which
        is not the same as proving they are the project the run says it launched:
        a result naming project C whose slots were project A's live panes passed it
        and then reported a column for a project the workspace does not hold
        (review j#99938 finding_2). ``expected_own_key`` closes that by making the
        run's own claim part of the join, and the resolved key rides back on the
        decision so no caller re-derives a second one.
        """
        if not own_index:
            return None, ""
        by_locator = {
            pane.locator: pane for members in groups.values() for pane in members
        }
        keys = set()
        for locator, slot in sorted(own_index.items()):
            pane = by_locator.get(locator)
            if pane is None:
                return None, (
                    f"this run launched pane {locator!r} but the shared workspace "
                    "inventory does not hold it"
                )
            if pane.assigned_name != slot.assigned_name or pane.role != slot.provider:
                return None, (
                    f"pane {locator!r} carries an identity this run did not launch "
                    "there; refusing to treat it as this run's own"
                )
            keys.add(pane.pair_key)
        if len(keys) != 1:
            return None, (
                f"this run's launched panes span {len(keys)} project pairs; refusing "
                "to exempt an ambiguous set"
            )
        resolved = keys.pop()
        if expected_own_key is not None and resolved != expected_own_key:
            return None, (
                f"this run reports project {expected_own_key!r} but the panes it "
                f"launched decode to {resolved!r}; refusing to reason about a run "
                "whose own identity the workspace does not corroborate"
            )
        return resolved, ""

    def _observable_refusal(self, pane: CoordinatorPane) -> "tuple[str, bool]":
        """``(refusal, cwd_unresolved)`` for row facts — own included.

        Review j#99931 finding_1: the own exemption was argued from two facts a
        just-launched slot cannot yet answer (its durable lane kind, its startup
        attestation) and then applied to three it can. Liveness, the detected
        provider and the working directory are read off the same row for every pane
        in the workspace, so they are required of every pane in the workspace.
        """
        if pane.stale:
            return (
                f"pane {pane.locator!r} is shell residue (its identity outlived its "
                "agent)",
                False,
            )
        if not pane.detected_provider:
            return (
                f"pane {pane.locator!r} reports no recognised provider, so its "
                "liveness is unproved",
                False,
            )
        if pane.detected_provider != pane.role:
            return (
                f"pane {pane.locator!r} is running {pane.detected_provider!r} while "
                f"its assigned name claims {pane.role!r}",
                False,
            )
        if not pane.cwd:
            return f"pane {pane.locator!r} reports no stable working directory", False
        resolved = self._workspaces.workspace_of(pane.cwd)
        if not resolved:
            return (
                f"pane {pane.locator!r} has a stable directory that resolves to no "
                "registered mozyo workspace",
                True,
            )
        if resolved != pane.workspace_id:
            return (
                f"pane {pane.locator!r} has stable workspace {resolved!r} while "
                f"its assigned name claims {pane.workspace_id!r}",
                False,
            )
        if pane.foreground_cwd and pane.foreground_cwd != pane.cwd:
            foreground = self._workspaces.workspace_of(pane.foreground_cwd)
            if foreground and foreground != pane.workspace_id:
                return (
                    f"pane {pane.locator!r} has a foreground process in workspace "
                    f"{foreground!r} while its assigned name claims "
                    f"{pane.workspace_id!r}",
                    False,
                )
        return "", False

    def _named_lane_refusal(
        self,
        groups: "Mapping[tuple[str, str], tuple[CoordinatorPane, ...]]",
        own_key: "Optional[tuple[str, str]]",
    ) -> str:
        """A foreign NAMED lane must be a delegated coordinator that still exists.

        Both facts, off the same current record: ``delegated_coordinator`` says
        what the lane is, ``active`` says it is still meant to be. A projection
        that kept only the kind let a hibernated lane's surviving panes act as an
        active coordinator (review j#99931 finding_3).

        This run's own lane is exempt HERE and only here: a managed
        ``delegated_coordinator`` writes its durable row on a different edge than
        its launch, and its kind was already proved by the caller that routed the
        pair to this workspace at all.
        """
        named = sorted(
            key for key in groups if key[1] != DEFAULT_LANE and key != own_key
        )
        if not named:
            return ""
        facts = self._lanes.lane_facts()
        if facts is None:
            return (
                "the durable lane authority is unreadable, so the named lane(s) "
                f"{named!r} in this workspace cannot be proved to be project "
                "coordinators"
            )
        for key in named:
            fact = facts.get(key)
            if fact is None or not fact.kind:
                return (
                    f"named lane {key!r} has no durable lane-kind; refusing to treat "
                    "it as a project coordinator"
                )
            if fact.kind != LANE_KIND_DELEGATED_COORDINATOR:
                return (
                    f"named lane {key!r} has durable lane-kind {fact.kind!r}, not "
                    f"{LANE_KIND_DELEGATED_COORDINATOR!r}; a non-coordinator lane in "
                    "the shared project-coordinator workspace is a placement this plan "
                    "will not reshape"
                )
            if fact.disposition != DISPOSITION_ACTIVE:
                return (
                    f"named lane {key!r} is {fact.disposition or 'unknown'!r}, not "
                    f"{DISPOSITION_ACTIVE!r}; its surviving panes are a conflict "
                    "between the durable lane state and the live inventory, not an "
                    "active coordinator"
                )
        return ""


def project_column_authority(home: Path) -> ProjectColumnAuthority:
    """The live authority, wired to the operator's durable stores."""
    return ProjectColumnAuthority(
        attestation=StoreAttestationPort(home),
        lanes=StoreLaneFactsPort(home),
        workspaces=IdentityWorkspaceResolver(home),
    )


__all__ = (
    "ROW_DISPOSITIONS",
    "ROW_IN_SCOPE",
    "ROW_OUT_OF_SCOPE",
    "ROW_REFUSED",
    "AttestationPort",
    "CoordinatorPane",
    "IdentityWorkspaceResolver",
    "LaneFact",
    "LaneFactsPort",
    "OwnSlot",
    "ProjectColumnAuthority",
    "RowVerdict",
    "ProjectGroupDecision",
    "StoreAttestationPort",
    "StoreLaneFactsPort",
    "WorkspaceResolverPort",
    "classify_inventory_row",
    "coordinator_panes_in",
    "group_by_pair",
    "project_column_authority",
)
