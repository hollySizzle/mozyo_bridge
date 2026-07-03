"""Display-only delegated-coordinator window-policy resolution (Redmine #12467).

Resolves the desired ``separate`` | ``shared`` window projection for one
delegated-coordinator tree unit, given the repo-local ``delegation_window_policy``
knob (parsed by :mod:`.config`) and the closed #12466 delegated-tree display
breadcrumb (``lane_kind`` / ``delegation_depth`` / ``delegation_parent`` /
``delegation_root`` / ``status``). It is the follow-up #3 named by
``vibes/docs/logics/delegated-coordinator-cockpit-display.md`` ``## 実装分割``:
"``presentation`` config の ``delegation_window_policy`` knob 解決".

Boundaries (kept enforced in code):

- **Display-only.** :class:`DelegationWindowDisplay` carries desired window-group
  metadata, never a routing / handoff / approval / close / send-preflight key.
  The fixed invariants in ``## 固定 invariant`` do not relax when ``shared`` is
  chosen — this resolver decides *display grouping only*, not routing.
- **Pure, no I/O.** A function of its arguments; it reads no tmux / Redmine /
  clock / config file. The caller loads the policy and passes the already-derived
  #12466 breadcrumb.
- **Fail-soft.** A unit with no delegation fact, or one whose tree the #12466
  foundation could only surface as ``diagnostic`` (off-contract / cycle /
  depth > 2), yields a window display that withholds the separate/shared decision
  rather than fabricating one — it never raises and never blocks a read-only
  table. An unexpected policy value falls back to the documented default
  (``shared``, #13085); the *config* layer is the fail-closed boundary
  (:func:`~.validation._checked_delegation_window_policy`), this display layer
  degrades gracefully.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    DEFAULT_DELEGATION_WINDOW_POLICY,
    DELEGATION_WINDOW_POLICY_MODES,
    DELEGATION_WINDOW_POLICY_SHARED,
)

#: No durable delegation fact -> no window projection (policy still echoed).
DELEGATION_WINDOW_STATUS_NONE = "none"
#: A coherent derived tree -> the separate/shared decision is trustworthy.
DELEGATION_WINDOW_STATUS_RESOLVED = "resolved"
#: A broken / off-contract tree (#12466 diagnostic) -> shown but untrusted; the
#: separate/shared decision is withheld rather than derived from a broken tree.
DELEGATION_WINDOW_STATUS_DIAGNOSTIC = "diagnostic"

# The #12466 delegation-display status values this resolver consumes. Kept as
# literals (not imported from delegation_display) so this domain leaf imports
# nothing upward — the dependency only ever points toward the leaves.
_SOURCE_STATUS_NONE = "none"
_SOURCE_STATUS_DIAGNOSTIC = "diagnostic"


@dataclass(frozen=True)
class DelegationWindowDisplay:
    """One unit's desired delegated-coordinator window projection (#12467).

    Display-only metadata: ``separated`` says whether this unit is *desired* to
    occupy its own cockpit window / column (vs. share one with its tree), and
    ``window_group`` is the public-safe display grouping key it folds onto. It is
    never a routing / approval / close key and never a guaranteed OS window.
    ``status`` distinguishes a trustworthy ``resolved`` decision from ``none``
    (no delegation fact) or ``diagnostic`` (a broken tree shown untrusted).
    """

    policy: str
    separated: bool
    window_group: str
    status: str

    def as_payload(self) -> dict:
        return {
            "window_policy": self.policy,
            "window_separated": self.separated,
            "window_group": self.window_group,
            "window_status": self.status,
        }


def _effective_policy(policy: object) -> str:
    """Fail-soft policy: an unexpected value degrades to the documented default.

    The config parser is the fail-closed boundary; by the time a policy reaches
    this display resolver it is normally already a valid mode. Defaulting an
    unexpected value here keeps the read-only display from raising on drift.
    """
    if isinstance(policy, str) and policy in DELEGATION_WINDOW_POLICY_MODES:
        return policy
    return DEFAULT_DELEGATION_WINDOW_POLICY


def resolve_delegation_window_display(
    policy: object,
    *,
    lane_kind: str,
    delegation_depth: "int | None",
    delegation_unit: str,
    delegation_root: str,
    status: str,
) -> DelegationWindowDisplay:
    """Resolve one unit's desired window projection under ``policy`` (Redmine #12467).

    ``policy`` is the repo-local ``delegation_window_policy`` (``separate`` |
    ``shared``); the remaining arguments are the #12466 delegated-tree breadcrumb
    for the unit (``delegation_unit`` is its ``<workspace_id>/<lane_id>`` display
    pointer). The result is display-only and fail-soft:

    - **No delegation fact** (``status`` ``none`` or a blank ``lane_kind``) ->
      :data:`DELEGATION_WINDOW_STATUS_NONE`: no separate/shared decision; the
      effective policy is still echoed so the surface is explicit.
    - **Broken tree** (``status`` ``diagnostic`` or a withheld
      ``delegation_depth``) -> :data:`DELEGATION_WINDOW_STATUS_DIAGNOSTIC`: shown
      but untrusted, never a fabricated decision from a tree the foundation could
      not derive.
    - **Derived tree**: the tree root (depth 0, the parent coordinator) is always
      its own top-of-tree window. A delegated coordinator / grandchild
      (depth >= 1) is ``separated`` under ``separate`` (its own window, keyed on
      its own unit) and folded under ``shared`` (``separated`` false, keyed on the
      tree's ``delegation_root``).
    """
    effective = _effective_policy(policy)

    if status == _SOURCE_STATUS_NONE or not (lane_kind or "").strip():
        return DelegationWindowDisplay(
            policy=effective,
            separated=False,
            window_group="",
            status=DELEGATION_WINDOW_STATUS_NONE,
        )

    if status == _SOURCE_STATUS_DIAGNOSTIC or delegation_depth is None:
        return DelegationWindowDisplay(
            policy=effective,
            separated=False,
            window_group="",
            status=DELEGATION_WINDOW_STATUS_DIAGNOSTIC,
        )

    # The tree root (the parent coordinator) is always its own window.
    if delegation_depth <= 0:
        return DelegationWindowDisplay(
            policy=effective,
            separated=True,
            window_group=delegation_unit,
            status=DELEGATION_WINDOW_STATUS_RESOLVED,
        )

    # Delegated coordinator / grandchild worker (depth >= 1).
    if effective == DELEGATION_WINDOW_POLICY_SHARED:
        return DelegationWindowDisplay(
            policy=effective,
            separated=False,
            window_group=delegation_root or delegation_unit,
            status=DELEGATION_WINDOW_STATUS_RESOLVED,
        )
    return DelegationWindowDisplay(
        policy=effective,
        separated=True,
        window_group=delegation_unit,
        status=DELEGATION_WINDOW_STATUS_RESOLVED,
    )


__all__ = (
    "DELEGATION_WINDOW_STATUS_NONE",
    "DELEGATION_WINDOW_STATUS_RESOLVED",
    "DELEGATION_WINDOW_STATUS_DIAGNOSTIC",
    "DelegationWindowDisplay",
    "resolve_delegation_window_display",
)
