"""The bounded post-launch startup probe for session-start (Redmine #13948, j#80989).

The observation :mod:`herdr_session_start` never made. ``_execute_slot`` returns the
moment ``herdr agent start`` hands back a well-formed, correctly-located locator — which
proves the launcher was *accepted*, not that anything is *running*. This module performs
the missing read, per role, after the fact, and maps it onto the pure three-axis verdict
in :mod:`...domain.startup_health`.

It composes two already-reviewed authorities rather than inventing classification:

- **#13760 startup admission** (:func:`evaluate_startup_admission`) for the visible
  startup screen. It already knows each provider's declared blockers, already refuses to
  guess for an unprofiled provider, already never returns pane text, and — critically —
  never *answers* the screen. This module wires it to launch time, which is the one
  boundary it was never connected to (it was built for send time, #13760, and operator
  gate projection, #13812).
- **#13637 self-attestation** (:func:`evaluate_attestation`) for locator-matched boot
  identity. The wrapper writes its record *before* exec and is non-blocking by contract
  (`herdr_agent_attest`); nothing here changes that. The launcher simply *reads* the
  store with a bounded deadline, which is the launcher-side enforcement that contract
  always said belonged to the adopt / doctor / send layers — session-start is now one of
  them (Answer j#80989 Q4).

**Bounded, and retried only where a retry can be right.** Two states are genuinely
"not yet" immediately after a start, and treating either as terminal would manufacture a
false failure:

- herdr has not surfaced the just-started agent in ``agent list`` yet (the same race the
  wrapper's own ``_ATTEST_LIST_RETRIES`` exists for), so a missing / not-yet-populated row
  is retried, not read as :data:`HEALTH_PROVIDER_EXITED`;
- the wrapper has not finished writing its record yet, so an absent record is retried, not
  read as :data:`HEALTH_ATTESTATION_TIMEOUT`.

States that cannot resolve themselves — a matched trust screen (only an operator clears
it), a drifted locator, an unprofiled provider, a record that exists but does not bind —
short-circuit immediately instead of burning the whole deadline.

The probe is read-only by construction: it lists, reads a pane, and reads a store. It
never types, never sends, never closes, never writes. A failure to observe is reported as
a named non-success, never as a success (#13845 discipline: absence of a liveness proof is
not proof of liveness).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.core.state.herdr_identity_attestation import (
    ATTEST_ABSENT,
    evaluate_attestation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_admission import (  # noqa: E501
    ADMISSION_ADMITTED,
    ADMISSION_BLOCKED,
    ADMISSION_UNKNOWN_PROVIDER,
    ADMISSION_UNREADABLE,
    evaluate_startup_admission,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    _agent_locator,
    _norm,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E501
    SLOT_STALE,
    classify_named_slot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_health import (  # noqa: E501
    ATTESTATION_ABSENT,
    ATTESTATION_INVALID,
    ATTESTATION_NOT_PROBED,
    ATTESTATION_OK,
    COMPENSATION_NOT_NEEDED,
    COMPENSATION_ROLLBACK_OWED,
    DISPOSITION_ADOPTED,
    DISPOSITION_FRESH_LAUNCHED,
    HEALTH_ATTESTATION_TIMEOUT,
    HEALTH_DETAIL,
    HEALTH_HEALTHY,
    HEALTH_INVENTORY_UNREADABLE,
    HEALTH_PROVIDER_EXITED,
    HEALTH_RECEIVER_UNREADABLE,
    HEALTH_SHELL_RESIDUE,
    HEALTH_STARTUP_INTERACTION,
    SCREEN_BLOCKED,
    SCREEN_CLEAR,
    SCREEN_UNPROFILED,
    SCREEN_UNREADABLE,
    SlotHealth,
    classify_startup_health,
)

def live_visible_reader(binary: str, runner, timeout: float, *, lines: int = 80):
    """Bind the visible-pane read onto the launcher's OWN injected transport seam.

    Deliberately built from ``binary`` + ``runner`` + ``timeout`` — the same three the
    rest of session-start invokes herdr through — rather than constructing a
    ``HerdrCliTransport``. Everything session-start does is already substitutable at that
    one seam, and a probe that reached around it would spawn a real ``herdr`` from tests
    that inject a fake runner: the launch would be simulated and the health read would
    not, which is precisely the kind of half-faked path that lets a defect through.

    The argv is ``agent read <locator> --source visible --lines N`` (the E11 schema
    :meth:`HerdrCliTransport.read_pane` issues); the payload is parsed by that module's
    own parser, so the nesting stays in one place (#13322).

    Returns ``None`` on any failed read rather than an empty string: a blank read is not
    evidence of a clear screen (the #13760 live lane saw an empty composer *after* the
    dialog ate the body), and :func:`evaluate_startup_admission` treats both as unreadable.
    """

    def _read(locator: str) -> object:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
            _invoke,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (  # noqa: E501
            DEFAULT_PANE_READ_SOURCE,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
            _parse_read_payload,
        )

        # `_invoke` raises on a non-zero / mechanical failure; the admission gate catches
        # it and calls the receiver unreadable — which is the correct fail-closed reading.
        completed = _invoke(
            binary,
            ["agent", "read", locator, "--source", DEFAULT_PANE_READ_SOURCE,
             "--lines", str(lines)],
            runner,
            timeout,
            env=None,
        )
        content, _ = _parse_read_payload(completed.stdout)
        return content

    return _read


#: Returns raw ``agent list`` rows, or ``None`` when the inventory could not be read.
#: ``None`` and ``[]`` are different facts and must never be conflated.
Lister = Callable[[], Optional[Sequence[Mapping[str, object]]]]
#: ``assigned_name -> IdentityAttestationRecord | None``.
AttestationReader = Callable[[str], object]
#: ``locator -> visible pane text``. May return a non-str or raise; both read unreadable.
VisibleReader = Callable[[str], object]

#: Default bounded deadline: 40 polls x 0.25s = 10s per slot, spent only while a slot is
#: still in a legitimately transient state. A healthy slot returns on its first poll.
DEFAULT_PROBE_POLLS = 40
DEFAULT_PROBE_INTERVAL = 0.25

#: #13760 admission vocabulary -> neutral domain screen facts. Explicit and total: an
#: unrecognised admission outcome must fail closed, never fall through to "clear".
_SCREEN_BY_ADMISSION: dict[str, str] = {
    ADMISSION_ADMITTED: SCREEN_CLEAR,
    ADMISSION_BLOCKED: SCREEN_BLOCKED,
    ADMISSION_UNREADABLE: SCREEN_UNREADABLE,
    ADMISSION_UNKNOWN_PROVIDER: SCREEN_UNPROFILED,
}

#: Health states that a later poll could legitimately change. Everything else is terminal
#: and short-circuits: waiting on an operator-owned trust screen, a drifted locator, or a
#: record that exists and does not bind only burns the deadline to reach the same verdict.
_RETRYABLE = frozenset(
    {
        HEALTH_INVENTORY_UNREADABLE,
        HEALTH_PROVIDER_EXITED,
        HEALTH_SHELL_RESIDUE,
        HEALTH_ATTESTATION_TIMEOUT,
        HEALTH_RECEIVER_UNREADABLE,
    }
)


def _screen_of(
    provider: str, locator: str, read_visible: VisibleReader, registry=None
) -> tuple[str, str]:
    """Classify the visible startup screen via #13760 (never raises, never returns text)."""
    admission = evaluate_startup_admission(
        provider_id=provider,
        read_visible=lambda: read_visible(locator),
        registry=registry,
    )
    screen = _SCREEN_BY_ADMISSION.get(admission.outcome, SCREEN_UNREADABLE)
    return screen, admission.blocker_id


def _attestation_of(
    *,
    assigned_name: str,
    live_locator: str,
    workspace_id: str,
    provider: str,
    lane: str,
    read_attestation: AttestationReader,
) -> str:
    """Join the stored self-attestation with this live slot via #13637 (never raises)."""
    try:
        record = read_attestation(assigned_name)
    except Exception:  # noqa: BLE001 - an unreadable store is never an attested slot
        return ATTESTATION_INVALID
    join = evaluate_attestation(
        record,
        live_locator=live_locator,
        expected_workspace_id=workspace_id,
        expected_role=provider,
        expected_lane=lane,
    )
    if join.ok:
        return ATTESTATION_OK
    # Only a wholly absent record can still arrive: the wrapper writes once, before exec,
    # so stale / foreign / missing-env / conflict are already final.
    return ATTESTATION_ABSENT if join.state == ATTEST_ABSENT else ATTESTATION_INVALID


def _read_rows(list_rows: Lister):
    """One inventory read. ``None`` on any failure — never an empty list (fail-closed)."""
    try:
        return list_rows()
    except Exception:  # noqa: BLE001 - an unreadable inventory is never an empty one
        return None


def _observe_once(
    *,
    provider: str,
    assigned_name: str,
    launched_locator: str,
    workspace_id: str,
    lane: str,
    rows,
    read_attestation: AttestationReader,
    read_visible: VisibleReader,
    attested_launch: bool = True,
    registry=None,
) -> tuple[str, str]:
    """Classify one live slot against an ALREADY-READ inventory -> ``(health, blocker)``.

    Takes ``rows`` rather than a lister on purpose (Redmine #13948 correction, j#81034):
    the inventory read belongs to the poll ROUND, not to the slot. One read per round
    means the call count is a function of how long the pair takes to come up, never of
    how many roles were requested — and every role in a round is judged against the same
    snapshot, so a pair can never be classified from two different views of the world.
    """
    if rows is None:
        return HEALTH_INVENTORY_UNREADABLE, ""
    matches = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and _norm(row.get(AGENT_KEY_NAME)) == _norm(assigned_name)
    ]
    if len(matches) != 1:
        # 0 = not surfaced (yet) or gone; >1 = a duplicate durable name, which we must not
        # resolve by guessing. Both are classified from the same positive facts below.
        return (
            classify_startup_health(
                inventory_readable=True,
                row_present=bool(matches),
                row_stale=False,
                live_locator="",
                launched_locator=launched_locator,
                screen=SCREEN_UNREADABLE,
                attestation=ATTESTATION_ABSENT,
            ),
            "",
        )
    row = matches[0]
    row_stale = classify_named_slot(row) == SLOT_STALE
    live_locator = _norm(_agent_locator(row))
    screen = SCREEN_UNREADABLE
    blocker_id = ""
    attestation = ATTESTATION_ABSENT
    # Only read the pane / store when the process-level facts have not already decided the
    # verdict: a residue pane has no agent to screen, and a drifted locator is not ours.
    if not row_stale and live_locator and live_locator == _norm(launched_locator):
        screen, blocker_id = _screen_of(provider, live_locator, read_visible, registry)
        if screen == SCREEN_CLEAR and not attested_launch:
            # No wrapper ran, so no record will ever appear. Say that, rather than
            # waiting a deadline out and calling it a timeout (Redmine #13637 fallback).
            attestation = ATTESTATION_NOT_PROBED
        elif screen == SCREEN_CLEAR:
            attestation = _attestation_of(
                assigned_name=assigned_name,
                live_locator=live_locator,
                workspace_id=workspace_id,
                provider=provider,
                lane=lane,
                read_attestation=read_attestation,
            )
    health = classify_startup_health(
        inventory_readable=True,
        row_present=True,
        row_stale=row_stale,
        live_locator=live_locator,
        launched_locator=_norm(launched_locator),
        screen=screen,
        attestation=attestation,
    )
    return health, (blocker_id if health == HEALTH_STARTUP_INTERACTION else "")


def _slot_health(
    *, slot_provider, assigned_name, locator, disposition, health, blocker_id
) -> SlotHealth:
    healthy = health == HEALTH_HEALTHY
    owed = (not healthy) and disposition == DISPOSITION_FRESH_LAUNCHED
    return SlotHealth(
        provider=slot_provider,
        assigned_name=assigned_name,
        disposition=disposition,
        health=health,
        locator=locator,
        blocker_id=blocker_id,
        # This run started it and it did not come up: the effect is owed a compensation,
        # which ONLY the explicit public rollback rail may perform (Answer j#80991).
        compensation=COMPENSATION_ROLLBACK_OWED if owed else COMPENSATION_NOT_NEEDED,
        detail=HEALTH_DETAIL.get(health, ""),
    )


def probe_startup_health(
    *,
    provider: str,
    assigned_name: str,
    launched_locator: str,
    workspace_id: str,
    lane: str,
    list_rows: Lister,
    read_attestation: AttestationReader,
    read_visible: VisibleReader,
    disposition: str = DISPOSITION_FRESH_LAUNCHED,
    attested_launch: bool = True,
    registry=None,
    polls: int = DEFAULT_PROBE_POLLS,
    interval: float = DEFAULT_PROBE_INTERVAL,
    sleeper: Callable[[float], None] = time.sleep,
) -> SlotHealth:
    """Observe ONE live slot until healthy, terminal, or the deadline expires.

    The single-slot entry point (a caller with one role, and the unit under test for the
    per-slot decision). :func:`probe_session_health` is what a run uses: it shares one
    inventory read across every role of a poll round.

    Returns the LAST verdict, not an optimistic one. ``polls`` / ``interval`` / ``sleeper``
    are injected so tests drive the deadline deterministically without wall clock. Never
    raises — every port failure is already a named non-success.

    ``disposition`` distinguishes a slot this run started from one it adopted. Both are
    probed (an adopted pair sitting on a trust screen is not a usable pair either —
    Answer j#80989 Q4), but only a fresh launch can ever owe a compensation, because only
    what this action started is this action's to undo.
    """
    health = HEALTH_INVENTORY_UNREADABLE
    blocker_id = ""
    attempts = max(1, int(polls))
    for attempt in range(attempts):
        health, blocker_id = _observe_once(
            provider=provider,
            assigned_name=assigned_name,
            launched_locator=launched_locator,
            workspace_id=workspace_id,
            lane=lane,
            rows=_read_rows(list_rows),
            read_attestation=read_attestation,
            read_visible=read_visible,
            attested_launch=attested_launch,
            registry=registry,
        )
        if health == HEALTH_HEALTHY or health not in _RETRYABLE:
            break
        if attempt < attempts - 1:
            sleeper(interval)
    return _slot_health(
        slot_provider=provider,
        assigned_name=assigned_name,
        locator=launched_locator,
        disposition=disposition,
        health=health,
        blocker_id=blocker_id,
    )


#: Outcomes that name a live slot this run is accountable for, and the disposition each
#: carries into the probe. A slot outside this map (a dry-run plan, a read-only surfacing
#: of somebody else's residue) is never probed and never becomes healthy.
_PROBED_DISPOSITION: dict[str, str] = {
    "launched": DISPOSITION_FRESH_LAUNCHED,
    "adopted": DISPOSITION_ADOPTED,
}


@dataclass(frozen=True)
class StartupProbe:
    """The probe's injection seam, carried as one value (Redmine #13948).

    Bundled rather than threaded as four parameters so the composition root
    (:mod:`herdr_session_start`, already at its module-health ceiling) grows by one
    argument, and so a test can drive the whole bounded deadline — no wall clock, no
    live herdr — by passing a fake reader and a counting sleeper.
    """

    visible_reader: Optional[VisibleReader] = None
    # Resolved at CONSTRUCTION, not at class definition: a dataclass default is baked in
    # at import, so a caller (or a test) that rebinds the module-level bound would have no
    # effect at all — silently, which is the worst way for a knob to not work.
    polls: int = field(default_factory=lambda: DEFAULT_PROBE_POLLS)
    interval: float = field(default_factory=lambda: DEFAULT_PROBE_INTERVAL)
    sleeper: Callable[[float], None] = field(default_factory=lambda: time.sleep)
    registry: object = None


def attach_startup_health(
    result,
    *,
    workspace_id: str,
    binary: str,
    runner,
    timeout: float,
    attestation_read: AttestationReader,
    attested_launch: bool = True,
    probe: Optional[StartupProbe] = None,
) -> None:
    """Run pass 3 over a completed run and replace its slots with health-carrying ones.

    The composition root's whole share of #13948: it binds the live ports (``agent list``
    and the visible-pane read) and hands off, so the launcher keeps only orchestration.
    In place, because ``SessionStartResult`` is the run's mutable accumulator.
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
        _list_rows,
    )

    cfg = probe or StartupProbe()
    result.slots = probe_session_health(
        slots=result.slots,
        workspace_id=workspace_id,
        lane=result.lane_id,
        list_rows=lambda: _list_rows(binary, runner, timeout),
        read_attestation=attestation_read,
        read_visible=cfg.visible_reader or live_visible_reader(binary, runner, timeout),
        attested_launch=attested_launch,
        probe=cfg,
    )


def probe_session_health(
    *,
    slots: Sequence[object],
    workspace_id: str,
    lane: str,
    list_rows: Lister,
    read_attestation: AttestationReader,
    read_visible: VisibleReader,
    attested_launch: bool = True,
    probe: Optional[StartupProbe] = None,
) -> list:
    """Probe every accountable slot of a run and return the health-carrying results.

    Runs as a pass of its own, AFTER every launch, so the providers boot concurrently and
    a two-role pair does not pay one deadline per role. Slots that this run only planned
    or surfaced read-only are returned untouched with ``health = not_probed`` — which is
    not success, and is exactly how a ``stale_named_slot`` pair stops exiting 0.

    **One inventory read per poll round, shared by every role** (Redmine #13948 correction,
    j#81034). Two reasons, and both are contract:

    - *Cost*: `session-start` (and `heal_lane_column`, which calls it) previously issued
      exactly one ``agent list``. A per-slot-per-poll read made that count a function of
      how many roles were requested, which broke callers that reason about the herdr call
      sequence — a real regression this correction removes rather than renumbers.
    - *Consistency*: every role in a round is judged against the SAME snapshot. Reading
      per slot lets a pair be classified from two different views of the world, so "claude
      gone, codex live" could be an artifact of read ordering rather than a fact.

    The deadline is bounded and explicit: at most ``probe.polls`` rounds spaced by
    ``probe.interval`` (default 40 x 0.25s = 10s), spent only while at least one slot is
    still in a legitimately transient state. A pair that comes up healthy costs ONE round;
    a terminal verdict (trust screen, drift, unprofiled, attestation mismatch, unwrapped
    launch) short-circuits without waiting. That bound is what `heal_lane_column` inherits.
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (  # noqa: E501
        SlotResult,
    )

    cfg = probe or StartupProbe()
    targets = []
    for index, slot in enumerate(slots):
        disposition = _PROBED_DISPOSITION.get(slot.outcome, "")
        if disposition and slot.locator:
            targets.append((index, slot, disposition))
    if not targets:
        return list(slots)

    verdicts: dict = {}
    attempts = max(1, int(cfg.polls))
    for attempt in range(attempts):
        rows = _read_rows(list_rows)  # the round's single view of the live world
        pending = False
        for index, slot, disposition in targets:
            settled = verdicts.get(index)
            if settled is not None and (
                settled[0] == HEALTH_HEALTHY or settled[0] not in _RETRYABLE
            ):
                continue  # already decided; do not re-read a settled slot
            health, blocker_id = _observe_once(
                provider=slot.provider,
                assigned_name=slot.assigned_name,
                launched_locator=slot.locator,
                workspace_id=workspace_id,
                lane=lane,
                rows=rows,
                read_attestation=read_attestation,
                read_visible=read_visible,
                # The wrap fact is a property of THIS run's launches. An adopted slot was
                # started by an earlier run, whose own wrapper (or lack of one) already
                # decided whether it has a record — and pass 1 only adopts when it does.
                attested_launch=(
                    attested_launch or disposition != DISPOSITION_FRESH_LAUNCHED
                ),
                registry=cfg.registry,
            )
            verdicts[index] = (health, blocker_id)
            if health != HEALTH_HEALTHY and health in _RETRYABLE:
                pending = True
        if not pending:
            break
        if attempt < attempts - 1:
            cfg.sleeper(cfg.interval)

    probed: list = list(slots)
    for index, slot, disposition in targets:
        health, blocker_id = verdicts[index]
        settled = _slot_health(
            slot_provider=slot.provider,
            assigned_name=slot.assigned_name,
            locator=slot.locator,
            disposition=disposition,
            health=health,
            blocker_id=blocker_id,
        )
        probed[index] = SlotResult(
            provider=slot.provider,
            assigned_name=slot.assigned_name,
            outcome=slot.outcome,
            locator=slot.locator,
            detail=slot.detail,
            health=settled.health,
            blocker_id=settled.blocker_id,
            compensation=settled.compensation,
            health_detail=settled.detail,
        )
    return probed


__all__ = (
    "DEFAULT_PROBE_INTERVAL",
    "DEFAULT_PROBE_POLLS",
    "AttestationReader",
    "Lister",
    "VisibleReader",
    "probe_session_health",
    "probe_startup_health",
)
