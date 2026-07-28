"""Shared sublane tab resolution (Redmine #14567, Design Answer j#91144 Decision 3).

``sublane_tab_topology.mode: shared_tab`` puts every non-default lane of a project into
ONE herdr tab inside the sublane host workspace, so an operator sees every lane without
switching tabs (owner intent 2026-07-27). This module owns how that single tab is
identified, and nothing else: the workspace axis (#13380) and the per-lane tab axis
(#13411) stay in :mod:`herdr_lane_topology`, and the geometry inside the tab stays in
:mod:`herdr_lane_geometry`.

Why a LABEL is the authority here
---------------------------------
The #13411 tab axis joins on the live ``agent list`` inventory (a lane's own slots pin
their ``tab_id``). That is sufficient when the question is "where are MY slots", and it is
**not** sufficient here. Two concurrent clean-slate lane launches would both observe an
empty inventory and both mint a tab: a tab that has been created but whose first
``agent start`` has not landed yet contributes no inventory row at all, so an
inventory-only resolver is blind to exactly the tab the peer just made (Design Answer
j#91144 Decision 3 — this refuted the worker's inventory-only proposal in j#91131).

So the shared tab is identified by its stable **label**
(:data:`SHARED_SUBLANE_TAB_LABEL`), read back with ``herdr tab list`` — the same
backend-readable adopt authority Redmine #14139 established for the shared coordinators
*workspace*, for the same reason (the container must be recognisable before anything of
ours is inside it). The label gates *adopt*; it is never routing, identity, or liveness
authority — those remain the mzb1 assigned name exactly as before.

The label alone still leaves the clean-slate race open (both processes read "no labelled
tab", both create), so the resolve→create runs under the workspace-scoped single-flight
fence :func:`...sublane_tab_fence.sublane_tab_create_lock`, re-reading the labels **under
the lock** (double-checked). The critical section covers the tab create/adopt only — never
an ``agent start``, never a split — so lane launching itself is not serialised.

Live inventory is still read, as a **consistency check rather than an authority**
(:func:`verify_shared_tab_consistency`): in shared mode every live non-default slot in the
host workspace must already sit in the authority tab. A loose (pre-#13411) pane, a slot in
some other tab, or slots spread across several tabs means the host is mid-transition from
the per-lane topology, and this build refuses to place a new pair rather than silently
adopt, rename, or move what is live (Non-goal: no implicit move of an existing live lane;
explicit live re-placement is #14605). The documented way through is to retire every lane
and relaunch under ``shared_tab``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.core.state.sublane_tab_fence import (
    SublaneTabCreateLockUnavailable,
    SublaneTabCreateReleaseError,
    sublane_tab_create_lock,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    AGENT_KEY_TAB,
    HerdrSessionStartError,
    _lane_live_slot_tabs,
    _tab_target_for_lane,
    _workspace_prefix,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    DEFAULT_LANE,
    _agent_locator,
    _norm,
    decode_assigned_name,
)

#: The stable label of a project's single shared sublane tab (Redmine #14567). Unlike the
#: per-lane tab label (#13411, cosmetic — the join key there is the lane's own ``tab_id``),
#: this label IS the backend-readable adopt authority: a fresh shared tab is created
#: carrying it, and :func:`resolve_shared_tab_from_labels` adopts an existing tab ONLY when
#: it carries exactly this label. Constant per project rather than derived from the lane,
#: precisely because the tab is shared across lanes.
SHARED_SUBLANE_TAB_LABEL = "sublanes"


def _parse_tab_list(stdout: object) -> Optional[dict]:
    """``{tab_id: label}`` from a herdr ``tab list`` payload (fail-closed).

    The shared tab is adopted on an EXACT label match, so the launch must read the live
    tab labels. Accepts the herdr envelope shape::

        {"result": {"type": "tab_list",
                    "tabs": [{"tab_id": "w3:t1", "label": "sublanes"},
                             {"tab_id": "w3:t2", "label": ""}, ...]}}

    and the tolerant variants a bare list of tab objects / an object carrying the list
    under ``tabs`` — mirroring :func:`herdr_lane_topology._parse_workspace_list`, whose
    tolerance was measured against the real ``workspace list``.

    The live shape IS now measured (herdr 0.7.4, read-only capture, pinned by
    ``test_the_real_herdr_074_payload_parses``): the envelope above is exactly what herdr
    sends, and each row carries additional keys (``agent_status`` / ``focused`` /
    ``number`` / ``pane_count`` / ``workspace_id``) that this parser ignores. Rows are
    therefore accepted on the two fields the authority needs and rejected — as a whole
    payload — on anything it cannot read.

    The label is kept **raw / verbatim** (NOT trimmed or case-folded): the adopt authority
    is an EXACT label match, so a padded ``" sublanes "`` or a case-variant ``"Sublanes"``
    is a DIFFERENT label and must not be normalised into the authority label. herdr
    auto-labels a tab created without ``--label`` with its NUMBER (measured: ``"1"``), so
    "unlabelled" is not an empty-string case in practice; a non-string label still maps to
    ``""`` defensively, and neither can ever equal the shared label. An EMPTY list is a
    valid readable result — there really are no tabs — and yields ``{}``.

    Returns ``None`` — "labels unreadable", which the resolver treats as fail-closed —
    when the payload is not JSON, exposes no recognisable tab container, **repeats a
    ``tab_id``** (a herdr identity that appears twice in one snapshot is an identity
    conflict: keeping the last-seen label would make the whole label authority
    order-dependent), **or contains any row this parser cannot read** — a non-mapping
    element, or one whose ``tab_id`` is missing / blank / non-string.

    That last rule is why ``{}`` means something precise (review j#91241 F1). ``{}`` is
    the positive claim "this workspace has no tabs", and the resolver acts on it by
    CREATING one. Skipping unreadable rows would let a container that is plainly non-empty
    produce that same ``{}``, so a payload whose rows key the id differently (say ``id``
    instead of ``tab_id``) would read as "no shared tab exists" and mint a duplicate beside
    the real one. The live ``tab list`` shape has not been measured yet, which is exactly
    the condition under which "I could not read this" must never be reported as "there is
    nothing there". When the real payload is confirmed, widen the accepted shape here
    deliberately — never by resuming the skip. Never a guess; never raises.
    """
    payload = stdout
    if isinstance(stdout, str):
        try:
            payload = json.loads(stdout)
        except (ValueError, TypeError):
            return None
    container = _tab_list_container(payload)
    if container is None:
        return None
    labels: dict = {}
    for entry in container:
        if not isinstance(entry, Mapping):
            return None
        raw_tab_id = entry.get("tab_id")
        # The ``isinstance`` check is load-bearing: ``_norm`` stringifies anything, so a
        # numeric / structured ``tab_id`` would otherwise become a plausible-looking token
        # ("7") that no herdr tab can ever match — a misread payload dressed as data.
        if not isinstance(raw_tab_id, str):
            return None
        tab_id = _norm(raw_tab_id)
        if not tab_id:
            return None
        if tab_id in labels:
            # A duplicate herdr tab identity in one snapshot: the label authority must not
            # depend on which row we saw last. Fail closed on the whole payload.
            return None
        raw_label = entry.get("label")
        # Verbatim — no strip / case-fold (see the docstring).
        labels[tab_id] = raw_label if isinstance(raw_label, str) else ""
    return labels


def _tab_list_container(payload: object) -> Optional[list]:
    """The list of tab objects inside a decoded ``tab list`` payload."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        candidate = payload.get("tabs")
        if isinstance(candidate, list):
            return candidate
        result = payload.get("result")
        if isinstance(result, Mapping):
            return _tab_list_container(result)
    return None


def resolve_shared_tab_from_labels(
    tab_labels: "Optional[Mapping[str, str]]",
    shared_label: str,
    *,
    target_workspace: str,
) -> str:
    """The shared tab carrying ``shared_label`` in ``target_workspace`` (``""`` -> create).

    - ``tab_labels is None`` (the ``tab list`` read failed) fails closed — never guess.
    - Among the tabs carrying ``shared_label`` (an EXACT, verbatim match — no trim /
      case-fold) **whose id belongs to ``target_workspace``**: exactly one -> ADOPT it
      (idempotent join, including a labelled tab with no live slot yet — a partial-failure
      husk or a concurrent peer's not-yet-launched tab is still the shared tab and must be
      adopted, not duplicated); more than one -> fail closed (ambiguous shared tab).
    - none -> ``""``: the caller creates the tab with ``shared_label`` under the
      single-flight fence, so concurrent clean-slate launches converge on one.

    The workspace filter matters because ``tab list`` may be answered for the whole server:
    a tab labelled ``sublanes`` in a DIFFERENT herdr workspace is a different project's
    shared tab and must never be adopted here. Candidates are sorted so the decision does
    not depend on payload iteration order.
    """
    if tab_labels is None:
        raise HerdrSessionStartError(
            "shared sublane tab labels are unreadable (herdr tab list returned no "
            "recognisable payload); refuse to guess the shared tab for workspace "
            f"{target_workspace!r}"
        )
    candidates = sorted(
        tab_id
        for tab_id, label in tab_labels.items()
        if label == shared_label and _workspace_prefix(tab_id) == target_workspace
    )
    if len(candidates) > 1:
        raise HerdrSessionStartError(
            f"multiple herdr tabs in workspace {target_workspace!r} carry the shared "
            f"sublane label {shared_label!r} ({candidates!r}); refuse to guess which one "
            "is the shared tab"
        )
    return candidates[0] if candidates else ""


def host_lane_slot_tabs(
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    target_workspace: str,
) -> "tuple[tuple[str, str], ...]":
    """``(lane_id, tab_id)`` of every live NON-default slot located in ``target_workspace``.

    The inventory basis for the shared-tab consistency check and for the container-scoped
    occupancy the shared topology needs. Unlike
    :func:`herdr_lane_topology._lane_live_slot_tabs` this is **not** scoped to one lane:
    under ``shared_tab`` the container is shared, so what matters is every lane's slots in
    it. ``tab_id`` is ``""`` for a loose (pre-#13411) pane.

    Default-lane (coordinator) slots are excluded: they never live in the sublane host.
    """
    slots: list = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decode = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decode.ok or decode.identity is None:
            continue
        if decode.identity.workspace_id != workspace_id:
            continue
        lane = decode.identity.lane_id or DEFAULT_LANE
        if lane == DEFAULT_LANE:
            continue
        locator = _agent_locator(row)
        if not locator or _workspace_prefix(locator) != target_workspace:
            continue
        slots.append((lane, _norm(row.get(AGENT_KEY_TAB))))
    return tuple(slots)


def verify_shared_tab_consistency(
    host_slots: "Sequence[tuple[str, str]]",
    *,
    authority_tab: str,
    target_workspace: str,
    shared_label: str,
) -> None:
    """Fail closed unless every live host slot already sits in ``authority_tab``.

    The mixed-topology guard (Design Answer j#91144 Decision 3). In ``shared_tab`` mode the
    live inventory is a **consistency check, not an authority**: the label decided which
    tab is the shared one, and this asserts the host is actually in that topology before a
    pane is created.

    Two refusals, distinguished because their remedies differ:

    - ``authority_tab`` is empty (no labelled tab exists) but live lane slots ARE present:
      the host is running the per-lane topology and the config now says ``shared_tab``.
      Creating a shared tab here would leave the workspace half-and-half, and adopting or
      renaming a live lane's tab would move an existing lane implicitly. Refuse.
    - a labelled tab exists but some live slot is loose or in a different tab: the same
      mid-transition state, seen from the other side.

    A host with no live lane slots at all is consistent with either outcome (clean slate),
    so it passes and the caller adopts or mints the shared tab.
    """
    stray = sorted({tab for _, tab in host_slots if tab != authority_tab})
    if not stray:
        return
    lanes = sorted({lane for lane, tab in host_slots if tab != authority_tab})
    where = ", ".join(repr(tab) if tab else "loose (no tab)" for tab in stray)
    if not authority_tab:
        detail = (
            f"no herdr tab in workspace {target_workspace!r} carries the shared sublane "
            f"label {shared_label!r}, but lanes {lanes!r} are live there in {where}"
        )
    else:
        detail = (
            f"the shared sublane tab is {authority_tab!r}, but lanes {lanes!r} are live "
            f"in {where}"
        )
    raise HerdrSessionStartError(
        f"sublane_tab_topology is 'shared_tab' and {detail}; this host is mid-transition "
        "from the per-lane topology. Refusing to create a pane: an existing live lane is "
        "never moved, adopted, or relabelled implicitly. Retire every lane in this "
        "workspace and relaunch under 'shared_tab', or set 'sublane_tab_topology.mode' "
        "back to 'per_lane_tab'."
    )


def resolve_shared_tab_target(
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    target_workspace: str,
    *,
    list_tabs,
    create_tab,
    home: "Optional[Path]" = None,
    shared_label: str = SHARED_SUBLANE_TAB_LABEL,
) -> "tuple[str, str]":
    """The shared tab this lane joins, adopting or minting it: ``(tab_id, created_pane)``.

    The whole ``lock -> tab list -> resolve -> verify -> adopt|create`` critical section
    (Design Answer j#91144 Decision 3). ``created_pane`` is the empty root pane id of a tab
    this run MINTED (``""`` when the tab was adopted), so the caller reclaims exactly what
    it created and never a pane it merely found.

    Ordering is what keeps the refusals zero-actuation: the labels are read and the
    consistency guard runs BEFORE any ``tab create``, so a mid-transition host is refused
    without having created anything. The re-read happens under the lock, so a peer that
    won the race is observed rather than duplicated.

    ``list_tabs`` / ``create_tab`` are injected (the herdr command pair), keeping this
    function free of subprocess knowledge and directly testable.

    Every fence failure is converted to :class:`HerdrSessionStartError` (review j#91241 F2),
    because that is the only type the launch front doors catch — a raw lock error would
    escape ``herdr session-start`` / the bare ``mozyo`` launch as an unformatted traceback
    instead of the fail-closed message they render. The two phases are converted separately
    because they are true of different things, and the wording states only what THIS rail
    can vouch for:

    - **acquisition** runs before any herdr command here, so no tab and no agent exist.
      It deliberately does NOT claim "no workspace was created": the host workspace is
      resolved by ``herdr_host_workspace.resolve_host_workspace`` EARLIER in the same run,
      so a fresh sublane host may well have been minted already. Copying the coordinator
      fence's wider "no workspace / tab / agent" sentence would make this message false.
    - **release** runs after the body, so the shared tab was already resolved — adopted, or
      created on a clean slate — and no agent has started yet. A labelled, slot-less tab may
      remain; a re-run adopts it idempotently rather than minting a second one.
    """
    if not target_workspace:
        raise HerdrSessionStartError(
            "shared sublane tab resolution requires a resolved host workspace; refuse to "
            "list or create a tab without one"
        )
    try:
        with sublane_tab_create_lock(workspace_id, home=home):
            # Read the labels UNDER the lock: a peer that created the shared tab between
            # our decision to launch and this point must be observed, not duplicated.
            target_tab = resolve_shared_tab_from_labels(
                list_tabs(target_workspace),
                shared_label,
                target_workspace=target_workspace,
            )
            verify_shared_tab_consistency(
                host_lane_slot_tabs(rows, workspace_id, target_workspace),
                authority_tab=target_tab,
                target_workspace=target_workspace,
                shared_label=shared_label,
            )
            if target_tab:
                return target_tab, ""
            return create_tab(target_workspace, shared_label)
    except SublaneTabCreateReleaseError as exc:
        raise HerdrSessionStartError(
            "the shared sublane tab was resolved for workspace "
            f"{target_workspace!r} but the single-flight lock could not be released "
            f"({exc}); no agent was started. A tab labelled {shared_label!r} may remain "
            "with no slots in it — re-run to adopt it idempotently (no duplicate is "
            "created)."
        ) from exc
    except SublaneTabCreateLockUnavailable as exc:
        raise HerdrSessionStartError(
            "could not acquire the shared sublane tab single-flight lock for workspace "
            f"{target_workspace!r} ({exc}); no tab and no agent were created. Re-run once "
            "the home lock is reachable."
        ) from exc


@dataclass(frozen=True)
class LaneTabPlacement:
    """Which tab a lane's launches join, and what this run had to create to get there.

    - :attr:`tab_id` — the resolved herdr tab (``""`` = launch loose, the pre-#13411
      legacy-heal case).
    - :attr:`created_pane_id` — the empty root pane of a tab this run MINTED (``""`` when
      the tab was adopted), so the caller reclaims exactly what it created.
    - :attr:`lane_slot_tabs` — the tab ids of this lane's live slots in the host workspace.
    - :attr:`host_slot_tabs` — ``(lane_id, tab_id)`` for EVERY lane's live slots there.
      Empty outside ``shared_tab``: the per-lane container never needs the other lanes.
    """

    tab_id: str
    created_pane_id: str
    lane_slot_tabs: "tuple[str, ...]"
    host_slot_tabs: "tuple[tuple[str, str], ...]"


def resolve_lane_tab(
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    target_workspace: str,
    lane_id: str,
    *,
    shared_tab: bool,
    list_tabs,
    create_tab,
    home: "Optional[Path]" = None,
    shared_label: str = SHARED_SUBLANE_TAB_LABEL,
) -> LaneTabPlacement:
    """The tab a non-default lane's launches join, under either topology (Redmine #14567).

    The single tab-axis entry point, so the session-start orchestrator holds no topology
    branch of its own.

    ``per_lane_tab`` — the #13411 placement, unchanged. The lane's own live/adopted slots
    pin their tab (a heal rejoins the SAME tab, never splitting the pair across tabs). When
    nothing pins one, a tab is minted explicitly ONLY for a FRESH lane (no own live slots),
    labelled with the lane key (cosmetic) so its empty root pane is a known handle to
    reclaim. A heal of a legacy pre-#13411 lane whose live slots are LOOSE panes (own slots
    present, no tab pinned) launches loose too, keeping the pair together — it migrates to a
    tab on a full relaunch (the #13380 cohabiting precedent, which drains via retire). The
    fresh-vs-loose decision reads the lane's WHOLE live inventory, NOT this run's requested
    plans (review j#74433 finding 1): a single-provider heal requests one provider, so the
    lane's OTHER live slot is in the inventory but never in the plans, and counting
    requested adopts alone would mint a fresh tab for a live loose sibling.

    ``shared_tab`` — :func:`resolve_shared_tab_target`: the label is the authority, the
    resolve→create runs under the single-flight fence, and a host mid-transition from the
    per-lane topology is refused before anything is created. The lane's own slots do not
    pin the tab here (the label does); the consistency guard is what proves they are in it.

    The caller must only invoke this for a non-default lane that is actually launching: the
    default lane has no tab at all, and a run that launches nothing needs no container.
    """
    lane_slot_tabs = tuple(
        _lane_live_slot_tabs(rows, workspace_id, target_workspace, lane_id)
    )
    if shared_tab:
        host_slot_tabs = host_lane_slot_tabs(rows, workspace_id, target_workspace)
        tab_id, created_pane_id = resolve_shared_tab_target(
            rows,
            workspace_id,
            target_workspace,
            list_tabs=list_tabs,
            create_tab=create_tab,
            home=home,
            shared_label=shared_label,
        )
        return LaneTabPlacement(
            tab_id=tab_id,
            created_pane_id=created_pane_id,
            lane_slot_tabs=lane_slot_tabs,
            host_slot_tabs=host_slot_tabs,
        )
    tab_id = _tab_target_for_lane(rows, workspace_id, target_workspace, lane_id)
    created_pane_id = ""
    if not tab_id and not lane_slot_tabs:
        tab_id, created_pane_id = create_tab(target_workspace, lane_id)
    return LaneTabPlacement(
        tab_id=tab_id,
        created_pane_id=created_pane_id,
        lane_slot_tabs=lane_slot_tabs,
        host_slot_tabs=(),
    )


__all__ = (
    "SHARED_SUBLANE_TAB_LABEL",
    "LaneTabPlacement",
    "resolve_lane_tab",
    "_parse_tab_list",
    "host_lane_slot_tabs",
    "resolve_shared_tab_from_labels",
    "resolve_shared_tab_target",
    "verify_shared_tab_consistency",
)
