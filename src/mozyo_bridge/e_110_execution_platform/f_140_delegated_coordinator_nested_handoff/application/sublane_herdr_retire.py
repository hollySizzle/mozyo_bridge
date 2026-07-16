"""herdr sublane retire guarded close (Redmine #13331 option A → #13377 shared model).

The tmux ``sublane retire`` is preflight / runbook only — the destructive half (pane kill
/ ``git worktree remove`` / branch delete) is gated behind a Design Consultation per
``vibes/docs/logics/worktree-lifecycle-boundary.md``. j#73314 (that design consultation's
answer) authorizes ONE narrow herdr actuation for retire: closing the lane's own
**managed** agents. Under the #13377 shared project workspace model (design j#73613)
those are the lane unit's slots — ``mzb1_<project-ws>_codex_<lane>`` /
``mzb1_<project-ws>_claude_<lane>`` — and retire **never closes a workspace itself**:
the coordinator pair's project workspace is untouched, and the dedicated sublane host
workspace the lane slots live in (#13380) keeps hosting every other lane. When the LAST
lane's slots close, herdr auto-closing the now-empty host (live-measured, #13380) is an
incidental herdr behaviour — harmless, not a retire postcondition; the next lane re-mints
the host on demand. A *legacy* pre-#13377 lane (its own ``wt_<hash>`` workspace,
default-lane slots) still closes through the compatibility plan, where the old
last-pane-close workspace vanish remains an incidental herdr behaviour, not a
postcondition. Either way the authorization covers **only** the lane's managed gateway /
worker slots: no ``git worktree remove`` (still runbook), never a foreign / unmanaged
agent, never another lane's slots, never the default-lane coordinator pair.

This module is that guarded close, kept opt-in (``sublane retire --execute``) and gated
on the existing fail-closed retire preflight (``may_retire`` — issue closed / owner
approved / callbacks drained / verified / durable record / target known). It is
structurally safe: :func:`plan_herdr_retire_close` only ever lists the lane unit's (and
its legacy twin's) managed slots as close targets; anything else sharing those units is
recorded as foreign for the audit trail but never acted on.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (
    _close_base_pane,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
    _resolve_binary_or_die,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    DEFAULT_LANE,
    _agent_locator,
    _norm,
    _norm_lane,
    decode_assigned_name,
    is_lane_workspace_token,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    COMMAND_TIMEOUT_SECONDS,
    Runner,
)

#: The two managed provider roles a lane unit's slots carry, under the DEFAULT binding
#: (gateway=codex, worker=claude). This is the built-in default; the actuation caller
#: passes the binding-resolved ``managed_roles`` (Redmine #13569 Increment 2B) so a lane
#: whose worker/gateway provider was rebound retires ITS slots, and a provider the binding
#: does not assign is never a retire target.
_MANAGED_ROLES = ("codex", "claude")

# ---------------------------------------------------------------------------
# Actuation verdict (Redmine #13754): the fail-closed outcome of `retire --execute`.
# ---------------------------------------------------------------------------

#: The lane's expected managed target was resolved at action time and every planned
#: slot actually closed. The only state a real close reports.
ACTUATION_CLOSED = "closed"
#: A *verified* idempotent no-op: the durable lifecycle says the lane is already
#: retired AND zero expected managed slots are live in the resolved unit. Nothing was
#: closed because there was provably nothing left to close.
ACTUATION_VERIFIED_NOOP = "verified_noop"
#: Fail-closed: the retire could not prove it retired the lane. Never exit 0.
ACTUATION_BLOCKED = "blocked"

#: Blocked reasons. Each names a distinct thing the actuation could not establish —
#: an operator reading the JSON must be able to tell a mis-aimed root from a dead
#: inventory from an unproven zero-close.
REASON_NO_WORKTREE_ANCHOR = "no_worktree_anchor"
REASON_WORKSPACE_UNRESOLVED = "workspace_unresolved"
REASON_INVENTORY_UNREADABLE = "inventory_unreadable"
REASON_PROVIDER_UNRESOLVED = "provider_unresolved"
REASON_PROVIDER_NOT_LAUNCHABLE = "provider_not_launchable"
REASON_CLOSE_FAILED = "close_failed"
REASON_ZERO_CLOSE_UNPROVEN = "zero_close_unproven"
#: Action-time identity attestation (Redmine #13754 F1, j#78475). The requested
#: ``(issue, lane)`` do not name the same durable lane unit: under the shared project
#: workspace model the worktree resolves the project (not the lane), so the requested
#: lane_label could target a DIFFERENT lane's live pair (a foreign close). Each names a
#: distinct way the owner binding failed to attest the target before any close.
REASON_ISSUE_LANE_MISMATCH = "issue_lane_mismatch"
REASON_LANE_OWNER_UNVERIFIED = "lane_owner_unverified"
REASON_LIFECYCLE_UNREADABLE = "lifecycle_unreadable"
#: Worktree-binding attestation (Redmine #13754 R2-F1, design j#78572, A+C). The lane's
#: durable lifecycle records a canonical worktree binding; the caller's ``--worktree``
#: must resolve to that exact token before any close, or a sibling lane's worktree could
#: drive the retire (the dirty check would probe the wrong worktree, and the wrong pair
#: could close). ``mismatch`` — the caller's worktree token differs from the lane's
#: recorded binding. ``unverified`` — the lane has no recorded worktree binding at all
#: (a v1/v2 / unbound row), so it cannot be attested and fails closed until re-declared.
REASON_WORKTREE_BINDING_MISMATCH = "worktree_binding_mismatch"
REASON_WORKTREE_BINDING_UNVERIFIED = "worktree_binding_unverified"


@dataclass(frozen=True)
class RetireActuation:
    """The fail-closed verdict of a guarded retire close (Redmine #13754).

    Before #13754 the actuation had no verdict at all: every failure to resolve the
    lane (a mis-aimed ``--repo`` / ``--worktree`` root, an unreadable inventory, an
    unresolved provider binding) folded into an empty :class:`HerdrRetireCloseResult`
    that was indistinguishable from a genuine "already retired" — and the command
    exited 0 off the *preflight* verdict alone, so a coordinator read ``retire_ok`` +
    ``closed: []`` and believed a still-live pair had been retired (#13748 j#77473).

    The verdict separates the two things that empty result conflated:

    - :data:`ACTUATION_CLOSED` — the expected managed target resolved at action time
      and every planned slot closed. A real retire.
    - :data:`ACTUATION_VERIFIED_NOOP` — the durable lifecycle proves the lane is
      already retired AND the (genuinely read) live inventory shows zero expected
      managed slots in the resolved unit. An idempotent re-run, *verified* against
      both authorities rather than inferred from an empty list.
    - :data:`ACTUATION_BLOCKED` — anything else, with the ``reason`` that could not be
      established. Never a success.

    ``expected_live`` is the measurement the no-op claim rests on: the expected managed
    roles found live in the targeted unit(s). It is a *live-inventory* fact, matching
    the lifecycle component's own boundary (``lane_lifecycle``: a recorded release is
    "not proof that the slots are gone"; liveness re-reads the inventory).
    """

    state: str
    reason: str = ""
    detail: str = ""
    workspace_id: str = ""
    lane_id: str = ""
    closed: tuple[tuple[str, str], ...] = ()
    failed: tuple[tuple[str, str, str], ...] = ()
    foreign_names: tuple[str, ...] = ()
    expected_live: tuple[str, ...] = ()
    durable_retirement: str = ""

    @property
    def ok(self) -> bool:
        """Did this actuation retire the lane? (the command's exit-code authority)"""
        return self.state in (ACTUATION_CLOSED, ACTUATION_VERIFIED_NOOP)

    def as_payload(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "closed": [{"role": r, "locator": loc} for r, loc in self.closed],
            "failed": [
                {"role": r, "locator": loc, "detail": d} for r, loc, d in self.failed
            ],
            "foreign_names": list(self.foreign_names),
            "expected_live": list(self.expected_live),
            "durable_retirement": self.durable_retirement,
        }


def blocked_actuation(
    reason: str,
    *,
    detail: str = "",
    workspace_id: str = "",
    lane_id: str = "",
) -> RetireActuation:
    """A fail-closed actuation verdict: the retire proved nothing and must not exit 0."""
    return RetireActuation(
        state=ACTUATION_BLOCKED,
        reason=reason,
        detail=detail,
        workspace_id=workspace_id,
        lane_id=lane_id,
    )


@dataclass(frozen=True)
class ExpectedSlotRow:
    """One expected managed-slot row found in a targeted unit (Redmine #13845 j#80148).

    ``slot_key`` is the row's **canonical slot identity** — the decoded
    ``(workspace_id, lane_id, role)``, which is one-to-one with its herdr assigned name.
    It is what a caller must count multiplicity by (review j#80187 R3-F1): the shared
    ``(project workspace, lane, role)`` slot and its legacy ``(worktree token, default,
    role)`` compatibility twin share a ROLE but are two distinct slots that legitimately
    coexist (``test_legacy_twin_closes_alongside_shared_unit``), so keying on ``role``
    alone reads that normal shape as a uniqueness violation.
    """

    workspace_id: str
    lane_id: str
    role: str
    locator: str
    row: Mapping[str, object]

    @property
    def slot_key(self) -> tuple[str, str, str]:
        return (self.workspace_id, self.lane_id, self.role)


def expected_slot_rows(
    rows: Sequence[Mapping[str, object]],
    plan: HerdrRetireClosePlan,
    *,
    managed_roles: Sequence[str] = _MANAGED_ROLES,
) -> tuple[ExpectedSlotRow, ...]:
    """Every expected managed-slot row in the plan's targeted unit(s), raw (pure).

    The unaggregated scan :func:`expected_live_slots` is defined over (Redmine #13845
    review j#80148). It yields one :class:`ExpectedSlotRow` per matching row — **including
    rows carrying no locator**, **without collapsing duplicates**, and **carrying each
    row's canonical slot identity** — because those are exactly the facts the aggregated
    measurement discards, and a caller that needs "is this unit quiescent?" rather than
    "which expected roles are live?" cannot reconstruct them from the role set.

    Scoped to the same two units :func:`plan_herdr_retire_close` targets — the shared
    ``(workspace_id, lane_id)`` unit and the legacy ``(legacy_workspace_id, default)``
    twin. Rows are returned in input order. Empty inputs match nothing.

    This is a scan, not a judgment: whether a locator-less row means "gone" or "cannot be
    read" is the caller's policy (see :func:`...herdr_slot_liveness.classify_named_slot`),
    and so is what counts as a duplicate — hence :attr:`ExpectedSlotRow.slot_key` is
    exposed rather than pre-aggregated.
    """
    managed = frozenset(managed_roles)
    found: list[ExpectedSlotRow] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decode = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decode.ok or decode.identity is None:
            continue
        identity = decode.identity
        if identity.role not in managed:
            continue
        row_lane = _norm_lane(identity.lane_id)
        in_shared = bool(
            plan.workspace_id
            and plan.lane_id
            and identity.workspace_id == plan.workspace_id
            and row_lane == plan.lane_id
        )
        in_legacy = bool(
            plan.legacy_workspace_id
            and identity.workspace_id == plan.legacy_workspace_id
            and row_lane == DEFAULT_LANE
        )
        if in_shared or in_legacy:
            found.append(
                ExpectedSlotRow(
                    workspace_id=identity.workspace_id,
                    lane_id=row_lane,
                    role=identity.role,
                    locator=_agent_locator(row),
                    row=row,
                )
            )
    return tuple(found)


def expected_live_slots(
    rows: Sequence[Mapping[str, object]],
    plan: HerdrRetireClosePlan,
    *,
    managed_roles: Sequence[str] = _MANAGED_ROLES,
) -> tuple[str, ...]:
    """The expected managed roles that are LIVE in the plan's targeted unit(s) (pure).

    The measurement the zero-close fence rests on. It is deliberately independent of
    ``plan.close_targets``: a slot can be live in the unit and still not be a close
    target (the #13569 pair-atomic substitution fence zeroes the targets while leaving
    the matching half live). Reporting "nothing to close" off an empty target list would
    call that substitution a successful retire; measuring the live expected slots
    directly does not.

    Scoped to the same two units :func:`plan_herdr_retire_close` targets — the shared
    ``(workspace_id, lane_id)`` unit and the legacy ``(legacy_workspace_id, default)``
    twin — and to rows carrying a live locator. Empty inputs measure nothing live.

    **What this deliberately does NOT measure** (Redmine #13845 review j#80148): it is an
    aggregate over the MANAGED roles that carry a locator, so its empty result means "no
    expected role is live", never "the unit is empty". It drops (a) unexpected occupants —
    see ``plan.foreign_names``, (b) rows with no locator, and (c) duplicate multiplicity
    (roles collapse into a set). A caller whose contract needs quiescence rather than
    liveness must read :func:`expected_slot_rows` as well; reading this empty result as
    absence is the j#80115 / j#80148 fail-open. Behaviour is unchanged — this is now
    expressed over the raw scan so the two share one scoping definition.
    """
    return tuple(
        sorted(
            {
                found.role
                for found in expected_slot_rows(
                    rows, plan, managed_roles=managed_roles
                )
                if found.locator
            }
        )
    )


def decide_retire_actuation(
    plan: HerdrRetireClosePlan,
    result: HerdrRetireCloseResult,
    *,
    expected_live: Sequence[str],
    already_retired: bool,
) -> RetireActuation:
    """The fail-closed verdict for an executed retire close (pure, Redmine #13754).

    ``retire_ok`` is limited to a real close or a *verified* idempotent no-op:

    - any failed close leaves a live managed agent, so it is blocked (a partially
      closed pair is not a retired lane);
    - a close that actually closed slots is :data:`ACTUATION_CLOSED`;
    - a zero-close is :data:`ACTUATION_VERIFIED_NOOP` only when BOTH authorities agree
      the lane is gone — the durable lifecycle says ``retired`` (``already_retired``,
      read fail-closed by the caller) AND zero expected managed slots are live. Either
      one alone is not proof: a durable record is not liveness (``lane_lifecycle``
      boundary), and an empty live measurement alone cannot distinguish "already
      retired" from "we never found the lane" — which is exactly the #13748 j#77473
      failure this fence exists to stop;
    - every other zero-close is :data:`REASON_ZERO_CLOSE_UNPROVEN`.
    """
    live = tuple(expected_live)
    common = dict(
        workspace_id=result.workspace_id,
        lane_id=result.lane_id,
        closed=result.closed,
        failed=result.failed,
        foreign_names=result.foreign_names,
        expected_live=live,
    )
    if result.failed:
        return RetireActuation(
            state=ACTUATION_BLOCKED,
            reason=REASON_CLOSE_FAILED,
            detail=(
                f"{len(result.failed)} managed slot(s) failed to close; "
                "the lane still holds live agents"
            ),
            **common,
        )
    if result.closed:
        return RetireActuation(state=ACTUATION_CLOSED, **common)
    if already_retired and not live:
        return RetireActuation(
            state=ACTUATION_VERIFIED_NOOP,
            detail=(
                "durable lifecycle records the lane retired and no expected managed "
                "slot is live; nothing left to close"
            ),
            **common,
        )
    if live:
        detail = (
            "zero slots closed while expected managed slot(s) are still live "
            f"({', '.join(live)}) — the lane was not retired"
        )
    else:
        detail = (
            "zero slots closed and the durable lifecycle does not record this lane "
            "retired — an unproven no-op is not a retire (re-run against the lane's "
            "own root while its pair is live, or record the retirement durably)"
        )
    return RetireActuation(
        state=ACTUATION_BLOCKED,
        reason=REASON_ZERO_CLOSE_UNPROVEN,
        detail=detail,
        **common,
    )


@dataclass(frozen=True)
class HerdrRetireClosePlan:
    """The fail-closed plan for a lane's guarded retire close.

    ``close_targets`` are the ``(role, locator)`` pairs of the lane unit's managed
    gateway / worker slots — ``(workspace_id, lane_id)`` under the shared project
    workspace model, plus the legacy twin ``(legacy_workspace_id, default)`` when a
    pre-#13377 lane's slots are still live. These are the ONLY agents this retire ever
    closes. ``foreign_names`` records any *other* managed-scheme agent decoded into the
    targeted unit(s) (an unexpected role, or — for a legacy per-lane workspace — any
    other occupant): informational for the audit trail and never a close target.
    """

    workspace_id: str
    lane_id: str = ""
    legacy_workspace_id: str = ""
    close_targets: tuple[tuple[str, str], ...] = ()
    foreign_names: tuple[str, ...] = ()

    @property
    def has_targets(self) -> bool:
        return bool(self.close_targets)


@dataclass(frozen=True)
class HerdrRetireCloseResult:
    """The outcome of executing a guarded retire close (per-target, non-fatal)."""

    workspace_id: str
    lane_id: str = ""
    closed: tuple[tuple[str, str], ...] = ()  # (role, locator) successfully closed
    failed: tuple[tuple[str, str, str], ...] = ()  # (role, locator, detail)
    foreign_names: tuple[str, ...] = ()

    def as_payload(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "closed": [{"role": r, "locator": loc} for r, loc in self.closed],
            "failed": [
                {"role": r, "locator": loc, "detail": d} for r, loc, d in self.failed
            ],
            "foreign_names": list(self.foreign_names),
        }


def plan_herdr_retire_close(
    rows: Sequence[Mapping[str, object]],
    *,
    workspace_id: str,
    lane_id: str = "",
    legacy_workspace_id: str = "",
    managed_roles: Sequence[str] = _MANAGED_ROLES,
) -> HerdrRetireClosePlan:
    """Decide which managed slots to close for the lane (pure, fail-closed).

    Decodes each ``agent list`` row's ``mzb1`` name (#13247); a row is a close target iff
    it carries a live locator and decodes to one of the lane's units:

    - ``(workspace_id, lane_id, codex|claude)`` — the shared-model lane slots (#13377).
      A **default** ``lane_id`` is refused for a non-legacy workspace: the project
      workspace's default-lane pair is the coordinator, never a retire target (the only
      default-lane close is a legacy ``wt_<hash>`` workspace's own pair);
    - ``(legacy_workspace_id, default, codex|claude)`` — the pre-#13377 compatibility
      twin, considered only when ``legacy_workspace_id`` is a well-formed lane token.

    A managed-scheme row inside a targeted unit that is NOT a gateway / worker slot — or
    any other occupant of a targeted legacy workspace — is recorded in ``foreign_names``
    (never closed). Every other row (other lanes, the coordinator pair, other
    workspaces, undecodable rows) is ignored. Empty inputs match nothing.
    """
    managed = tuple(managed_roles)
    ws = _norm(workspace_id)
    lane = _norm_lane(lane_id) if _norm(lane_id) else ""
    legacy_ws = _norm(legacy_workspace_id)
    if legacy_ws and not is_lane_workspace_token(legacy_ws):
        legacy_ws = ""
    # A legacy token passed as the workspace (a pre-#13377 caller shape) keeps the old
    # whole-workspace semantics: it IS the legacy twin (default-lane pair closes, every
    # other occupant is recorded as foreign).
    if is_lane_workspace_token(ws) and not legacy_ws:
        legacy_ws, ws = ws, ""
    # The targetable lane of a registry (project) workspace requires an explicit
    # non-default lane — its default-lane coordinator pair is never a retire target.
    unit_lane = lane if ws and lane and lane != DEFAULT_LANE else ""
    close_targets: list[tuple[str, str]] = []
    foreign: list[str] = []
    # Pair-atomic attestation (Redmine #13569 R1-F4): the retire must never partially
    # close a lane whose live providers do not match the binding-expected pair. The
    # mismatch signal is a *substitution* at the lane's own position (workspace + lane
    # match): an expected ``managed_roles`` slot is MISSING while an unexpected (non-
    # managed) slot is PRESENT — i.e. a wrong-provider agent stands where a bound provider
    # should. When that holds, no slot is closed (zero-close); the matching half is never
    # closed while the wrong-provider half stays live. Distinguished from the benign cases:
    #   - an EXTRA non-managed slot alongside the full expected pair (both present) is not a
    #     substitution — the pair closes, the extra is recorded foreign;
    #   - partial liveness (an expected slot already gone, nothing unexpected in its place)
    #     is not a substitution — the surviving expected slot(s) still close.
    # For the built-in pair (live == expected) this is byte-identical. The attestation is
    # PER UNIT (Redmine #13569 R2-F4b): a substitution is detected within the shared unit or
    # within the legacy twin SEPARATELY — a provider present in the legacy twin must never
    # mask a substitution in the shared unit (or vice versa), so the two units' present /
    # unexpected sets are tracked independently.
    expected = set(managed)
    shared_present: set[str] = set()
    shared_unexpected = False
    legacy_present: set[str] = set()
    legacy_unexpected = False
    if not ws and not legacy_ws:
        return HerdrRetireClosePlan(workspace_id=ws, lane_id=lane)
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = row.get(AGENT_KEY_NAME)
        decode = decode_assigned_name(name)
        if not decode.ok or decode.identity is None:
            continue
        identity = decode.identity
        row_lane = _norm_lane(identity.lane_id)
        if legacy_ws and identity.workspace_id == legacy_ws:
            # Legacy per-lane workspace: its default-lane managed pair closes; any
            # other occupant is recorded (the workspace will not disappear while
            # they live) and never closed.
            if row_lane == DEFAULT_LANE and identity.role in managed:
                legacy_present.add(identity.role)
                locator = _agent_locator(row)
                if locator:
                    close_targets.append((identity.role, locator))
                continue
            if row_lane == DEFAULT_LANE:
                # An unexpected provider AT the legacy lane's own position.
                legacy_unexpected = True
            foreign.append(_norm(name))
            continue
        if ws and unit_lane and identity.workspace_id == ws and row_lane == unit_lane:
            if identity.role in managed:
                shared_present.add(identity.role)
                locator = _agent_locator(row)
                if locator:
                    close_targets.append((identity.role, locator))
                continue
            # An unexpected provider AT the targeted lane unit's own position.
            shared_unexpected = True
            foreign.append(_norm(name))
    # Substitution in EITHER unit (an expected provider missing there while an unexpected one
    # is live in that same unit) fails the WHOLE plan closed — never a partial close of a
    # mis-identified lane (the unexpected slot is already recorded in ``foreign``).
    shared_substitution = bool(expected - shared_present) and shared_unexpected
    legacy_substitution = bool(expected - legacy_present) and legacy_unexpected
    if shared_substitution or legacy_substitution:
        close_targets = []
    return HerdrRetireClosePlan(
        # Echo the caller-visible unit: a legacy-only close reports its token.
        workspace_id=ws or legacy_ws,
        lane_id=unit_lane,
        legacy_workspace_id=legacy_ws,
        close_targets=tuple(close_targets),
        foreign_names=tuple(foreign),
    )


def execute_herdr_retire_close(
    plan: HerdrRetireClosePlan,
    *,
    env: Optional[Mapping[str, str]] = None,
    runner: Optional[Runner] = None,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> HerdrRetireCloseResult:
    """Close the planned managed slots via ``herdr pane close`` (per-target, non-fatal).

    Only ``plan.close_targets`` are closed — never a foreign row, never the project
    workspace itself (Redmine #13377: retire removes the lane's slots; the shared
    workspace keeps hosting the coordinator pair and every other lane). Each close is
    non-fatal (a failed close leaves a live agent, recorded, not raised), mirroring the
    #13330 base pane reclaim's non-fatal ``pane close`` contract.
    """
    environ = env if env is not None else os.environ
    binary = _resolve_binary_or_die(environ)
    run = runner or subprocess.run
    closed: list[tuple[str, str]] = []
    failed: list[tuple[str, str, str]] = []
    for role, locator in plan.close_targets:
        ok, detail = _close_base_pane(binary, locator, run, timeout, environ)
        if ok:
            closed.append((role, locator))
        else:
            failed.append((role, locator, detail))
    return HerdrRetireCloseResult(
        workspace_id=plan.workspace_id,
        lane_id=plan.lane_id,
        closed=tuple(closed),
        failed=tuple(failed),
        foreign_names=plan.foreign_names,
    )


__all__ = (
    "ACTUATION_BLOCKED",
    "ACTUATION_CLOSED",
    "ACTUATION_VERIFIED_NOOP",
    "REASON_CLOSE_FAILED",
    "REASON_INVENTORY_UNREADABLE",
    "REASON_ISSUE_LANE_MISMATCH",
    "REASON_LANE_OWNER_UNVERIFIED",
    "REASON_LIFECYCLE_UNREADABLE",
    "REASON_NO_WORKTREE_ANCHOR",
    "REASON_PROVIDER_NOT_LAUNCHABLE",
    "REASON_PROVIDER_UNRESOLVED",
    "REASON_WORKSPACE_UNRESOLVED",
    "REASON_WORKTREE_BINDING_MISMATCH",
    "REASON_WORKTREE_BINDING_UNVERIFIED",
    "REASON_ZERO_CLOSE_UNPROVEN",
    "HerdrRetireClosePlan",
    "HerdrRetireCloseResult",
    "RetireActuation",
    "blocked_actuation",
    "decide_retire_actuation",
    "execute_herdr_retire_close",
    "expected_live_slots",
    "plan_herdr_retire_close",
)
