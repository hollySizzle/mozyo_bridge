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
    HibernateNonCandidate,
    LifecycleAnchor,
    bind_lifecycle_anchor,
)


def bind_active_lifecycle_anchor(
    issue_id: str, *, home: Optional[Path] = None
) -> "LifecycleAnchor | HibernateNonCandidate":
    """Re-bind the single active lane anchor for ``issue_id`` from the read-only lifecycle store.

    A thin shell: the read is :func:`load_lane_lifecycle_readonly` (non-creating, ``mode=ro``,
    fail-closed to ``None``); the decision is the pure :func:`bind_lifecycle_anchor`. ``()`` (absent
    store) folds to ``active_lifecycle_record_absent``; ``None`` (unreadable) to
    ``lifecycle_store_unreadable``.
    """
    records = load_lane_lifecycle_readonly(home=home)
    return bind_lifecycle_anchor(records, issue_id=issue_id)
