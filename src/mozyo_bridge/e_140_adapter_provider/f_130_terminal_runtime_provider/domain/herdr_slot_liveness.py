"""Composite liveness for a durable-name herdr slot (Redmine #13518 host-restart recovery).

The host-restart recovery reconciler (design #13520 j#75276, finding #13518 j#75329) exists to
stop one specific false positive: after a host reboot the Claude / Codex TUI in a lane pane
exits, leaving a foreground ``-zsh`` with **no** child agent, yet the durable assigned-name row
survives in ``herdr agent list``. Session-start's adopt planning matched that row on its
``name`` alone and reported it ``live / adopted`` — a *shell residue* mistaken for a live agent
(j#75329 root cause). Adopting it routes a handoff into a bare shell that parses the marker as a
zsh command; the work never runs.

This module is the reconciler's **runtime authority**: a pure predicate over a single
``agent list`` row that decides whether a name-matched row is a live, adoptable agent or a
:data:`SLOT_STALE` shell residue. It is deliberately *conservative in the never-clobber
direction* — it only refuses to adopt when the live inventory **positively** signals a dead
slot, so a row that carries no liveness signal at all (a legacy / minimal row shape) still
adopts exactly as before. That keeps the legitimate self-heal path (adopt a live sibling, a
cohabiting lane pair) byte-for-byte unchanged while catching the reproduced reboot residue.

The composite judgment combines the two signals a real ``agent list`` row carries (j#75328
live probe):

- the **detected provider agent** (the ``agent`` field): a live managed pane names its provider
  (``codex`` / ``claude``); a shell residue reports the field absent or blank;
- the **runtime status** (``agent_status`` / ``status`` / ``state``): a live pane reports a
  recognised herdr status (``working`` / ``idle`` / ``blocked`` / ``done``); a residue reports
  ``unknown``.

A destructive stale-pane close + same-slot relaunch is **not** decided here — that stays an
owner-approved recovery gate (j#75331). This predicate only classifies; the caller surfaces a
stale slot as a read-only recovery plan rather than blind-adopting or blind-launching it.
"""

from __future__ import annotations

from typing import Mapping

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (
    RUNTIME_UNKNOWN,
    map_agent_status,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    _norm,
)

#: A name-matched row backed by a live, adoptable agent.
SLOT_LIVE = "live"
#: A name-matched row that is only shell / name residue — never blind-adopt (#13518 j#75329).
SLOT_STALE = "stale_named_slot"

#: The row key naming the detected provider agent of a managed pane. On a live pane it holds
#: the provider (``codex`` / ``claude``); a shell residue reports it absent or blank. ``name``
#: is always the durable-identity handle in these rows, so a co-present ``agent`` key is the
#: detected-agent signal, never a handle alias.
_DETECTED_AGENT_KEY = "agent"

#: The keys a herdr ``agent list`` row may carry its runtime status under (shared shape with
#: :mod:`...infrastructure.herdr_state`). The first present one is authoritative.
_STATUS_KEYS = ("agent_status", "status", "state")


def _status_signal(row: Mapping[str, object]) -> "str | None":
    """The row's runtime status mapped to a runtime receiver-state, or ``None`` if unsignalled.

    ``None`` means the row carries **no** status field at all (a minimal / legacy row shape) —
    distinct from a present ``unknown`` status. Only a *present* status participates in the
    stale judgment, so a row that never reported a status is never reclassified as stale on that
    axis alone (backward-compatible adopt).
    """
    for key in _STATUS_KEYS:
        if key in row:
            return map_agent_status(row.get(key))
    return None


def classify_named_slot(row: Mapping[str, object]) -> str:
    """Classify a name-matched ``agent list`` row as :data:`SLOT_LIVE` or :data:`SLOT_STALE`.

    ``row`` must already be known to match the requested durable assigned name; this decides
    only whether that identity is backed by a live agent. Conservative in the never-clobber
    direction — returns :data:`SLOT_STALE` **only** on a positive shell-residue signal:

    - the detected-agent field is present but blank (``agent`` reported, no provider), or
    - a runtime status is present and maps to :data:`RUNTIME_UNKNOWN`, with no live detected
      agent to override it.

    A present, non-blank detected agent (a named provider) always reads live — it is a positive
    liveness signal that overrides an ``unknown`` status (a briefly-unreadable but real agent is
    not clobbered). A row carrying neither a detected-agent field nor a status field reads live
    (a legacy / minimal shape adopts unchanged). Pure; never raises.
    """
    detected_present = _DETECTED_AGENT_KEY in row
    detected = _norm(row.get(_DETECTED_AGENT_KEY)) if detected_present else ""
    if detected:
        # A positively detected provider agent is live regardless of a transient status read.
        return SLOT_LIVE
    if detected_present:
        # The field is present but blank — herdr reports the pane has no managed agent.
        return SLOT_STALE
    status = _status_signal(row)
    if status == RUNTIME_UNKNOWN:
        # A present-but-unknown status with no detected agent is the reproduced reboot residue.
        return SLOT_STALE
    return SLOT_LIVE


__all__ = (
    "SLOT_LIVE",
    "SLOT_STALE",
    "classify_named_slot",
)
