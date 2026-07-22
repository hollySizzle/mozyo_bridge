"""herdr lane placement / topology — the pure decision core (Redmine #13411).

Cohesive sibling of :mod:`herdr_session_start` (the subprocess-driving session
orchestrator). This module owns the *pure* placement decisions and payload
parsers the session-start flow makes before it launches anything — no
subprocess, no ambient I/O — so the orchestrator stays under its module-health
baseline while the two-axis placement model grows a second axis.

Two-axis placement
------------------
A mozyo workspace occupies a constant "project 1 + host 1" herdr terminal
workspaces (Redmine #13380): the project workspace hosts the coordinator pair
(default lane) and a single **sublane host workspace** hosts every lane slot.
Redmine #13411 subdivides that host along a second axis — **lane = tab**: every
non-default lane occupies ONE dedicated herdr *tab* inside the host workspace,
with its gateway + worker placed as a split pair inside that tab, so a host with
N lanes shows N tabs instead of 2N loose panes (owner intent #13377 j#73654 "親・
子・孫の 3 ウィンドウ" + the 7 lane = 14 pane density concern).

- :func:`_launch_target_for_lane` resolves the herdr *workspace* a lane's
  launches join (#13380 axis).
- :func:`_tab_target_for_lane` resolves the herdr *tab* within that workspace a
  non-default lane's launches join (#13411 axis).

Both of these two axes key on the live mzb1 inventory only (``tab_id`` /
assigned-name identity), never on a herdr label: the identity model
(``mzb1_<project-ws>_<role>_<lane>``) and route authority are unchanged; only the
herdr placement subdivides.

The one deliberate exception is the **operator-scoped shared coordinators space**
(Redmine #14139, ``shared_space`` mode, :func:`_shared_coordinator_target`): there
the default-lane pair spans every project's mozyo ``workspace`` identity, so the
inventory alone cannot tell the shared space from a per-project coordinator
window. That axis — and ONLY that axis — uses the stable workspace *label*
(:data:`SHARED_COORDINATOR_WORKSPACE_LABEL`) as the backend-readable adopt
authority. It still never touches the mzb1 identity or route authority; the label
gates *adopt*, nothing else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.core.state.workspace_registry import (
    _is_linked_worktree,
    _main_worktree_root,
    load_workspace_by_path,
    read_anchor,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    DEFAULT_LANE,
    _agent_locator,
    _norm,
    decode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    valid_target,
)

#: The herdr ``agent list`` row key carrying a slot's tab id (``wN:tM``). Real
#: 0.7.1 rows expose ``tab_id`` alongside ``workspace_id`` (measured #13380
#: j#73668); a lane's slots share one tab, and a heal reads it to rejoin.
AGENT_KEY_TAB = "tab_id"


class HerdrSessionStartError(ValueError):
    """A herdr session-start step cannot proceed (fail-closed)."""


def herdr_workspace_segment(repo_root: Path, *, home: Optional[Path] = None) -> str:
    """The mzb1 ``workspace`` segment for ``repo_root`` (Redmine #13377, design j#73613).

    The single, read-only resolver every herdr identity site shares so mint-time
    (:func:`prepare_session`) and resolve-time (send, retire, projection, lane
    read-back) always agree:

    - a **linked git worktree** (a sublane lane checkout) → the **main checkout's**
      ``workspace_id``, inherited with the SAME precedence as the canonical
      worktree inheritance (:func:`workspace_registry._inherited_worktree_result` /
      :func:`resolve_canonical_session`, #13152): the main **registry row** first,
      then the main **anchor**. Reading only the anchor (the pre-#13595 shape)
      missed a *registry-only* main — anchor absent (anchors are untracked), row
      present — where the identity still inherits; a ``--dry-run`` there fell
      fail-closed while the canonical execute path inherited it, and (this being the
      single mint==resolve resolver) send / retire / projection missed it too
      (Redmine #13595 R1-F1). Under the shared project workspace model (#13377 Opt3,
      superseding the per-lane ``wt_<hash>`` workspace of #13331 j#73357) a lane's
      agents live in the project workspace as ``mzb1_<project-ws>_<role>_<lane>``
      slots, so the ``workspace`` segment is the project identity and the *lane*
      segment is the discriminant. The legacy per-lane token
      (:func:`derive_lane_workspace_token`) is no longer minted for new slots; it
      survives only as the compatibility key for pre-#13377 rows (legacy resolve /
      retire) and as the lane metadata record's stable per-worktree join key;
    - otherwise (**standalone / main checkout**) → the registry / anchor
      ``workspace_id``, read-only (no registration), byte-for-byte the prior
      behaviour. ``""`` when no anchor resolves (the caller decides whether that is
      fatal — :func:`prepare_session` fails closed; the resolve sites treat ``""``
      as "not a resolvable workspace").

    Read-only in every branch (no registration / anchor / ``last_seen`` write). The
    inheritance change is monotonic: a main with a row + agreeing anchor and an
    anchor-only main are byte-for-byte unchanged; only a registry-only main flips
    ``""`` -> the canonically-inherited id (the same value
    :func:`register_workspace` would produce), so no consumer's non-empty result
    changes — a previously fail-closed registry-only main now resolves correctly.
    """
    resolved = Path(repo_root).expanduser().resolve()
    if _is_linked_worktree(resolved):
        main_root = _main_worktree_root(resolved)
        if main_root is None:
            return ""
        record = load_workspace_by_path(main_root, home=home)
        if record is not None:
            workspace_id = _norm(record.workspace_id)
            if workspace_id:
                return workspace_id
        anchor = read_anchor(main_root)
        return _norm(anchor.get("workspace_id")) if isinstance(anchor, dict) else ""
    anchor = read_anchor(resolved)
    return _norm(anchor.get("workspace_id")) if isinstance(anchor, dict) else ""


def _workspace_prefix(locator: str) -> str:
    """The herdr workspace id (``wN``) of a ``wN:pM`` locator (``""`` if unparseable).

    herdr terminal locators are ``<workspace>:<pane>`` (e.g. ``w2:p3``); the part
    before the first ``:`` is the workspace the pane lives in. Returns ``""`` for a
    blank / colonless / malformed handle so the caller fails closed rather than
    guessing a launch target.
    """
    loc = _norm(locator)
    if ":" not in loc:
        return ""
    prefix = loc.split(":", 1)[0]
    return prefix if valid_target(prefix) else ""


def _tab_id_of_row(row: Mapping[str, object]) -> str:
    """The herdr tab id (``wN:tM``) an ``agent list`` row reports (``""`` if absent).

    A lane's slots live in a dedicated tab (Redmine #13411); a heal / mixed
    adopt+launch reads the live slot's ``tab_id`` to rejoin the SAME tab rather
    than split the gateway/worker pair into a fresh one. Fail-soft to ``""`` — the
    caller decides (an own live slot with no readable tab fails closed).
    """
    if not isinstance(row, Mapping):
        return ""
    return _norm(row.get(AGENT_KEY_TAB))


def _launch_target_for_lane(
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    lane_id: str,
    adopted_locators: Sequence[str],
) -> str:
    """The herdr workspace this lane's launches must join (``""`` -> create one).

    Dedicated sublane host workspace model (Redmine #13380, refining the #13377
    shared project workspace): one mozyo workspace occupies exactly TWO herdr
    terminal workspaces — the project workspace hosting the coordinator pair
    (default lane) and a single **sublane host workspace** hosting every lane
    slot — so the workspace count stays a constant "project 1 + host 1", still
    never scaling with the lane count. The identity model is unchanged (the mzb1
    ``workspace`` segment stays the project identity, j#73613); only the herdr
    placement splits. The target is picked from the live inventory, in order:

    1. the lane's OWN live slots (plus this run's adopted slots — always
       same-lane) pin the target. A heal never splits a gateway/worker pair
       across workspaces, even for a lane still cohabiting the coordinator's
       workspace (pre-#13380 placement, which drains via retire).
    2. a non-default lane with no own pins joins the workspace the OTHER live
       lane slots occupy, EXCLUDING any workspace the live default-lane
       (coordinator) slots occupy. The exclusion is what lands a new lane in
       the dedicated host instead of the coordinator's window while legacy
       cohabiting lanes are still alive.
    3. nothing pins one -> ``""``: the caller creates the workspace explicitly
       (the project workspace for the default lane, the labelled sublane host
       for a lane slot). A lane-zero host cannot linger to be rejoined — herdr
       auto-closes a workspace with its last pane (live-measured, #13380) — so
       the next lane simply re-mints it on demand.

    The default lane only ever joins its own pins (rule 1): the coordinator
    pair never lands in the sublane host, mirroring the separation.

    Raises when any pin set spans more than one herdr workspace: refusing to
    guess which one the launches belong to (the #13330 fail-closed posture).
    """
    lane = _norm(lane_id) or DEFAULT_LANE
    own = [loc for loc in adopted_locators if loc]
    sibling_lanes: list = []
    coordinator: list = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decode = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decode.ok or decode.identity is None:
            continue
        if decode.identity.workspace_id != workspace_id:
            continue
        locator = _agent_locator(row)
        if not locator:
            continue
        row_lane = decode.identity.lane_id or DEFAULT_LANE
        if row_lane == lane:
            own.append(locator)
        elif row_lane == DEFAULT_LANE:
            coordinator.append(locator)
        else:
            sibling_lanes.append(locator)
    own_prefixes = {p for p in (_workspace_prefix(loc) for loc in own) if p}
    if len(own_prefixes) > 1:
        raise HerdrSessionStartError(
            f"live slots of lane {lane!r} span multiple herdr workspaces "
            f"{sorted(own_prefixes)!r}; refuse to guess which one new launches "
            "belong to"
        )
    if own_prefixes:
        return next(iter(own_prefixes))
    if lane == DEFAULT_LANE:
        return ""
    coordinator_prefixes = {
        p for p in (_workspace_prefix(loc) for loc in coordinator) if p
    }
    host_prefixes = {
        p for p in (_workspace_prefix(loc) for loc in sibling_lanes) if p
    } - coordinator_prefixes
    if len(host_prefixes) > 1:
        raise HerdrSessionStartError(
            f"lane slots of mozyo workspace {workspace_id!r} span multiple herdr "
            f"workspaces {sorted(host_prefixes)!r} outside the coordinator's; "
            "refuse to guess which one is the sublane host"
        )
    return next(iter(host_prefixes)) if host_prefixes else ""


#: The stable label of the single shared coordinators herdr workspace (Redmine
#: #14139, ``shared_space`` placement mode). Unlike the sublane host / tab labels
#: (#13380 / #13411, which are cosmetic and never a join key), this label IS the
#: **backend-readable adopt authority**: a fresh shared space is created carrying
#: it, and :func:`_shared_coordinator_target` adopts an existing space ONLY when a
#: live workspace carries exactly this label (R2 review j#83383 F1 / Design Answer
#: j#83385 Decision 1). Constant, not derived from any project, precisely because
#: the space is shared across projects. (The mzb1 assigned-name identity and route
#: authority are still unchanged — this label gates *adopt*, not identity/routing.)
SHARED_COORDINATOR_WORKSPACE_LABEL = "coordinators"


def _parse_workspace_list(stdout: object) -> Optional[dict]:
    """``{herdr_workspace_id: label}`` from a herdr ``workspace list`` payload (fail-closed).

    The shared coordinators space (Redmine #14139 ``shared_space``) is identified by
    its stable ``label`` (Design Answer j#83385 Decision 1: the label is the
    backend-readable authority, NOT a locator-prefix guess), so a launch that adopts
    the space must read the live workspace labels. Accepts the herdr envelope shape::

        {"result": {"type": "workspace_list",
                    "workspaces": [{"workspace_id": "w1", "label": "coordinators"},
                                   {"workspace_id": "w2", "label": ""}, ...]}}

    and the tolerant variants a bare list of workspace objects / an object carrying
    the list under ``workspaces`` (mirroring the ``agent list`` tolerance). Each
    entry contributes ``workspace_id -> label``. The label is kept **raw / verbatim**
    (NOT trimmed or case-folded): the shared space is adopted only on an EXACT label
    match (spec §5.1.1 / Design Answer j#83385 Decision 1 / R4 review j#83473 F1), so
    a padded ``" coordinators "`` or a case-variant ``"Coordinators"`` is a DIFFERENT
    label and must not be normalised into the authority label. A missing / non-string
    label is the empty string (present but unlabelled, so it never matches). An entry
    with no ``workspace_id`` is skipped. An EMPTY list is a valid readable result (no
    workspaces) and yields ``{}``.

    Returns ``None`` — "labels unreadable", which the resolver treats as
    fail-closed — when the payload is not JSON, exposes no recognisable workspace
    container, **or repeats a ``workspace_id``** (a herdr identity that appears
    twice in one snapshot is an identity conflict: keeping the last-seen label would
    make the whole label authority order-dependent — R2 review j#83425 F1 / Design
    Answer j#83385 Decision 1 "identity conflict は typed fail-closed"). Never a
    guess; never raises.
    """
    payload = stdout
    if isinstance(stdout, str):
        try:
            payload = json.loads(stdout)
        except (ValueError, TypeError):
            return None
    container = _workspace_list_container(payload)
    if container is None:
        return None
    labels: dict = {}
    for entry in container:
        if not isinstance(entry, Mapping):
            continue
        workspace_id = _norm(entry.get("workspace_id"))
        if not workspace_id:
            continue
        if workspace_id in labels:
            # A duplicate herdr workspace identity in one snapshot: the label
            # authority must not depend on which row we saw last. Fail closed on the
            # whole payload rather than pick a winner.
            return None
        raw_label = entry.get("label")
        # Verbatim — no strip / case-fold: the adopt authority is an EXACT label
        # match, so normalising here would let a padded / case-variant label pass
        # as the shared label (R4 review j#83473 F1).
        labels[workspace_id] = raw_label if isinstance(raw_label, str) else ""
    return labels


def _workspace_list_container(payload: object) -> Optional[list]:
    """The list of workspace objects inside a decoded ``workspace list`` payload."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        candidate = payload.get("workspaces")
        if isinstance(candidate, list):
            return candidate
        result = payload.get("result")
        if isinstance(result, Mapping):
            return _workspace_list_container(result)
    return None


def _shared_coordinator_own_target(
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    adopted_locators: Sequence[str],
) -> str:
    """This project's own live/adopted default-lane pin, or ``""`` if none (no label read).

    Step 1 of the shared-space resolution, split out so the caller can resolve an
    own-pin heal WITHOUT reading the workspace labels first (Redmine #14139 R4 review
    j#83473 F2): the spec §5.1.1 contract is that own identity pins the target, so a
    heal must not depend on the ``workspace list`` command succeeding. Returns the
    single herdr workspace this project's own live default-lane slots (plus this
    run's adopted slots — always same-lane, same-project) occupy, or ``""`` when the
    project has no own coordinator slot yet. Raises when own slots span more than one
    herdr workspace (identity conflict — refuse to guess).
    """
    own = [loc for loc in adopted_locators if loc]
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decode = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decode.ok or decode.identity is None:
            continue
        if (decode.identity.lane_id or DEFAULT_LANE) != DEFAULT_LANE:
            continue
        if decode.identity.workspace_id != workspace_id:
            continue
        locator = _agent_locator(row)
        if locator:
            own.append(locator)
    own_prefixes = {p for p in (_workspace_prefix(loc) for loc in own) if p}
    if len(own_prefixes) > 1:
        raise HerdrSessionStartError(
            f"live coordinator slots of workspace {workspace_id!r} span multiple herdr "
            f"workspaces {sorted(own_prefixes)!r}; refuse to guess which one new launches "
            "belong to"
        )
    return next(iter(own_prefixes)) if own_prefixes else ""


def _shared_coordinator_target(
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    adopted_locators: Sequence[str],
    workspace_labels: Optional[Mapping[str, str]],
    shared_label: str,
) -> str:
    """The shared coordinators herdr workspace the default-lane pair joins (``""`` -> create).

    ``shared_space`` placement mode (Redmine #14139, operator-scoped): every
    project's coordinator pair (default lane) shares ONE stable herdr workspace,
    each project a column, so an operator oversees every project's coordinators in
    a single window (the tmux-era overview). The ``per_project_space`` default is
    unchanged and never reaches here — it keeps :func:`_launch_target_for_lane`.

    The shared space is identified by its **stable label** (``shared_label`` ==
    :data:`SHARED_COORDINATOR_WORKSPACE_LABEL`), the backend-readable authority
    (Design Answer j#83385 Decision 1 — R1 review j#83383 F1). A live foreign
    default-lane workspace is NOT assumed to be the shared space just because it
    holds a coordinator pair: a ``per_project_space`` coordinator workspace is
    indistinguishable from the shared one by inventory alone, so R1's prefix-only
    guess would wrongly ADOPT a per-project window on a mode transition. Resolution,
    in order:

    1. this project's OWN live/adopted default-lane slots pin the target
       (:func:`_shared_coordinator_own_target`) — no label read needed (rejoining its
       own live space). The caller resolves this BEFORE reading labels so an own-pin
       heal never depends on the ``workspace list`` command (R4 review j#83473 F2);
       this function re-checks it so it stays correct when called directly.
    2. no own pins, so a join/create decision is needed and the labels are the
       authority. ``workspace_labels is None`` (the ``workspace list`` read failed)
       fails closed — never guess. Among the herdr workspaces carrying ``shared_label``
       (an EXACT, verbatim match — no trim / case-fold, R4 review j#83473 F1),
       INCLUDING a labelled workspace with no live default-lane slot yet (R5 review
       j#83516 F1 — a partial-failure husk or a concurrent peer's not-yet-launched
       space is still the shared space and must be adopted, not duplicated):

       - exactly one -> ADOPT it (idempotent join; this is what crosses the mozyo
         ``workspace`` identity boundary safely, gated on the label);
       - more than one -> fail closed (ambiguous shared space);

    3. no labelled candidate at all:

       - but foreign default-lane pairs ARE live (in un/differently-labelled
         per-project workspaces) -> fail closed. This is the mode-transition guard:
         refuse to silently promote a per-project coordinator window to the shared
         space (Decision 1: no implicit promotion / relabel);
       - otherwise (no foreign coordinator pair live at all) -> ``""``: the caller
         creates the shared workspace with ``shared_label`` UNDER the single-flight
         fence (``coordinator_placement_fence.coordinator_shared_create_lock``), so
         concurrent clean-slate launches converge to one workspace.

    Only default-lane (coordinator) slots are ever consulted: a sublane slot never
    pins the coordinators space (its placement is the untouched #13380/#13411 axis).
    Own pins spanning more than one herdr workspace fail closed (identity conflict).
    """
    own_target = _shared_coordinator_own_target(rows, workspace_id, adopted_locators)
    if own_target:
        return own_target

    if workspace_labels is None:
        raise HerdrSessionStartError(
            "shared coordinators workspace labels are unreadable (herdr workspace list "
            f"returned no recognisable payload); refuse to guess the shared space for "
            f"workspace {workspace_id!r}"
        )
    # Candidates are the herdr workspaces carrying the EXACT shared label — read from
    # the labels directly, INCLUDING a labelled workspace with no live default-lane
    # slot yet (R5 review j#83516 F1). That covers two idempotency cases the earlier
    # "labelled AND has a live slot" set missed: a partial-failure HUSK (created +
    # labelled, then its agent-start failed) and a concurrent peer's workspace created
    # under the single-flight fence but not yet launched into — both are the shared
    # space and must be ADOPTED, not duplicated. The match is EXACT / verbatim (no
    # `_norm`): a padded or case-variant label is a different label (R4 F1). Sorted for
    # an inventory-iteration-independent decision (Design Answer j#83385 Decision 2).
    labelled_candidates = sorted(
        ws for ws, label in workspace_labels.items() if label == shared_label
    )
    if len(labelled_candidates) > 1:
        raise HerdrSessionStartError(
            f"multiple herdr workspaces carry the shared coordinators label "
            f"{shared_label!r} ({labelled_candidates!r}); refuse to guess which one is "
            "the shared space"
        )
    if labelled_candidates:
        return labelled_candidates[0]
    # No labelled shared workspace exists. If foreign per-project coordinator pairs
    # ARE live (in un/differently-labelled workspaces), this is a mode transition:
    # refuse to promote a per-project window to the shared space (Decision 1).
    foreign_by_workspace: dict = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decode = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decode.ok or decode.identity is None:
            continue
        if (decode.identity.lane_id or DEFAULT_LANE) != DEFAULT_LANE:
            continue
        if decode.identity.workspace_id == workspace_id:
            continue
        locator = _agent_locator(row)
        if not locator:
            continue
        prefix = _workspace_prefix(locator)
        if prefix:
            foreign_by_workspace.setdefault(prefix, True)
    if foreign_by_workspace:
        raise HerdrSessionStartError(
            "shared_space launch found live coordinator pairs but none in a workspace "
            f"labelled {shared_label!r} "
            f"({sorted(foreign_by_workspace)!r}); refuse to promote a per-project "
            "coordinator workspace to the shared space (retire the per-project pairs, "
            "or launch into an existing shared space)"
        )
    return ""


def _lane_live_slot_tabs(
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    target_workspace: str,
    lane_id: str,
) -> list:
    """Tab ids of every same-lane live slot located in ``target_workspace``.

    The inventory basis for the fresh-vs-loose and tab-occupancy decisions
    (Redmine #13411 review j#74433 finding 1). A single-provider heal requests only
    ONE provider, so the lane's OTHER live slot is present in the inventory (``rows``)
    but never in this run's requested ``plans``; counting requested adopts alone
    would miss it and (a) drop the ``--split right`` a heal beside a live tabbed
    sibling needs, and (b) mint a fresh tab for a loose legacy sibling, splitting the
    pair. Reading the whole lane's live slots from ``rows`` fixes both.

    Each element is the slot's ``tab_id`` (``""`` for a loose pre-#13411 pane). Only
    slots whose locator is in ``target_workspace`` are counted (a lane's legacy slot
    cohabiting a different herdr workspace drains via retire, the #13380 axis).
    ``workspace_id`` is the mozyo identity segment; ``target_workspace`` is the
    resolved herdr terminal workspace.
    """
    lane = _norm(lane_id) or DEFAULT_LANE
    tabs: list = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decode = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decode.ok or decode.identity is None:
            continue
        if decode.identity.workspace_id != workspace_id:
            continue
        if (decode.identity.lane_id or DEFAULT_LANE) != lane:
            continue
        locator = _agent_locator(row)
        if not locator or _workspace_prefix(locator) != target_workspace:
            continue
        tabs.append(_tab_id_of_row(row))
    return tabs


def _tab_target_for_lane(
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    target_workspace: str,
    lane_id: str,
) -> str:
    """The herdr tab this lane's launches join within ``target_workspace`` (``""`` -> create).

    Lane=tab subdivision (Redmine #13411, on the #13380 dedicated sublane host):
    every non-default lane occupies ONE dedicated herdr tab inside the single
    sublane host workspace, so a host with N lanes shows N tabs instead of 2N
    loose panes. The tab is picked from the live inventory:

    1. the lane's OWN live slots in ``target_workspace`` pin their tab — a heal /
       mixed adopt+launch rejoins the SAME tab so the gateway/worker pair is
       never split across tabs. Own slots spanning more than one tab fail closed.
    2. nothing pins one -> ``""``. The caller distinguishes two ``""`` cases by
       whether the lane has any live/adopted slot: a FRESH lane (no own slots)
       mints a dedicated tab; a heal of a legacy pre-#13411 lane whose live slots
       are loose panes (no ``tab_id``) launches loose too, keeping the pair
       together (it migrates to a tab on a full relaunch — the #13380 cohabiting
       precedent, drains via retire), never split into a fresh tab.

    Only slots whose locator lives in ``target_workspace`` pin the tab: a lane's
    legacy pre-#13411 slot cohabiting a different herdr workspace never does (it
    drains via retire, exactly like the #13380 workspace axis). ``workspace_id``
    is the mozyo identity segment (name decode); ``target_workspace`` is the
    resolved herdr terminal workspace the launches join.
    """
    lane = _norm(lane_id) or DEFAULT_LANE
    own_tabs = {
        tab
        for tab in _lane_live_slot_tabs(rows, workspace_id, target_workspace, lane_id)
        if tab
    }
    if len(own_tabs) > 1:
        raise HerdrSessionStartError(
            f"live slots of lane {lane!r} occupy multiple herdr tabs "
            f"{sorted(own_tabs)!r} in workspace {target_workspace!r}; refuse to "
            "guess which one new launches belong to"
        )
    return next(iter(own_tabs)) if own_tabs else ""


def resolve_launch_order(
    providers: Sequence[str], config_order: Optional[Sequence[str]]
) -> list:
    """The requested providers, reordered by the configured launch order (pure).

    Config-driven placement (Redmine #13646, Design Answer j#76564 Q2): ``config_order`` is
    a full provider permutation naming who occupies the container FIRST (and therefore who
    splits beside them). Reordering the REQUESTED providers is the only way to realize a
    role order, because herdr ``agent start`` has no pane-target flag — order is launch
    order (live ``--help`` characterization j#76559).

    It never grows the request: a single-provider heal stays a single provider (an ``order``
    naming both providers must not launch an unrequested peer). ``None`` returns the
    requested sequence unchanged (byte-invariant).
    """
    if config_order is None:
        return list(providers)
    rank = {provider: index for index, provider in enumerate(config_order)}
    return sorted(providers, key=lambda provider: rank.get(provider, len(rank)))


def resolve_split_direction(lane_class: str, config_split: Optional[str]) -> str:
    """The ``--split`` direction a splitting slot of ``lane_class`` uses (``""`` = none).

    Config-driven placement (Redmine #13646, Design Answer j#76564 Q3): a configured
    ``split`` wins; otherwise the legacy discipline applies — ``right`` for a ``sublane``
    slot (byte-for-byte the pre-#13646 literal) and NO split for the ``default``
    (coordinator) pair, which delegates to the herdr server default unless explicitly
    configured. ``""`` means the caller emits no ``--split`` flag at all.
    """
    if config_split is not None:
        return config_split
    return "right" if lane_class == "sublane" else ""


def initial_container_occupancy(
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    target_workspace: str,
    lane_id: str,
    *,
    lane_class: str,
    target_tab: str,
    lane_slot_tabs: Sequence[str],
    count_default_lane: bool,
) -> int:
    """How many of this lane's slots already occupy the container a launch splits into.

    The container differs by lane class (Redmine #13411 tab axis + #13646 default axis):

    - a ``sublane``'s container is its dedicated lane TAB, so only same-lane live slots
      ALREADY IN ``target_tab`` count. Read from the whole live inventory, not this run's
      requested plans (review j#74433 finding 1): a single-provider heal requests one
      provider, so the lane's OTHER live slot is in the inventory but never in ``plans`` —
      counting requested adopts alone would drop the split a heal beside a live tabbed
      sibling needs. A freshly minted tab starts empty (0), so its first launch occupies
      and its second splits. A loose (pre-#13411, tab-less) heal has no ``target_tab`` and
      counts 0, so it stays loose — byte-invariant.
    - the ``default`` lane has no tab, so its container is the project WORKSPACE itself:
      the coordinator pair's own live slots in ``target_workspace``. This is what makes a
      fresh pair's 2nd slot split beside the 1st and a heal split beside the live sibling.
      ``count_default_lane`` is False when nothing launches or no split is configured, so
      an unset default lane never reads it and stays byte-for-byte the pre-#13646 launch.

    Live slots count regardless of how this run classified them (adopt / unattested /
    stale): they occupy a pane either way, and a launch must split beside a live pane.
    """
    if lane_class == "sublane":
        return sum(1 for tab in lane_slot_tabs if tab == target_tab) if target_tab else 0
    if not count_default_lane:
        return 0
    return len(_lane_live_slot_tabs(rows, workspace_id, target_workspace, lane_id))


def resolve_placement_policy(
    lane_placement: object, lane_class: str
) -> "tuple[Optional[str], Optional[tuple[str, ...]]]":
    """The lane class's configured ``(split, order)``, or ``(None, None)`` when unset.

    The one adapter between the repo-local ``lane_placement`` config record (Redmine
    #13646) and the pure placement decisions below, so the session-start composition root
    holds no config-shape knowledge and this module keeps no config import (it only calls
    ``.resolve(lane_class)``). ``None`` config — or a lane class the config omits — yields
    ``(None, None)``: inherit the legacy launch discipline everywhere (byte-invariant).
    """
    if lane_placement is None:
        return None, None
    resolved = lane_placement.resolve(lane_class)  # type: ignore[attr-defined]
    return resolved.split, resolved.order


def resolve_placement_policy_for_role(
    lane_placement: object, lane_class: str, lane_kind: "Optional[str]"
) -> "tuple[Optional[str], Optional[tuple[str, ...]]]":
    """The lane's configured ``(split, order)`` under the ``role > lane_class > default`` precedence.

    Redmine #13647 (Design Answer j#85645, disposition j#85650). The precedence is a
    typed fall-through over the SAME :class:`ResolvedPlacement` the lane-class layer
    already returns, so it is a strict superset of :func:`resolve_placement_policy`:

    1. **lane-kind layer** — consulted ONLY when a durable ``lane_kind`` is supplied
       AND the config's ``by_lane_kind`` block explicitly declares that kind
       (:meth:`LanePlacementConfig.has_lane_kind`). This is the only path that can
       diverge from the pre-#13647 result.
    2. **lane-class layer** — otherwise the existing ``default`` / ``sublane``
       resolution (:func:`resolve_placement_policy`), byte-for-byte.
    3. **legacy default** — no config yields ``(None, None)``.

    Byte-invariance: ``lane_kind is None`` (no durable kind fact — the launch path's
    fallback) OR a config with no matching ``by_lane_kind`` entry both fall straight
    through to step 2, so every existing repo and every kind-unresolved launch keeps
    today's exact ``(split, order)``. An unknown ``by_lane_kind`` key is rejected at
    config parse time, not here; runtime "kind simply unresolved" is a fall-through,
    never an error (the issue's close condition).
    """
    if lane_placement is None:
        return None, None
    if lane_kind is not None and lane_placement.has_lane_kind(lane_kind):  # type: ignore[attr-defined]
        resolved = lane_placement.resolve_by_lane_kind(lane_kind)  # type: ignore[attr-defined]
        return resolved.split, resolved.order
    return resolve_placement_policy(lane_placement, lane_class)


def resolve_focus_first_launch(
    *,
    config_split: Optional[str],
    config_order: Optional[Sequence[str]],
    launch_count: int,
    container_occupancy: int,
) -> bool:
    """True iff this run's FIRST launch must carry ``--focus`` (pure).

    The R1-F1 fix (review j#76613, Design Answer R1 j#76616). herdr splits a container's
    ACTIVE pane and ``agent start`` has no pane-target flag, so when every launch is
    ``--no-focus`` the container's empty ROOT pane stays active: the second slot's
    ``--split <dir>`` splits the root rather than the first agent, and reclaiming the root
    (after all launches, #13330) collapses that split away — leaving only the outer default
    ``right`` split the first agent implicitly created. The configured direction silently
    never applies (live-measured on BOTH the tab-less default pair and the lane tab: the
    pre-#13646 ``--split right`` literal only *looked* correct because it coincides with
    herdr's default direction, j#76622). Focusing the first launch pins the container's
    split target to that agent, so the second slot splits the AGENT and the direction
    survives the reclaim.

    Deliberately narrow (j#76616), so nothing else changes shape:

    - ``container_occupancy == 0`` — a FRESH container. A heal / mixed adopt joins a
      container whose only pane is the live sibling, which is therefore already the split
      target; a live pane is never focused / moved / swapped.
    - ``launch_count >= 2`` — a full pair. A single-provider request has no second slot to
      place, so the focus policy never fires.
    - explicit placement (``config_split`` or ``config_order`` set) — an UNSET lane class
      keeps ``--no-focus`` on every launch and stays byte-for-byte the pre-#13646 argv.
      (An unset sublane therefore keeps its historical — coincidentally correct — ``right``
      layout; only an explicitly configured lane class opts into the corrected placement.)

    Note this keys on the CONFIG being explicit, not on the effective split direction: an
    unset ``sublane`` still resolves ``split_direction == "right"`` by legacy default, and
    must NOT gain a ``--focus`` it never had.
    """
    if container_occupancy != 0 or launch_count < 2:
        return False
    return config_split is not None or config_order is not None


@dataclass(frozen=True)
class ContainerPlan:
    """How this run places its launches inside the target container (pure value).

    - :attr:`split_direction` — the ``--split`` value a splitting slot uses (``""`` = none).
    - :attr:`occupancy` — how many of the lane's slots already occupy the container, so the
      first launch into a fresh one occupies and the rest split beside it.
    - :attr:`focus_first` — whether the first launch must carry ``--focus`` to own the
      container's split target (the R1-F1 fix).
    """

    split_direction: str
    occupancy: int
    focus_first: bool


def resolve_container_plan(
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    target_workspace: str,
    lane_id: str,
    *,
    lane_class: str,
    target_tab: str,
    lane_slot_tabs: Sequence[str],
    config_split: Optional[str],
    config_order: Optional[Sequence[str]],
    launch_count: int,
) -> ContainerPlan:
    """The whole container placement plan for this run (pure; the single entry point).

    Composes the three decisions the session-start composition root needs — the effective
    split direction (:func:`resolve_split_direction`), the container's initial occupancy
    (:func:`initial_container_occupancy`), and whether the first launch must own the split
    target (:func:`resolve_focus_first_launch`) — so the orchestrator makes ONE call and
    holds no placement logic of its own.

    The default-lane occupancy is only counted when a split direction is configured, so an
    unset default lane never reads the inventory and stays byte-for-byte the pre-#13646 launch.
    """
    split_direction = resolve_split_direction(lane_class, config_split)
    occupancy = initial_container_occupancy(
        rows,
        workspace_id,
        target_workspace,
        lane_id,
        lane_class=lane_class,
        target_tab=target_tab,
        lane_slot_tabs=lane_slot_tabs,
        count_default_lane=bool(
            launch_count and target_workspace and config_split is not None
        ),
    )
    focus_first = resolve_focus_first_launch(
        config_split=config_split,
        config_order=config_order,
        launch_count=launch_count,
        container_occupancy=occupancy,
    )
    return ContainerPlan(
        split_direction=split_direction, occupancy=occupancy, focus_first=focus_first
    )


def slot_placement(
    kind: str,
    provider: str,
    *,
    split_direction: str,
    occupancy: int,
    config_order: Optional[Sequence[str]],
    focus_first: bool = False,
) -> "tuple[str, bool, bool]":
    """One slot's ``(--split value, focus, order_deferred)`` decision (pure).

    A slot splits only when it actually LAUNCHES into an already-occupied container; the
    container's first launch occupies it and emits no ``--split``. Adopted / planned /
    stale / unattested slots launch nothing, so they never carry a placement flag.

    ``focus`` is set on the FIRST launch into a fresh container when ``focus_first`` applies
    (see :func:`resolve_focus_first_launch`): that pins the container's split target to the
    first agent so the later slots split the AGENT, not the empty root pane that would be
    reclaimed out from under the split (R1-F1, j#76613 / j#76616). Only the first launch is
    ever focused — a splitting slot never is.

    ``order_deferred`` (Design Answer j#76564 Q2) flags the one case the configured order
    cannot be satisfied physically: the configured PRIMARY (``config_order[0]`` — the
    provider that should occupy the container) is launching as a split beside a sibling
    that is already live. herdr ``agent start`` has no pane-target flag and moving a live
    pane is forbidden (no live relayout), so the launch proceeds in the configured
    direction and the caller records ``order_deferred_until_full_relaunch`` instead of
    silently claiming the order was applied. A full relaunch of the pair realizes it.
    """
    if kind != "launch":
        return "", False, False
    if occupancy <= 0:
        return "", bool(focus_first), False
    deferred = bool(config_order is not None and provider == config_order[0])
    return split_direction, False, deferred


def _host_workspace_label(repo_root: Path) -> str:
    """Operator-readable label for a minted sublane host workspace (cosmetic only).

    Derived from the MAIN checkout's directory name — the project surface the
    operator recognises — not the lane worktree's (whose basename carries the
    lane). Purely observability: every join decision keys on the live mzb1
    inventory, never on this label (a herdr label is neither unique nor durable
    identity, and a lane-zero host auto-closes anyway).
    """
    try:
        resolved = Path(repo_root).expanduser().resolve()
    except OSError:
        return "sublanes"
    main_root = _main_worktree_root(resolved)
    base = (main_root or resolved).name
    return f"{base}_sublanes" if base else "sublanes"


def _parse_workspace_created(stdout: object) -> Optional[tuple[str, str]]:
    """``(workspace_id, root_pane_id)`` from a herdr ``workspace create`` payload.

    Real herdr shape (coordinator-measured, #13330 probe)::

        {"result": {"type": "workspace_created",
                    "workspace": {"workspace_id": "w3", ...},
                    "root_pane": {"pane_id": "w3:p1", ...}, ...}}

    Every fresh workspace is born with exactly this ``root_pane`` — the empty base
    shell #13330 reclaims. Returns ``None`` (so the caller fails closed and reclaims
    nothing) when the payload is not JSON, not a ``workspace_created`` envelope, or
    either id is missing / blank / malformed — never a guessed pane handle.
    """
    if not isinstance(stdout, str):
        return None
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return None
    if _norm(result.get("type")) != "workspace_created":
        return None
    workspace = result.get("workspace")
    root_pane = result.get("root_pane")
    if not isinstance(workspace, Mapping) or not isinstance(root_pane, Mapping):
        return None
    workspace_id = _norm(workspace.get("workspace_id"))
    root_pane_id = _norm(root_pane.get("pane_id"))
    if not workspace_id or not valid_target(workspace_id):
        return None
    if not root_pane_id or not valid_target(root_pane_id):
        return None
    return workspace_id, root_pane_id


def _parse_started_agent(stdout: object) -> Optional[tuple[str, str]]:
    """``(pane_id, tab_id)`` from a herdr ``agent start`` payload (fail-closed).

    Live herdr 0.7.1 output (coordinator-measured / probe #13411 j#74434): a single
    JSON object whose ``result.agent`` carries the transient ``pane_id`` locator and,
    for a tabbed launch, the landed ``tab_id`` alongside it::

        {"result": {"type": "agent_started",
                    "agent": {"pane_id": "w1:p2", "workspace_id": "w1",
                              "tab_id": "w1:t1", "name": "..."}}}

    Returns ``(pane_id, tab_id)`` — ``tab_id`` is ``""`` when the launch carried no
    tab (a default-lane launch, or a payload that omits it). Returns ``None`` (so the
    caller fails closed with "no usable live locator") when the payload is not JSON,
    ``result.type`` is not ``agent_started``, or the ``pane_id`` is missing / blank —
    never a blank handle. The caller separately verifies the landed workspace / tab.
    """
    if not isinstance(stdout, str):
        return None
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return None
    if _norm(result.get("type")) != "agent_started":
        return None
    agent = result.get("agent")
    if not isinstance(agent, Mapping):
        return None
    pane_id = _norm(agent.get("pane_id"))
    if not pane_id:
        return None
    return pane_id, _norm(agent.get("tab_id"))


def _parse_tab_created(stdout: object) -> Optional[tuple[str, str]]:
    """``(tab_id, root_pane_id)`` from a herdr ``tab create`` payload (fail-closed).

    Real herdr shape (coordinator spike #13380 j#73668)::

        {"result": {"type": "tab_created",
                    "tab": {"tab_id": "w3:t1", ...},
                    "root_pane": {"pane_id": "w3:p2", ...}}}

    A freshly created tab is born with exactly this empty ``root_pane`` — the tab
    analogue of the #13330 workspace base pane, reclaimed once the lane's agents
    land. Returns ``None`` (so the caller fails closed and reclaims nothing) when
    the payload is not JSON, not a ``tab_created`` envelope, or either id is
    missing / blank / malformed — never a guessed pane handle.
    """
    if not isinstance(stdout, str):
        return None
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return None
    if _norm(result.get("type")) != "tab_created":
        return None
    tab = result.get("tab")
    root_pane = result.get("root_pane")
    if not isinstance(tab, Mapping) or not isinstance(root_pane, Mapping):
        return None
    tab_id = _norm(tab.get("tab_id"))
    root_pane_id = _norm(root_pane.get("pane_id"))
    if not tab_id or not valid_target(tab_id):
        return None
    if not root_pane_id or not valid_target(root_pane_id):
        return None
    return tab_id, root_pane_id


__all__ = (
    "AGENT_KEY_TAB",
    "HerdrSessionStartError",
    "_host_workspace_label",
    "SHARED_COORDINATOR_WORKSPACE_LABEL",
    "_lane_live_slot_tabs",
    "_launch_target_for_lane",
    "_shared_coordinator_own_target",
    "_shared_coordinator_target",
    "ContainerPlan",
    "initial_container_occupancy",
    "resolve_container_plan",
    "resolve_focus_first_launch",
    "resolve_launch_order",
    "resolve_placement_policy",
    "resolve_split_direction",
    "slot_placement",
    "_parse_started_agent",
    "_parse_tab_created",
    "_parse_workspace_created",
    "_parse_workspace_list",
    "_tab_id_of_row",
    "_tab_target_for_lane",
    "_workspace_prefix",
    "herdr_workspace_segment",
)
