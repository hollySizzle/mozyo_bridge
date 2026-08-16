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

    The published ``paths`` contract is *repo-relative*, and it is now enforced
    before the resolver sees anything (review j#102186 finding_3). An absolute or
    repo-escaping path is refused with a fixed reason token; previously it reached
    the resolver, whose ``ValueError`` named the server's own absolute repo root
    and was returned verbatim to the caller.
    """
    from mozyo_bridge.docs_tools import (
        CatalogContext,
        CatalogUnreadableError,
        OverlayError,
        resolve_paths_detailed,
    )
    from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.repo_path import (  # noqa: E501
        normalize_repo_relative_paths,
    )

    normalized = normalize_repo_relative_paths(list(arguments.get("paths", ()) or ()))
    if not normalized.ok:
        return ToolOutcome(
            payload={
                "error": "invalid_path",
                "rejected": [r.as_payload() for r in normalized.rejected],
                "resolutions": [],
                "overlay_applied": False,
            },
            is_error=True,
            summary=(
                f"{len(normalized.rejected)} path(s) are not repo-relative; "
                "`paths` must stay inside the repo"
            ),
        )

    include_local = bool(arguments.get("include_local", True))
    catalog_context = CatalogContext.build(
        context.repo_root, context.catalog_path, context.overlay_path
    )
    try:
        results, overlay = resolve_paths_detailed(
            catalog_context, list(normalized.accepted), include_local=include_local
        )
    except OverlayError:
        # Fixed reason, never the exception text: a catalog / overlay error message
        # routinely embeds the absolute path it failed on, and that path is the
        # server's, not the caller's.
        return ToolOutcome(
            payload={
                "error": "docs_overlay",
                "reason": "the local catalog overlay could not be read",
                "resolutions": [],
                "overlay_applied": False,
            },
            is_error=True,
            summary="the local catalog overlay could not be read",
        )
    except (CatalogUnreadableError, OSError, ValueError) as exc:
        # CatalogUnreadableError joined this clause when #15514 rewrote the reader
        # to raise it instead of the raw OSError / YAML errors it previously let
        # escape; without it the typed refusal raised straight through this tool.
        # Its message is value-free by that issue's contract, but this tool keeps
        # its own fixed reason regardless — one shape for every unreadable cause.
        return ToolOutcome(
            payload={
                "error": "docs_catalog",
                "reason": "the docs catalog could not be read",
                "exception": type(exc).__name__,
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
        summary=f"resolved governing docs for {len(normalized.accepted)} path(s)",
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

#: The `plan` fields this tool publishes (review j#103251 r4f3). The replayable
#: contract surface of a WorkflowStepOutcome, and nothing from its execution
#: wiring: `target_pane` / `self_pane` name live panes, `repo_root` is a private
#: filesystem path, `project_scope` is internal routing state, and `detail` is
#: resolver free text that may interpolate any of them. An allowlist — never a
#: denylist — so a field added to the outcome later stays private until someone
#: decides, in review, that it is public.
PLAN_PUBLIC_FIELDS = (
    "state",
    "next_action",
    "execution",
    "reason",
    "next_owner",
    "primitive",
    "durable_anchor",
    "caller_role",
    "callback_classification",
    "callback_to_role",
    "ok",
)


def _public_plan(payload: Mapping[str, Any]) -> dict:
    """The allowlisted projection of a step-outcome payload."""
    return {name: payload[name] for name in PLAN_PUBLIC_FIELDS if name in payload}


def run_workflow_step_plan(
    arguments: Mapping[str, Any], context: ReadPlanContext
) -> ToolOutcome:
    """Resolve — and only resolve — the next safe workflow step for this lane.

    The lane is resolved through the **shared** ``resolve_step_plan`` entry, which
    performs the same backend selection the CLI's ``workflow step`` performs
    (herdr-native resolution under ``terminal_transport.backend: herdr``, the tmux
    pane rail otherwise) and then runs that backend's resolver. Review j#102186
    finding_2 caught this handler calling the tmux rail unconditionally, which made
    it report ``lane_unresolved`` on a herdr-backed repo where the CLI resolves a
    real lane — a second, backend-blind state machine.

    Nothing is dispatched. Deliberately: the resolved outcome for an executable
    leg names a *primitive* that would deliver a handoff, and delivering it is a
    mutating operation with its own authority gates (#15152). Reporting the plan
    is read-only; running it is not, and this Feature's tools are read-only.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_step_plan_resolution import (  # noqa: E501
        LaneUnavailable,
        resolve_step_plan,
    )

    notes: list[str] = []
    anchor = _anchor_from(arguments, notes)
    try:
        resolution = resolve_step_plan(
            context.repo_root,
            anchor=anchor,
            issue=str(arguments.get("issue", "") or "").strip(),
            journal=str(arguments.get("journal", "") or "").strip(),
        )
    except LaneUnavailable as exc:
        return ToolOutcome(
            payload={
                "error": "lane_unresolved",
                "message": str(exc),
                "plan": {},
                "execution": EXECUTION_PLAN_ONLY,
                "executed": False,
                "source_health": _health(True, [str(exc)] + notes),
            },
            is_error=True,
            summary="the current lane could not be resolved from the live runtime",
        )

    # `resolution.outcome` is the SAFE outcome — the rail's result after the store
    # reconcile and the durable startup gate. Reporting the raw rail result here
    # was review j#102599 r3f1: it let this tool describe a forward step as safe
    # on a lane the CLI would have refused to step.
    #
    # And reporting it VERBATIM was review j#103251 r4f3: `as_payload()` carries
    # the execution wiring the CLI dispatches with — `target_pane` / `self_pane`
    # (pane identities), `repo_root` (a private filesystem path), `project_scope`
    # — and `detail` free text the resolver may have threaded any of those into.
    # This surface is a plan REPORT for an LLM client: it must name what to do
    # next, never where the server's panes and checkouts live. So the projection
    # is an explicit allowlist of the replayable contract fields, and everything
    # else — structured or free-text — is dropped rather than scrubbed.
    plan = _public_plan(resolution.outcome.as_payload())
    if resolution.reconciled is not None:
        # Already a public-safe projection by its own contract ("no pane id").
        plan.update(resolution.reconciled.reconcile_payload_fields())
    payload = {
        "plan": plan,
        "backend": resolution.backend,
        "execution": EXECUTION_PLAN_ONLY,
        "executed": False,
        # True when a safety composition changed what the rail alone resolved, so a
        # caller can see that a gate — not the lane's own state — decided this.
        "safety_gated": resolution.gated,
        "source_health": _health(bool(notes), notes),
    }
    return ToolOutcome(
        payload=payload,
        is_error=not bool(getattr(resolution.outcome, "ok", True)),
        summary=(
            f"next step resolved on the {resolution.backend} backend: "
            f"{plan.get('next_action') or plan.get('reason') or 'unknown'} "
            "(plan only; nothing was dispatched)"
        ),
    )


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
    "PLAN_PUBLIC_FIELDS",
    "ReadPlanContext",
    "ToolOutcome",
    "run_docs_resolve",
    "run_workflow_glance",
    "run_workflow_step_plan",
)
