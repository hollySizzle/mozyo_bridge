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

        token = _worktree_token(repo_root, worktree_path, lane_label)
        if token is None:
            return ADOPT_DECL_BAD_TOKEN
        try:
            key = LaneLifecycleKey(workspace, lane)
            decision = DecisionPointer(
                source="redmine", issue_id=issue, journal_id=journal
            )
        except (DecisionPointerError, ValueError):
            return ADOPT_DECL_BAD_ANCHOR
        try:
            result = store_factory().declare_lane(
                key,
                decision=decision,
                binding_kind=BINDING_KIND_ISSUE,
                issue_id=issue,
                declared_slots=pins,
                worktree_identity=token,
            )
        except (LaneLifecycleError, DecisionPointerError, OSError, ProcessPinError):
            return ADOPT_DECL_DECLARE_ERROR
        # ``applied`` is true for a fresh declare AND an idempotent exact-duplicate adopt
        # (same live pins); a refusal (owner conflict, or a recycled generation whose live
        # pins differ) wrote nothing — a legitimate zero-write.
        return ADOPT_DECL_DECLARED if result.applied else ADOPT_DECL_OWNER_CONFLICT

    outcome = _attempt()
    if outcome == ADOPT_DECL_DECLARED:
        return outcome
    # R3-F3: a non-``declared`` adopt leaves the lane owner-unbound ONLY if the lane is not
    # already the active owner of its issue. A prior create / adopt that bound it means the
    # lane is safe to dispatch even when THIS adopt could not (re-)declare typed pins (a
    # recycled generation, an unattested fake environment, a divergent re-declare) — the
    # #13809 hibernate blocker is only the genuinely rowless case.
    return owner_bound_or(
        outcome, store_factory=store_factory, workspace=workspace, issue=issue, lane=lane
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

    The shared owner-bound resolution (Redmine #13810 R4-F3): a fail-closed adopt outcome —
    including an ``unreadable_inventory`` that the ops adapter hit BEFORE this module could
    gate — proceeds ONLY when the state DB (a separate authority from the live herdr
    inventory) confirms this exact lane already owns the issue. It never proceeds on
    inference: an unreadable / unresolved store, or an owner that is a different / no lane,
    keeps ``reason`` so the caller fails closed before dispatch.
    """
    if _lane_is_active_owner(
        store_factory, _norm(workspace), _norm(issue), _norm(lane)
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


__all__ = (
    "declare_adopted_owner_row",
    "owner_bound_or",
    "ADOPT_DECL_OWNER_UNBOUND",
    "ADOPT_DECL_ZERO_WRITE",
    "ADOPT_DECL_NOT_ADOPTED",
    "ADOPT_DECL_UNREADABLE",
    "ADOPT_DECL_DECLARED",
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
