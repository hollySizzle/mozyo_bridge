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
    "HerdrRetireClosePlan",
    "HerdrRetireCloseResult",
    "execute_herdr_retire_close",
    "plan_herdr_retire_close",
)
