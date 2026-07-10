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

Both key on the live mzb1 inventory only (``tab_id`` / assigned-name identity),
never on a cosmetic label: the identity model (``mzb1_<project-ws>_<role>_<lane>``)
and route authority are unchanged; only the herdr placement subdivides.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.core.state.workspace_registry import _main_worktree_root
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
    own_tabs: set = set()
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
        tab = _tab_id_of_row(row)
        if tab:
            own_tabs.add(tab)
    if len(own_tabs) > 1:
        raise HerdrSessionStartError(
            f"live slots of lane {lane!r} occupy multiple herdr tabs "
            f"{sorted(own_tabs)!r} in workspace {target_workspace!r}; refuse to "
            "guess which one new launches belong to"
        )
    return next(iter(own_tabs)) if own_tabs else ""


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
    "_launch_target_for_lane",
    "_parse_tab_created",
    "_parse_workspace_created",
    "_tab_id_of_row",
    "_tab_target_for_lane",
    "_workspace_prefix",
)
