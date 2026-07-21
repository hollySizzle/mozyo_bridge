"""Action-time projection of a provider startup blocker as an operator gate (#13812).

The read-only / dry-run half of #13762 (Design Answer j#78409, Task split #13762-A).
It turns a live receiver observation into a typed **projection**: either the positive
``operator_action_required`` gate (a durable :class:`OperatorStartupGate` to record),
or one of the fail-closed, **zero-write** dispositions (unreadable / unknown provider /
identity mismatch / newer or stale generation / ambiguous target / superseded / already
clear). It reuses #13760's pre-send classifier verbatim — it never re-implements startup
detection — and adds only the identity / generation stale判定 the durable gate needs.

Zero-write by construction (the #13812 boundary "default dry-run … DB/process/input/
outbox write 0"): :func:`project_operator_startup_gate` takes no store, no process
handle, and no outbox fence — only an injected read and already-resolved observation
facts — and RETURNS a value. There is no code path here that mutates anything. The
reserve / send that a cleared gate eventually drives is the resume tranche (#13813),
deliberately not wired here.

What each input owns (j#78409 責務分離 / action-time preflight):

- the **workspace / target identity** is resolved by the caller against the registry /
  workspace anchor and the live runtime, and handed in as a validated
  :class:`~...domain.operator_startup_gate.GateTarget` plus a resolution status. This
  module never derives identity from a saved projection, a pane title, or a readiness
  cache — it is given the action-time truth.
- the **startup state** is read once, at action time, through the caller's bound
  ``read_visible`` primitive and classified by #13760's
  :func:`evaluate_startup_admission`. Only the provider id and the fixed blocker id
  ever leave that classifier; the pane body never reaches this module.
- an optional **existing gate** is the durable record to re-project against; its pinned
  target and action generation are what a fresh observation is judged stale against.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (
    GateClassification,
    GateTarget,
    OperatorStartupGate,
    OriginalRequest,
    build_required_gate,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_admission import (
    ADMISSION_ADMITTED,
    ADMISSION_BLOCKED,
    ADMISSION_UNKNOWN_PROVIDER,
    ADMISSION_UNREADABLE,
    StartupAdmission,
    evaluate_startup_admission,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (
    AgentProviderProfileRegistry,
)

# ---------------------------------------------------------------------------
# Projection dispositions. Exactly one positive outcome projects a durable gate;
# every other outcome is a fail-closed, zero-write classification with a distinct
# token so a caller can report the accurate cause (j#78409 negative matrix).
# ---------------------------------------------------------------------------
#: A startup blocker is present and the target identity / generation are valid: the
#: projection carries a fresh ``required`` :class:`OperatorStartupGate` to record.
PROJECT_OPERATOR_ACTION_REQUIRED = "operator_action_required"
#: No startup blocker (the receiver is past its startup screens) and, when an existing
#: gate is supplied, the identity / generation still match: the receiver is admissible
#: and no operator action is required (j#78409 "already clear"). Zero-write.
PROJECT_STARTUP_CLEAR = "startup_clear"
#: The receiver's visible pane could not be read (or read blank) — its startup state is
#: unknown. Fail-closed, never treated as clear (#13760 invariant 4). Zero-write.
PROJECT_UNREADABLE = "unreadable"
#: The receiver resolved to a provider with no registered profile; its startup screens
#: cannot be classified. Fail-closed. Zero-write.
PROJECT_UNKNOWN_PROVIDER = "unknown_provider"
#: The runtime could not resolve a single well-formed target (no live identity). The
#: gate cannot be pinned to a blank target. Zero-write.
PROJECT_IDENTITY_UNRESOLVED = "identity_unresolved"
#: More than one live target matched — an ambiguous identity. Fail-closed rather than
#: guess which one the gate is about. Zero-write.
PROJECT_AMBIGUOUS_TARGET = "ambiguous_target"
#: An existing gate was supplied but the live observation names a DIFFERENT managed
#: target (workspace / repo / lane / role / name / provider). Stale; zero-write.
PROJECT_IDENTITY_MISMATCH = "identity_mismatch"
#: The live target matches the gate's identity but at a NEWER agent generation (the
#: worker was relaunched past the approved generation): the approval is stale, so the
#: gate is superseded. Zero-write (j#78409 "newer generation → stale approval").
PROJECT_NEWER_GENERATION = "newer_generation"
#: The live observation is at an OLDER generation than the gate's pinned target — a
#: stale snapshot that must not actuate the approved (newer) generation. Zero-write.
PROJECT_STALE_GENERATION = "stale_generation"
#: A re-projection was supplied an ``existing_gate`` but the caller's continuation
#: identity — ``gate_id`` / ``action_generation`` / ``original_request`` — does not
#: match it. A re-projection must CONTINUE the same gate under the same action
#: generation (the approval scope is ``one_target_one_action_generation``); a caller
#: presenting a different gate id / action generation / original request is trying to
#: re-bind an existing gate and fails closed before any read (review j#79003 Finding
#: 1). Zero-write.
PROJECT_GATE_BINDING_MISMATCH = "gate_binding_mismatch"

#: All recognized dispositions.
PROJECTION_DISPOSITIONS: frozenset[str] = frozenset(
    {
        PROJECT_OPERATOR_ACTION_REQUIRED,
        PROJECT_STARTUP_CLEAR,
        PROJECT_UNREADABLE,
        PROJECT_UNKNOWN_PROVIDER,
        PROJECT_IDENTITY_UNRESOLVED,
        PROJECT_AMBIGUOUS_TARGET,
        PROJECT_IDENTITY_MISMATCH,
        PROJECT_NEWER_GENERATION,
        PROJECT_STALE_GENERATION,
        PROJECT_GATE_BINDING_MISMATCH,
    }
)

# ---------------------------------------------------------------------------
# Resolution status of the caller's action-time target resolution. The caller
# resolves the managed target against the registry + live runtime and reports which
# of these three shapes it got; the projection never re-derives it.
# ---------------------------------------------------------------------------
#: Exactly one well-formed live target resolved (``target`` is present).
RESOLUTION_RESOLVED = "resolved"
#: More than one live candidate matched the target slot -> ambiguous.
RESOLUTION_AMBIGUOUS = "ambiguous"
#: No live target could be resolved (blank / gone / not well-formed).
RESOLUTION_UNRESOLVED = "unresolved"

_RESOLUTION_STATUSES: frozenset[str] = frozenset(
    {RESOLUTION_RESOLVED, RESOLUTION_AMBIGUOUS, RESOLUTION_UNRESOLVED}
)


class OperatorStartupProjectionError(ValueError):
    """A projection was called with an incoherent action-time observation.

    Distinct from a fail-closed *disposition* (which is a legitimate, returned
    result): this is raised only for a caller contract violation — a resolution
    status the projection does not recognize, or a ``resolved`` status with no target.
    """


@dataclass(frozen=True)
class ObservedStartupTarget:
    """The caller's action-time target resolution, handed to the projection.

    ``resolution`` is one of :data:`RESOLUTION_RESOLVED` / :data:`RESOLUTION_AMBIGUOUS`
    / :data:`RESOLUTION_UNRESOLVED`, reflecting what the registry + live-runtime
    resolution produced. ``target`` is the validated live
    :class:`~...domain.operator_startup_gate.GateTarget` — present iff
    ``resolution == RESOLUTION_RESOLVED``. Keeping resolution explicit (rather than
    inferring it from ``target is None``) lets the projection tell "ambiguous" from
    "gone", which are different fail-closed causes.
    """

    resolution: str
    target: Optional[GateTarget] = None

    def __post_init__(self) -> None:
        if self.resolution not in _RESOLUTION_STATUSES:
            raise OperatorStartupProjectionError(
                f"observed target resolution {self.resolution!r} is not recognized; "
                f"allowed: {sorted(_RESOLUTION_STATUSES)}"
            )
        if self.resolution == RESOLUTION_RESOLVED and not isinstance(
            self.target, GateTarget
        ):
            raise OperatorStartupProjectionError(
                "a resolved observation must carry a well-formed GateTarget"
            )
        if self.resolution != RESOLUTION_RESOLVED and self.target is not None:
            raise OperatorStartupProjectionError(
                f"a {self.resolution!r} observation must not carry a target"
            )


@dataclass(frozen=True)
class OperatorStartupGateProjection:
    """The typed, zero-write result of one action-time projection.

    ``disposition`` is the sole authority (a member of
    :data:`PROJECTION_DISPOSITIONS`). ``gate`` is the projected ``required``
    :class:`OperatorStartupGate` — present **only** for
    :data:`PROJECT_OPERATOR_ACTION_REQUIRED`. ``admission`` is #13760's underlying
    :class:`StartupAdmission` when the classifier ran (absent when the projection
    short-circuited on identity / generation before reading the pane).

    ``detail`` is a **human-only** one-line explanation for logs / exception text; it
    is deliberately NOT part of any pasteable / machine-readable surface. The whole
    point of a projection is a pasteable durable record, and a free-text field cannot
    be guaranteed path/secret-safe the way closed tokens can, so
    :meth:`to_telemetry_dict` emits only the closed ``disposition`` token, the
    fixed-token admission, and the path-safe gate — never ``detail`` (review j#79003
    Finding 4). The built-in projector's details are fixed prose, but the telemetry
    contract does not depend on that.
    """

    disposition: str
    gate: Optional[OperatorStartupGate] = None
    admission: Optional[StartupAdmission] = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.disposition not in PROJECTION_DISPOSITIONS:
            raise OperatorStartupProjectionError(
                f"projection disposition {self.disposition!r} is not recognized; "
                f"allowed: {sorted(PROJECTION_DISPOSITIONS)}"
            )
        if self.disposition == PROJECT_OPERATOR_ACTION_REQUIRED and self.gate is None:
            raise OperatorStartupProjectionError(
                "an operator_action_required projection must carry the required gate"
            )
        if self.disposition != PROJECT_OPERATOR_ACTION_REQUIRED and self.gate is not None:
            raise OperatorStartupProjectionError(
                f"a {self.disposition!r} projection must not carry a gate"
            )

    @property
    def requires_operator_action(self) -> bool:
        """True only when a durable ``required`` gate was projected."""
        return self.disposition == PROJECT_OPERATOR_ACTION_REQUIRED

    def to_telemetry_dict(self) -> dict:
        """Fixed tokens only — no pane text, no paths, no free-form ``detail``.

        Durable-record safe by construction: only the closed ``disposition`` token,
        the fixed-token admission, and the path-safe gate projection. ``detail`` is
        human-only and never enters this pasteable surface (review j#79003 Finding 4).
        """
        record: dict = {"disposition": self.disposition}
        if self.admission is not None:
            record["startup_admission"] = self.admission.to_telemetry_dict()
        if self.gate is not None:
            record["gate"] = self.gate.public_projection()
        return record


def project_operator_startup_gate(
    *,
    observed: ObservedStartupTarget,
    read_visible: Callable[[], object],
    original_request: OriginalRequest,
    gate_id: str,
    action_generation: int,
    profile_version: str,
    classifier_version: str,
    observed_at: str,
    existing_gate: Optional[OperatorStartupGate] = None,
    registry: Optional[AgentProviderProfileRegistry] = None,
) -> OperatorStartupGateProjection:
    """Project the receiver's live startup state as an operator gate (zero-write).

    Order of judgment, each step fail-closed before the next:

    1. **Identity resolution.** An ``ambiguous`` observation yields
       :data:`PROJECT_AMBIGUOUS_TARGET`; an ``unresolved`` one yields
       :data:`PROJECT_IDENTITY_UNRESOLVED`. A gate cannot be pinned to a blank or
       ambiguous target.
    2. **Gate-binding fence (re-projection).** When ``existing_gate`` is supplied, the
       caller must be CONTINUING that gate: the ``gate_id`` / ``action_generation`` /
       ``original_request`` passed in must equal the existing gate's. A divergence is a
       caller trying to re-bind an existing gate to a different action generation — a
       violation of the ``one_target_one_action_generation`` approval scope — and
       yields :data:`PROJECT_GATE_BINDING_MISMATCH` before any read (review j#79003
       Finding 1).
    3. **Stale判定 against an existing gate.** The live target is compared to the gate's
       pin: a different managed identity is :data:`PROJECT_IDENTITY_MISMATCH`; a newer
       generation is :data:`PROJECT_NEWER_GENERATION` (the gate is superseded); an older
       generation is :data:`PROJECT_STALE_GENERATION`.
    4. **Startup classification.** #13760's :func:`evaluate_startup_admission` reads the
       pane once and classifies it: admitted -> :data:`PROJECT_STARTUP_CLEAR`; blocked
       -> :data:`PROJECT_OPERATOR_ACTION_REQUIRED` with a fresh ``required`` gate pinned
       to the live target and the matched blocker id; unreadable ->
       :data:`PROJECT_UNREADABLE`; unknown provider -> :data:`PROJECT_UNKNOWN_PROVIDER`.

    The ``read_visible`` primitive is invoked at most once (only when steps 1–3 pass),
    exactly as #13760's pre-send gate reads it. Nothing here writes: the function's
    output is the projection, and the caller decides whether to durably record it — the
    record itself is the resume tranche's (#13813) to reserve and send against.
    """
    # 1. The identity must resolve to exactly one well-formed live target.
    if observed.resolution == RESOLUTION_AMBIGUOUS:
        return OperatorStartupGateProjection(
            disposition=PROJECT_AMBIGUOUS_TARGET,
            detail=(
                "more than one live target matched the managed identity slot; fail "
                "closed rather than pin a gate to an ambiguous target"
            ),
        )
    if observed.resolution == RESOLUTION_UNRESOLVED or observed.target is None:
        return OperatorStartupGateProjection(
            disposition=PROJECT_IDENTITY_UNRESOLVED,
            detail=(
                "no live target resolved from the registry or runtime; a gate cannot "
                "be pinned to a blank target"
            ),
        )
    live_target: GateTarget = observed.target

    if existing_gate is not None:
        # 2. Gate-binding fence: a re-projection continues the SAME gate under the SAME
        #    action generation. The approval scope is one_target_one_action_generation,
        #    so a caller presenting a different gate id / action generation / original
        #    request is re-binding an existing gate and fails closed BEFORE any read —
        #    otherwise the blocked branch below would mint a fresh gate at the new
        #    action generation, silently defeating the fence (review j#79003 Finding 1).
        if (
            gate_id != existing_gate.gate_id
            or action_generation != existing_gate.action_generation
            or original_request != existing_gate.original_request
        ):
            return OperatorStartupGateProjection(
                disposition=PROJECT_GATE_BINDING_MISMATCH,
                detail=(
                    "re-projection identity (gate id / action generation / original "
                    "request) does not match the existing gate; a re-projection must "
                    "continue the same gate under the same action generation"
                ),
            )

        # 3. Stale判定: the gate is honored only against a live target that matches its
        #    pinned identity AND its exact agent generation.
        pinned = existing_gate.target
        if not live_target.same_identity(pinned):
            return OperatorStartupGateProjection(
                disposition=PROJECT_IDENTITY_MISMATCH,
                detail=(
                    "live target names a different managed identity than the gate's "
                    "pinned target; the approval does not apply"
                ),
            )
        if live_target.agent_generation > pinned.agent_generation:
            return OperatorStartupGateProjection(
                disposition=PROJECT_NEWER_GENERATION,
                detail=(
                    f"live agent generation {live_target.agent_generation} is newer "
                    f"than the gate's approved generation {pinned.agent_generation}; "
                    "the approval is stale and the gate is superseded"
                ),
            )
        if live_target.agent_generation < pinned.agent_generation:
            return OperatorStartupGateProjection(
                disposition=PROJECT_STALE_GENERATION,
                detail=(
                    f"live agent generation {live_target.agent_generation} is older "
                    f"than the gate's approved generation {pinned.agent_generation}; a "
                    "stale snapshot must not actuate the approved generation"
                ),
            )

    # 4. Read the pane once and classify with #13760's shared, fail-closed evaluator.
    admission = evaluate_startup_admission(
        provider_id=live_target.provider_id,
        read_visible=read_visible,
        registry=registry,
    )
    if admission.outcome == ADMISSION_ADMITTED:
        return OperatorStartupGateProjection(
            disposition=PROJECT_STARTUP_CLEAR,
            admission=admission,
            detail=(
                f"receiver provider {admission.provider_id} shows no startup blocker; "
                "it is admissible and no operator action is required"
            ),
        )
    if admission.outcome == ADMISSION_BLOCKED:
        gate = build_required_gate(
            gate_id=gate_id,
            action_generation=action_generation,
            original_request=original_request,
            target=live_target,
            classification=GateClassification(
                blocker_id=admission.blocker_id,
                profile_version=profile_version,
                classifier_version=classifier_version,
                observed_at=observed_at,
            ),
        )
        return OperatorStartupGateProjection(
            disposition=PROJECT_OPERATOR_ACTION_REQUIRED,
            gate=gate,
            admission=admission,
            detail=(
                f"receiver provider {admission.provider_id} is showing the "
                f"{admission.blocker_id} startup screen; an operator UI action is "
                "required before the original request can resume"
            ),
        )
    if admission.outcome == ADMISSION_UNREADABLE:
        return OperatorStartupGateProjection(
            disposition=PROJECT_UNREADABLE,
            admission=admission,
            detail=(
                "the receiver's visible pane could not be read; its startup state is "
                "unknown and is never treated as clear"
            ),
        )
    # ADMISSION_UNKNOWN_PROVIDER is the only remaining outcome; keep the mapping total.
    if admission.outcome == ADMISSION_UNKNOWN_PROVIDER:
        return OperatorStartupGateProjection(
            disposition=PROJECT_UNKNOWN_PROVIDER,
            admission=admission,
            detail=(
                f"receiver resolved to provider {admission.provider_id!r}, which has "
                "no registered profile; its startup screens cannot be classified"
            ),
        )
    raise OperatorStartupProjectionError(  # pragma: no cover - defensive, mapping is total
        f"unhandled startup admission outcome {admission.outcome!r}"
    )


__all__ = (
    "PROJECTION_DISPOSITIONS",
    "PROJECT_AMBIGUOUS_TARGET",
    "PROJECT_GATE_BINDING_MISMATCH",
    "PROJECT_IDENTITY_MISMATCH",
    "PROJECT_IDENTITY_UNRESOLVED",
    "PROJECT_NEWER_GENERATION",
    "PROJECT_OPERATOR_ACTION_REQUIRED",
    "PROJECT_STALE_GENERATION",
    "PROJECT_STARTUP_CLEAR",
    "PROJECT_UNKNOWN_PROVIDER",
    "PROJECT_UNREADABLE",
    "RESOLUTION_AMBIGUOUS",
    "RESOLUTION_RESOLVED",
    "RESOLUTION_UNRESOLVED",
    "ObservedStartupTarget",
    "OperatorStartupGateProjection",
    "OperatorStartupProjectionError",
    "project_operator_startup_gate",
)
