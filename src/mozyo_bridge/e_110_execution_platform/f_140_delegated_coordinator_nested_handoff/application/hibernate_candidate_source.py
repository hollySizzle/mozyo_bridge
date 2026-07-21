"""Read-only lifecycle binding for hibernate candidates (Redmine #14219, tranche T1).

The one impure step T1 owns: read the lane lifecycle store *read-only* and hand the rows to the
pure fold :func:`bind_lifecycle_anchor`. It performs no mutation and creates nothing — the read
goes through :func:`load_lane_lifecycle_readonly`, which opens the store ``mode=ro`` and returns
``None`` (fail-closed) on any unknown / newer / malformed / partial schema.

Binding the git head, the review / integration / CI / dogfood-delegation authorities, and the
supervisor leg that actuates are all tranche T2. This module deliberately stops at the lifecycle
anchor.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from mozyo_bridge.core.state.lane_lifecycle import load_lane_lifecycle_readonly

from ..domain.hibernate_candidate import (
    HibernateCandidate,
    HibernateNonCandidate,
    LifecycleAnchor,
    SelectedLane,
    bind_lifecycle_anchor,
)


def bind_active_lifecycle_anchor(
    selected: SelectedLane, *, home: Optional[Path] = None
) -> "LifecycleAnchor | HibernateNonCandidate":
    """Re-bind and confirm the EXACT ``selected`` lane from the read-only lifecycle store.

    A thin shell: the read is :func:`load_lane_lifecycle_readonly` (non-creating, ``mode=ro``,
    fail-closed to ``None``); the decision is the pure :func:`bind_lifecycle_anchor`, which confirms
    the record matches ``selected`` on workspace / lane / generation / revision. ``()`` (absent
    store) folds to ``active_lifecycle_record_absent``; ``None`` (unreadable) to
    ``lifecycle_store_unreadable``.
    """
    records = load_lane_lifecycle_readonly(home=home)
    return bind_lifecycle_anchor(records, selected=selected)


def still_current(candidate: HibernateCandidate, *, home: Optional[Path] = None) -> bool:
    """Action-time revalidation (Redmine #14219 T2): is the candidate's exact anchor still current?

    The public hibernate CAS pins to its own fresh read (not the request's ``expected_revision``,
    unless a project-gateway binding is set), so a lane that drifted between candidate build and
    actuation would otherwise be hibernated in its new state — evidence the candidate never proved.
    This re-reads the read-only store and confirms the record STILL matches the candidate's exact
    ``(workspace, lane, generation, revision)``; any drift, absence, ambiguity, or unreadable store
    fails closed to ``False`` (reusing the T1 binder, which already fails closed on each of those).
    """
    selected = SelectedLane(
        issue_id=candidate.issue_id,
        repo_workspace_id=candidate.anchor.repo_workspace_id,
        lane_id=candidate.anchor.lane_id,
        lane_generation=candidate.anchor.lane_generation,
        revision=candidate.anchor.revision,
    )
    return isinstance(bind_active_lifecycle_anchor(selected, home=home), LifecycleAnchor)
