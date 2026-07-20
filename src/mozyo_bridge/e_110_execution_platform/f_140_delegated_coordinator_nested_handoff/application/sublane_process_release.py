"""Shared lane-unit disposition actuation helpers (Redmine #13681 W2 / #13682).

The tombstone-free process-release driver and the lane-unit inventory / attestation
helpers that a disposition transition (supersede #13681, hibernate + resume #13682)
needs. Extracted from ``sublane_supersede`` so the two use cases share one battle-tested
release path rather than duplicating the fail-closed pin machinery that took #13681
R1-R4 (j#77247 / j#77292 / j#77307 / j#77322) to get right — a copy would be four
rounds of subtle bugs waiting to diverge.

Three concerns live here, all pure over an injected IO port / read callable so tests
drive fakes:

- :func:`unit_slots` — a lane unit's live managed ``{role: (assigned_name, locator)}``.
- :func:`evaluate_pair_attestation` — is a lane's gateway/worker pair both live AND each
  carrying a generation-matched #13637 startup self-attestation? (supersede gates the
  *recovery* successor on this; resume gates the *freshly relaunched* pair on it.)
- :func:`drive_process_release` + :func:`pin_matched_close_plan` + :func:`release_pins`
  — open (or idempotently resume) a release generation on a lane that has already left
  ``active``, and close only the slots it durably pinned, only when their live locator
  still matches. Never removes a worktree, deletes a branch, or writes a metadata
  tombstone (it closes managed panes through :func:`execute_herdr_retire_close`).

Boundary: this drives *process* release only. The disposition CAS (``active ->
superseded`` / ``active -> hibernated`` / ``hibernated -> active``) is the caller's, on
the #13689 :class:`LaneLifecycleStore`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

from mozyo_bridge.core.state.herdr_identity_attestation import (
    IdentityAttestationRecord,
    evaluate_attestation,
)
from mozyo_bridge.core.state.lane_pin_role import (
    resolve_declared_pin_pair,
)
from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    RELEASE_NOT_REQUESTED,
    RELEASE_PARTIAL,
    RELEASE_RELEASED,
    RELEASE_REQUESTED,
    LaneLifecycleError,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ProcessGenerationPin,
    ProcessPinError,
    ReleasePin,
    ReleasePinError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
    HerdrRetireClosePlan,
    HerdrRetireCloseResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
    GATEWAY_ROLE,
    WORKER_ROLE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    _agent_locator,
    _norm,
    _norm_lane,
    decode_assigned_name,
)

#: The two managed slots a lane unit carries (gateway + worker), under the resolved
#: binding. Shared by the inventory helpers and the pin matcher.
_LANE_ROLES = (GATEWAY_ROLE, WORKER_ROLE)


# ---------------------------------------------------------------------------
# Injected IO port (the subset a release driver needs).
# ---------------------------------------------------------------------------


@runtime_checkable
class SublaneReleaseOps(Protocol):
    """The side effects the tombstone-free release driver needs, injected for tests."""

    def live_rows(self) -> Sequence[Mapping[str, object]]: ...

    def execute_close(self, plan: HerdrRetireClosePlan) -> HerdrRetireCloseResult: ...


# ---------------------------------------------------------------------------
# Lane-unit live inventory.
# ---------------------------------------------------------------------------


def unit_slots(
    rows: Sequence[Mapping[str, object]], workspace_id: str, lane_id: str
) -> dict[str, tuple[str, str]]:
    """``{role: (assigned_name, locator)}`` for a lane unit's live managed slots."""
    want = _norm_lane(lane_id)
    slots: dict[str, tuple[str, str]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = row.get(AGENT_KEY_NAME)
        decode = decode_assigned_name(name)
        if not decode.ok or decode.identity is None:
            continue
        identity = decode.identity
        if identity.workspace_id != workspace_id:
            continue
        if _norm_lane(identity.lane_id) != want:
            continue
        if identity.role not in _LANE_ROLES:
            continue
        locator = _agent_locator(row)
        if not locator:
            continue
        slots.setdefault(identity.role, (_norm(name), locator))
    return slots


def evaluate_pair_attestation(
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    lane: str,
    read_attestation: Callable[[str], Optional[IdentityAttestationRecord]],
    *,
    fresh_after: Optional[str] = None,
) -> tuple[bool, bool, str]:
    """``(both_slots_live, attested, detail)`` for a lane's gateway/worker pair.

    A pair is *attested* only when BOTH managed slots are live AND each carries a
    #13637 startup self-attestation that is generation-matched to its **live locator**
    (:func:`evaluate_attestation`). supersede uses this on the recovery successor (a
    brand-new *different* lane, so a survivor is impossible); ``fresh_after`` is not
    passed there.

    ``fresh_after`` (resume, Redmine #13682) adds the missing half of a *freshness*
    proof. The locator is the tmux pane-id, which changes only when the process truly
    dies — so a pane that **survived** hibernate's release keeps its locator and still
    matches its own *pre-hibernate* attestation, and the locator pin alone cannot tell a
    survivor from a genuine relaunch. When ``fresh_after`` is given (the lane's
    hibernation timestamp), a slot additionally must carry a self-attestation
    ``observed_at`` **strictly after** it — a fresh relaunch self-attests after the lane
    hibernated, a survivor's record predates it. A missing / not-after ``observed_at`` is
    ``stale_generation`` (fail closed).
    """
    slots = unit_slots(rows, workspace_id, lane)
    if GATEWAY_ROLE not in slots or WORKER_ROLE not in slots:
        return False, False, "lane is not both-slots live"
    threshold = _norm(fresh_after) if fresh_after is not None else ""
    for role in _LANE_ROLES:
        assigned_name, locator = slots[role]
        record = read_attestation(assigned_name)
        join = evaluate_attestation(
            record,
            live_locator=locator,
            expected_workspace_id=workspace_id,
            expected_role=role,
            expected_lane=lane,
        )
        if not join.ok:
            return True, False, f"{role}: {join.state}"
        if threshold:
            # A locator-matched attestation proves a FRESH generation only when it was
            # observed after the lane hibernated (a survivor's record predates it). Both
            # timestamps are fixed-width UTC ISO-seconds, so a lexical compare is a time
            # compare. An absent / not-after stamp fails closed.
            observed = _norm(record.observed_at) if record is not None else ""
            if not observed or observed <= threshold:
                return True, False, f"{role}: stale_generation"
    return True, True, "both slots attested and generation-matched"


# ---------------------------------------------------------------------------
# Tombstone-free process release.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseOutcome:
    """The outcome of a tombstone-free process release on a non-active lane."""

    action_id: str
    process_release: str
    closed: tuple[tuple[str, str], ...] = ()
    failed: tuple[tuple[str, str, str], ...] = ()
    foreign_names: tuple[str, ...] = ()
    detail: str = ""
    #: Redmine #13843 review F3: the caller passed an ``expected_revision`` (a T1-verified
    #: authority token) and the driver's fresh row read did NOT match it — the lifecycle
    #: authority advanced between the caller's re-validation and the driver read, so the
    #: driver closed NOTHING (a zero-close admission block, not "nothing to release"). The
    #: caller treats this as a re-validation block, never a success. ``False`` for a caller
    #: that passes no ``expected_revision`` (the supersede path, unchanged).
    admission_blocked: bool = False

    def as_payload(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "process_release": self.process_release,
            "closed": [{"role": r, "locator": loc} for r, loc in self.closed],
            "failed": [
                {"role": r, "locator": loc, "detail": d} for r, loc, d in self.failed
            ],
            "foreign_names": list(self.foreign_names),
            "admission_blocked": self.admission_blocked,
            "detail": self.detail,
        }


def pin_matched_close_plan(
    pins: Sequence[ReleasePin],
    rows: Sequence[Mapping[str, object]],
    *,
    workspace_id: str,
    lane_id: str,
) -> Optional[HerdrRetireClosePlan]:
    """Close plan honoring the durable pins with full stable-identity re-resolution.

    R1 F1 (j#77247) + R2-F1 (j#77292): a pinned slot is a close target ONLY when BOTH

    - the pin's assigned name **decodes to exactly this generation's unit and role**
      ``(workspace_id, lane_id, pin.role)`` — the full ``ReleasePin`` stable identity, not
      just a name string; and
    - a live row with that same assigned name still carries the pin's **exact locator**
      (the slot was not recycled into a new agent generation and is not gone).

    A single semantically-inconsistent pin — one that decodes to a foreign unit / role, or
    is undecodable — is a corrupt pin set: the WHOLE generation fails closed (returns
    ``None`` so the caller closes nothing), rather than a partial set that might include a
    foreign pane. The pins, re-resolved against the live inventory, are the sole authority
    for what this stale action may close (``ReleasePin`` contract).

    The live inventory is matched as a **set of exact ``(assigned_name, locator)`` pairs**
    (R2-F1 j#77292 + R3-F2 j#77307), never a name→last-locator map: a pin is a target iff
    its exact pair is live, which is independent of the row order and never lets an
    already-recycled locator masquerade as the pinned one. If the same assigned name is
    live at **more than one locator** (an ambiguous inventory), the generation fails closed
    rather than guess which live pane is the pinned process — so a still-live pinned slot is
    never silently dropped and recorded ``released``.
    """
    want_lane = _norm_lane(lane_id)

    def _decodes_to_unit(name: str, role: str) -> bool:
        decode = decode_assigned_name(name)
        if not decode.ok or decode.identity is None:
            return False
        identity = decode.identity
        return (
            identity.workspace_id == workspace_id
            and _norm_lane(identity.lane_id) == want_lane
            and identity.role == role
        )

    live_pairs: set[tuple[str, str]] = set()
    locators_by_name: dict[str, set[str]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = _norm(row.get(AGENT_KEY_NAME))
        locator = _agent_locator(row)
        if name and locator:
            live_pairs.add((name, locator))
            locators_by_name.setdefault(name, set()).add(locator)

    targets: list[tuple[str, str]] = []
    for pin in pins:
        if pin.role not in _LANE_ROLES or not _decodes_to_unit(
            pin.assigned_name, pin.role
        ):
            # A pin naming a foreign unit / role, or an undecodable one: the pin set is
            # corrupt. Fail the whole generation closed rather than risk a foreign close.
            return None
        if len(locators_by_name.get(pin.assigned_name, ())) > 1:
            # The pinned assigned name is live at more than one locator — an ambiguous
            # inventory. Fail the whole generation closed rather than guess which live pane
            # is the pinned process (and never record `released` over an unresolved slot).
            return None
        if (pin.assigned_name, pin.locator) in live_pairs:
            targets.append((pin.role, pin.locator))
    return HerdrRetireClosePlan(
        workspace_id=workspace_id, lane_id=lane_id, close_targets=tuple(targets)
    )


def _live_provider(row: Mapping[str, object], decoded_provider: str) -> str:
    """The provider a live row carries for the exact-generation match (Redmine #13811 F1).

    The herdr row surfaces the bound provider two ways — its ``provider`` field and its
    detected-agent field (``agent``, which on a live pane holds the provider id) — and
    falls back to the name-encoded provider token when neither is surfaced (the #13846
    slot-less-path shape ``... or norm(provider)``). ``decoded_provider`` is that token: in
    the current gateway/peer topology the assigned name's role segment IS the provider
    (``GATEWAY_ROLE='codex'`` / ``WORKER_ROLE='claude'``), so a decode gives an honest,
    non-fabricated provider even when the live row omits the explicit field.
    """
    return (
        _norm(row.get("provider"))
        or _norm(row.get("agent"))
        or _norm(decoded_provider)
    )


def declared_generation_exactly_live(
    declared_pins: Sequence[ProcessGenerationPin],
    rows: Sequence[Mapping[str, object]],
    *,
    workspace_id: str,
    lane_id: str,
) -> bool:
    """Does every LIVE managed slot of the lane exactly match its declared pin? (pure).

    The project-gateway action-time exact-generation fence (Redmine #13811; design #13780
    j#78386 §1-3): a process-only lifecycle action may release ONLY the lane's declared
    generation, so a caller must prove the *current* live inventory carries that exact
    generation before it releases anything.

    **Match on the declared identity, not on a provider-as-slot alias (Redmine #13811 R4
    F1).** A declared ``ProcessGenerationPin.role`` names a canonical SLOT (``gateway`` /
    ``worker``, #13920) and its ``provider`` names the provider that fills it — the two are
    independent (a **swapped** binding declares ``gateway`` filled by ``claude`` and
    ``worker`` by ``codex``). A live herdr row's decoded ``identity.role`` is that provider
    token, NOT a slot label — so it is NEVER re-mapped to a slot (that alias broke a valid
    swapped binding). Instead each live row is matched to a declared pin by the strong
    identity the row actually carries — its **assigned name** — and then the pin's
    ``provider`` + ``locator`` (+ ``runtime_revision``, both-observed only) are verified via
    :meth:`ProcessGenerationPin.binds_same_generation`. The declared set is first resolved
    through :func:`resolve_declared_pin_pair`, so a foreign / mixed-vocabulary / duplicate /
    incomplete declaration fails closed (never guessed past).

    Any live row that is not exactly a declared pin — an **undeclared** assigned name, a
    **wrong-provider** row (``pin.provider`` is a real match axis), a **recycled** locator, a
    **raw-duplicate** assigned name (the same name in the inventory more than once, counted
    BEFORE the locator / liveness filter so a locatorless or stale duplicate cannot slip past,
    Redmine #13811 R4 F3 / ``herdr-native-identity.md`` §3.4), or an **ambiguous** one (one
    name live at two locators) — is a newer / foreign generation the declared authority does
    not name, and the action fails closed rather than close it (§2 "newer generation / stale
    approval -> zero-actuation"). A declared slot that is simply **gone** (no live row) needs
    no close and does not block (the 0/1/2-slot / dead-process case).

    ``runtime_revision`` is supplementary evidence, not identity (#13810 R4-F1 / #13846):
    the herdr process-generation discriminant is the **live locator**, and the startup
    self-attestation store records NO runtime version, so a live row surfaces an empty
    revision. :meth:`~ProcessGenerationPin.binds_same_generation` therefore treats revision
    as a discriminant ONLY when BOTH sides observe it (a re-launched newer runtime), and a
    declared-non-empty / live-unobserved pairing is not a mismatch — matching the
    action-time generation contract in ``managed-state-model.md`` (``runtime_revision`` is
    非 discriminant when either side is empty). A caller wanting a strict revision fence
    would re-introduce the #13846 false conflict.
    """
    # Resolve the declared set (foreign role / mixed-vocabulary / duplicate slot / half pair
    # all fail closed, #13920), then key the declared pins by their assigned NAME — the strong
    # identity a live row carries. Two declared slots sharing one assigned name (a degenerate
    # same-name pair) is itself ambiguous and fails closed.
    pair = resolve_declared_pin_pair(declared_pins)
    if not pair.ok:
        return False
    declared_by_name: dict[str, ProcessGenerationPin] = {}
    for pin in (pair.gateway, pair.worker):
        name = _norm(pin.assigned_name)
        if name in declared_by_name:
            return False
        declared_by_name[name] = pin

    want_ws = _norm(workspace_id)
    want_lane = _norm_lane(lane_id)
    # Pass over the raw inventory. RAW assigned-name multiplicity is counted for every
    # in-scope managed candidate BEFORE the locator / liveness filter (Redmine #13811 R4 F3):
    # a locatorless or stale duplicate of a live name is a herdr name-uniqueness violation the
    # dedupe below would otherwise hide. Only locator-bearing rows become match candidates.
    raw_name_count: dict[str, int] = {}
    live_by_name: dict[str, set[tuple[str, str, str]]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decode = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decode.ok or decode.identity is None:
            continue
        identity = decode.identity
        if identity.workspace_id != want_ws or _norm_lane(identity.lane_id) != want_lane:
            continue
        assigned_name = _norm(row.get(AGENT_KEY_NAME))
        if not assigned_name:
            continue
        raw_name_count[assigned_name] = raw_name_count.get(assigned_name, 0) + 1
        locator = _agent_locator(row)
        if not locator:
            continue
        # ``runtime_revision`` is empty for a normal herdr row (no live observation surface,
        # #13810 R4-F1); a richer surface that DOES carry it enters the both-observed match.
        live_by_name.setdefault(assigned_name, set()).add(
            (
                locator,
                _live_provider(row, identity.role),
                _norm(row.get("runtime_revision")),
            )
        )
    if any(count > 1 for count in raw_name_count.values()):
        # A raw duplicate assigned name (exact, locatorless, or stale) — fail closed.
        return False

    for assigned_name, live in live_by_name.items():
        if len(live) != 1:
            # This name is live at more than one distinct observation — an ambiguous
            # inventory. Fail closed rather than guess the declared process.
            return False
        (locator, provider, runtime_revision) = next(iter(live))
        declared = declared_by_name.get(assigned_name)
        if declared is None:
            # A live managed row the declaration never named — a newer / foreign generation.
            # Fail closed.
            return False
        try:
            # Compare on the declared pin's own slot label (so ``role`` is trivially equal and
            # the discriminants are provider / locator / revision) — the live provider token is
            # never re-mapped to a slot.
            live_pin = ProcessGenerationPin(
                role=declared.role,
                provider=provider,
                assigned_name=assigned_name,
                locator=locator,
                runtime_revision=runtime_revision,
            )
        except ProcessPinError:
            # A pin that cannot form an identity is anomalous — fail closed.
            return False
        if not declared.binds_same_generation(live_pin):
            # A live row that is not the declared generation (wrong provider / recycled
            # locator / both-observed-revision drift): fail closed.
            return False
    return True


def declared_generation_attested(
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    lane: str,
    read_attestation: Callable[[str], Optional[IdentityAttestationRecord]],
) -> bool:
    """Is every LIVE managed slot generation-matched startup-attested? (pure, fail-closed).

    The project-gateway action-time attestation gate (Redmine #13811 R1 F1 item 4; design
    #13780 j#78386 §2 "startup self-attestation を action-time 再読 ... unattested ->
    zero-actuation"). A release closes the lane's CURRENT live slots, so before any mutation
    each live target must carry a #13637 startup self-attestation that is generation-matched
    to its **live locator** (:func:`evaluate_attestation`). A missing / stale (locator-drift)
    / conflict / unreadable attestation on any live slot fails the gate (pre-CAS
    zero-write / zero-close): an optional ``attested_at`` declaration snapshot is not an
    action-time attestation, so this re-reads the store now rather than trusting the pin.

    An empty live inventory (nothing to close) is vacuously attested — there is no target
    to actuate. ``read_attestation`` raising (an unreadable store) is folded to a non-match
    for that slot (fail closed), never mistaken for "attested".
    """
    slots = unit_slots(rows, workspace_id, lane)
    for role, (assigned_name, locator) in slots.items():
        try:
            record = read_attestation(assigned_name)
        except Exception:  # noqa: BLE001 — attestation unreadable -> fail closed (NOT attested)
            return False
        join = evaluate_attestation(
            record,
            live_locator=locator,
            expected_workspace_id=workspace_id,
            expected_role=role,
            expected_lane=lane,
        )
        if not join.ok:
            return False
    return True


def release_pins(
    rows: Sequence[Mapping[str, object]], workspace_id: str, lane: str
) -> list[ReleasePin]:
    """Pin the lane's live managed slots as the release generation's targets."""
    pins: list[ReleasePin] = []
    for role, (assigned_name, locator) in unit_slots(rows, workspace_id, lane).items():
        try:
            pins.append(
                ReleasePin(role=role, assigned_name=assigned_name, locator=locator)
            )
        except ReleasePinError:
            continue
    return pins


def drive_process_release(
    *,
    store: LaneLifecycleStore,
    ops: SublaneReleaseOps,
    key: LaneLifecycleKey,
    lane_id: str,
    workspace_id: str,
    action_id: str,
    rows: Optional[Sequence[Mapping[str, object]]] = None,
    expected_revision: Optional[int] = None,
) -> ReleaseOutcome:
    """Open (or idempotently resume) a release generation and close the lane's slots.

    Tombstone-free: closes the managed panes through :func:`execute_herdr_retire_close`
    (via ``ops.execute_close``) and never removes a worktree, deletes a branch, or writes
    a metadata tombstone. A partial close leaves the generation open and re-drivable — a
    re-run resumes it (pane close is idempotent, unlike a send).

    ``action_id`` is the caller's generation id (``supersede:<lane>`` / ``hibernate:<lane>``)
    for a *fresh* generation; when a generation is already open the stored action id is
    resumed instead, never a second one opened.

    ``rows`` lets a caller pass a live inventory snapshot it has already read (and whose
    readability it has already vetted, Redmine #13682 R1-F1): an empty ``rows`` then means
    a *confirmed*-empty inventory ("the processes are already gone"), never an *unreadable*
    one folded to empty. When ``rows`` is ``None`` the driver reads it via ``ops.live_rows``
    (the supersede path, whose fail-open to ``()`` is its documented boundary).

    ``expected_revision`` (Redmine #13843 review F3) binds the release to the caller's exact
    T1-verified lifecycle authority: when supplied and the driver's FRESH row read does not
    carry that revision, the lifecycle authority advanced between the caller's re-validation
    and this read (a check-then-act race), so the driver closes NOTHING and returns
    ``admission_blocked`` (a zero-close, not "nothing to release"). ``None`` (the supersede
    path) skips the check — unchanged.

    Only a lane that has already left ``active`` is released here — a lane still holding
    its work is never a release target (the caller's disposition CAS must land first).
    """
    try:
        rec = store.get(key)
    except (LaneLifecycleError, OSError):
        return ReleaseOutcome(
            action_id=action_id,
            process_release=RELEASE_NOT_REQUESTED,
            detail="lifecycle store unreadable during release",
        )
    if rec is None or rec.lane_disposition == DISPOSITION_ACTIVE:
        # Not left active — nothing to release (never release an active owner).
        return ReleaseOutcome(
            action_id=action_id,
            process_release=(rec.process_release if rec else RELEASE_NOT_REQUESTED),
            detail="lane is still active; no release",
        )
    if expected_revision is not None and rec.revision != expected_revision:
        # Redmine #13843 review F3: the lifecycle authority advanced since the caller's T1
        # re-validation — close NOTHING and report the admission block (never re-bind a stale
        # retry to a newer release authority).
        return ReleaseOutcome(
            action_id=action_id,
            process_release=rec.process_release,
            admission_blocked=True,
            detail=(
                f"release admission blocked: revision drift (expected {expected_revision}, "
                f"row {rec.revision}); zero-close"
            ),
        )

    rows = ops.live_rows() if rows is None else rows
    if rec.process_release == RELEASE_NOT_REQUESTED:
        pins = release_pins(rows, workspace_id, lane_id)
        if not pins:
            # No live managed slots to release — the processes are already gone. A
            # non-active lane already draws zero capacity (W4), so leaving the generation
            # unopened is honest, not a gap.
            return ReleaseOutcome(
                action_id=action_id,
                process_release=RELEASE_NOT_REQUESTED,
                detail="no live managed slots to release",
            )
        try:
            opened = store.request_release(
                key,
                expected_revision=rec.revision,
                action_id=action_id,
                pins=pins,
            )
        except (ReleasePinError, LaneLifecycleError, OSError) as exc:
            return ReleaseOutcome(
                action_id=action_id,
                process_release=rec.process_release,
                detail=f"release request failed ({type(exc).__name__})",
            )
        if not opened.applied:
            return ReleaseOutcome(
                action_id=action_id,
                process_release=rec.process_release,
                detail=f"release request refused ({opened.reason})",
            )
        rec = store.get(key) or rec
    elif rec.process_release in (RELEASE_REQUESTED, RELEASE_PARTIAL):
        # Resume the open generation, closing whatever slots remain live.
        action_id = rec.release_action_id or action_id
    else:  # RELEASE_RELEASED — the generation already finished.
        return ReleaseOutcome(
            action_id=rec.release_action_id or action_id,
            process_release=RELEASE_RELEASED,
            detail="release generation already released",
        )

    # Close only the slots this generation durably pinned, and only when their live
    # locator STILL matches — never a pane recycled into a NEW agent generation between a
    # partial close and its resume. Corrupt pins fail closed (never degrade to fewer
    # targets, leaving slots alive).
    try:
        stored_pins = rec.pins
    except ReleasePinError:
        return ReleaseOutcome(
            action_id=action_id,
            process_release=rec.process_release,
            detail="release pins unreadable; fail closed (no slots closed)",
        )
    plan = pin_matched_close_plan(
        stored_pins, rows, workspace_id=workspace_id, lane_id=lane_id
    )
    if plan is None:
        # R2-F1: the pin set is semantically inconsistent with the lane unit — fail closed
        # (close nothing) rather than risk killing a foreign pane.
        return ReleaseOutcome(
            action_id=action_id,
            process_release=rec.process_release,
            detail="release pins inconsistent with lane unit; fail closed (no slots closed)",
        )
    close = ops.execute_close(plan)
    target = RELEASE_RELEASED if not close.failed else RELEASE_PARTIAL
    try:
        recorded = store.record_release_outcome(
            key,
            action_id=action_id,
            expected_revision=rec.revision,
            target=target,
        )
    except (LaneLifecycleError, OSError) as exc:
        return ReleaseOutcome(
            action_id=action_id,
            process_release=rec.process_release,
            closed=close.closed,
            failed=close.failed,
            foreign_names=close.foreign_names,
            detail=f"release outcome record failed ({type(exc).__name__})",
        )
    return ReleaseOutcome(
        action_id=action_id,
        process_release=target if recorded.applied else rec.process_release,
        closed=close.closed,
        failed=close.failed,
        foreign_names=close.foreign_names,
        detail=(
            "release recorded"
            if recorded.applied
            else f"release outcome refused ({recorded.reason})"
        ),
    )


__all__ = (
    "ReleaseOutcome",
    "SublaneReleaseOps",
    "declared_generation_attested",
    "declared_generation_exactly_live",
    "drive_process_release",
    "evaluate_pair_attestation",
    "pin_matched_close_plan",
    "release_pins",
    "unit_slots",
)
