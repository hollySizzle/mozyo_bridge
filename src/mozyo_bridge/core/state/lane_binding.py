"""Lane owner-binding identity predicate (Redmine #13811).

The single place a lifecycle action decides *"is this the lane my request names?"*
for BOTH owner-binding kinds — an **issue** lane (owned by ``issue_id``) and a
**project-gateway** lane (owned by a canonical full ``project_scope``, Design Answer
#13780 j#78386). Every process-only lifecycle action (hibernate / quarantine / replace /
retire) resolves the lane by its ``(workspace_id, lane_id)`` key, reads the shared
:class:`...lane_lifecycle_model.LaneLifecycleRecord`, and then must verify that the row's
owner binding is the one the caller intended before it CAS-moves anything.

That verification used to be hard-coded as ``record.issue_id == issue`` at every action
site (correct for the only binding kind that existed). A project-gateway lane owns a
**scope**, not an issue — its ``issue_id`` is empty and ``binding_kind`` is
``project_gateway`` (Redmine #13810) — so the issue check can never match it. This module
folds the two-kind identity into one pure predicate so:

- the distinction lives in ONE place (one-rule-one-home), not re-derived per action;
- an **issue** caller (``project_scope=""``) keeps the byte-identical ``record.issue_id ==
  issue`` verdict — no behaviour change for issue-owned lanes;
- a **project-gateway** caller (a non-empty ``project_scope``) is matched on the full
  scope AND the ``project_gateway`` kind AND an empty ``issue_id``, so a scope is never
  confused with an issue and a row of the wrong kind never matches.

The **decision anchor** is deliberately NOT the binding: a project-gateway lane owns a
scope, but the Redmine ``(issue, journal)`` that authorizes each of its state changes is
still a real durable pointer (a journal is only addressable through its issue,
``DecisionPointer`` R2-F1). The action builds that anchor from its ``--issue`` /
``--journal`` exactly as before; this predicate only decides *which lane* the anchor is
allowed to act on, and ``DecisionPointer.authorizes_binding`` already accepts any complete
anchor for an (empty-``issue_id``) project lane.
"""

from __future__ import annotations

from mozyo_bridge.core.state.lane_lifecycle_model import (
    BINDING_KIND_PROJECT_GATEWAY,
    LaneLifecycleRecord,
    norm,
)


def record_matches_binding(
    record: LaneLifecycleRecord | None,
    *,
    issue_id: str = "",
    project_scope: str = "",
) -> bool:
    """Does ``record`` carry the owner binding the caller names? (pure, fail-closed).

    Exactly one binding kind is expressed by which argument is non-empty:

    - ``project_scope`` non-empty -> **project-gateway** binding. Matches only a row that
      is ``binding_kind='project_gateway'`` AND whose ``project_scope`` equals the
      requested scope AND whose ``issue_id`` is empty (a project lane never owns an issue).
      A scope is the full canonical string, never a digest — the caller passes what it
      declared, so a divergent scope is a non-match, not a coerced one.
    - ``project_scope`` empty -> **issue** binding. Matches ``record.issue_id == issue_id``
      — byte-identical to the pre-#13811 hard-coded check every action used, so an
      issue-owned lane's identity verdict is unchanged.

    ``None`` (no row) is never a match. An empty ``issue_id`` on the issue path only
    matches an (also empty) legacy row, but every in-scope action requires a non-empty
    issue anchor before it reaches this predicate, so that pairing is unreachable there.
    """
    if record is None:
        return False
    scope = norm(project_scope)
    if scope:
        return (
            norm(record.binding_kind) == BINDING_KIND_PROJECT_GATEWAY
            and norm(record.project_scope) == scope
            and not norm(record.issue_id)
        )
    return norm(record.issue_id) == norm(issue_id)


__all__ = ("record_matches_binding",)
