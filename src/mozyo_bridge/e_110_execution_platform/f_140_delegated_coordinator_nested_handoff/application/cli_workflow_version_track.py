"""CLI surface for ``workflow version-track`` — Version-scoped drain tracking (#15844).

``mozyo-bridge workflow version-track --version-id <id>`` is the read-only pass a project
coordinator runs to answer ADR-0011's drain question for a whole Redmine Version: *is any
sublane under this Version still owed a terminal state?*

It exists because no other surface can answer it. Every neighbouring enumeration starts
from the lane set (``reboot-audit`` from the lifecycle rows, ``drain-queue`` / ``glance``
from the active roster), and the one that does start from a Version —
``workflow dispatch-plan`` — projects onto ``open_leaf_issues`` because it answers the
dispatch question, so a left-behind lane sits on the closed side of that projection and
can never appear. The #15789 shape (issue closed, work integrated, lane still ``active``)
therefore falls through all of them at once.

Two authorities, and only two
-----------------------------

- the Version's issue set, via ``read_live_fixed_version_bucket`` (#13687) — which already
  owns the project double-resolution, the declared-host match, the cross-project Version
  guard and the confirmed-open gate;
- the lane lifecycle rows this repo's workspace owns, via
  ``load_lane_lifecycle_readonly`` scoped exactly as ``reboot-audit`` scopes them.

**Git and the live inventory are deliberately not read.** Those two axes decide *how to
recover* a lane, which is ``reboot-audit``'s job; they are not needed to decide *whether
something is owed*. Leaving them out means tracking still answers while herdr is down or a
worktree has vanished — so the lanes in the worst shape do not disappear from tracking
precisely when they most need to be in it.

Read-only by construction: no Redmine write, no lifecycle write, no pane, no handoff. Exit
0 whenever a snapshot was produced (a finding is not a command failure — a consumer loop
must be able to read a non-zero exit as "I could not look"), non-zero only when an
authority could not be read.

Design spec: ``vibes/docs/specs/version-owning-project-coordinator.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.version_lane_tracking import (  # noqa: E501
    TrackedLane,
    UnscopedLane,
    VersionTrackingSnapshot,
    build_version_tracking,
    is_terminal_lane_disposition,
    join_version_issues,
    render_version_tracking_text,
)


class VersionTrackingUnavailable(RuntimeError):
    """An AUTHORITY could not be read, so no snapshot exists (#15844).

    Distinct from "this Version has nothing owed", which is a legitimate empty result. An
    unresolvable workspace identity or an unreadable lifecycle store means tracking does
    not know what exists, and a tracker that cannot see is not a tracker that found
    nothing. Surfaced as a non-zero exit, unlike a per-issue ``unknown_issue_state``,
    which is a normal finding inside a snapshot that WAS produced.
    """


def _lanes_by_issue(
    records: Sequence[Any], workspace_id: str
) -> tuple[dict[str, list[TrackedLane]], list[Any]]:
    """Partition this workspace's lifecycle rows into ``(by issue, all rows)``."""
    mine = [r for r in records if getattr(r, "repo_workspace_id", "") == workspace_id]
    by_issue: dict[str, list[TrackedLane]] = {}
    for record in mine:
        issue_id = str(getattr(record, "issue_id", "") or "").strip()
        lane = TrackedLane(
            lane_id=str(getattr(record, "lane_id", "") or ""),
            lane_disposition=str(getattr(record, "lane_disposition", "") or ""),
        )
        by_issue.setdefault(issue_id, []).append(lane)
    for lanes in by_issue.values():
        lanes.sort(key=lambda lane: lane.lane_id)
    return by_issue, mine


def _unscoped_lanes(
    scoped_records: Sequence[Any], tracked_issue_ids: frozenset[str]
) -> tuple[UnscopedLane, ...]:
    """Non-terminal lanes outside the tracked Version, from ALREADY workspace-scoped rows.

    ``scoped_records`` is :func:`_lanes_by_issue`'s ``mine`` — the workspace filter is
    applied there, once. Re-filtering here would be a second copy of the scoping rule
    that no test could distinguish from its absence (measured: mutating it away left the
    whole suite green), and a guard nothing can measure is not a guard.

    Reported unconditionally, including when empty (design spec `## 3.2`): scoping to one
    Version creates a blind spot, and a blind spot nobody enumerates is where the #15789
    lane went. A lane carrying no issue id at all lands here too — it cannot be joined to
    any Version, so dropping it would be the same silent omission by another route.
    """
    out: list[UnscopedLane] = []
    for record in scoped_records:
        disposition = str(getattr(record, "lane_disposition", "") or "")
        if is_terminal_lane_disposition(disposition):
            continue
        issue_id = str(getattr(record, "issue_id", "") or "").strip()
        if issue_id and issue_id in tracked_issue_ids:
            continue
        out.append(
            UnscopedLane(
                lane_id=str(getattr(record, "lane_id", "") or ""),
                issue_id=issue_id,
                lane_disposition=disposition,
            )
        )
    return tuple(sorted(out, key=lambda lane: (lane.issue_id, lane.lane_id)))


def gather_version_tracking(
    repo_root: Path,
    *,
    version_id: Optional[str] = None,
    version_name: Optional[str] = None,
    home: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
    bucket: Optional[Any] = None,
    resolved_version: tuple[str, str] = ("", ""),
    lifecycle_rows: Optional[Sequence[Any]] = None,
) -> VersionTrackingSnapshot:
    """Join the Version's issues with this workspace's lane rows into one snapshot.

    ``bucket`` / ``resolved_version`` / ``lifecycle_rows`` are injectable so the join can
    be exercised without a network or a live store; production supplies none of them.

    Fails closed on an unreadable authority rather than degrading to an empty-looking
    snapshot: an unresolvable workspace identity would otherwise make the scoping filter
    match nothing and render as "this Version owns no lanes", which is indistinguishable
    from a Version that genuinely owns none — the exact fail-open ``reboot-audit`` review
    j#89191 finding 4 removed from the adjacent surface.
    """
    from mozyo_bridge.core.state.lane_lifecycle_readonly import (
        load_lane_lifecycle_readonly,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        repo_scope_workspace_id,
    )

    if bucket is None:
        bucket, resolved_version = _read_live_bucket(
            repo_root,
            version_id=version_id,
            version_name=version_name,
            home=home,
            environ=environ,
        )

    workspace_id = repo_scope_workspace_id(repo_root, home=home)
    if not workspace_id:
        raise VersionTrackingUnavailable(
            "the repo's workspace identity could not be resolved, so the lanes this repo "
            "owns cannot be determined. This is an unreadable authority, not an empty one"
        )

    records = (
        tuple(lifecycle_rows)
        if lifecycle_rows is not None
        else load_lane_lifecycle_readonly(home=home)
    )
    if records is None:
        # `load_lane_lifecycle_readonly` returns None for its fail-closed cases (an
        # unreadable / newer / malformed component schema) and () only for a genuinely
        # absent store. Folding the two together would report an unreadable lane
        # authority as "nothing is owed" — the worst possible direction for a surface
        # whose entire purpose is catching what was left behind.
        raise VersionTrackingUnavailable(
            "the lane lifecycle store could not be read (unreadable, or a newer / "
            "malformed component schema). No tracking snapshot can be produced; this is "
            "NOT the same as the store having no rows"
        )

    by_issue, mine = _lanes_by_issue(records, workspace_id)
    issues = getattr(bucket, "issues", ()) or ()
    facts = join_version_issues(issues, by_issue)
    tracked_ids = frozenset(fact.issue_id for fact in facts)
    return build_version_tracking(
        version_id=resolved_version[0],
        version_name=resolved_version[1],
        issues=facts,
        unscoped_lanes=_unscoped_lanes(mine, tracked_ids),
    )


def _read_live_bucket(
    repo_root: Path,
    *,
    version_id: Optional[str],
    version_name: Optional[str],
    home: Optional[Path],
    environ: Optional[Mapping[str, str]],
) -> tuple[Any, tuple[str, str]]:
    """The live, project-scoped, confirmed-open Version bucket (#13687), or fail closed.

    The confirmed-open gate is inherited, not relaxed. A Version being drained is by
    definition open; admitting a closed or locked one would mean tracking the leftovers of
    a Version whose close gate should never have passed them in the first place, and
    weakening the gate here would reopen it for ``dispatch-plan`` too.
    """
    from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.live_fixed_version_bucket import (  # noqa: E501
        read_live_fixed_version_bucket,
    )
    from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_version_issue_source import (  # noqa: E501
        RedmineVersionReadUnavailable,
    )

    try:
        live = read_live_fixed_version_bucket(
            repo_root=repo_root,
            bucket_id=(version_id or "").strip() or None,
            bucket_name=(version_name or "").strip() or None,
            environ=environ,
            home=home,
        )
    except RedmineVersionReadUnavailable as exc:
        # The f_120 read raises its own typed unavailability (credential / declared-host
        # mismatch / project / version-not-found / version-not-open / transport). That is
        # the same class of outcome this surface calls unavailable, so it is translated
        # rather than reclassified — the reason token travels with it.
        raise VersionTrackingUnavailable(
            f"{exc} (reason={getattr(exc, 'reason', 'unknown')})"
        ) from exc
    resolution = live.provider.resolve_bucket(live.version_id)
    if not resolution.resolved or resolution.bucket is None:
        skip = resolution.skip
        raise VersionTrackingUnavailable(
            "the Version bucket did not resolve: "
            f"{getattr(skip, 'reason', 'unknown')} "
            f"({getattr(skip, 'detail', '') or 'no detail'})"
        )
    return resolution.bucket, (live.version_id, live.version_name or "")


def cmd_workflow_version_track(args: argparse.Namespace) -> int:
    repo = (getattr(args, "repo", None) or "").strip()
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    version_id = (getattr(args, "version_id", "") or "").strip()
    version_name = (getattr(args, "version_name", "") or "").strip()
    as_json = bool(getattr(args, "as_json", False))

    if not version_id and not version_name:
        # A tracker with no Version is not a tracker of every Version; refusing beats
        # silently picking one.
        print(
            "workflow version-track: --version-id or --version-name is required",
            file=sys.stderr,
        )
        return 2

    try:
        snapshot = gather_version_tracking(
            repo_root,
            version_id=version_id or None,
            version_name=version_name or None,
        )
    except VersionTrackingUnavailable as exc:
        return _print_unavailable(str(exc), as_json=as_json)

    if as_json:
        print(
            json.dumps(
                snapshot.as_payload(), ensure_ascii=False, indent=2, sort_keys=True
            )
        )
    else:
        print(render_version_tracking_text(snapshot), file=sys.stdout)
    # Read-only: exit 0 whenever the snapshot itself was produced. Owed lanes are a
    # finding, not a command failure — a non-zero exit here would make the tracker
    # unusable in the very loop that is supposed to consume it.
    return 0


def _print_unavailable(detail: str, *, as_json: bool) -> int:
    payload = {
        "state": "unavailable",
        "detail": detail,
        "issues": [],
        "issue_count": 0,
        "unscoped_lanes": [],
        "unscoped_lane_count": 0,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"workflow version-track: unavailable\n  detail: {detail}", file=sys.stderr)
    return 1


def register_version_track(workflow_sub: Any) -> None:
    """Register ``workflow version-track`` onto the ``workflow`` subparser (#15844)."""
    parser = workflow_sub.add_parser(
        "version-track",
        description=(
            "Redmine #15844: READ-ONLY Version-scoped drain tracking. Joins a Redmine "
            "Version's whole issue set (open AND closed) with the lane lifecycle rows "
            "this repo's workspace owns, and returns a per-issue disposition: drain_owed "
            "(the issue is closed but a non-terminal lane still holds it — the #15789 "
            "shape) / in_flight / settled / lane_terminal_issue_open / umbrella_open / "
            "undispatched / unknown_issue_state. Unlike `workflow dispatch-plan`, which "
            "projects the same Version onto its open leaf issues to answer the dispatch "
            "question, this reads the closed side too — which is where a left-behind lane "
            "lives. Non-terminal lanes whose issue is outside this Version are always "
            "listed separately, so a Version-scoped pass never reads as a host-wide one. "
            "Names the lane and hands off to `sublane reboot-audit`; it does not choose a "
            "recovery rail and performs no drain. Writes nothing, sends nothing. Exits 0 "
            "whenever a snapshot was produced, non-zero only when an authority could not "
            "be read."
        ),
        help=(
            "Read-only: what is still owed a drain under one Redmine Version "
            "(closed issues included, so left-behind lanes are visible). Mutates nothing."
        ),
    )
    parser.add_argument(
        "--version-id",
        dest="version_id",
        default="",
        metavar="ID",
        help="The Redmine Version's numeric id (must be visible to this repo's project)",
    )
    parser.add_argument(
        "--version-name",
        dest="version_name",
        default="",
        metavar="NAME",
        help=(
            "Select the Version by name instead of id; an ambiguous name is refused, "
            "never guessed"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit exactly one structured envelope as JSON (pasteable into a journal)",
    )

    from mozyo_bridge.application.cli_common import add_repo_option

    add_repo_option(parser)
    parser.set_defaults(func=cmd_workflow_version_track)


__all__ = (
    "VersionTrackingUnavailable",
    "cmd_workflow_version_track",
    "gather_version_tracking",
    "register_version_track",
)
