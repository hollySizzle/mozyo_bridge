"""Managed-marker classification PoC (Redmine #11699).

Decides whether a session / pane is a mozyo-managed unit, following the
layered marker policy of ``managed-state-model.md`` (#11695):

- **primary: workspace registry anchor** — a registered workspace
  (``.mozyo-bridge/workspace.json``) is the authoritative "this is a mozyo
  workspace" signal, because it is one with identity (#11429). When a
  pane's repo root resolves to a registered workspace, the unit is
  ``managed`` regardless of any runtime marker.
- **secondary: tmux user option** (``@mozyo_managed``) — a runtime marker
  mozyo sets on the session/pane. It confirms management for a *running*
  unit even before (or without) registry registration, and is robust to
  rename / derivation drift in a way a name prefix is not.
- **display-only: session name prefix** (``mozyo-``) — NEVER an authority
  boundary (#10796); a foreign session could spell the same prefix. It is
  not consulted here at all; classification ignores it.

A unit matching neither primary nor secondary is ``unmanaged`` /
``runtime-only`` — the owner-adopted coexistence model (#56369): such
units are surfaced, not excluded, and the handoff safety boundary (live
tmux) applies to them identically.

This module is **classification only**. It does not touch liveness,
handoff target resolution, or preflight — those stay runtime-authoritative
(#11698 invariant).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# tmux user option carrying the secondary runtime marker. Value is an
# opaque truthy token; presence is what matters.
MANAGED_OPTION = "@mozyo_managed"
MANAGED_OPTION_VALUE = "1"

MANAGED = "managed"
UNMANAGED = "unmanaged"

SOURCE_REGISTRY_ANCHOR = "registry-anchor"
SOURCE_TMUX_OPTION = "tmux-option"
SOURCE_NONE = "none"


@dataclass(frozen=True)
class ManagedClassification:
    """Whether a unit is mozyo-managed and which layer decided it."""

    state: str           # MANAGED | UNMANAGED
    source: str          # which layer matched (or SOURCE_NONE)
    runtime_only: bool   # True when unmanaged (surfaced, not excluded)

    def as_payload(self) -> dict:
        return {
            "state": self.state,
            "source": self.source,
            "runtime_only": self.runtime_only,
        }


def _has_registry_anchor(repo_root: str | None) -> bool:
    """Primary check: does the pane's repo root carry a registry anchor?"""
    if not repo_root:
        return False
    from mozyo_bridge.workspace_registry import read_anchor

    return read_anchor(Path(repo_root)) is not None


def classify_managed(
    *,
    repo_root: str | None,
    tmux_marker: str | None,
) -> ManagedClassification:
    """Classify one unit. Primary (anchor) then secondary (tmux option).

    ``repo_root`` is the pane's inferred repo root (runtime-derived; this
    function does not re-derive it). ``tmux_marker`` is the value of the
    ``@mozyo_managed`` user option already read from the unit (``None`` if
    unset). Name prefix is intentionally not a parameter — it is never an
    authority signal.
    """
    if _has_registry_anchor(repo_root):
        return ManagedClassification(
            state=MANAGED, source=SOURCE_REGISTRY_ANCHOR, runtime_only=False
        )
    if tmux_marker:
        return ManagedClassification(
            state=MANAGED, source=SOURCE_TMUX_OPTION, runtime_only=False
        )
    return ManagedClassification(
        state=UNMANAGED, source=SOURCE_NONE, runtime_only=True
    )


def mark_target(target: str) -> bool:
    """Set the secondary runtime marker on a tmux target. Non-fatal PoC."""
    from mozyo_bridge.infrastructure.tmux_client import set_user_option

    return set_user_option(target, MANAGED_OPTION, MANAGED_OPTION_VALUE)


def read_target_marker(target: str) -> str | None:
    """Read the secondary runtime marker from a tmux target."""
    from mozyo_bridge.infrastructure.tmux_client import get_user_option

    return get_user_option(target, MANAGED_OPTION)
