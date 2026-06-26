"""Delegated coordinator launch/adopt decision resolver (Redmine #12457).

US #12454 (`親子孫 delegated coordinator cockpit window UX`) splits the parent ->
delegated coordinator route into a durable policy shape (#12456) and the runtime
primitives that act on it (#12457 / #12458). This module is the #12457 runtime
**decision** primitive: it connects the policy shape defined by the design specs

- ``vibes/docs/specs/delegation-policy-project-config.md`` (launch/adopt knobs),
- ``vibes/docs/specs/delegated-coordinator-role-profile.md`` (role vocabulary),
- ``vibes/docs/specs/delegated-coordinator-decision-records.md`` (decision /
  callback record fields),

to a deterministic, fail-closed launch/adopt decision over the candidate panes
``mozyo-bridge agents targets`` discovers.

Boundaries, kept enforced in code (the #12457 acceptance invariants):

- **Pure decision, no side effects.** No tmux, no filesystem, no send. This
  module *decides*; the caller performs read-only discovery and, for an adopt
  outcome, the gated ``handoff send`` to the child Codex gateway. A launch
  outcome is a structured *decision to launch* — core is not a worktree manager
  (``coordinator-sublane-development-flow.md``), so it never spawns a lane here.
- **``agents targets`` is candidate discovery only, never routing authority.** A
  candidate becomes adoptable only after deterministic filtering by role,
  canonical repo identity, lane state, and uniqueness collapses the set to
  exactly one strong, non-ambiguous Codex gateway.
- **The route always lands at the child project's Codex gateway.** A ``claude``
  candidate is never selectable and :data:`ROLE_CLAUDE` may not be the
  ``required_role`` — this is the "no direct cross-lane / cross-project Claude
  send" invariant baked into the selector itself.
- **The target repo identity gate is mandatory.** Without a canonical child repo
  identity the decision fails closed: selecting a visible pane from layout alone
  would recreate the #12455 missing-context (PASS-B) violation.
- **Ambiguity fails closed.** ``disabled`` mode fails closed with an explicit
  missing-policy reason; zero matches launch only if the mode permits, else fail
  closed; more than one match, or any weak / ambiguous matched identity, fails
  closed with a candidate summary the operator / auditor can act on.
- **No window / session / title / display proximity is ever a selection
  signal.** Selection reads role, repo identity, lane id, and the resolver
  provenance (``confidence`` / ``ambiguous``) only.

The module is pure (dataclasses + a small validation helper) and imports no
application, infrastructure, or sibling domain code, so it has no import-time
dependency on tmux and stays trivially testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence


class DelegationLaunchAdoptError(ValueError):
    """A launch/adopt request is structurally invalid (bad mode / role / target).

    Inherits :class:`ValueError` for the same fail-closed semantics the sibling
    domain records use (``RepoLocalConfigError`` / ``RoleProfileError`` /
    ``ModuleRegistryError``). Raised for *caller* mistakes (an unknown mode, a
    ``claude`` ``required_role``, a callback-target shape that omits the required
    delegation parent). A *policy* fail-closed outcome (disabled / missing
    identity / ambiguity) is **not** an error — it is a first-class
    :class:`LaunchAdoptDecision` with ``outcome == OUTCOME_FAIL_CLOSED`` so the
    caller records it durably rather than crashing.
    """


# --- launch/adopt mode vocabulary (delegation-policy-project-config.md) --------

#: No delegated route formation. A context-free smoke should PASS-B with this as
#: the explicit missing-policy reason rather than silently forming a route.
LAUNCH_ADOPT_MODE_DISABLED = "disabled"
#: Adopt an existing visible child Codex gateway only when the deterministic
#: selection produces exactly one strong, non-ambiguous candidate.
LAUNCH_ADOPT_MODE_ADOPT_EXISTING = "adopt_existing"
#: Always launch a new delegated coordinator lane (the caller performs the
#: worktree / cockpit creation; this module only decides ``launch``).
LAUNCH_ADOPT_MODE_LAUNCH_NEW = "launch_new"
#: Prefer an exact durable-match adoption; if none exists, launch; if more than
#: one candidate matches, fail closed and record the ambiguity.
LAUNCH_ADOPT_MODE_LAUNCH_OR_ADOPT = "launch_or_adopt"

LAUNCH_ADOPT_MODES: frozenset[str] = frozenset(
    {
        LAUNCH_ADOPT_MODE_DISABLED,
        LAUNCH_ADOPT_MODE_ADOPT_EXISTING,
        LAUNCH_ADOPT_MODE_LAUNCH_NEW,
        LAUNCH_ADOPT_MODE_LAUNCH_OR_ADOPT,
    }
)

# --- decision outcomes --------------------------------------------------------

#: Adopt the single resolved child Codex gateway candidate.
OUTCOME_ADOPT = "adopt"
#: Launch a new delegated coordinator lane (no adoptable candidate / launch_new).
OUTCOME_LAUNCH = "launch"
#: Form no route; the ``reason`` names why (one of the ``REASON_*`` tokens).
OUTCOME_FAIL_CLOSED = "fail_closed"

# --- fail-closed reasons ------------------------------------------------------

#: ``disabled`` mode: delegation is turned off by policy.
REASON_DELEGATION_DISABLED = "delegation_disabled"
#: No canonical child repo identity supplied — the mandatory target repo gate.
REASON_MISSING_TARGET_REPO_IDENTITY = "missing_target_repo_identity"
#: ``adopt_existing`` with zero matching candidates and no launch permission.
REASON_NO_CANDIDATE = "no_candidate"
#: More than one candidate matched the deterministic filter (non-unique).
REASON_AMBIGUOUS_CANDIDATES = "ambiguous_candidates"
#: A matched candidate is weak / ambiguous, so the filtered set cannot be trusted
#: to be unique even when it currently holds one row.
REASON_UNSAFE_CANDIDATE_IDENTITY = "unsafe_candidate_identity"

# --- agent role tokens (mirror agent_discovery.AGENT_KIND_*) ------------------
# Kept as local literals so this pure decision module never imports
# ``agent_discovery`` (which pulls in the tmux infrastructure client at import).
ROLE_CODEX = "codex"
ROLE_CLAUDE = "claude"

#: The resolver provenance confidence that a candidate must carry to be
#: auto-selectable (mirrors ``agent_discovery.CONFIDENCE_STRONG``). A weak
#: (process-inferred) signal is never strong enough to adopt against.
CONFIDENCE_STRONG = "strong"

# --- callback target model (delegated-coordinator-decision-records.md §4) ------

#: Always-required callback to the parent coordinator route that retains parent
#: issue close / owner approval authority.
PURPOSE_DELEGATION_PARENT = "delegation_parent"
#: Child-project owning US coordinator (when distinct from the delegation parent).
PURPOSE_OWNING_US_COORDINATOR = "owning_us_coordinator"
#: Child-project audit coordinator (when distinct from the delegation parent).
PURPOSE_AUDIT_COORDINATOR = "audit_coordinator"

CALLBACK_TARGET_PURPOSES: frozenset[str] = frozenset(
    {
        PURPOSE_DELEGATION_PARENT,
        PURPOSE_OWNING_US_COORDINATOR,
        PURPOSE_AUDIT_COORDINATOR,
    }
)


@dataclass(frozen=True)
class CallbackTarget:
    """One purpose-tagged callback route anchor (decision-records §4.1).

    ``route`` is a durable anchor pointer (a Redmine route / lane pointer), never
    a private pane id or a window/title proximity hint. ``required`` marks a
    target whose callback outcome must be recorded before the delegated callback
    is complete.
    """

    purpose: str
    route: str
    required: bool = True

    def to_dict(self) -> dict[str, object]:
        return {"purpose": self.purpose, "route": self.route, "required": self.required}


def validate_callback_targets(
    targets: Optional[Iterable[CallbackTarget]],
) -> tuple[CallbackTarget, ...]:
    """Validate the delegated callback target set, failing closed on omission.

    Enforces the decision-records §4 fixed boundary that ``delegation_parent`` is
    always present and required with a non-empty route — the parent coordinator
    keeps parent issue close / owner approval authority, so a launch/adopt route
    that cannot call it back is invalid. ``owning_us_coordinator`` /
    ``audit_coordinator`` are not forced (they may legitimately be the same route
    as the parent), but an unknown purpose or an empty route fails closed so a
    required target is never silently dropped.

    Raises :class:`DelegationLaunchAdoptError` on any violation; returns the
    normalized tuple otherwise.
    """
    items = tuple(targets or ())
    seen_parent = False
    for target in items:
        if target.purpose not in CALLBACK_TARGET_PURPOSES:
            raise DelegationLaunchAdoptError(
                f"unknown callback target purpose {target.purpose!r}; expected one "
                f"of {sorted(CALLBACK_TARGET_PURPOSES)}"
            )
        if not (target.route or "").strip():
            raise DelegationLaunchAdoptError(
                f"callback target {target.purpose!r} must carry a non-empty durable "
                f"route anchor"
            )
        if target.purpose == PURPOSE_DELEGATION_PARENT and target.required:
            seen_parent = True
    if not seen_parent:
        raise DelegationLaunchAdoptError(
            "a required delegation_parent callback target is mandatory: the parent "
            "coordinator retains parent issue close / owner approval authority and "
            "every handoff-worthy state must be callbackable to it "
            "(delegated-coordinator-decision-records.md §4)."
        )
    return items


# --- candidate model ----------------------------------------------------------


@dataclass(frozen=True)
class DelegationCandidate:
    """A discovery candidate considered for delegated-coordinator adoption.

    A minimal, decoupled projection of a ``agents targets`` row
    (:class:`mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery.TargetCandidate`) carrying only
    the fields the deterministic selector reads: ``pane_id`` /  ``role`` (the
    routing identity), ``repo_root`` / ``workspace_id`` (the canonical identity
    gate inputs), ``lane_id`` / ``lane_label`` (lane state), and the resolver
    provenance (``confidence`` / ``ambiguous``) so a weak or ambiguous identity
    is visible rather than silently selected. ``session`` / ``window_name`` ride
    along for the audit summary only — they are display facts and never a
    selection signal.

    The application layer maps a ``TargetCandidate`` into this shape so the
    domain selector stays pure and the tests build it directly.
    """

    pane_id: str
    role: str
    repo_root: Optional[str]
    workspace_id: Optional[str] = None
    workspace_label: Optional[str] = None
    lane_id: str = ""
    lane_label: Optional[str] = None
    confidence: str = CONFIDENCE_STRONG
    ambiguous: bool = False
    session: str = ""
    window_name: str = ""

    @property
    def is_strong(self) -> bool:
        """True only for a strong, non-ambiguous identity (auto-select safe)."""
        return self.confidence == CONFIDENCE_STRONG and not self.ambiguous

    def summary_dict(self) -> dict[str, object]:
        """Audit-safe projection: identity columns only, no absolute-path leak."""
        return {
            "pane_id": self.pane_id,
            "role": self.role,
            "workspace_id": self.workspace_id,
            "workspace_label": self.workspace_label,
            "lane_id": self.lane_id or "default",
            "lane_label": self.lane_label,
            "repo_short": _repo_short(self.repo_root),
            "confidence": self.confidence,
            "ambiguous": self.ambiguous,
            "session": self.session,
        }


def _normalize_repo(path: Optional[str]) -> Optional[str]:
    """Normalize a repo-root string for identity comparison (trailing slash)."""
    if not path:
        return None
    return path.rstrip("/") or "/"


def _repo_short(path: Optional[str]) -> Optional[str]:
    """The checkout basename for compact / audit display (no absolute path)."""
    norm = _normalize_repo(path)
    if not norm:
        return None
    return norm.rsplit("/", 1)[-1] or norm


def repo_identity_matches(
    candidate_repo_root: Optional[str], target_repo_identity: Optional[str]
) -> bool:
    """Whether a candidate's repo root matches the canonical child repo identity.

    Exact normalized equality of the two resolved repo roots. The caller resolves
    the candidate's repo root from the pane cwd (``agent_discovery.infer_repo_root``)
    and supplies the canonical child repo identity from the explicit
    ``--target-repo`` gate, so this is a pure string comparison — it never walks
    the filesystem and never treats display proximity as identity. A missing
    repo root on either side is not a match (fail closed).
    """
    norm_candidate = _normalize_repo(candidate_repo_root)
    norm_target = _normalize_repo(target_repo_identity)
    if norm_candidate is None or norm_target is None:
        return False
    return norm_candidate == norm_target


# --- decision -----------------------------------------------------------------


@dataclass(frozen=True)
class LaunchAdoptDecision:
    """The resolved launch/adopt decision (Redmine #12457).

    ``outcome`` is one of :data:`OUTCOME_ADOPT` / :data:`OUTCOME_LAUNCH` /
    :data:`OUTCOME_FAIL_CLOSED`. For ``adopt`` :attr:`selected` is the single
    resolved child Codex gateway candidate; for ``launch`` / ``fail_closed`` it is
    ``None``. ``reason`` carries a ``REASON_*`` token only when fail-closed.
    :attr:`matched_candidates` is every candidate that passed the role + repo +
    lane filter (the audit summary the operator / auditor reads when the outcome
    is ambiguous), independent of how many were ultimately safe to select.
    """

    mode: str
    outcome: str
    required_role: str
    target_repo_identity: Optional[str]
    selected: Optional[DelegationCandidate] = None
    reason: Optional[str] = None
    child_project: Optional[str] = None
    matched_candidates: tuple[DelegationCandidate, ...] = ()

    @property
    def is_adopt(self) -> bool:
        return self.outcome == OUTCOME_ADOPT

    @property
    def is_launch(self) -> bool:
        return self.outcome == OUTCOME_LAUNCH

    @property
    def is_fail_closed(self) -> bool:
        return self.outcome == OUTCOME_FAIL_CLOSED

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "outcome": self.outcome,
            "reason": self.reason,
            "required_role": self.required_role,
            "target_repo_identity": self.target_repo_identity,
            "child_project": self.child_project,
            "selected": self.selected.summary_dict() if self.selected else None,
            "matched_candidates": [c.summary_dict() for c in self.matched_candidates],
        }


def resolve_launch_adopt(
    *,
    mode: str,
    candidates: Sequence[DelegationCandidate],
    target_repo_identity: Optional[str],
    required_role: str = ROLE_CODEX,
    excluded_lane_ids: Iterable[str] = (),
    child_project: Optional[str] = None,
) -> LaunchAdoptDecision:
    """Resolve a fail-closed launch/adopt decision over discovery candidates.

    The deterministic selection rule (delegation-policy-project-config.md +
    decision-records §2, and the j#63729 proposal ``### deterministic selection
    rule``):

    1. ``required_role`` must be the Codex gateway role — the route never lands
       directly at a child Claude. A ``claude`` ``required_role`` is a caller
       error (:class:`DelegationLaunchAdoptError`), not a fail-closed outcome.
    2. ``disabled`` mode forms no route and fails closed with
       :data:`REASON_DELEGATION_DISABLED`.
    3. The target repo identity gate is mandatory: a missing / blank
       ``target_repo_identity`` fails closed with
       :data:`REASON_MISSING_TARGET_REPO_IDENTITY` before any candidate is even
       matched (no canonical identity → no automatic selection).
    4. Filter candidates to those whose role is the Codex gateway, whose repo
       root matches the canonical child identity, and whose lane is not excluded.
    5. If any matched candidate is weak / ambiguous, fail closed with
       :data:`REASON_UNSAFE_CANDIDATE_IDENTITY` — a non-trustworthy identity in
       the matched set defeats the uniqueness guarantee.
    6. Apply the mode:
       - ``adopt_existing``: exactly one → adopt; zero →
         :data:`REASON_NO_CANDIDATE`; more than one →
         :data:`REASON_AMBIGUOUS_CANDIDATES`.
       - ``launch_new``: always ``launch`` (the explicit "create a new lane"
         choice); existing candidates are still surfaced in ``matched_candidates``
         for the audit record.
       - ``launch_or_adopt``: exactly one → adopt; zero → ``launch``; more than
         one → :data:`REASON_AMBIGUOUS_CANDIDATES`.

    Pure and deterministic over its inputs.
    """
    if mode not in LAUNCH_ADOPT_MODES:
        raise DelegationLaunchAdoptError(
            f"unknown launch_adopt_mode {mode!r}; expected one of "
            f"{sorted(LAUNCH_ADOPT_MODES)}"
        )
    if required_role != ROLE_CODEX:
        raise DelegationLaunchAdoptError(
            f"delegated coordinator route must land at the child Codex gateway; "
            f"required_role may not be {required_role!r} (no direct cross-lane / "
            f"cross-project Claude send)."
        )

    def _decision(
        outcome: str,
        *,
        selected: Optional[DelegationCandidate] = None,
        reason: Optional[str] = None,
        matched: tuple[DelegationCandidate, ...] = (),
    ) -> LaunchAdoptDecision:
        return LaunchAdoptDecision(
            mode=mode,
            outcome=outcome,
            required_role=required_role,
            target_repo_identity=(target_repo_identity or None),
            selected=selected,
            reason=reason,
            child_project=child_project,
            matched_candidates=matched,
        )

    if mode == LAUNCH_ADOPT_MODE_DISABLED:
        return _decision(OUTCOME_FAIL_CLOSED, reason=REASON_DELEGATION_DISABLED)

    if not (target_repo_identity or "").strip():
        # The mandatory identity gate: without a canonical child repo identity the
        # selector has no anchor and must not fall back to layout proximity.
        return _decision(
            OUTCOME_FAIL_CLOSED, reason=REASON_MISSING_TARGET_REPO_IDENTITY
        )

    excluded = {lane for lane in excluded_lane_ids if lane}
    matched = tuple(
        candidate
        for candidate in candidates
        if candidate.role == required_role
        and repo_identity_matches(candidate.repo_root, target_repo_identity)
        and candidate.lane_id not in excluded
    )

    unsafe = tuple(candidate for candidate in matched if not candidate.is_strong)
    if unsafe:
        return _decision(
            OUTCOME_FAIL_CLOSED,
            reason=REASON_UNSAFE_CANDIDATE_IDENTITY,
            matched=matched,
        )

    if mode == LAUNCH_ADOPT_MODE_LAUNCH_NEW:
        return _decision(OUTCOME_LAUNCH, matched=matched)

    if mode == LAUNCH_ADOPT_MODE_ADOPT_EXISTING:
        if len(matched) == 1:
            return _decision(OUTCOME_ADOPT, selected=matched[0], matched=matched)
        if not matched:
            return _decision(
                OUTCOME_FAIL_CLOSED, reason=REASON_NO_CANDIDATE, matched=matched
            )
        return _decision(
            OUTCOME_FAIL_CLOSED, reason=REASON_AMBIGUOUS_CANDIDATES, matched=matched
        )

    # LAUNCH_ADOPT_MODE_LAUNCH_OR_ADOPT
    if len(matched) == 1:
        return _decision(OUTCOME_ADOPT, selected=matched[0], matched=matched)
    if not matched:
        return _decision(OUTCOME_LAUNCH, matched=matched)
    return _decision(
        OUTCOME_FAIL_CLOSED, reason=REASON_AMBIGUOUS_CANDIDATES, matched=matched
    )


__all__ = (
    "DelegationLaunchAdoptError",
    "LAUNCH_ADOPT_MODE_DISABLED",
    "LAUNCH_ADOPT_MODE_ADOPT_EXISTING",
    "LAUNCH_ADOPT_MODE_LAUNCH_NEW",
    "LAUNCH_ADOPT_MODE_LAUNCH_OR_ADOPT",
    "LAUNCH_ADOPT_MODES",
    "OUTCOME_ADOPT",
    "OUTCOME_LAUNCH",
    "OUTCOME_FAIL_CLOSED",
    "REASON_DELEGATION_DISABLED",
    "REASON_MISSING_TARGET_REPO_IDENTITY",
    "REASON_NO_CANDIDATE",
    "REASON_AMBIGUOUS_CANDIDATES",
    "REASON_UNSAFE_CANDIDATE_IDENTITY",
    "ROLE_CODEX",
    "ROLE_CLAUDE",
    "CONFIDENCE_STRONG",
    "PURPOSE_DELEGATION_PARENT",
    "PURPOSE_OWNING_US_COORDINATOR",
    "PURPOSE_AUDIT_COORDINATOR",
    "CALLBACK_TARGET_PURPOSES",
    "CallbackTarget",
    "validate_callback_targets",
    "DelegationCandidate",
    "repo_identity_matches",
    "LaunchAdoptDecision",
    "resolve_launch_adopt",
)
