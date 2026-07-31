"""Pre-send startup-admission gate wiring for ``orchestrate_handoff`` (Redmine #13760).

``orchestrate_handoff`` (``application/commands.py``) is the largest module under the
module-health gate, so — exactly like the gateway-route gate (#12918) — the *gate* lives
here and ``commands.py`` keeps a single :func:`admit_receiver_startup_or_die` call.

The split is not only about line count. It puts the three concerns where they belong:

- the **data** (which screens a provider shows) is in the provider profile;
- the **classification** (does this pane show one) is the pure e_140 admission evaluator
  (:mod:`...f_130_terminal_runtime_provider.application.herdr_startup_admission`);
- the **routing consequence** (refuse the send, emit the structured outcome, fail closed)
  is here, with every other ``orchestrate_handoff`` gate.

So no provider-specific string ever appears in a transport or a command module — a
provider re-wording its trust dialog is a data edit, not a source edit (j#77948 boundary).

Why the gate is at the SEND boundary and not at readiness-probe time (j#77947 Q2): the
live #13582 j#77917 dispatch had a readiness projection that said ``status=ok`` while the
worker sat on the trust confirmation. Readiness is a different *moment* than the send and
a different *question* than "can this pane accept a body". This gate asks the second
question at the last instant before the first keystroke.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    make_outcome,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_admission import (
    ADMISSION_BLOCKED,
    evaluate_startup_admission,
    startup_admission_record_lines,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_update_authority_preflight import (  # noqa: E501
    UpdaterTargetProbe,
    evaluate_update_authority,
)
from mozyo_bridge.shared.errors import die

#: The blocked reason for a receiver that is mid-startup. A distinct token (not
#: ``receiver_blocked``, which is a POST-injection runtime block, and not
#: ``precondition_not_idle``, which is a busy pane): a startup screen is a live,
#: non-blank, ready-LOOKING pane, and telling those apart is the whole audit value.
REASON_STARTUP_INTERACTION = "receiver_startup_interaction_required"

#: An unreadable receiver keeps the EXISTING transport-failure vocabulary (j#77947
#: invariant 4). It must never be recorded as a startup blocker — "we could not see the
#: pane" and "the pane was showing a trust prompt" are different facts — and it must never
#: decay to admitted, which would type into a receiver nobody could see.
REASON_UNREADABLE = "target_unavailable"

#: The receiver's provider runs a different install than its own updater writes to, or
#: the executable this lane bound is no longer the one that is there (Redmine #14741).
#: Distinct from every startup-screen reason: the pane may be perfectly ready, and the
#: send would even land — but the lane is running a binary that no update can fix, which
#: is how the #14741 gateway loop stayed invisible while the transport recorded delivery.
REASON_UPDATE_AUTHORITY_SPLIT = "receiver_update_authority_split"


def _refuse_wrong_binary_or_pass(
    *,
    receiver: str,
    target: str,
    emit: Callable[..., None],
    record_format: str,
    record_command: Optional[str],
    anchor: Any,
    mode: Optional[str],
    kind: Optional[str],
    source: Optional[str],
    execution_root: Any,
    role_profile_contract: Optional[str],
    duplicate_lane_panes: Optional[list],
    ledger: Optional[Callable[[Any], None]],
    updater_targets: Optional[UpdaterTargetProbe],
    bound_executable_identity: str,
    observed_executable_identity: str,
) -> None:
    """The #14741 half of the pre-send fence: refuse a send to a wrong-binary lane.

    Runs only once the startup screen is CLEAR — the two questions are different and the
    order matters. A startup screen is about what the pane can accept *right now*; this
    is about whether the lane is running a binary that an update can even reach. A pane
    can be perfectly ready and still be the #14741 shape, which is precisely why the
    original loop was invisible: every projection said ready, the send "landed", and the
    process exited seconds later into a self-heal that restarted the same old binary.

    Only a **positively demonstrated** wrong binary refuses
    (:attr:`...UpdateAuthority.proven_wrong_binary`): the updater writes to a different
    install (``split``), or the executable is not the one this lane bound (``drifted``).
    An ``unknown`` authority does NOT refuse a send here — see that property's docstring
    for why turning the honest common case into a workspace-wide outage is not
    fail-closed. It still keeps the lane out of a green startup-health verdict, and it
    still refuses a *re-launch* (:attr:`...UpdateAuthority.admits_relaunch`).

    Zero-send by construction: this runs before the first injection, so a refusal has
    typed nothing and pressed no Enter.
    """
    authority = evaluate_update_authority(
        receiver,
        updater_targets=updater_targets,
        bound_identity=bound_executable_identity,
        observed_identity=observed_executable_identity,
    )
    if not authority.proven_wrong_binary:
        return

    outcome = make_outcome(
        status="blocked",
        reason=REASON_UPDATE_AUTHORITY_SPLIT,
        receiver=receiver,
        target=target,
        anchor=anchor,
        mode=mode,
        kind=kind,
        notification_marker=None,
        source=source,
        execution_root=execution_root,
    )
    emit(
        outcome,
        record_format=record_format,
        command=record_command,
        duplicate_lane_panes=duplicate_lane_panes or None,
        role_profile_contract=role_profile_contract,
    )
    if ledger is not None:
        ledger(outcome)
    die(
        f"blocked: the {receiver} receiver is not running the binary its own updater "
        f"writes to (update authority: {authority.authority}, executable binding: "
        f"{authority.binding}). target={target}. Nothing was typed and Enter was never "
        "pressed. Updating the provider cannot change what this lane runs, so an update "
        "that exits 0 is not a fix and a self-heal will restart the same binary. "
        "Re-point the trusted override at the install the updater owns, or remove the "
        "extra install — mozyo never relaxes to PATH first-match, never rewrites the "
        "override for you, and never accepts an update prompt on your behalf."
    )


def admit_receiver_startup_or_die(
    *,
    herdr_send: bool,
    receiver: str,
    target: str,
    read_lines: int,
    capture_pane: Callable[[str, int], str],
    emit: Callable[..., None],
    record_format: str,
    record_command: Optional[str],
    anchor: Any = None,
    mode: Optional[str] = None,
    kind: Optional[str] = None,
    source: Optional[str] = None,
    execution_root: Any = None,
    role_profile_contract: Optional[str] = None,
    duplicate_lane_panes: Optional[list] = None,
    ledger: Optional[Callable[[Any], None]] = None,
    updater_targets: Optional[UpdaterTargetProbe] = None,
    bound_executable_identity: str = "",
    observed_executable_identity: str = "",
) -> None:
    """Read the receiver once, at action time, and refuse a send it cannot accept.

    Runs after every die-able gate and target resolution, and **before** the first
    injection — so a refusal is zero-send by construction: no ``send_text``, no Enter,
    no C-u, no marker, no ACK, and nothing for the receiver to have half-received.

    Under the **tmux** backend this is the unchanged pane-snapshot preflight (the single
    ``capture_pane`` the rail always did). Under **herdr** the same single read is also
    classified against the resolved receiver provider's declared startup screens; a match
    (or an unreadable pane, or an unprofiled provider) emits a structured blocked outcome
    and ``die``s. An ADMITTED herdr send performs exactly the read it always performed and
    proceeds byte-for-byte as before.

    mozyo never *answers* the screen it finds. Clearing a trust / setup / login prompt is
    an operator action in the provider's own UI — the Enter that "delivered" j#77917 was
    absorbed as that dialog's default answer, which is how an Implementation Request was
    destroyed while the transport reported ``sent``.
    """
    if not herdr_send:
        # Byte-identical tmux path: the internal snapshot preflight, unchanged. The
        # startup vocabulary is herdr-scoped (that is where the managed-lane launch
        # topology and the #13760 evidence live); tmux keeps its existing behaviour.
        capture_pane(target, read_lines)
        return

    admission = evaluate_startup_admission(
        provider_id=receiver,
        read_visible=lambda: capture_pane(target, read_lines),
    )
    if admission.admitted:
        _refuse_wrong_binary_or_pass(
            receiver=receiver,
            target=target,
            emit=emit,
            record_format=record_format,
            record_command=record_command,
            anchor=anchor,
            mode=mode,
            kind=kind,
            source=source,
            execution_root=execution_root,
            role_profile_contract=role_profile_contract,
            duplicate_lane_panes=duplicate_lane_panes,
            ledger=ledger,
            updater_targets=updater_targets,
            bound_executable_identity=bound_executable_identity,
            observed_executable_identity=observed_executable_identity,
        )
        return

    blocked_reason = (
        REASON_STARTUP_INTERACTION
        if admission.outcome == ADMISSION_BLOCKED
        else REASON_UNREADABLE
    )
    outcome = make_outcome(
        status="blocked",
        reason=blocked_reason,
        receiver=receiver,
        target=target,
        anchor=anchor,
        mode=mode,
        kind=kind,
        notification_marker=None,
        source=source,
        execution_root=execution_root,
        startup_admission=admission.to_telemetry_dict(),
    )
    emit(
        outcome,
        record_format=record_format,
        command=record_command,
        duplicate_lane_panes=duplicate_lane_panes or None,
        role_profile_contract=role_profile_contract,
        startup_admission_lines=startup_admission_record_lines(admission),
    )
    if ledger is not None:
        # Ledger the refusal like every other terminal herdr outcome (#13300): a lane
        # that refused to send must be visible to the glance / supervisor projections,
        # not indistinguishable from a send that was never attempted.
        ledger(outcome)
    die(
        f"blocked: the {receiver} receiver did not admit a send (startup admission: "
        f"{admission.outcome}"
        + (f", blocker={admission.blocker_id}" if admission.blocker_id else "")
        + f"). target={target}. Nothing was typed and Enter was never pressed — a "
        "provider startup screen (trust confirmation / first-run setup / login) has no "
        "composer, and a blind Enter accepts its default answer instead of submitting "
        "the request (Redmine #13760). An operator clears the screen in the receiver's "
        "own UI, or a fresh receiver is launched past it; then re-issue THIS SAME "
        "durable anchor through the same high-level command — this refusal consumed no "
        "delivery, so the re-issue lands exactly once. Do not hand-type the body, and "
        "do not send raw keys."
    )
    raise AssertionError("unreachable")


__all__ = (
    "REASON_STARTUP_INTERACTION",
    "REASON_UNREADABLE",
    "admit_receiver_startup_or_die",
)
