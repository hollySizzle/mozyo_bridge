"""Who may be treated as a project-coordinator pane (Redmine #14996 R2).

The authority half of :mod:`herdr_project_column_reflow`, split out when the two
halves stopped fitting one module. The geometry half decides where a pane goes;
this one decides whether a pane may be reasoned about at all — and three review
rounds landed here rather than on the geometry, which is why it earns its own
surface.

The question is harder than it looks because a herdr assigned name decodes to a
PROVIDER token (``codex`` / ``claude``), not a workflow role. Decoding proves a
slot is one of ours; it does not say whose, nor that the pane is alive, nor that
the process behind it is the one that attested. Every conjunct below exists
because a review reproduced a pane being moved without it:

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
- and, in the other direction, a legitimate ``delegated_coordinator`` running
  from its linked worktree was refused by a registry-root containment test that
  the identity model never had (j#99913 finding_3).

Two rules follow, and every function here obeys them:

1. **Delegate to the canonical authority; never re-derive an equivalent.** Two
   of the six above were hand-written equivalents that had quietly dropped one of
   the original's conjuncts. ``evaluate_attestation`` owns the generation pin;
   ``herdr_workspace_segment`` owns what directory belongs to what workspace;
   ``is_role_grouped_project_coordinator`` owns the default-lane predicate.
2. **Unresolved evidence refuses; it never disappears.** Excluding a row you
   cannot explain changes the shape of the set the next check reasons about.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    evaluate_attestation,
)
from mozyo_bridge.core.state.lane_kind import LANE_KIND_DELEGATED_COORDINATOR
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
    AGENT_KEY_NAME,
    DEFAULT_LANE,
    _agent_locator,
    _norm,
    decode_assigned_name,
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


@dataclass(frozen=True)
class CoordinatorPane:
    """One identity-decoded pane in the shared workspace, plus its raw evidence.

    The evidence fields are carried rather than pre-judged so the authority join
    can REFUSE on what it cannot resolve instead of silently excluding it — the
    distinction review j#99904 finding_2 turned on.
    """

    locator: str
    assigned_name: str
    workspace_id: str
    lane_id: str
    role: str
    #: The pane's working directory as the inventory reports it (``""`` if absent).
    cwd: str = ""
    #: The canonical provider herdr detected in the pane (``""`` when unrecognised).
    #: The POSITIVE liveness signal, and — matched against :attr:`role` — a role proof.
    detected_provider: str = ""
    #: herdr reported the pane as shell residue (identity outlived its agent).
    stale: bool = False

    @property
    def pair_key(self) -> "tuple[str, str]":
        """The project pair this pane belongs to: its mozyo workspace + lane."""
        return (self.workspace_id, self.lane_id or DEFAULT_LANE)



def coordinator_panes_in(
    rows: Sequence[Mapping[str, object]], target_workspace: str
) -> "tuple[CoordinatorPane, ...]":
    """Every identity-decoded slot whose pane sits in ``target_workspace``.

    Identity is the herdr assigned name, never the pane position: a row we cannot
    decode, or one located in another herdr workspace, contributes nothing.

    Nothing else is dropped here — deliberately. An earlier cut filtered out rows
    :func:`classify_named_slot` read as :data:`SLOT_STALE`, which review j#99885
    finding_2 asked for and review j#99904 finding_2 then showed to be the wrong
    shape of fix: dropping a foreign pair's stale sibling made the pair *look*
    like a healthy one-pane group, so a plan was built and four panes were moved
    before the closing verdict failed. Unresolved evidence must REFUSE, not
    disappear, and the refusal has to happen before the first move — so it lives
    in :func:`resolve_project_groups`, which sees the whole set.

    Decoding is necessary but NOT sufficient to call a pane a coordinator: the
    assigned name's ``role`` is a provider token. :func:`resolve_project_groups`
    is the only producer a plan may consume.
    """
    panes: list = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decoded = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decoded.ok or decoded.identity is None:
            continue
        locator = _agent_locator(row)
        if not locator or _workspace_prefix(locator) != target_workspace:
            continue
        panes.append(
            CoordinatorPane(
                locator=locator,
                assigned_name=_norm(row.get(AGENT_KEY_NAME)),
                workspace_id=decoded.identity.workspace_id,
                lane_id=decoded.identity.lane_id or DEFAULT_LANE,
                role=decoded.identity.role,
                cwd=_norm(row.get("foreground_cwd") or row.get("cwd")),
                detected_provider=_detected_provider(row),
                stale=classify_named_slot(row) == SLOT_STALE,
            )
        )
    return tuple(panes)


def _detected_provider(row: Mapping[str, object]) -> str:
    """The canonical provider herdr detected in the pane, or ``""``.

    :func:`classify_named_slot` is deliberately conservative in the other
    direction — it returns ``live`` for a legacy / minimal row that carries no
    liveness field at all, because its job is never to clobber a real agent. That
    makes "not stale" the wrong predicate for deciding whether a FOREIGN pane may
    be moved (review j#99904 finding_2): absence of a residue signal is not
    evidence of a live provider.

    Returning the VALUE rather than a boolean is review j#99913 finding_2: a
    non-blank marker is not a role proof, and a Codex slot whose row reported
    ``claude`` passed the boolean form while its assigned name still decoded to
    ``codex``. The caller matches this against the decoded role, the same
    comparison ``sublane_adopt_declaration`` already makes against its wanted
    provider. Anything outside the canonical vocabulary is ``""`` — unknown, not
    "some provider".
    """
    detected = _norm(row.get(_DETECTED_AGENT_KEY))
    return detected if detected in LANE_PLACEMENT_PROVIDERS else ""


def group_by_pair(
    panes: Sequence[CoordinatorPane],
) -> "dict[tuple[str, str], tuple[CoordinatorPane, ...]]":
    """Panes grouped by ``(workspace_id, lane_id)`` — grouping only, no authority."""
    groups: dict = {}
    for pane in panes:
        groups.setdefault(pane.pair_key, []).append(pane)
    return {key: tuple(members) for key, members in groups.items()}


def _provider_shape_refusal(
    key: "tuple[str, str]", members: Sequence[CoordinatorPane]
) -> str:
    """``""`` iff this group's providers are a shape a coordinator pair can have.

    A distinct, non-empty subset of the canonical providers. Review j#99885
    finding_3 reproduced the hole this closes: two rows carrying the SAME assigned
    name were grouped as a pair, so an identity conflict — which the sibling
    resolver already fails closed on for this run's own lane — was reshaped as if
    it were a healthy codex/claude pair.

    A group of ONE live provider is deliberately allowed (finding_3 verdict
    j#99888 / dispute j#99890): :func:`_column_span` proves a full-height column
    from the layout regardless of how many panes stack in it, so a project that is
    currently short a slot still owns a real column. Failing here would report a
    neighbour's missing slot as THIS run's column failure — a mis-attribution, and
    one the slot / health axes already own.
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
            f"project pair {key!r} carries duplicate provider(s) {sorted(providers)!r} "
            "— an identity conflict, not a coordinator pair"
        )
    if len(providers) > len(LANE_PLACEMENT_PROVIDERS):
        return (
            f"project pair {key!r} holds {len(providers)} live panes, more than a "
            "coordinator pair can have"
        )
    return ""


def _lane_kind_index(home: Path) -> "Optional[dict[tuple[str, str], str]]":
    """``{(workspace_id, lane_id): lane_kind}`` from the durable lifecycle store.

    ``None`` when the store cannot be read version-compatibly — the same
    fail-closed disposition :func:`load_lane_lifecycle_readonly` defines, carried
    through so an unreadable authority refuses the reflow instead of letting a
    named lane default into "probably a coordinator".
    """
    records = load_lane_lifecycle_readonly(home=home)
    if records is None:
        return None
    return {
        (record.repo_workspace_id, _norm(record.lane_id) or DEFAULT_LANE): _norm(
            record.lane_kind
        )
        for record in records
    }


def _cwd_workspace(cwd: str, *, home: Path) -> str:
    """The mozyo workspace the pane's working directory belongs to, or ``""``.

    Resolved through :func:`herdr_workspace_segment` — the SAME read-only resolver
    the identity model mints and resolves every slot with — rather than a
    containment test against the registry root. Review j#99913 finding_3: a named
    lane runs from a LINKED WORKTREE that inherits the main checkout's
    ``workspace_id`` while living beside it, so "under the registry root" refused
    every legitimate managed ``delegated_coordinator`` and made the issue's own
    acceptance unreachable. Walking up to the nearest ancestor that resolves an
    identity covers a main checkout, a subdirectory of one, a linked worktree and
    a subdirectory of that with one rule, and it cannot drift from #13152 /
    #13377 because it IS that rule.
    """
    try:
        start = Path(cwd).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return ""
    for candidate in (start, *start.parents):
        try:
            resolved = herdr_workspace_segment(candidate, home=home)
        except (OSError, ValueError):
            return ""
        if resolved:
            return resolved
    return ""


def _foreign_evidence_refusal(pane: CoordinatorPane, *, home: Path) -> str:
    """``""`` iff a FOREIGN pane carries positive authority to be moved beside.

    Three conjuncts, each a durable fact written by somebody other than this run
    (review j#99904 finding_2 — "stale でない" proves nothing on its own), and
    each delegated to the canonical authority that already owns the question
    rather than re-derived here (review j#99913 findings 1 and 3 were both a
    hand-written equivalent that had dropped one of the original's conjuncts):

    - the detected provider **equals the role its assigned name decodes to**, so
      a live marker is a role proof rather than merely a sign of life;
    - :func:`evaluate_attestation` — the join adopt and doctor share — accepts the
      slot's self-attestation, which pins the PROCESS GENERATION through the
      recorded locator: a ``present`` record from a previous generation is never
      re-used;
    - the pane's cwd resolves, through the identity model's own resolver, to the
      very workspace its name claims.

    Any of the three being unresolvable is a refusal, never an exclusion.
    """
    if pane.stale:
        return f"pane {pane.locator!r} is shell residue (its identity outlived its agent)"
    if not pane.detected_provider:
        return (
            f"pane {pane.locator!r} reports no recognised provider, so its liveness is "
            "unproved"
        )
    if pane.detected_provider != pane.role:
        return (
            f"pane {pane.locator!r} is running {pane.detected_provider!r} while its "
            f"assigned name claims {pane.role!r}"
        )
    join = evaluate_attestation(
        HerdrIdentityAttestationStore(home=home).read(pane.assigned_name),
        live_locator=pane.locator,
        expected_workspace_id=pane.workspace_id,
        expected_role=pane.role,
        expected_lane=pane.lane_id,
    )
    if not join.ok:
        return (
            f"pane {pane.locator!r} has no usable startup self-attestation "
            f"({join.state})"
        )
    if not pane.cwd:
        return f"pane {pane.locator!r} reports no working directory"
    resolved = _cwd_workspace(pane.cwd, home=home)
    if not resolved:
        return (
            f"pane {pane.locator!r} runs in a directory that resolves to no registered "
            "mozyo workspace"
        )
    if resolved != pane.workspace_id:
        return (
            f"pane {pane.locator!r} runs in workspace {resolved!r} while its assigned "
            f"name claims {pane.workspace_id!r}"
        )
    return ""


def resolve_project_groups(
    rows: Sequence[Mapping[str, object]],
    target_workspace: str,
    *,
    home: Path,
    own_key: "Optional[tuple[str, str]]" = None,
    top_workspace_id: str = "",
) -> "tuple[dict[tuple[str, str], tuple[CoordinatorPane, ...]], str]":
    """``(project pairs, refusal)`` — the only group producer a plan may consume.

    Three authorities, in the order that keeps the common case free of the
    heaviest one (review j#99885 finding_2 / finding_3):

    1. **live-ness and provider shape** (:func:`coordinator_panes_in`,
       :func:`_provider_shape_refusal`) — pure, from the inventory row.
    2. **the mode's default-lane invariant, BOTH halves of it** — under
       ``role_grouped_space`` a default lane is a *project* coordinator exactly
       when its workspace is not the configured top. An earlier cut copied only
       the first half of
       :func:`...herdr_role_grouped_space.is_role_grouped_project_coordinator`
       and dropped its ``workspace_id != top_workspace_id`` conjunct, so a top
       pair that had ended up in this workspace was grouped as a project pair and
       six panes were moved (review j#99904 finding_1). The top pair belongs in
       its own dedicated workspace; finding it here is a placement this axis
       refuses rather than reshapes.
    3. **the durable ``lane_kind``** for every FOREIGN named lane, read from the
       generation-bound lifecycle store. Only ``delegated_coordinator`` joins the
       coordinator role group. An ``implementation`` lane in this workspace is a
       mis-placement this axis must not silently reshape, and a missing / unknown
       kind — or a store that cannot be read — is not evidence of one either. All
       three are a refusal, which the caller turns into a ZERO-MOVE typed failure.

    ``own_key`` is exempt from (3) and only from (3): this run's own lane kind was
    already proved by the caller — ``role_grouped_space`` classified it through
    :func:`...herdr_role_grouped_space.is_role_grouped_project_coordinator` before
    anything launched, which is the authority that decided this workspace was its
    placement at all. Re-deriving it from the lifecycle store would not strengthen
    that; it would only fail a managed ``delegated_coordinator`` whose durable row
    is written on a different edge than its launch, which is a live path (measured
    against ``HerdrSublaneActuatorOps.append_lane_column``). The finding this
    exemption preserves is about FOREIGN panes, and those keep the full join.

    4. **positive evidence for every FOREIGN pane** (:func:`_foreign_evidence_refusal`)
       — a detected provider, a matching self-attestation, and a cwd under the
       registry root of the project its name claims. This is the "identity / cwd /
       role 検証済み" set j#99845 asks for, stated as facts other writers left
       behind rather than as the absence of a residue signal.

    The four run in that order deliberately: each is cheaper than the next, and
    the pure ones need no store at all, so an inventory that is malformed on its
    face is refused without opening the lifecycle store, the registry or the
    attestation store.

    A non-empty refusal means no plan may be built; the groups returned with it
    are not usable. Every refusal here happens BEFORE the first pane move — that
    is the property, not merely the outcome (review j#99904 finding_2 measured
    four moves executed ahead of a closing failure).
    """
    groups = group_by_pair(coordinator_panes_in(rows, target_workspace))
    top = _norm(top_workspace_id)
    for key, members in sorted(groups.items()):
        refusal = _provider_shape_refusal(key, members)
        if refusal:
            return {}, refusal
        if top and key[0] == top and key[1] == DEFAULT_LANE:
            return {}, (
                f"the configured top coordinator {key!r} occupies this shared "
                "project-coordinator workspace; it belongs in its own dedicated one, "
                "and this plan will not reshape it"
            )
    named = sorted(
        key for key in groups if key[1] != DEFAULT_LANE and key != own_key
    )
    index = _lane_kind_index(home) if named else {}
    if index is None:
        return {}, (
            "the durable lane-kind authority is unreadable, so the named lane(s) "
            f"{named!r} in this workspace cannot be proved to be project coordinators"
        )
    for key in named:
        kind = index.get(key, "")
        if kind == LANE_KIND_DELEGATED_COORDINATOR:
            continue
        if not kind:
            return {}, (
                f"named lane {key!r} has no durable lane-kind; refusing to treat it as "
                "a project coordinator"
            )
        return {}, (
            f"named lane {key!r} has durable lane-kind {kind!r}, not "
            f"{LANE_KIND_DELEGATED_COORDINATOR!r}; a non-coordinator lane in the shared "
            "project-coordinator workspace is a placement this plan will not reshape"
        )
    for key, members in sorted(groups.items()):
        if key == own_key:
            continue
        for pane in members:
            refusal = _foreign_evidence_refusal(pane, home=home)
            if refusal:
                return {}, refusal
    return groups, ""


__all__ = (
    "CoordinatorPane",
    "coordinator_panes_in",
    "group_by_pair",
    "resolve_project_groups",
)
