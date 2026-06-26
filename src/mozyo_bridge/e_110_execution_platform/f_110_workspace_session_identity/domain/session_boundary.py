"""Session boundary UX: candidate detection, next-session prompt, pane lifecycle.

Redmine #12122 (parent UserStory #12113). These are pure domain helpers so the
session-boundary UX is replayable from durable Redmine state plus repo /
execution root, **not** from pane scrollback or tmux window / session naming.

Three concerns, all pure (no tmux, git, or ticket-system I/O):

- :func:`classify_boundary` turns observed signals into a boundary assessment
  (is this a boundary, did a state-preservation signal fire, what should happen
  before crossing it).
- :func:`build_boundary_prompt` renders the compact, pasteable next-session
  prompt a Codex session hands the operator / next Codex session. It reuses the
  :class:`~mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff.ExecutionRoot` redaction contract so the
  prompt never carries a private absolute path (Redmine #12098 review j#59662;
  ``vibes/docs/rules/public-private-boundary.md``).
- :func:`decide_pane_lifecycle` is the guarded Claude-pane lifecycle decision
  (reuse / allocate new / orphan / guarded kill). It encodes the non-goal that a
  pane with unfinished durable state is never reset or killed, and that any
  kill / discard stays owner-approval gated.

See ``vibes/docs/logics/session-boundary.md`` for the model these helpers
encode.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import ExecutionRoot

# --- Boundary candidate signal vocabulary ----------------------------------
#
# Three families. The split matters because the families drive different
# actions, not just different labels:
#
# - SCOPE signals: the work context itself changed enough that the next turn /
#   session must re-anchor (issue, parent scope, version, repo root, execution
#   root, gate transition).
# - PRESSURE signals: the current session is degrading and a clean boundary is
#   cheaper now than after a forced compact (context pressure, compact event,
#   large tool output, pane ambiguity).
# - PRESERVATION signals: unfinished durable state exists. These never *forbid*
#   recording a boundary — they forbid resetting / killing across it until the
#   state is captured in a durable record (dirty diff, running process, pending
#   approval, unrecorded journal).
SCOPE_SIGNALS: tuple[str, ...] = (
    "active_issue_change",
    "parent_scope_change",
    "version_change",
    "repo_root_change",
    "execution_root_change",
    "gate_transition",
)
PRESSURE_SIGNALS: tuple[str, ...] = (
    "context_pressure",
    "compact_event",
    "large_tool_output",
    "pane_ambiguity",
)
PRESERVATION_SIGNALS: tuple[str, ...] = (
    "dirty_diff",
    "running_process",
    "pending_approval",
    "unrecorded_journal",
)
SESSION_BOUNDARY_SIGNALS: tuple[str, ...] = (
    SCOPE_SIGNALS + PRESSURE_SIGNALS + PRESERVATION_SIGNALS
)

NEXT_ACTORS: tuple[str, ...] = ("owner", "claude", "codex")


class SessionBoundaryError(ValueError):
    """Raised when boundary inputs are malformed (unknown signal, leaked path)."""


@dataclass(frozen=True)
class BoundaryAssessment:
    """Result of :func:`classify_boundary`.

    - ``is_boundary``: at least one scope or pressure signal fired, so the
      session should re-anchor / hand off rather than silently continue.
    - ``fired``: the recognised signals that fired, in canonical order.
    - ``preservation_required``: a preservation signal fired, so a durable
      record must capture the unfinished state **before** any reset / kill.
    - ``recommended_action``: a short, deterministic phrase the doc/runbook and
      the CLI both reference (no scrollback-derived wording).
    """

    is_boundary: bool
    fired: tuple[str, ...]
    preservation_required: bool
    recommended_action: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_boundary(signals: object) -> BoundaryAssessment:
    """Classify observed boundary signals into a :class:`BoundaryAssessment`.

    ``signals`` is any iterable of signal names. Unknown names raise
    :class:`SessionBoundaryError` rather than being silently dropped, so a
    typo in a runbook command surfaces instead of yielding a misleadingly
    "no boundary" answer. Order of the input does not matter; ``fired`` is
    returned in canonical :data:`SESSION_BOUNDARY_SIGNALS` order so the output
    is deterministic and diff-stable.
    """
    try:
        requested = list(signals)  # type: ignore[arg-type]
    except TypeError as exc:  # pragma: no cover - defensive
        raise SessionBoundaryError(f"signals must be iterable, got {signals!r}") from exc
    unknown = sorted({s for s in requested if s not in SESSION_BOUNDARY_SIGNALS})
    if unknown:
        raise SessionBoundaryError(
            "unknown boundary signal(s): "
            + ", ".join(unknown)
            + f"; expected one of {list(SESSION_BOUNDARY_SIGNALS)}"
        )
    present = set(requested)
    fired = tuple(s for s in SESSION_BOUNDARY_SIGNALS if s in present)
    scope_or_pressure = any(
        s in present for s in (SCOPE_SIGNALS + PRESSURE_SIGNALS)
    )
    preservation_required = any(s in present for s in PRESERVATION_SIGNALS)
    if not fired:
        action = "no boundary signal; continue the current session"
    elif preservation_required and not scope_or_pressure:
        # Only preservation signals fired: not itself a reason to switch
        # sessions, but the unfinished state must be recorded before any
        # reset / kill is even considered.
        action = (
            "record the unfinished state in a durable journal before any "
            "reset/kill; not a scope boundary on its own"
        )
    elif preservation_required:
        action = (
            "record a boundary journal capturing unfinished state, then hand "
            "off; do not reset/kill the pane until that state is durable"
        )
    else:
        action = "record a boundary journal and emit a next-session prompt"
    return BoundaryAssessment(
        is_boundary=scope_or_pressure,
        fired=fired,
        preservation_required=preservation_required,
        recommended_action=action,
    )


# --- Next-session boundary prompt ------------------------------------------


def _reject_absolute(value: str, field_name: str) -> str:
    """Guard a pasteable field against a leaked absolute / home path.

    The boundary prompt is meant to be pasted into the next Codex session and
    may end up quoted in a Redmine journal, so it must carry portable pointers
    only. A repo pointer is an identifier (canonical session name / workspace
    id), never a filesystem path; reject anything that smells like one so the
    redaction contract cannot be bypassed by a caller passing a raw path.
    """
    if value.startswith("/") or value.startswith("~") or value.startswith("\\"):
        raise SessionBoundaryError(
            f"{field_name} must be a portable identifier, not an absolute/home "
            f"path: {value!r}. Keep absolute paths in the structured (--json) "
            "output only (public-private-boundary)."
        )
    return value


@dataclass(frozen=True)
class BoundaryPrompt:
    """Inputs for :func:`build_boundary_prompt`.

    Carries exactly the fields the parent US (#12113) requires a next-session
    prompt to surface. ``repo_pointer`` is a **portable** identifier (canonical
    session name / workspace id), never an absolute path; the absolute repo
    root and execution-root workdir live only in the structured outcome.
    """

    issue: str
    journal: str
    repo_pointer: str
    parent_issue: Optional[str] = None
    commit: Optional[str] = None
    target_lane: Optional[str] = None
    execution_root: Optional[ExecutionRoot] = None
    gate_state: Optional[str] = None
    verification_state: Optional[str] = None
    residual_risks: tuple[str, ...] = ()
    pending_action: Optional[str] = None
    next_actor: Optional[str] = None
    signals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.issue or not self.journal:
            raise SessionBoundaryError(
                "boundary prompt requires both issue and journal (the durable "
                "anchor)"
            )
        _reject_absolute(self.repo_pointer, "repo_pointer")
        if self.next_actor is not None and self.next_actor not in NEXT_ACTORS:
            raise SessionBoundaryError(
                f"next_actor must be one of {list(NEXT_ACTORS)}, got "
                f"{self.next_actor!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # asdict already expanded execution_root (a dataclass) into a dict.
        return payload


def build_boundary_prompt(prompt: BoundaryPrompt) -> str:
    """Render the compact, pasteable next-session boundary prompt (markdown).

    The output leads with the same durable-anchor contract the handoff
    notification uses: the journal anchor is the source of truth and must be
    read from the ticket system before acting; everything below it is a pointer,
    not a new authority. No absolute path is ever emitted — the execution root
    uses the portable :meth:`ExecutionRoot.record_pointer` form, and the repo is
    referenced by its portable identifier.
    """
    anchor = f"#{prompt.issue} j#{prompt.journal}"
    lines: list[str] = [
        "## Next-session boundary prompt",
        "",
        (
            f"Resume from the durable anchor {anchor}; read it from the "
            "source-of-truth system before acting. Everything below is a "
            "pointer, not a new authority."
        ),
        "",
        f"- Ticket: `#{prompt.issue}`"
        + (f" (parent US `#{prompt.parent_issue}`)" if prompt.parent_issue else ""),
        f"- Durable anchor: `{anchor}`",
        f"- Repo: `{prompt.repo_pointer}` (portable identifier; resolve the "
        "checkout from the workspace registry, not from pane location)",
    ]
    if prompt.target_lane:
        lines.append(f"- Target lane: `{prompt.target_lane}`")
    if prompt.execution_root is not None:
        lines.append(
            f"- Execution root: {prompt.execution_root.record_pointer()}"
        )
    lines.append(f"- Commit: `{prompt.commit}`" if prompt.commit else "- Commit: none")
    lines.append(f"- Gate state: {prompt.gate_state or '—'}")
    lines.append(f"- Verification: {prompt.verification_state or '—'}")
    if prompt.residual_risks:
        lines.append("- Residual risks:")
        lines.extend(f"  - {risk}" for risk in prompt.residual_risks)
    else:
        lines.append("- Residual risks: none recorded")
    if prompt.pending_action:
        actor = f" ({prompt.next_actor})" if prompt.next_actor else ""
        lines.append(f"- Pending action{actor}: {prompt.pending_action}")
    elif prompt.next_actor:
        lines.append(f"- Next actor: {prompt.next_actor}")
    if prompt.signals:
        lines.append(
            "- Boundary signals: " + ", ".join(f"`{s}`" for s in prompt.signals)
        )
    return "\n".join(lines)


# --- Claude pane lifecycle decision ----------------------------------------

PANE_LIFECYCLE_DECISIONS: tuple[str, ...] = (
    "reuse",
    "new",
    "orphan",
    "guarded_kill",
    "blocked",
)
# What an operator / Codex may be *considering* doing with the existing Claude
# pane at a boundary. "kill" and "discard" are the destructive requests the
# guard protects.
PANE_LIFECYCLE_REQUESTS: tuple[str, ...] = (
    "reuse",
    "new",
    "orphan",
    "kill",
    "discard",
)


@dataclass(frozen=True)
class PaneLifecycleState:
    """Observed state feeding :func:`decide_pane_lifecycle`.

    ``requested`` is the action under consideration. The preservation booleans
    mirror :data:`PRESERVATION_SIGNALS`. ``owner_approved_kill`` records that an
    owner close/kill approval has been collected through the Codex window
    (never observed off a Claude pane).
    """

    requested: str = "new"
    same_lane: bool = False
    dirty_diff: bool = False
    running_process: bool = False
    pending_approval: bool = False
    unrecorded_journal: bool = False
    owner_approved_kill: bool = False

    def blockers(self) -> tuple[str, ...]:
        present = {
            "dirty_diff": self.dirty_diff,
            "running_process": self.running_process,
            "pending_approval": self.pending_approval,
            "unrecorded_journal": self.unrecorded_journal,
        }
        return tuple(s for s in PRESERVATION_SIGNALS if present[s])


@dataclass(frozen=True)
class PaneLifecycleDecision:
    """Result of :func:`decide_pane_lifecycle`."""

    decision: str
    blockers: tuple[str, ...] = field(default_factory=tuple)
    rationale: str = ""

    @property
    def is_blocked(self) -> bool:
        return self.decision == "blocked"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide_pane_lifecycle(state: PaneLifecycleState) -> PaneLifecycleDecision:
    """Decide the Claude pane lifecycle action under the guarded-kill contract.

    Rules (parent US #12113 acceptance criteria):

    - The default leans to **new** pane allocation. An empty / "new" request, or
      a "reuse" request against a pane from a different lane, resolves to a fresh
      pane so contexts do not bleed across lanes.
    - **reuse** is allowed only for a same-lane pane; preservation signals do not
      block reuse (reusing preserves the state).
    - **orphan** (stop managing the pane, leave it running) is always allowed; it
      is non-destructive and preserves any unfinished state.
    - **kill / discard** is destructive. It is *blocked* whenever any
      preservation signal is set (dirty diff, running process, pending approval,
      unrecorded journal) — these are never reset/killed across a boundary — and
      blocked when no owner kill approval has been collected. Only a clean,
      owner-approved request yields ``guarded_kill``.
    """
    requested = state.requested or "new"
    if requested not in PANE_LIFECYCLE_REQUESTS:
        raise SessionBoundaryError(
            f"requested must be one of {list(PANE_LIFECYCLE_REQUESTS)}, got "
            f"{requested!r}"
        )
    blockers = state.blockers()

    if requested in ("kill", "discard"):
        if blockers:
            return PaneLifecycleDecision(
                decision="blocked",
                blockers=blockers,
                rationale=(
                    "guarded cleanup: unfinished durable state present ("
                    + ", ".join(blockers)
                    + "). Record it in a durable journal and let it drain; do "
                    "not kill/discard the pane. Orphan it instead if the lane "
                    "must move on."
                ),
            )
        if not state.owner_approved_kill:
            return PaneLifecycleDecision(
                decision="blocked",
                blockers=(),
                rationale=(
                    "kill/discard is owner-approval gated. Collect owner "
                    "approval through the Codex window (never off a Claude "
                    "pane), record it in a durable journal, then retry."
                ),
            )
        return PaneLifecycleDecision(
            decision="guarded_kill",
            blockers=(),
            rationale=(
                "clean pane with owner kill approval recorded; guarded kill is "
                "permitted."
            ),
        )

    if requested == "orphan":
        return PaneLifecycleDecision(
            decision="orphan",
            blockers=blockers,
            rationale=(
                "orphan is non-destructive: the pane keeps running and keeps "
                "its state; stop managing it and re-anchor the lane elsewhere."
            ),
        )

    if requested == "reuse":
        if state.same_lane:
            return PaneLifecycleDecision(
                decision="reuse",
                blockers=blockers,
                rationale=(
                    "same-lane pane; reuse preserves its context. Re-anchor it "
                    "from the durable journal, not from scrollback."
                ),
            )
        return PaneLifecycleDecision(
            decision="new",
            blockers=blockers,
            rationale=(
                "requested reuse but the pane belongs to a different lane; "
                "allocate a new pane so lane contexts do not bleed together."
            ),
        )

    # requested == "new" (or defaulted): the safe default.
    return PaneLifecycleDecision(
        decision="new",
        blockers=blockers,
        rationale=(
            "default lean: allocate a fresh pane for the boundary and re-anchor "
            "it from the durable journal."
        ),
    )
