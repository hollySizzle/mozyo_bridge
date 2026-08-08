"""Project-column geometry for the shared coordinator workspace (Redmine #14996 R2).

``shared_space`` and the project-coordinator surface of ``role_grouped_space``
collect every project's coordinator pair in ONE herdr workspace so an operator
oversees them all at once. The pairs converge correctly
— identity, cwd and workspace are right — but the *geometry* did not: a second
project's pair landed as an L rather than its own column (live finding j#99833 —
the first project's Codex in the top left, the appended pair stacked in the top
right, and the first project's Claude spanning the whole bottom row).

Why a launch-argv fix cannot reach it
-------------------------------------
Measured on herdr 0.7.4, in a disposable instance:

- ``agent start`` has **no pane-target flag**. A launch splits whichever pane is
  active, so where the appended pair lands is not something the argv decides.
- After the first project's pair, the tab's ROOT divider is the one between that
  pair's two panes. Every subsequent ``pane split`` subdivides a *leaf*, so the
  root ``down`` divider — the full-width one the bottom pane spans — survives
  every launch. No sequence of leaf splits produces two full-height columns.
- ``pane move <p> --tab <same tab>`` is a ``same_tab`` no-op, ``pane swap`` only
  exchanges positions, and ``pane resize`` only moves an existing divider. A
  ``pane move`` from another tab *without* ``--target-pane`` also nests into the
  focused pane rather than inserting at the root.

So the appended column can only be created by briefly detaching one pane of the
column it splits — exactly the two-step bounce the live-relayout runbook
(#13648 recipe B) fixes, and exactly what the operator did by hand in #15017
j#99831 to restore the even 2x2. Redmine #14996 j#99845 authorises that narrow
relayout for this one case and requires it to be verified rather than assumed.

Boundary — narrow, launch-time, verified (j#99845)
--------------------------------------------------
- **Only the exact-labelled shared coordinator workspace** (``coordinators`` or
  ``project-coordinators``), only when this run
  freshly launched a FULL pair into a tab that already holds another project's
  coordinator panes. An adopt-only run, a single-provider heal, a dry run and a
  first-project launch all resolve :data:`COLUMN_NOT_APPLICABLE` and move nothing.
- **Only panes proved to be coordinator panes, and proved BEFORE the first move.**
  A decodable assigned name is not enough: its ``role`` field is a PROVIDER token
  (``codex`` / ``claude``), not a workflow role, so decoding alone cannot tell a
  project coordinator from an implementation slot that was mis-placed into this
  workspace (review j#99885 finding_2 bounced one) or from the TOP pair, which
  belongs in its own dedicated workspace (review j#99904 finding_1 moved six
  panes). :func:`resolve_project_groups` is the only producer a plan may consume,
  and it joins: provider shape, both halves of the mode's default-lane invariant
  (including ``workspace_id != top_workspace_id``), the durable ``lane_kind`` of
  every foreign NAMED lane, and — for every FOREIGN pane — a detected provider
  EQUAL to its decoded role, a self-attestation accepted by the canonical
  :func:`evaluate_attestation` (which pins the process generation), and a cwd that
  the identity model's own resolver maps back to the workspace its name claims.
  Unresolved evidence REFUSES; it is never filtered away, because a filtered-away
  stale sibling made a pair look healthy and four panes moved before the closing
  verdict caught it (review j#99904 finding_2). Identity and route authority stay
  untouched — a bounce moves a pane, it never closes, restarts or renames one.
- **Every placement is explicitly targeted.** Each step passes ``--target-pane``,
  so the result does not depend on which pane happened to be active before the
  launch (j#99845: "起動前focus非依存").
- **A moved Unit keeps its measured internal ratio.** Detaching its lower pane
  destroys that divider, so both the normal return and failure recovery pass the
  saved ratio to Herdr and the closing layout must reproduce the stored ratio and
  rendered cell extent (#15126).
- **Only after the canonical startup pass has settled, and only if it admitted
  this run's own launch.** A booting pane and a dead one report the same inventory
  row, so this pass reads the workspace only once the bounded startup probe
  (#13948) has turned that ambiguity into a verdict (:func:`_premature_read_refusal`
  — #14996 R3, live finding j#100135); and because the authority exempts own panes
  from the startup attestation, a verdict that is not ``healthy`` refuses the whole
  step before the read rather than being lost (:func:`_unadmitted_launch_refusal` —
  review j#100188 finding_1, which measured six live pane moves without it).
- **Fail closed, and say what is still detached.** A refused step stops the
  sequence, best-effort re-attaches whatever this run had detached, and reports
  :data:`COLUMN_FAILED`. A pane that vanished, a temp tab that cannot be
  accounted for, or a final layout that is not columnar is never a success.

This is not a general live-relayout rail: nothing here reads a config, nothing
here touches a pair the run did not append to, and the only foreign pane it ever
moves is the one whose column the new pair splits — returned to the same place
beneath its own partner.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    HerdrSessionStartError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_authority import (  # noqa: E501
    CoordinatorPane,
    OwnSlot,
    ProjectColumnAuthority,
    ProjectGroupDecision,
    coordinator_panes_in,
    project_column_authority,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pair_split_ratio import (  # noqa: E501
    LayoutSnapshot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
    _invoke,
    _list_rows,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_balance import (  # noqa: E501
    MAX_EQUAL_PROJECT_COLUMNS,
    ColumnRatioTarget,
    balance_project_columns,
    balanced_column_verdict,
    columnar_verdict,
    plan_equal_column_ratios,
    read_pane_layout,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_internal_ratio import (  # noqa: E501
    ColumnInternalRatio,
    capture_internal_ratios,
    internal_ratios_match,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (  # noqa: E501
    SLOT_LAUNCHED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    DEFAULT_LANE,
    _norm,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_health import (  # noqa: E501
    HEALTH_HEALTHY,
    HEALTH_NOT_PROBED,
    HEALTH_OUTCOMES,
)

#: No project-column reflow was owed. The resting value: every non-shared coordinator
#: placement, a dry run, an adopt-only run, a single-provider heal, and the first project
#: to reach the shared workspace all land here without reading a layout.
COLUMN_NOT_APPLICABLE = "not_applicable"
#: The tab was ALREADY columnar after the launch, so nothing was moved. A success
#: that costs zero pane moves — checked before any bounce, never assumed.
COLUMN_MATCHED = "matched"
#: The bounce ran and the closing ``pane layout`` read confirms every project pair
#: owns one full-height column. Only a measured layout produces this token.
COLUMN_APPLIED = "applied"
#: The append bounce produced and measured full-height columns, but equal-width
#: balancing is not representable for more than ten columns.  This is deliberately
#: not a success token: the configured placement completion must replace it with a
#: measured matched/applied/deferred verdict before session-start may succeed.
COLUMN_PREPARED = "prepared_for_configured_placement"
#: Configured ordering is intentionally postponed because at least one Unit has
#: only one admitted provider pane.  Moving the complete neighbours around that
#: mixed set would make the missing half's eventual position ambiguous.  This is
#: a successful, zero-write deferral; a later full-pair launch may converge it.
COLUMN_DEFERRED = "deferred_until_full_pair_set"
#: The reflow was owed and could not be established. Never reported as success;
#: the detail names the refusing step and any pane left outside the tab.
COLUMN_FAILED = "failed"

#: The closed outcome vocabulary, in the order a reader should read it.
COLUMN_OUTCOMES: tuple[str, ...] = (
    COLUMN_NOT_APPLICABLE,
    COLUMN_MATCHED,
    COLUMN_APPLIED,
    COLUMN_PREPARED,
    COLUMN_DEFERRED,
    COLUMN_FAILED,
)

#: The outcomes a run may call successful on this axis. Enumerated rather than
#: derived as "everything except :data:`COLUMN_FAILED`" — the same discipline
#: :data:`...herdr_pair_split_ratio.RATIO_SUCCESS_OUTCOMES` adopted after a typo
#: in the negative comparison reported unknown tokens as success (j#91418 R5-F1).
COLUMN_SUCCESS_OUTCOMES: frozenset = frozenset(
    {COLUMN_NOT_APPLICABLE, COLUMN_MATCHED, COLUMN_APPLIED, COLUMN_DEFERRED}
)

#: Herdr's stable pane ``cwd`` is independent of the foreground process cwd, but
#: a fresh inventory read can still fail to resolve it.  The authority keeps
#: requiring that stable value; this budget lets the caller re-read only an
#: unresolved stable cwd on THIS run's fresh panes.  A missing cwd, a cwd resolving
#: to the wrong registered workspace, and all foreign, malformed or conflicting
#: evidence remain immediate fail-closed verdicts.
OWN_OBSERVATION_RETRY_BUDGET_SECONDS = 1.0
OWN_OBSERVATION_RETRY_INTERVAL_SECONDS = 0.05
OWN_OBSERVATION_RETRY_MAX_READS = 20


@dataclass(frozen=True)
class ColumnAttach:
    """One targeted re-placement: put ``pane`` on ``direction`` of ``target``."""

    pane: str
    direction: str
    target: str
    ratio: "Optional[float]" = None


@dataclass(frozen=True)
class ColumnReflowPlan:
    """The bounce that turns the appended pair into its own full-height column.

    ``detach`` panes leave for a temp tab (herdr auto-closes it once empty), in
    order; ``attach`` puts them back, each against an explicit ``--target-pane``.
    The plan is pure: it names panes and directions, it runs nothing.
    """

    detach: "tuple[str, ...]"
    attach: "tuple[ColumnAttach, ...]"
    anchor_pane: str
    internal_ratios: "tuple[ColumnInternalRatio, ...]"


def _anchor_group(
    layout: LayoutSnapshot,
    foreign: "Mapping[tuple[str, str], tuple[CoordinatorPane, ...]]",
) -> "tuple[str, tuple[str, ...], str]":
    """``(top pane, panes to bounce, refusal)`` for the column the new one splits.

    The rightmost live column is the anchor, so the appended project lands on the
    right and no existing column is reordered (j#99833 acceptance 3: left-right
    order is launch-order best-effort, and unrelated columns are left alone).
    Relative x order survives a detach — removing a pane only collapses splits —
    so the anchor chosen from this layout is still the rightmost one once this
    run's own panes have left.
    """
    ranked: list = []
    for key, members in foreign.items():
        rects = []
        for pane in members:
            rect = layout.panes.get(pane.locator)
            if rect is None:
                return "", (), f"pane {pane.locator!r} of pair {key!r} is not in the tab"
            rects.append((rect, pane.locator))
        if len(rects) > 2:
            return "", (), (
                f"project pair {key!r} holds {len(rects)} live panes; refusing to "
                "reshape a pair whose shape this plan does not recognise"
            )
        stacked = sorted(rects, key=lambda entry: (entry[0].y, entry[1]))
        ranked.append((max(rect.x for rect, _ in rects), stacked, key))
    if not ranked:
        return "", (), "no other project coordinator pair shares the workspace"
    ranked.sort(key=lambda entry: (entry[0], entry[2]))
    _x, stacked, _key = ranked[-1]
    return stacked[0][1], tuple(locator for _rect, locator in stacked[1:]), ""


def plan_project_columns(
    layout: LayoutSnapshot,
    groups: "Mapping[tuple[str, str], tuple[CoordinatorPane, ...]]",
    own_key: "tuple[str, str]",
    own_launched: Sequence[str],
) -> "tuple[Optional[ColumnReflowPlan], str]":
    """``(plan, refusal)`` for appending ``own_launched`` as its own column (pure).

    The sequence, all of it explicitly targeted:

    1. this run's two panes leave for temp tabs, which collapses the tab back to
       the geometry the other projects had before the launch;
    2. the anchor column's lower pane leaves too, so the anchor's top pane is the
       leaf a ``right`` split turns into a root-level column boundary;
    3. the new primary attaches to the RIGHT of the anchor top, the new secondary
       ``down`` from it, and the anchor's lower pane returns ``down`` beneath its
       own partner — restoring it to exactly where it was.
    """
    own = tuple(locator for locator in own_launched if locator)
    if len(own) != 2:
        return None, (
            f"a project column is appended by a full fresh pair; this run reports "
            f"{len(own)} launched locator(s)"
        )
    if own_key not in groups:
        return None, "this run's own pair is not in the shared workspace inventory"
    own_members = {pane.locator for pane in groups[own_key]}
    missing = [locator for locator in own if locator not in own_members]
    if missing:
        return None, f"launched pane(s) {missing!r} carry no decodable pair identity"
    foreign = {key: members for key, members in groups.items() if key != own_key}
    anchor_top, anchor_rest, refusal = _anchor_group(layout, foreign)
    if refusal:
        return None, refusal
    anchor_keys = [
        key
        for key, members in foreign.items()
        if anchor_top in {member.locator for member in members}
    ]
    if len(anchor_keys) != 1:
        return None, "the rightmost anchor pane does not identify one Unit"
    for locator in own:
        if layout.panes.get(locator) is None:
            return None, f"launched pane {locator!r} is not in the tab layout"
    internal_ratios, refusal = capture_internal_ratios(
        layout,
        {
            own_key: groups[own_key],
            anchor_keys[0]: foreign[anchor_keys[0]],
        },
    )
    if refusal:
        return None, refusal
    internal_by_lower = {
        item.lower: item.ratio for item in internal_ratios
    }
    own_internal = next(
        (item for item in internal_ratios if item.key == own_key), None
    )
    if (
        own_internal is None
        or own_internal.top != own[0]
        or own_internal.lower != own[1]
    ):
        return None, (
            "the fresh pair's measured top/lower order does not match its launch order"
        )
    return (
        ColumnReflowPlan(
            detach=(own[1], own[0]) + anchor_rest,
            attach=(
                ColumnAttach(pane=own[0], direction="right", target=anchor_top),
                ColumnAttach(
                    pane=own[1], direction="down", target=own[0],
                    ratio=own_internal.ratio,
                ),
            )
            + tuple(
                ColumnAttach(
                    pane=locator,
                    direction="down",
                    target=anchor_top,
                    ratio=internal_by_lower.get(locator),
                )
                for locator in anchor_rest
            ),
            anchor_pane=anchor_top,
            internal_ratios=internal_ratios,
        ),
        "",
    )

def _move_result(stdout: object) -> "Optional[tuple[str, str]]":
    """``(pane_id, tab_id)`` a ``pane move`` landed on, or ``None`` if it did not.

    ``changed`` is checked rather than the exit code: ``pane move --tab <same
    tab>`` exits 0 while reporting ``changed:false`` / ``reason:same_tab`` and
    moving nothing, so a zero exit is not evidence that the pane went anywhere.
    """
    if not isinstance(stdout, str):
        return None
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return None
    move = result.get("move_result")
    if not isinstance(move, Mapping) or move.get("changed") is not True:
        return None
    pane = move.get("pane")
    if not isinstance(pane, Mapping):
        return None
    pane_id = _norm(pane.get("pane_id"))
    tab_id = _norm(pane.get("tab_id"))
    return (pane_id, tab_id) if pane_id and tab_id else None


def detach_pane(
    pane_id: str, *, binary: str, runner, timeout: float, env
) -> "tuple[str, str]":
    """Bounce ``pane_id`` out to its own temp tab — ``(temp tab id, refusal)``.

    herdr auto-closes the temp tab when its last pane leaves, so the pane's way
    back is the only cleanup this ever needs.
    """
    try:
        completed = _invoke(
            binary,
            ["pane", "move", pane_id, "--new-tab", "--no-focus"],
            runner,
            timeout,
            env=env,
        )
    except HerdrSessionStartError as exc:
        return "", f"herdr refused to detach pane {pane_id!r} ({exc})"
    landed = _move_result(completed.stdout)
    if landed is None:
        return "", f"herdr reported no completed move for pane {pane_id!r}"
    return landed[1], ""


def attach_pane(
    attach: ColumnAttach, tab_id: str, *, binary: str, runner, timeout: float, env
) -> str:
    """Place one detached pane against an explicit target — ``""`` iff it landed."""
    placement = [
        "pane", "move", attach.pane,
        "--tab", tab_id,
        "--split", attach.direction,
    ]
    if attach.ratio is not None:
        placement.extend(("--ratio", f"{attach.ratio:.9g}"))
    placement.extend(("--target-pane", attach.target, "--no-focus"))
    try:
        completed = _invoke(
            binary,
            placement,
            runner,
            timeout,
            env=env,
        )
    except HerdrSessionStartError as exc:
        return (
            f"herdr refused to place pane {attach.pane!r} {attach.direction} of "
            f"{attach.target!r} ({exc})"
        )
    landed = _move_result(completed.stdout)
    if landed is None:
        return f"herdr reported no completed move for pane {attach.pane!r}"
    if landed[1] != tab_id:
        return (
            f"pane {attach.pane!r} landed in tab {landed[1]!r}, not the shared "
            f"project-coordinator tab {tab_id!r}"
        )
    return ""


def _identity_map(
    rows: Sequence[Mapping[str, object]], target_workspace: str
) -> "dict[str, str]":
    """``{locator: assigned_name}`` for the panes in ``target_workspace``.

    A read of what the workspace holds, taken before and after the bounce. A
    refusal from the reader means the inventory changed into a shape the authority
    would not accept, which is itself a difference worth failing on, so it is
    folded into the map as a sentinel rather than swallowed.
    """
    panes, refusal = coordinator_panes_in(rows, target_workspace)
    if refusal:
        return {"": refusal}
    return {pane.locator: pane.assigned_name for pane in panes}


def _restore_detached(
    detached: Sequence[str],
    tab_id: str,
    planned: Sequence[ColumnAttach],
    *,
    binary: str,
    runner,
    timeout: float,
    env,
) -> "tuple[str, ...]":
    """Best-effort return of still-detached panes; the ones that stayed out.

    A failed reflow must not leave an agent parked in an invisible temp tab with
    no record of it. Each pane is returned using the same target, direction and
    measured ratio as the normal plan. Whatever could not be placed is named in
    the failure detail rather than silently abandoned.
    """
    stranded: list = []
    pending = set(detached)
    for attach in planned:
        if attach.pane not in pending:
            continue
        refusal = attach_pane(
            attach,
            tab_id,
            binary=binary,
            runner=runner,
            timeout=timeout,
            env=env,
        )
        if refusal:
            stranded.append(attach.pane)
        pending.remove(attach.pane)
    stranded.extend(sorted(pending))
    return tuple(stranded)


def _is_settled_health(value: object) -> bool:
    """True only for a health verdict the canonical probe has actually written."""
    return (
        isinstance(value, str)
        and value in HEALTH_OUTCOMES
        and value != HEALTH_NOT_PROBED
    )


def _unadmitted_launch_refusal(launched_slots: Sequence[object]) -> str:
    """Refuse to move live panes for a launch that did not pass startup admission.

    Distinct from the ordering question above: the pass has RUN, and it said no.

    Why this run's own verdict has to be a precondition here, when foreign panes
    are judged from the workspace instead: the authority deliberately exempts this
    run's own panes from the startup attestation, because a just-launched slot
    could not yet answer it (review j#99931 finding_1). Moving the geometry pass
    after pass 3 made it answerable — so keeping the exemption while declining to
    read the verdict would exempt the fact twice. An own-side
    ``attestation_mismatch`` / ``locator_drift`` / ``provider_exited`` is not
    reconstructible from the next inventory read, and the measured result was six
    live pane moves on a launch the run itself went on to report as not ok
    (review j#100188 finding_1, reproduced here).

    The admitted set is exactly ``{HEALTH_HEALTHY}`` — a closed policy, not a
    carve-out list. :mod:`...domain.startup_health` states it: "Only
    ``HEALTH_HEALTHY`` is a positive success verdict", and an unwrapped launch is
    ``attestation_unavailable`` precisely so it cannot read as green. Tokens that
    look merely informational (``startup_evidence_unavailable``,
    ``attestation_unavailable``) are included for that reason: each one already
    makes ``SessionStartResult.ok`` false, so admitting it here would only mean
    causing a live geometry effect for a run that is reporting failure anyway.
    Nothing is killed either way; the pair stays live and its placement is what
    the run declines to change.
    """
    unadmitted = sorted(
        (_norm(getattr(slot, "locator", "")), getattr(slot, "health", ""))
        for slot in launched_slots
        if _norm(getattr(slot, "locator", ""))
        and _is_settled_health(getattr(slot, "health", None))
        and getattr(slot, "health", "") != HEALTH_HEALTHY
    )
    if not unadmitted:
        return ""
    named = ", ".join(f"{locator!r} ({health})" for locator, health in unadmitted)
    return (
        f"this run's own launch did not pass startup admission — pane(s) {named}; "
        "refusing to place a column for a launch the startup pass did not admit; "
        "no live pane was moved"
    )


def _premature_read_refusal(launched_slots: Sequence[object]) -> str:
    """Refuse to read the inventory before the canonical liveness pass settled.

    A pane herdr has just started reports the SAME row shape as shell residue —
    the ``agent`` field present and blank — until the provider boots into it. That
    ambiguity is not this module's to resolve: the canonical startup pass already
    owns it, and owns it as a *deadline* rather than a verdict, which is why
    ``HEALTH_SHELL_RESIDUE`` sits in its retryable set (#13948) and is only
    reported once a bounded number of re-observations still see it.

    The live rollout ran this geometry pass BEFORE that one, so a fresh, healthy
    server-management pair was called shell residue and its first column failed
    (#14996 R3, live finding j#100135). The fix is the call order, and this is what
    keeps it from being merely positional: a launched slot still carrying
    :data:`HEALTH_NOT_PROBED` means the pass that decides liveness has not run over
    it, so the read is premature and is refused — at zero pane moves — instead of
    being judged. A slot missing the axis entirely is treated the same way; absent
    evidence of the pass is not evidence that it ran.

    Slots with no locator are outside this question rather than exempt from it:
    the probe only ever targets a locator, so an unaddressable slot carries no
    ordering evidence either way, and it is refused one step later by the authority
    on the axis that names its actual defect (j#99955) rather than being reported
    here under a cause that is not its own.

    "Settled" is membership in the health vocabulary, not "some non-empty string":
    the canonical `_norm` is ``str(value).strip()``, so ``None`` / ``0`` / ``[]``
    would each normalise to something that is not ``not_probed`` and read as a
    settled verdict — the same promotion of a malformed value that j#99971 found on
    the inventory side. Every verdict a real probe writes is a member, so nothing
    a launch legitimately produces is refused by asking.
    """
    premature = sorted(
        _norm(getattr(slot, "locator", ""))
        for slot in launched_slots
        if _norm(getattr(slot, "locator", ""))
        and not _is_settled_health(getattr(slot, "health", None))
    )
    if not premature:
        return ""
    return (
        f"the startup-liveness pass has not settled pane(s) {premature!r}, so their "
        "inventory rows cannot yet distinguish a booting provider from shell "
        "residue; no live pane was moved"
    )


def _resolve_project_groups_after_startup(
    *,
    binary: str,
    runner,
    timeout: float,
    authority: ProjectColumnAuthority,
    target_workspace: str,
    own_slots: Sequence[OwnSlot],
    expected_own_key: "tuple[str, str]",
    top_workspace_id: str,
    retry_budget_seconds: float,
    retry_interval_seconds: float,
    sleeper=None,
    monotonic=None,
) -> "tuple[Sequence[Mapping[str, object]], ProjectGroupDecision]":
    """Resolve once, then briefly re-read only a typed own-cwd refusal.

    Startup health proves the launched process and generation.  It does not make
    an unresolved stable ``cwd`` into workspace evidence.  The dynamic
    ``foreground_cwd`` is evaluated separately by the authority and never enters
    this retry decision.

    Retrying the authority as a whole is important.  Each read re-validates every
    row, and anything except an unresolved cwd on an own fresh pane clears the
    retryable flag and stops the loop before a pane move.  The first read retains
    the caller's normal timeout; subsequent reads are capped to the remaining
    one-second budget and a maximum count, so a changing backend cannot turn
    stabilization into an unbounded wait.
    """
    clock = monotonic if monotonic is not None else time.monotonic
    pause = sleeper if sleeper is not None else time.sleep

    def read_and_resolve(read_timeout: float):
        rows = _list_rows(binary, runner, read_timeout)
        decision = authority.resolve(
            rows,
            target_workspace=target_workspace,
            own_slots=own_slots,
            expected_own_key=expected_own_key,
            top_workspace_id=top_workspace_id,
        )
        return rows, decision

    rows, decision = read_and_resolve(timeout)
    if decision.ok or not decision.retryable_own_cwd_unresolved:
        return rows, decision

    budget = max(float(retry_budget_seconds), 0.0)
    interval = max(float(retry_interval_seconds), 0.0)
    deadline = clock() + budget
    extra_reads = 0
    while (
        decision.retryable_own_cwd_unresolved
        and extra_reads < OWN_OBSERVATION_RETRY_MAX_READS
    ):
        remaining = deadline - clock()
        if remaining <= 0.0:
            break
        pause(min(interval, remaining))
        remaining = deadline - clock()
        if remaining <= 0.0:
            break
        rows, decision = read_and_resolve(min(float(timeout), remaining))
        extra_reads += 1
        if decision.ok or not decision.retryable_own_cwd_unresolved:
            break
    return rows, decision


def reflow_project_columns(
    result,
    *,
    project_coordinator: bool,
    launched: int,
    initial_occupancy: int,
    dry_run: bool,
    binary: str,
    runner,
    timeout: float,
    env,
    home: Path,
    top_workspace_id: str = "",
    authority: "Optional[ProjectColumnAuthority]" = None,
    own_observation_retry_budget_seconds: float = (
        OWN_OBSERVATION_RETRY_BUDGET_SECONDS
    ),
    own_observation_retry_interval_seconds: float = (
        OWN_OBSERVATION_RETRY_INTERVAL_SECONDS
    ),
    sleeper=None,
    monotonic=None,
) -> "tuple[str, str]":
    """Give this run's appended pair its own column — ``(outcome, detail)``.

    Owed only by a FRESH full pair appended to a shared project-coordinator
    workspace that already holds another project (``initial_occupancy == 0`` is
    this pair's own occupancy — an adopt or a heal has a live sibling and is left
    alone). Everything before the first pane move is decided from identity and a
    measured layout; everything after it is measured again before the run claims
    the geometry it wanted.

    ``project_coordinator`` is the caller's placement classification and the ONLY
    thing it contributes; the pair's identity and its resolved herdr workspace are
    read off ``result``, which already carries both, so the two can never disagree.
    """
    if not project_coordinator or dry_run:
        return COLUMN_NOT_APPLICABLE, (
            "this run is not a fresh shared project-coordinator launch"
        )
    if launched < 2 or initial_occupancy != 0:
        return COLUMN_NOT_APPLICABLE, (
            "no project column is appended by an adopt-only run or a heal beside a "
            "live sibling; no live pane is moved"
        )
    target_workspace = _norm(result.herdr_workspace_id)
    if not target_workspace:
        return COLUMN_FAILED, "the run reports no resolved shared herdr workspace"
    # EVERY launched slot is handed over, including one whose locator is blank.
    # Filtering those out here would be the same silent exclusion the authority
    # refuses on inside the workspace: a launch this run reports but cannot address
    # is a contradiction the run must fail on, not a row to drop on the way in.
    launched_slots = tuple(
        slot for slot in result.slots if getattr(slot, "outcome", "") == SLOT_LAUNCHED
    )
    # Two questions about this run's own launch, in the only order they compose in:
    # whether the canonical pass has answered at all, then what it answered. Both
    # land before the inventory read, so either costs zero pane moves.
    refusal = _premature_read_refusal(launched_slots) or _unadmitted_launch_refusal(
        launched_slots
    )
    if refusal:
        return COLUMN_FAILED, refusal
    own_slots = tuple(
        OwnSlot(
            locator=getattr(slot, "locator", ""),
            assigned_name=getattr(slot, "assigned_name", ""),
            provider=getattr(slot, "provider", ""),
        )
        for slot in launched_slots
    )
    own_launched = tuple(slot.locator for slot in own_slots)
    rows, decision = _resolve_project_groups_after_startup(
        binary=binary,
        runner=runner,
        timeout=timeout,
        authority=authority or project_column_authority(home),
        target_workspace=target_workspace,
        own_slots=own_slots,
        # The run's own claim is an INPUT to the join, not a second derivation
        # beside it: a result naming one project whose slots were another's live
        # panes once reported a column for a project the workspace does not hold
        # (review j#99938 finding_2).
        expected_own_key=(result.workspace_id, _norm(result.lane_id) or DEFAULT_LANE),
        top_workspace_id=top_workspace_id,
        retry_budget_seconds=own_observation_retry_budget_seconds,
        retry_interval_seconds=own_observation_retry_interval_seconds,
        sleeper=sleeper,
        monotonic=monotonic,
    )
    if not decision.ok:
        return COLUMN_FAILED, f"{decision.refusal}; no live pane was moved"
    groups = decision.groups
    own_key = decision.own_key
    if not [key for key in groups if key != own_key]:
        return COLUMN_NOT_APPLICABLE, (
            "this project is the only coordinator pair in the shared workspace, so "
            "its pair already owns the whole tab"
        )
    if not own_launched:
        return COLUMN_FAILED, "this run launched a pair but reports no live locator"
    layout = read_pane_layout(
        own_launched[0], binary=binary, runner=runner, timeout=timeout, env=env
    )
    if layout is None:
        return COLUMN_FAILED, "pane layout could not be read or parsed"
    tab_id = _norm(layout.tab_id)
    if not tab_id:
        return COLUMN_FAILED, "the shared project-coordinator tab id is unreadable"
    columnar, reason = columnar_verdict(layout, groups)
    before = _identity_map(rows, target_workspace)
    if columnar:
        return _verify_reflow(
            before,
            groups,
            target_workspace,
            tab_id,
            anchor=own_launched[0],
            geometry_changed=False,
            internal_ratios=(),
            binary=binary,
            runner=runner,
            timeout=timeout,
            env=env,
        )
    plan, refusal = plan_project_columns(layout, groups, own_key, own_launched)
    if plan is None:
        return COLUMN_FAILED, f"{refusal} (observed geometry: {reason})"
    detached: list = []
    for pane_id in plan.detach:
        _temp_tab, step_refusal = detach_pane(
            pane_id, binary=binary, runner=runner, timeout=timeout, env=env
        )
        if step_refusal:
            stranded = _restore_detached(
                detached, tab_id, plan.attach,
                binary=binary, runner=runner, timeout=timeout, env=env,
            )
            return COLUMN_FAILED, _stranded_detail(step_refusal, stranded)
        detached.append(pane_id)
    for attach in plan.attach:
        step_refusal = attach_pane(
            attach, tab_id, binary=binary, runner=runner, timeout=timeout, env=env
        )
        if step_refusal:
            # `attach.pane` is still detached (it is removed only on success), so it
            # is restored with its planned target/direction/ratio rather than being
            # dropped from the accounting the failure detail is built from.
            stranded = _restore_detached(
                tuple(detached), tab_id, plan.attach,
                binary=binary, runner=runner, timeout=timeout, env=env,
            )
            return COLUMN_FAILED, _stranded_detail(step_refusal, stranded)
        detached.remove(attach.pane)
    return _verify_reflow(
        before,
        groups,
        target_workspace,
        tab_id,
        anchor=plan.anchor_pane,
        geometry_changed=True,
        internal_ratios=plan.internal_ratios,
        binary=binary,
        runner=runner,
        timeout=timeout,
        env=env,
    )


def _stranded_detail(refusal: str, stranded: Sequence[str]) -> str:
    """A failure detail that never hides a pane left outside the shared tab."""
    if not stranded:
        return f"{refusal}; every detached pane was returned to the shared tab"
    return (
        f"{refusal}; pane(s) {sorted(stranded)!r} are NOT in the shared "
        "project-coordinator tab and need the live-relayout runbook to be replaced"
    )


def _verify_reflow(
    before: "Mapping[str, str]",
    groups: "Mapping[tuple[str, str], tuple[CoordinatorPane, ...]]",
    target_workspace: str,
    tab_id: str,
    *,
    anchor: str,
    geometry_changed: bool,
    internal_ratios: Sequence[ColumnInternalRatio],
    binary: str,
    runner,
    timeout: float,
    env,
) -> "tuple[str, str]":
    """Measure and equalise the produced columns — identity first, then geometry.

    Identity comes first deliberately: a layout that looks right tells us nothing
    if a pane came back under a different assigned name, and that is the property
    the whole placement model rests on (``spec-herdr-native-identity``).
    """
    after = _identity_map(_list_rows(binary, runner, timeout), target_workspace)
    if after != before:
        lost = sorted(set(before) - set(after))
        changed = sorted(
            locator
            for locator in set(before) & set(after)
            if before[locator] != after[locator]
        )
        return COLUMN_FAILED, (
            "the shared workspace inventory changed across the reflow "
            f"(missing: {lost!r}, renamed: {changed!r}); the geometry is not claimed"
        )
    layout = read_pane_layout(
        anchor, binary=binary, runner=runner, timeout=timeout, env=env
    )
    if layout is None:
        return COLUMN_FAILED, "the closing pane layout could not be read or parsed"
    if _norm(layout.tab_id) != tab_id:
        return COLUMN_FAILED, (
            f"the closing layout reports tab {layout.tab_id!r}, not {tab_id!r}"
        )
    columnar, reason = columnar_verdict(layout, groups)
    if not columnar:
        return COLUMN_FAILED, f"the reflowed tab is still not columnar: {reason}"
    if len(groups) > MAX_EQUAL_PROJECT_COLUMNS:
        outcome = COLUMN_PREPARED
        action = "now own" if geometry_changed else "already own"
        return outcome, (
            f"{len(groups)} project pair(s) {action} full-height columns; "
            "configured placement must establish their final relative widths"
        )
    ratios_ok, ratio_detail = internal_ratios_match(layout, internal_ratios)
    if not ratios_ok:
        return COLUMN_FAILED, (
            "a Unit's internal ratio changed across project-column reflow: "
            f"{ratio_detail}"
        )
    resized, refusal = balance_project_columns(
        layout,
        groups,
        binary=binary,
        runner=runner,
        timeout=timeout,
        env=env,
    )
    if refusal:
        return COLUMN_FAILED, refusal
    closing = read_pane_layout(
        anchor, binary=binary, runner=runner, timeout=timeout, env=env
    )
    if closing is None:
        return COLUMN_FAILED, "the balanced pane layout could not be read or parsed"
    balanced, reason = balanced_column_verdict(closing, groups)
    if not balanced:
        return COLUMN_FAILED, f"the project columns are still not balanced: {reason}"
    ratios_ok, ratio_detail = internal_ratios_match(closing, internal_ratios)
    if not ratios_ok:
        return COLUMN_FAILED, (
            "a Unit's internal ratio changed during project-column balancing: "
            f"{ratio_detail}"
        )
    final_inventory = _identity_map(
        _list_rows(binary, runner, timeout), target_workspace
    )
    if final_inventory != before:
        return COLUMN_FAILED, (
            "the shared workspace inventory changed during project-column balancing"
        )
    outcome = COLUMN_APPLIED if geometry_changed or resized else COLUMN_MATCHED
    action = "now own" if outcome == COLUMN_APPLIED else "already own"
    return outcome, (
        f"{len(groups)} project pair(s) {action} equal-width full-height columns "
        f"in tab {tab_id}"
    )


__all__ = (
    "COLUMN_APPLIED",
    "COLUMN_DEFERRED",
    "COLUMN_FAILED",
    "COLUMN_MATCHED",
    "COLUMN_NOT_APPLICABLE",
    "COLUMN_PREPARED",
    "COLUMN_OUTCOMES",
    "COLUMN_SUCCESS_OUTCOMES",
    "ColumnAttach",
    "ColumnRatioTarget",
    "ColumnReflowPlan",
    "CoordinatorPane",
    "attach_pane",
    "balanced_column_verdict",
    "columnar_verdict",
    "coordinator_panes_in",
    "detach_pane",
    "plan_project_columns",
    "plan_equal_column_ratios",
    "read_pane_layout",
    "reflow_project_columns",
)
