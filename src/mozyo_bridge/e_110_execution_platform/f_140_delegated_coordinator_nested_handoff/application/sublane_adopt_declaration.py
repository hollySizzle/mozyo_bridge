"""Standard live-adopt owner-row backfill (Redmine #13809 / #13810 F1).

The standard live-adopt path (``sublane create --no-dispatch --execute`` onto a live
gateway+worker pair) skips ``append_lane_column``, so it never reached the create-path
lifecycle declaration and the adopted lane stayed **owner-rowless** — the measured
``original_identity_unknown`` that blocks ``sublane hibernate`` (#13809).

This module is the fail-closed gate + declaration for that path, extracted from
:class:`...sublane_actuator_herdr_ops.HerdrSublaneActuatorOps` so the ops adapter stays a
cohesive, under-threshold unit. The ops adapter hands raw ``agent list`` rows plus the
resolved ``(workspace_id, lane_id)`` unit; this module does the fail-closed gate over the
**raw** inventory and declares the owner binding through the common
:class:`...lane_declaration.LaneDeclarationStore.declare_lane`.

Fail-closed gate (Redmine #13810 R3-F1 / R3-F2, review j#78890):

- **raw candidate multiplicity** — each expected provider slot must resolve to EXACTLY ONE
  live candidate. A duplicate ``mzb1`` name (two rows decoding to one slot) is not
  collapsed "first wins"; it is an ambiguous target and fails closed. ``herdr-native-identity.md``
  §2/§3: a duplicate assigned name is a fail-closed condition.
- **liveness** — the single candidate must be :data:`SLOT_LIVE`; a locator-bearing stale
  shell residue (:data:`SLOT_STALE`) is never adopted.
- **startup self-attestation** — each slot must join its startup self-attestation as
  :data:`ATTEST_OK` (present, and generation-bound to the live locator). Absent / stale /
  missing / conflict is zero-write. ``herdr-native-identity.md`` §5: adopt requires a
  present self-attestation generation-bound to the live locator.

Only when every slot passes are typed :class:`ProcessGenerationPin` s built from the exact
live evidence (role / provider / assigned_name / **locator**) and stored as
``declared_slots`` — so a recycled generation (different live locators) is NOT an idempotent
duplicate. ``runtime_revision`` is left empty: the herdr generation discriminant is the
locator (the attestation store deliberately records NO runtime version), so a runtime
revision is never fabricated. No process is closed/relaunched and no route is mutated.

Legacy-owner residual (Redmine #13809 j#78944 / j#78945): a **pre-#13754** owner row is
already ``active`` and already owns the issue, but its ``worktree_identity`` is empty, so
``declare_lane`` reads the gate-verified live worktree as a *divergent* re-declare and
refuses — leaving ``retire --execute`` permanently blocked on ``worktree_binding_unverified``.
When declare_lane refuses, this module attempts the bounded
:meth:`...lane_declaration.LaneDeclarationStore.backfill_active_binding` CAS, which fills
**only** that one empty binding field (and the empty ``declared_slots`` snapshot) on the row
the lane already owns, reported as :data:`ADOPT_DECL_BACKFILLED`. A non-empty mismatch, a
different / non-active / project-gateway row, a recycled generation, or a revision race is
zero-write — declare_lane's "a divergent re-declare must not overwrite" is preserved, not
generally relaxed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.core.state.herdr_identity_attestation import (
    ATTEST_OK,
    HerdrIdentityAttestationStore,
    evaluate_attestation,
)
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore
from mozyo_bridge.core.state.lane_lifecycle import (
    BINDING_KIND_ISSUE,
    DecisionPointer,
    DecisionPointerError,
    LaneLifecycleError,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ProcessGenerationPin,
    ProcessPinError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
    GATEWAY_ROLE,
    WORKER_ROLE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    _agent_locator,
    _norm,
    _norm_lane,
    decode_assigned_name,
    derive_directory_lane_token,
    derive_lane_workspace_token,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E501
    SLOT_LIVE,
    classify_named_slot,
)

# -- outcome vocabulary (a status token, never an exception across the boundary) ---------

ADOPT_DECL_NOT_ADOPTED = "not_adopted"
#: The ops adapter could not read the live inventory / resolve the provider pair (herdr
#: down, unconfigured binary, unbound provider): unreadable -> zero-write, owner-unbound.
ADOPT_DECL_UNREADABLE = "unreadable_inventory"
ADOPT_DECL_NO_ANCHOR = "no_exact_anchor"
ADOPT_DECL_UNRESOLVED_UNIT = "unresolved_unit"
ADOPT_DECL_INCOMPLETE_PAIR = "incomplete_live_pair"
ADOPT_DECL_DUPLICATE_CANDIDATES = "duplicate_live_candidates"
ADOPT_DECL_STALE_SLOT = "stale_named_slot"
ADOPT_DECL_AMBIGUOUS_LOCATORS = "ambiguous_locators"
ADOPT_DECL_UNATTESTED = "unattested_slot"
ADOPT_DECL_BAD_TOKEN = "unresolvable_worktree_token"
ADOPT_DECL_BAD_ANCHOR = "unusable_decision_anchor"
#: The declaration was applied (a fresh owner row, or an idempotent exact-duplicate adopt).
ADOPT_DECL_DECLARED = "declared"
#: An EXISTING ``active`` legacy owner row (already owns the issue) whose ``worktree_identity``
#: was empty had its missing worktree binding + typed pins filled by the bounded backfill CAS
#: (Redmine #13809 residual j#78944 / j#78945). Owner-bound and safe to dispatch, like
#: :data:`ADOPT_DECL_DECLARED`, but reported distinctly so the pre-existing-incomplete-row
#: correction is regressed apart from the rowless declaration.
ADOPT_DECL_BACKFILLED = "backfilled"
#: THIS adopt could not (re-)declare (a gate failure, or a divergent re-declare), BUT the
#: lane is ALREADY the active owner of its issue — a prior create / adopt bound it, so the
#: lane is owner-bound and safe to dispatch (Redmine #13810 R3-F3). Only a genuinely
#: owner-unbound lane (no prior binding) fails closed.
ADOPT_DECL_ALREADY_OWNED = "already_owned"
#: The common declaration service refused (another lane owns the issue, or a divergent
#: binding — e.g. a recycled generation whose live pins differ — already exists): a
#: legitimate zero-write, not a store error.
ADOPT_DECL_OWNER_CONFLICT = "owner_conflict"
ADOPT_DECL_DECLARE_ERROR = "declare_error"

#: The outcomes that BLOCK the adopted lane before dispatch (Redmine #13810 R3-F3 / R4-F3):
#: the adopt did not end owner-bound — the owner declaration was refused by a fail-closed
#: condition (an ambiguous / stale / unattested / recycled live pair, an owner conflict, a
#: store error) OR the unit could not be read / resolved (``unreadable`` / ``unresolved_unit``,
#: an inventory that failed AFTER the lane was confirmed) — AND the lane is not already the
#: active owner (an ``already_owned`` re-check that reads the state DB, never inference). So
#: dispatching would report a false success while the ``original_identity_unknown`` hibernate
#: blocker (#13809) stays in place. Every OTHER outcome proceeds: a fresh / idempotent
#: ``declared``, an ``already_owned`` lane (a prior create / adopt bound it), a non-gated
#: ``not_adopted`` create, or the owner-unbound-BY-DESIGN ``no_exact_anchor`` (a journal-less
#: adopt, which the create path also declares nothing for — blocking it would refuse
#: legitimate journal-less create/adopt flows, not close the #13809 gap).
ADOPT_DECL_OWNER_UNBOUND = frozenset(
    {
        ADOPT_DECL_UNREADABLE,
        ADOPT_DECL_UNRESOLVED_UNIT,
        ADOPT_DECL_INCOMPLETE_PAIR,
        ADOPT_DECL_DUPLICATE_CANDIDATES,
        ADOPT_DECL_STALE_SLOT,
        ADOPT_DECL_AMBIGUOUS_LOCATORS,
        ADOPT_DECL_UNATTESTED,
        ADOPT_DECL_BAD_TOKEN,
        ADOPT_DECL_BAD_ANCHOR,
        ADOPT_DECL_OWNER_CONFLICT,
        ADOPT_DECL_DECLARE_ERROR,
    }
)

#: The zero-write outcomes: no owner row was written (fail-closed), for any reason other
#: than a successful declaration (``declared`` / ``already_owned``) or a ``declare_error``
#: store failure surfaced to the caller. ``unreadable`` is included: an inventory that could
#: not be read wrote no owner row.
ADOPT_DECL_ZERO_WRITE = frozenset(
    {
        ADOPT_DECL_NO_ANCHOR,
        ADOPT_DECL_UNREADABLE,
        ADOPT_DECL_UNRESOLVED_UNIT,
        ADOPT_DECL_INCOMPLETE_PAIR,
        ADOPT_DECL_DUPLICATE_CANDIDATES,
        ADOPT_DECL_STALE_SLOT,
        ADOPT_DECL_AMBIGUOUS_LOCATORS,
        ADOPT_DECL_UNATTESTED,
        ADOPT_DECL_BAD_TOKEN,
        ADOPT_DECL_BAD_ANCHOR,
        ADOPT_DECL_OWNER_CONFLICT,
    }
)


def _worktree_token(repo_root: Path, worktree_path: str, lane_label: str) -> Optional[str]:
    """The lane's canonical worktree identity token, or ``None`` if unresolvable.

    The SAME token the create-path metadata / lifecycle declaration is keyed on
    (``_record_lane_metadata``): a non-git directory lane whose runtime root collapses onto
    the workspace root is scoped by ``(workspace root, lane_label)``; a git lane's distinct
    worktree keeps its ``wt_`` token. Writer and reader compute it identically.
    """
    try:
        resolved = Path(worktree_path).expanduser().resolve()
        is_workspace_root = resolved == repo_root.expanduser().resolve()
    except OSError:
        return None
    if is_workspace_root:
        return derive_directory_lane_token(str(resolved), lane_label)
    return derive_lane_workspace_token(str(resolved))


def _resolve_attested_slot(
    *,
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    lane_id: str,
    provider: str,
    role: str,
    attestation_store,
) -> tuple[Optional[ProcessGenerationPin], str]:
    """Resolve ONE provider's live, attested slot into a typed pin, or a zero-write reason.

    Returns ``(pin, "")`` on success or ``(None, reason)`` on any fail-closed condition: a
    duplicate assigned name (RAW candidate multiplicity), a stale shell residue, a missing
    locator, or a startup self-attestation that is not present + generation-bound to the
    live locator.
    """
    want_lane = _norm_lane(lane_id)
    candidates = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decode = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decode.ok or decode.identity is None:
            continue
        identity = decode.identity
        if identity.workspace_id != workspace_id:
            continue
        if _norm_lane(identity.lane_id) != want_lane:
            continue
        if identity.role != provider:
            continue
        candidates.append(row)
    # RAW candidate multiplicity is checked BEFORE the liveness filter (Redmine #13810
    # R4-F2, review j#78909 / ``herdr-native-identity.md`` §3.4 / §5): a duplicate assigned
    # name is ``multiple_matches`` — a herdr name-uniqueness violation this never guesses
    # past, even when one row is live and the other is a locator-bearing stale residue.
    if len(candidates) > 1:
        return (None, ADOPT_DECL_DUPLICATE_CANDIDATES)
    if not candidates:
        return (None, ADOPT_DECL_INCOMPLETE_PAIR)
    row = candidates[0]
    if classify_named_slot(row) != SLOT_LIVE:
        # A locator-bearing stale shell residue is never adopted.
        return (None, ADOPT_DECL_STALE_SLOT)
    assigned_name = _norm(row.get(AGENT_KEY_NAME))
    locator = _norm(_agent_locator(row))
    if not assigned_name or not locator:
        return (None, ADOPT_DECL_INCOMPLETE_PAIR)
    record = attestation_store.read(assigned_name)
    join = evaluate_attestation(
        record,
        live_locator=locator,
        expected_workspace_id=workspace_id,
        expected_role=provider,
        expected_lane=lane_id,
    )
    if not join.ok:
        # absent / stale / missing / conflict startup self-attestation -> zero-write.
        return (None, ADOPT_DECL_UNATTESTED)
    # role/provider/assigned_name/locator are the typed identity; ``attested_at`` carries the
    # verified startup self-attestation's ``observed_at`` (real evidence, R4-F1); the herdr
    # generation discriminant is the locator, so ``runtime_revision`` stays empty (there is
    # no herdr runtime-version surface to observe — it is never fabricated).
    try:
        pin = ProcessGenerationPin(
            role=role,
            provider=provider,
            assigned_name=assigned_name,
            locator=locator,
            attested_at=_norm(record.observed_at) if record is not None else "",
        )
    except ProcessPinError:
        return (None, ADOPT_DECL_INCOMPLETE_PAIR)
    return (pin, "")


def declare_adopted_owner_row(
    *,
    journal: str,
    issue: str,
    lane_label: str,
    repo_root: Path,
    worktree_path: str,
    workspace_id: str,
    lane_id: str,
    providers: tuple[str, str],
    rows: Sequence[Mapping[str, object]],
    attestation_home: Optional[Path] = None,
    store_factory: Callable[[], LaneDeclarationStore] = LaneDeclarationStore,
    attestation_store_factory: Optional[Callable[[], object]] = None,
) -> str:
    """Declare an adopted lane's owner binding, or fail closed zero-write (Redmine #13809).

    ``providers`` is the ``(gateway_provider, worker_provider)`` pair; ``rows`` are the RAW
    ``agent list`` rows; ``(workspace_id, lane_id)`` is the unit the ops adapter resolved.
    Returns a status token from the outcome vocabulary — the caller propagates it (only
    :data:`ADOPT_DECL_DECLARED` authorizes proceeding to dispatch, R3-F3).

    Writes an owner row ONLY when an exact anchor is present, both provider slots resolve to
    exactly one live, attested candidate with distinct locators, and the declaration service
    applies it. Every gate failure is zero-write; a duplicate exact adopt is idempotent; a
    store error is returned as ``declare_error`` so the caller can log without breaking the
    actuation.
    """
    journal = _norm(journal)
    issue = _norm(issue)
    lane_label = _norm(lane_label)
    if not (journal and issue and lane_label):
        return ADOPT_DECL_NO_ANCHOR
    workspace = _norm(workspace_id)
    lane = _norm(lane_id)
    if not (workspace and lane):
        return ADOPT_DECL_UNRESOLVED_UNIT
    # The lane's canonical worktree token — computed ONCE so the declaration attempt and the
    # owner-completeness resolution below agree on the exact token this adopt resolved. It is
    # the completeness anchor: a lane is "already established" (safe to dispatch on a gate /
    # CAS refusal) only when the state DB owner row is bound to THIS exact token.
    worktree_token = _worktree_token(repo_root, worktree_path, lane_label)

    def _attempt() -> str:
        if attestation_store_factory is not None:
            attestation_store = attestation_store_factory()
        else:
            attestation_store = HerdrIdentityAttestationStore(home=attestation_home)
        gateway_provider, worker_provider = providers
        pins: list[ProcessGenerationPin] = []
        seen_locators: set[str] = set()
        for provider, role in (
            (gateway_provider, GATEWAY_ROLE),
            (worker_provider, WORKER_ROLE),
        ):
            pin, reason = _resolve_attested_slot(
                rows=rows,
                workspace_id=workspace,
                lane_id=lane,
                provider=provider,
                role=role,
                attestation_store=attestation_store,
            )
            if pin is None:
                return reason
            if pin.locator in seen_locators:
                # Two slots on one locator is an ambiguous / recycled target.
                return ADOPT_DECL_AMBIGUOUS_LOCATORS
            seen_locators.add(pin.locator)
            pins.append(pin)

        if worktree_token is None:
            return ADOPT_DECL_BAD_TOKEN
        token = worktree_token
        try:
            key = LaneLifecycleKey(workspace, lane)
            decision = DecisionPointer(
                source="redmine", issue_id=issue, journal_id=journal
            )
        except (DecisionPointerError, ValueError):
            return ADOPT_DECL_BAD_ANCHOR
        try:
            store = store_factory()
            result = store.declare_lane(
                key,
                decision=decision,
                binding_kind=BINDING_KIND_ISSUE,
                issue_id=issue,
                declared_slots=pins,
                worktree_identity=token,
            )
            # ``applied`` is true for a fresh declare AND an idempotent exact-duplicate
            # adopt (same live pins); a refusal wrote nothing.
            if result.applied:
                return ADOPT_DECL_DECLARED
            # declare_lane refused. The one refusal this residual (Redmine #13809 j#78945)
            # closes is a **pre-#13754 legacy** owner row: already ``active`` and already
            # owns this issue, but with an empty ``worktree_identity`` — so declare_lane read
            # the live worktree as a divergent re-declare and refused, leaving retire blocked
            # on ``worktree_binding_unverified``. Attempt the bounded missing-field backfill
            # CAS on THIS row (guarded on declare_lane's observed revision). It fills only the
            # empty binding + typed pins; a non-empty mismatch / different issue / non-active /
            # recycled generation / revision race is zero-write, so declare_lane's
            # divergent-re-declare refusal is preserved, not generally relaxed.
            backfill = store.backfill_active_binding(
                key,
                expected_revision=result.revision,
                issue_id=issue,
                worktree_identity=token,
                declared_slots=pins,
            )
        except (LaneLifecycleError, DecisionPointerError, OSError, ProcessPinError):
            return ADOPT_DECL_DECLARE_ERROR
        if backfill.applied:
            return ADOPT_DECL_BACKFILLED
        # Neither a fresh declaration nor a legacy backfill applied: another lane owns the
        # issue, or a divergent binding (a recycled generation whose live pins differ) already
        # exists — a legitimate zero-write.
        return ADOPT_DECL_OWNER_CONFLICT

    outcome = _attempt()
    if outcome in (ADOPT_DECL_DECLARED, ADOPT_DECL_BACKFILLED):
        # Owner-bound: a fresh declaration, an idempotent duplicate, or a legacy row whose
        # missing worktree binding was just filled — all leave the lane the active owner.
        return outcome
    # A gate failure / backfill CAS refusal leaves the lane safe to dispatch ONLY when the
    # state DB confirms it is already established as THIS EXACT lane — the owner row is bound
    # to this exact worktree token (a COMPLETE binding), not merely owns the issue (review
    # j#78975 F1). A legacy INCOMPLETE owner row (empty worktree binding) is exactly what
    # this residual fixes: letting an ambiguous / unattested / stale live pair, a non-empty
    # worktree mismatch, or a revision race collapse to ``already_owned`` there would bypass
    # the items 2/3 fail-closed gate and dispatch to a lane whose binding is still unmet. Its
    # #13754 retire fence would stay ``worktree_binding_unverified`` while dispatch proceeds.
    # (An ``unreadable_inventory`` — herdr fully down, no observation — is handled separately
    # by the ops adapter on ownership authority, R4-F3; only a POSITIVELY suspicious readable
    # inventory fails closed here.)
    return complete_owner_bound_or(
        outcome,
        store_factory=store_factory,
        workspace=workspace,
        issue=issue,
        lane=lane,
        worktree_token=worktree_token,
    )


def owner_bound_or(
    reason: str,
    *,
    workspace: str,
    issue: str,
    lane: str,
    store_factory: Callable[[], LaneDeclarationStore] = LaneDeclarationStore,
) -> str:
    """``already_owned`` when ``lane`` is verified the active owner, else ``reason``.

    The **ownership-only** resolution for the ``unreadable_inventory`` path (Redmine #13810
    R4-F3): when herdr is fully down the ops adapter cannot gate at all, so it falls back to
    the state DB (a separate authority) — this lane may dispatch when it already owns the
    issue, because a transient inventory outage must not hard-block a lane that genuinely
    owns its work. It never proceeds on inference: an unreadable / unresolved store, or an
    owner that is a different / no lane, keeps ``reason`` so the caller fails closed.

    A **readable** but suspicious inventory (an ambiguous / unattested / stale live pair) or
    a CAS refusal uses :func:`complete_owner_bound_or` instead, which additionally requires a
    COMPLETE binding — an owner row is not a licence to dispatch past a positively observed
    problem (review j#78975 F1).
    """
    if _lane_is_active_owner(
        store_factory, _norm(workspace), _norm(issue), _norm(lane)
    ):
        return ADOPT_DECL_ALREADY_OWNED
    return reason


def complete_owner_bound_or(
    reason: str,
    *,
    workspace: str,
    issue: str,
    lane: str,
    worktree_token: Optional[str],
    store_factory: Callable[[], LaneDeclarationStore] = LaneDeclarationStore,
) -> str:
    """``already_owned`` only when ``lane`` owns ``issue`` with a COMPLETE, EXACT binding.

    The completeness-aware owner resolution for a gate failure / CAS refusal on a readable
    inventory (review j#78975 F1 / j#79015 F2). Unlike :func:`owner_bound_or`, owning the
    issue is not enough: the state DB owner row must ALSO carry BOTH halves of a complete
    binding — a non-empty ``worktree_identity`` equal to the exact token this adopt resolved,
    AND a non-empty typed-pin snapshot with the gateway + worker roles. That is what
    distinguishes a lane genuinely already established as THIS exact lane (a completed create /
    adopt, or a recycled generation of the same worktree — safe to dispatch, preserving
    Redmine #13810 R3-F1/R3-F3) from a legacy row incomplete on EITHER axis (an empty worktree
    binding, or the v4->v5 pins-only gap) or a DIFFERENT-worktree binding, all of which keep
    ``reason`` so the caller fails closed before dispatch.

    ``worktree_token`` is ``None`` when the lane's worktree could not be resolved; there is
    then no exact binding to confirm, so the row is never treated as complete (fail closed).
    """
    if _lane_owns_issue_with_binding(
        store_factory,
        _norm(workspace),
        _norm(issue),
        _norm(lane),
        _norm(worktree_token) if worktree_token is not None else "",
    ):
        return ADOPT_DECL_ALREADY_OWNED
    return reason


def _lane_is_active_owner(
    store_factory: Callable[[], LaneDeclarationStore],
    workspace: str,
    issue: str,
    lane: str,
) -> bool:
    """Is ``lane`` the active owner of ``issue`` in ``workspace`` right now? (fail-closed)

    Reads the SAME state DB the declaration writes to (``store_factory().path``). An
    unreadable store, or an owner that resolves to a different / no lane, reads False — the
    caller then treats the adopt as owner-unbound and fails closed.
    """
    try:
        owner = LaneLifecycleStore(path=store_factory().path).resolve_owner(
            workspace, issue
        )
    except (LaneLifecycleError, OSError):
        return False
    return owner.resolved and owner.lane_id == lane


def _lane_owns_issue_with_binding(
    store_factory: Callable[[], LaneDeclarationStore],
    workspace: str,
    issue: str,
    lane: str,
    worktree_token: str,
) -> bool:
    """Does ``lane`` own ``issue`` with a COMPLETE, EXACT binding? (fail-closed)

    The completeness half of the owner check (review j#78975 F1 / j#79015 F2). Reads the same
    state DB and additionally requires the owner row to carry BOTH halves of a complete binding:

    - ``worktree_identity`` non-empty and equal to ``worktree_token`` (the exact worktree);
    - a ``declared_slots`` snapshot that decodes valid and carries the gateway + worker
      provider-role pins (a non-empty typed pin set).

    An empty token, an unreadable store, an owner that is a different / no lane, a row whose
    worktree binding is empty / different, OR a row whose typed pins are empty / undecodable /
    missing a required role reads False — so a legacy row that is incomplete on EITHER axis
    (empty worktree binding, or the v4->v5 pins-only gap) is never treated as an established
    lane the caller may dispatch to on a gate / CAS failure.
    """
    if not worktree_token:
        return False
    try:
        store = LaneLifecycleStore(path=store_factory().path)
        owner = store.resolve_owner(workspace, issue)
        if not (owner.resolved and owner.lane_id == lane):
            return False
        record = store.get(LaneLifecycleKey(workspace, lane))
    except (LaneLifecycleError, OSError, ValueError):
        return False
    if record is None or _norm(record.worktree_identity) != worktree_token:
        return False
    return _binding_has_required_pins(record)


def _binding_has_required_pins(record) -> bool:
    """Does the row's declared-slot snapshot decode valid with both required roles? (fail-closed)

    The typed-pins half of completeness (review j#79015 F2): a complete binding records the
    provider-role slots this adopt path declares — the gateway and the worker. An empty
    snapshot (the v4->v5 pins-only gap), an undecodable one (fail-closed by contract), or one
    missing either required role is NOT complete, so the row is not an established lane.
    """
    try:
        pins = record.declared_pins  # decode_declared_slots; raises on a corrupt snapshot
    except ProcessPinError:
        return False
    roles = {pin.role for pin in pins}
    return GATEWAY_ROLE in roles and WORKER_ROLE in roles


__all__ = (
    "declare_adopted_owner_row",
    "owner_bound_or",
    "complete_owner_bound_or",
    "ADOPT_DECL_OWNER_UNBOUND",
    "ADOPT_DECL_ZERO_WRITE",
    "ADOPT_DECL_NOT_ADOPTED",
    "ADOPT_DECL_UNREADABLE",
    "ADOPT_DECL_DECLARED",
    "ADOPT_DECL_BACKFILLED",
    "ADOPT_DECL_ALREADY_OWNED",
    "ADOPT_DECL_OWNER_CONFLICT",
    "ADOPT_DECL_DECLARE_ERROR",
    "ADOPT_DECL_NO_ANCHOR",
    "ADOPT_DECL_UNRESOLVED_UNIT",
    "ADOPT_DECL_INCOMPLETE_PAIR",
    "ADOPT_DECL_DUPLICATE_CANDIDATES",
    "ADOPT_DECL_STALE_SLOT",
    "ADOPT_DECL_AMBIGUOUS_LOCATORS",
    "ADOPT_DECL_UNATTESTED",
    "ADOPT_DECL_BAD_TOKEN",
    "ADOPT_DECL_BAD_ANCHOR",
)
