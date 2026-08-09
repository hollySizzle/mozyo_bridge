"""The three read/plan tool handlers (Redmine #15161).

``docs_resolve`` / ``workflow_glance`` / ``workflow_step_plan``, each calling the
same in-process application processing the corresponding CLI command calls. None of
them shells out: there is no ``subprocess`` import in this module, no ``mozyo-bridge``
argv is composed anywhere, and no handler reads another command's stdout. The
CLI-parity tests assert that structurally rather than trusting this paragraph.

Each handler returns a :class:`ToolOutcome` — a structured payload plus an
``is_error`` flag — so a caller never parses prose to learn what happened. The
``source_health`` group on the two workflow tools carries ``degraded`` plus the
notes explaining *which* source was unreadable, so an empty projection is never
read as "nothing is active".

``workflow_step_plan`` is the plan half of ``workflow step`` and nothing else. It
runs the same pure ``resolve_workflow_step`` state machine and reports the
resolved outcome with ``execution="plan_only"``. It does not dispatch the
resolved primitive, deliver a handoff, or write a durable record — executing a
step stays on the CLI until #15152 connects durable authority verification to the
mutating tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

#: The value ``workflow_step_plan`` always reports for ``execution``. A fixed
#: token, so a consumer can assert "this surface never executed" rather than
#: inferring it from the absence of a dispatch record.
EXECUTION_PLAN_ONLY = "plan_only"


@dataclass(frozen=True)
class ToolOutcome:
    """One tool call's structured result.

    ``is_error`` marks a *tool execution* error (MCP reports these in the result
    with ``isError: true``, not as a JSON-RPC error) — a source that could not be
    read, a refusal the caller can act on. A protocol-level fault (unknown tool,
    schema violation) never reaches here; the dispatcher answers those with a
    JSON-RPC error.
    """

    payload: Mapping[str, Any]
    is_error: bool = False
    #: A short human-facing summary rendered into the result's text content. The
    #: structured payload stays authoritative; this is a label, not the answer.
    summary: str = ""


@dataclass(frozen=True)
class ReadPlanContext:
    """What the handlers need from their environment.

    ``repo_root`` is resolved once by the server from its launch directory rather
    than read per call, so a long-lived server cannot have its notion of "this
    repo" drift mid-session. ``catalog_path`` / ``overlay_path`` are optional
    explicit overrides for the docs catalog.
    """

    repo_root: Path
    catalog_path: Optional[str] = None
    overlay_path: Optional[str] = None
    #: Test seam: an explicit Redmine fixture path for the glance projection.
    redmine_fixture_path: Optional[str] = None
    #: Test seam: when False the glance builds no live Redmine adapter.
    redmine_live: bool = True
    #: Test seam: explicit store paths for the glance adapters.
    store_paths: Mapping[str, str] = field(default_factory=dict)


def _health(degraded: bool, notes) -> dict:
    return {"degraded": bool(degraded), "notes": [str(n) for n in notes]}


# --- docs_resolve ---------------------------------------------------------- #


def run_docs_resolve(
    arguments: Mapping[str, Any], context: ReadPlanContext
) -> ToolOutcome:
    """Resolve the governing documents for the requested repo-relative paths.

    Calls ``docs_tools.resolve_paths_detailed`` — the same resolver
    ``cmd_docs_resolve`` calls — and returns its structured results directly. The
    CLI's job on top of this is rendering (text / markdown / JSON) and printing a
    stderr overlay notice; both are presentation, so the API reports
    ``overlay_applied`` as a field instead.
    """
    from mozyo_bridge.docs_tools import (
        CatalogContext,
        OverlayError,
        resolve_paths_detailed,
    )

    paths = [str(p) for p in arguments.get("paths", ())]
    include_local = bool(arguments.get("include_local", True))
    catalog_context = CatalogContext.build(
        context.repo_root, context.catalog_path, context.overlay_path
    )
    try:
        results, overlay = resolve_paths_detailed(
            catalog_context, paths, include_local=include_local
        )
    except OverlayError as exc:
        return ToolOutcome(
            payload={
                "error": "docs_overlay",
                "message": str(exc),
                "resolutions": [],
                "overlay_applied": False,
            },
            is_error=True,
            summary="the local catalog overlay could not be read",
        )
    except (OSError, ValueError) as exc:
        return ToolOutcome(
            payload={
                "error": "docs_catalog",
                "message": f"{type(exc).__name__}: {exc}",
                "resolutions": [],
                "overlay_applied": False,
            },
            is_error=True,
            summary="the docs catalog could not be read",
        )
    return ToolOutcome(
        payload={
            "resolutions": list(results),
            "overlay_applied": bool(overlay.applied),
            "overlay_document_count": int(getattr(overlay, "document_count", 0) or 0),
        },
        summary=f"resolved governing docs for {len(paths)} path(s)",
    )


# --- workflow_glance ------------------------------------------------------- #


def run_workflow_glance(
    arguments: Mapping[str, Any], context: ReadPlanContext
) -> ToolOutcome:
    """Project the active lanes onto workflow state + next action + delivery anomaly.

    Uses the shared glance pipeline end to end: :func:`roster_for` for the
    repo-scoped roster, :func:`build_glance_sources` for the adapters (the same
    builder the CLI now calls, so both entries resolve the same stores),
    ``active_lane_snapshots`` for the fold input, and ``fold_glance_rows`` /
    ``glance_payload`` for the projection. No judgement is re-implemented here.

    The closed-issue partition is kept: a lane whose issue is already closed is
    coordinator debt, not active workflow, and it is reported in its own group
    rather than mixed into the rows a reader treats as "in flight".
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_snapshot_source import (  # noqa: E501
        active_lane_snapshots,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_source_wiring import (  # noqa: E501
        GlanceFixtureError,
        build_glance_sources,
        roster_for,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_glance import (  # noqa: E501
        fold_glance_rows,
        glance_payload,
    )

    issues = [str(i) for i in arguments.get("issues", ()) or ()]
    notes: list[str] = []
    degraded = False

    roster, roster_error = roster_for(issues, context.repo_root)
    if roster_error:
        degraded = True
        notes.append(str(roster_error))

    try:
        sources = build_glance_sources(
            store_path=context.store_paths.get("store"),
            reconcile_store_path=context.store_paths.get("reconcile_store"),
            ledger_path=context.store_paths.get("ledger"),
            redmine_fixture_path=context.redmine_fixture_path,
            redmine_live=context.redmine_live,
        )
    except GlanceFixtureError as exc:
        return ToolOutcome(
            payload={
                "error": "glance_source",
                "message": str(exc),
                "rows": [],
                "source_health": _health(True, [str(exc)]),
            },
            is_error=True,
            summary="a configured glance source could not be read",
        )

    collection = active_lane_snapshots(
        roster,
        redmine_source=sources.redmine_source,
        store=sources.store,
        ledger=sources.ledger,
        reconcile_store=sources.reconcile_store,
        authority_index=sources.index(),
    )
    notes.extend(str(note) for note in collection.notes)
    degraded = degraded or collection.degraded

    snapshots = list(collection.snapshots)
    active = [s for s in snapshots if s.signal.issue_open]
    closed_debt = [s for s in snapshots if not s.signal.issue_open]

    payload = glance_payload(fold_glance_rows(active), degraded=degraded, notes=tuple(notes))
    payload["closed_coordinator_debt"] = [
        {
            "issue": row.issue_id,
            "lane": row.lane,
            "workflow_state": row.workflow_state,
            "next_action": row.next_action,
            "next_owner": row.next_owner,
        }
        for row in fold_glance_rows(closed_debt)
    ]
    payload["source_health"] = _health(degraded, notes)
    return ToolOutcome(
        payload=payload,
        summary=(
            f"{len(active)} active lane(s), {len(closed_debt)} closed-issue debt row(s)"
            + (" (degraded: some sources unreadable)" if degraded else "")
        ),
    )


# --- workflow_step_plan ---------------------------------------------------- #


def run_workflow_step_plan(
    arguments: Mapping[str, Any], context: ReadPlanContext
) -> ToolOutcome:
    """Resolve — and only resolve — the next safe workflow step for this lane.

    The lane is resolved from the live runtime the same way ``workflow step``
    resolves it, then handed to the pure ``resolve_workflow_step`` state machine.
    The resolved outcome is reported with ``execution="plan_only"``.

    Nothing is dispatched. Deliberately: the resolved outcome for an executable
    leg names a *primitive* that would deliver a handoff, and delivering it is a
    mutating operation with its own authority gates (#15152). Reporting the plan
    is read-only; running it is not, and this Feature's tools are read-only.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (  # noqa: E501
        resolve_workflow_step,
    )

    notes: list[str] = []
    try:
        candidates, self_pane = _lane_candidates()
    except _LaneUnavailable as exc:
        return ToolOutcome(
            payload={
                "error": "lane_unresolved",
                "message": str(exc),
                "plan": {},
                "execution": EXECUTION_PLAN_ONLY,
                "source_health": _health(True, [str(exc)]),
            },
            is_error=True,
            summary="the current lane could not be resolved from the live runtime",
        )

    anchor = _anchor_from(arguments, notes)
    outcome = resolve_workflow_step(candidates, self_pane=self_pane, anchor=anchor)
    plan = outcome.as_payload()
    # The plan describes what *would* be safe next. Strip nothing, but state the
    # boundary in the payload so a reader cannot mistake a resolved executable leg
    # for a performed one.
    return ToolOutcome(
        payload={
            "plan": plan,
            "execution": EXECUTION_PLAN_ONLY,
            "executed": False,
            "source_health": _health(bool(notes), notes),
        },
        is_error=not bool(getattr(outcome, "ok", True)),
        summary=(
            f"next step resolved: {plan.get('next_action') or plan.get('reason') or 'unknown'} "
            "(plan only; nothing was dispatched)"
        ),
    )


class _LaneUnavailable(RuntimeError):
    """The live lane could not be resolved (no tmux runtime, no self pane)."""


def _lane_candidates():
    """Discover the lane's target candidates + this pane, or fail closed.

    Reads the live runtime through the same discovery the ``workflow step`` CLI
    uses. A server started outside a managed pane has no lane, and that is a
    refusal — never a default lane, which would resolve a step for somebody else's
    work.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
        cli_workflow,
    )

    try:
        cli_workflow.require_tmux()
        self_pane = cli_workflow.current_pane()
    except SystemExit as exc:
        raise _LaneUnavailable(
            f"no live terminal runtime for this server process ({exc})"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - a runtime read failure is a refusal, not a crash
        raise _LaneUnavailable(
            f"the current pane could not be resolved ({type(exc).__name__})"
        ) from exc
    if not self_pane:
        raise _LaneUnavailable("the current pane could not be resolved")
    try:
        candidates = cli_workflow._discover_candidates()
    except Exception as exc:  # noqa: BLE001
        raise _LaneUnavailable(
            f"lane candidates could not be discovered ({type(exc).__name__})"
        ) from exc
    return candidates, self_pane


def _anchor_from(arguments: Mapping[str, Any], notes: list):
    """Build the already-determined durable anchor from the arguments, or ``None``.

    Exactly the CLI's ``_anchor_from_args`` rule: ``issue`` carries the anchor and
    ``journal`` is optional. Deliberately *not* stricter — the shared-boundary
    invariant is that this entry adds no gate the CLI does not run, so requiring a
    journal here would make the same anchor acceptable via the CLI and refused via
    MCP. A ``journal`` supplied without an ``issue`` is not an anchor at all, and
    is reported as a note rather than silently dropped.
    """
    issue = str(arguments.get("issue", "") or "").strip()
    journal = str(arguments.get("journal", "") or "").strip()
    if not issue:
        if journal:
            notes.append("`journal` without `issue` is not an anchor; it was ignored")
        return None
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (  # noqa: E501
        WorkflowAnchor,
    )

    return WorkflowAnchor(issue=issue, journal=journal)


__all__ = (
    "EXECUTION_PLAN_ONLY",
    "ReadPlanContext",
    "ToolOutcome",
    "run_docs_resolve",
    "run_workflow_glance",
    "run_workflow_step_plan",
)
