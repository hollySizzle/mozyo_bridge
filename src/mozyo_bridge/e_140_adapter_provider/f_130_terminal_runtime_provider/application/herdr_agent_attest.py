"""Startup self-attestation self-check, run BEFORE the provider exec (Redmine #13637).

The managed launch no longer execs the provider (``claude`` / ``codex``) directly.
It wraps it in this bounded self-check use-case (Design Answer j#76462 refinement 3):
the wrapper runs *as the herdr-spawned agent process*, so — and only here — it can
inspect its OWN ``os.environ`` and truthfully observe whether the identity triplet
herdr injected via ``--env`` actually reached the process the provider will inherit.
It records that observation as a generation-bound self-attestation
(:mod:`mozyo_bridge.core.state.herdr_identity_attestation`) and then ``exec``s the
provider, replacing itself — so from the provider's point of view nothing changed
except that its parent left a durable, honest record of its boot identity env.

Why a wrapper and not a launcher-side check: herdr exposes no surface returning a
launched process's env, and a live process's env cannot be mutated externally
(POSIX). The launcher can prove it *passed* ``--env`` but not that the value *landed*;
only the spawned process can read its own env. This is that read (j#76456
characterisation, j#76462 Answer).

Generation binding (refinement 2): the record pins the **live locator** the agent
resolves for itself here (``herdr agent list`` self-lookup), the only externally
observable discriminant a later adopt / doctor can compare against the live
inventory — so a stale record from a previous process generation is never re-used.

Non-blocking by contract: every step is best-effort. A failed self-lookup records an
empty locator (which the read side treats as ``stale`` — fail-closed), a failed
store write degrades to an absent record (fail-closed too), and NEITHER stops the
``exec``. Blocking the boot on a missing env would kill the operator's pane; the
adopt / doctor / send-time layers are where the fail-closed enforcement lives, not
here. This wrapper never sends, never mutates a workspace, never closes a pane.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import time
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.core.state.herdr_identity_attestation import (
    IdentityAttestationRecord,
    classify_identity_env,
    record_identity_attestation,
)
from mozyo_bridge.core.state.startup_execution_events import (
    STAGE_ATTESTATION_WRITE_FAILED,
    STAGE_ATTESTATION_WRITE_SUCCEEDED,
    STAGE_PROVIDER_EXEC_CALL_REACHED,
    STAGE_PROVIDER_EXEC_FAILED,
    STAGE_PROVIDER_EXEC_REJECTED,
    STAGE_SELF_LOOKUP_FAILED,
    STAGE_SELF_LOOKUP_STARTED,
    STAGE_SELF_LOOKUP_SUCCEEDED,
    STAGE_SELF_LOOKUP_TIMED_OUT,
    STAGE_WRAPPER_ENTERED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (
    MOZYO_PROVIDER_ARGV0_ENV,
    MOZYO_STARTUP_ACTION_ID_ENV,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    DEFAULT_LANE,
    _agent_locator,
    _norm,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_state import (
    _extract_list_rows,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    COMMAND_TIMEOUT_SECONDS,
    TerminalTransportError,
    resolve_herdr_binary,
)

#: Total wall-clock budget for the pre-exec self-lookup (Redmine #14231, clarification
#: j#84743). The pre-#14231 shape was 3 retries x a 10s per-call timeout = up to ~30s
#: spent BEFORE the provider is exec'd, while the launcher's own startup-health probe
#: only waits 10s (40 x 0.25s) — an inversion in which a "best-effort, non-blocking"
#: lookup could outlive the deadline that decides whether the launch is healthy. This
#: cap keeps the whole lookup inside a small fraction of that probe window; the number
#: of attempts inside it is an implementation detail, but the budget is never zero or
#: negative.
SELF_LOOKUP_TOTAL_BUDGET_SECONDS = 2.0

#: Closed, value-free reasons a bounded self-lookup can fail (Redmine #14231). Each names
#: WHICH observation failed, never a path / payload / env value.
SELF_LOOKUP_REASON_BINARY_UNRESOLVED = "binary_unresolved"
SELF_LOOKUP_REASON_LIST_UNREADABLE = "list_unreadable"
SELF_LOOKUP_REASON_ROW_ABSENT = "row_absent"
SELF_LOOKUP_REASON_ROW_AMBIGUOUS = "row_ambiguous"

#: The attestation write did not happen because no exact locator was resolved (Redmine
#: #14231 coordinator interpretation j#84865). Distinct from a store write that RAISED:
#: nothing was attempted, because an empty-locator record is not a valid exact identity
#: and is no longer written. The action's event projection carries this typed outcome.
ATTESTATION_REASON_LOCATOR_UNAVAILABLE = "locator_unavailable"
#: The attestation store write itself failed (the best-effort writer returned no
#: persisted record). Value-free: names the step, never the store error text.
ATTESTATION_REASON_STORE_WRITE_FAILED = "store_write_failed"

#: The injected ``MOZYO_PROVIDER_ARGV0`` alias did not re-verify as a trusted alias of the
#: exec target at this boundary (#14017's fail-closed check), so the exec was refused.
EXEC_REASON_ARGV0_ALIAS_UNBOUND = "argv0_alias_unbound"
#: The ``exec`` call itself raised (a missing / non-executable target). Value-free.
EXEC_REASON_EXEC_RAISED = "exec_raised"

#: A live ``agent list`` lister: returns raw herdr rows, or ``None`` on any failure.
Lister = Callable[[], Optional[Sequence[Mapping[str, object]]]]


def _build_event_appender(action_id: str):
    """Build the wrapper's ``(stage, bounded_reason="") -> None`` event sink (never raises).

    Returns a no-op when ``action_id`` is empty (an unwrapped launch, an older launcher
    that does not inject :data:`MOZYO_STARTUP_ACTION_ID_ENV`, or a test path) so the
    pre-#14231 launch shape stays byte-invariant. When it is present the sink appends to
    the action's optional projection through the best-effort
    :func:`...startup_execution_events.append_execution_event`, which already swallows
    every failure — an evidence-recording problem must never stop a provider boot.
    """
    if not action_id:
        return lambda stage, bounded_reason="": None

    def _append(stage: str, bounded_reason: str = "") -> None:
        try:
            from mozyo_bridge.core.state.startup_execution_events import (
                append_execution_event,
            )
            from mozyo_bridge.core.state.startup_transaction_fence import (
                StartupTransactionFence,
            )

            append_execution_event(
                StartupTransactionFence(),
                action_id,
                stage,
                bounded_reason=bounded_reason,
            )
        except Exception:  # noqa: BLE001 — evidence recording never blocks the boot
            return

    return _append


def _match_own_locator(
    assigned_name: str, rows: Optional[Sequence[Mapping[str, object]]]
) -> tuple[str, str]:
    """Pick THIS agent's locator out of one ``agent list`` payload (pure, never raises).

    Returns ``(locator, reason)`` — exactly one is non-empty. ``rows is None`` is an
    unreadable read (:data:`SELF_LOOKUP_REASON_LIST_UNREADABLE`), zero matches is
    :data:`SELF_LOOKUP_REASON_ROW_ABSENT` (herdr may not have surfaced the just-started
    agent yet — the ONLY retryable case), more than one is
    :data:`SELF_LOOKUP_REASON_ROW_AMBIGUOUS`, and an exactly-one match carrying an empty
    locator is ``row_absent`` too (a row without a locator identifies nothing).
    """
    if rows is None:
        return "", SELF_LOOKUP_REASON_LIST_UNREADABLE
    matches = [
        row
        for row in rows
        if isinstance(row, Mapping) and _norm(row.get(AGENT_KEY_NAME)) == assigned_name
    ]
    if len(matches) > 1:
        return "", SELF_LOOKUP_REASON_ROW_AMBIGUOUS
    if not matches:
        return "", SELF_LOOKUP_REASON_ROW_ABSENT
    locator = _norm(_agent_locator(matches[0]))
    if not locator:
        return "", SELF_LOOKUP_REASON_ROW_ABSENT
    return locator, ""


def bounded_self_lookup(
    assigned_name: str,
    env: Mapping[str, str],
    *,
    runner=None,
    monotonic=None,
    total_budget_seconds: float = SELF_LOOKUP_TOTAL_BUDGET_SECONDS,
) -> tuple[str, str, str]:
    """Resolve THIS agent's live locator under a total wall-clock budget (never raises).

    Returns ``(locator, stage, bounded_reason)`` where ``stage`` is one of
    :data:`STAGE_SELF_LOOKUP_SUCCEEDED` / :data:`STAGE_SELF_LOOKUP_TIMED_OUT` /
    :data:`STAGE_SELF_LOOKUP_FAILED` and ``bounded_reason`` is a closed
    ``SELF_LOOKUP_REASON_*`` token (empty on success).

    Budget (Redmine #14231, clarification j#84743): the WHOLE lookup — every attempt
    plus the gaps between them — fits inside ``total_budget_seconds``, measured on the
    injected ``monotonic`` clock. Each subprocess timeout is capped to the time actually
    remaining, so the last attempt can never overrun the budget the way the pre-#14231
    3 x 10s retry loop could (which is what inverted the wrapper against the launcher's
    own 10s startup-health probe). A non-positive budget is refused as a caller error via
    ``max(..., 0)`` on the remaining time: the first attempt still runs with a floor, so
    the lookup never degrades into "no observation at all" silently.

    Retry policy: **only** :data:`SELF_LOOKUP_REASON_ROW_ABSENT` is retried, because it
    is the one genuinely transient case (herdr registration lag right after
    ``agent start``). An unresolvable binary, a failed / non-zero / unparseable read, and
    an ambiguous duplicate-name row are all conditions that a 2-second wait cannot fix —
    retrying them would spend the whole budget to reach the same verdict.

    **Terminal-hygiene invariant (defensive; NOT the #14017 root cause).** This runs
    *inside the herdr-spawned pane*, as the wrapper about to exec the interactive
    provider into that same pane. The ``agent list`` child is kept off the pane's
    controlling terminal on every standard fd: ``capture_output`` pipes stdout/stderr,
    and ``stdin=subprocess.DEVNULL`` + ``start_new_session=True`` give it no fd pointing
    at the pane PTY and no controlling-terminal association. (History: #14017 R1 commit
    86fc24bc hypothesised this detach WAS the provider-exit fix; installed dogfood
    refuted it — j#81858 / j#81867 — and the real correction is the exec-target / argv[0]
    decoupling in :func:`cmd_herdr_agent_attest`. This is kept as sound hygiene only.)
    """
    # Resolved at CALL time, not bound as a default: `subprocess.run` / `time.monotonic`
    # are patched at the module attribute by existing regression tests (#14017), and a
    # default-argument binding would freeze the import-time object past those patches.
    run = runner if runner is not None else subprocess.run
    clock = monotonic if monotonic is not None else time.monotonic
    deadline = clock() + max(float(total_budget_seconds), 0.0)
    try:
        binary = resolve_herdr_binary(env).path
    except TerminalTransportError:
        return "", STAGE_SELF_LOOKUP_FAILED, SELF_LOOKUP_REASON_BINARY_UNRESOLVED

    last_reason = SELF_LOOKUP_REASON_ROW_ABSENT
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return "", STAGE_SELF_LOOKUP_TIMED_OUT, last_reason
        # Cap each attempt's own timeout to the remaining budget so no single call can
        # outlive it; never exceed the shared command timeout either.
        attempt_timeout = min(remaining, float(COMMAND_TIMEOUT_SECONDS))
        rows: Optional[Sequence[Mapping[str, object]]]
        try:
            completed = run(
                [binary, "agent", "list"],
                capture_output=True,
                text=True,
                timeout=attempt_timeout,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError):
            rows = None
        else:
            rows = (
                _extract_list_rows(completed.stdout)
                if getattr(completed, "returncode", 1) == 0
                else None
            )
        locator, reason = _match_own_locator(assigned_name, rows)
        if locator:
            return locator, STAGE_SELF_LOOKUP_SUCCEEDED, ""
        if reason != SELF_LOOKUP_REASON_ROW_ABSENT:
            # Not a registration-lag case; a retry inside this budget cannot change it.
            return "", STAGE_SELF_LOOKUP_FAILED, reason
        last_reason = reason


def perform_self_attestation(
    *,
    assigned_name: str,
    workspace_id: str,
    role: str,
    lane: str,
    env: Mapping[str, str],
    replacement_action_id: str = "",
    home=None,
    now: Optional[str] = None,
    append_event=None,
    runner=None,
    monotonic=None,
    total_budget_seconds: float = SELF_LOOKUP_TOTAL_BUDGET_SECONDS,
) -> Optional[IdentityAttestationRecord]:
    """Observe this process's identity env + live locator; record it (best-effort).

    Classifies the ``env`` mapping (the caller passes ``os.environ``) against the
    launcher-expected identity, self-resolves the live locator under a bounded budget
    (:func:`bounded_self_lookup`), and — **only when an exact locator was resolved** —
    upserts a generation-bound record. Returns the record (persisted form when the write
    succeeded, else the in-memory record), or ``None`` when no locator was available and
    therefore nothing was written. Never raises.

    Redmine #14231 (coordinator interpretation j#84865): an empty-locator record is NO
    LONGER written. The attestation store's identity semantics include the exact locator;
    a record without one is not a valid exact identity, and writing it just to have a row
    put an ambiguous state in front of every reader. The failure is instead expressed on
    the action's own append-only event projection as
    ``attestation_write_failed`` + :data:`ATTESTATION_REASON_LOCATOR_UNAVAILABLE` — a
    typed outcome saying "attestation could not be persisted for this action", which is
    exactly what happened. Reading pre-existing empty-locator records stays compatible;
    that compatibility is not a reason to keep writing new ones. The provider boot
    continues either way (the wrapper never blocks the boot); the post-launch gate treats
    missing evidence as ``startup_evidence_unavailable``, never as proof the wrapper
    never ran or the provider exited.

    ``append_event`` is the injected ``(stage, bounded_reason) -> None`` sink for the
    typed stage events; ``None`` disables event recording entirely (every pre-#14231
    caller / test path stays byte-invariant).
    """
    emit = append_event or (lambda stage, bounded_reason="": None)
    verdict, detail = classify_identity_env(
        expected_workspace_id=workspace_id,
        expected_role=role,
        expected_lane=lane,
        env=env,
    )
    emit(STAGE_SELF_LOOKUP_STARTED)
    locator, stage, reason = bounded_self_lookup(
        assigned_name,
        env,
        runner=runner,
        monotonic=monotonic,
        total_budget_seconds=total_budget_seconds,
    )
    emit(stage, reason)
    if not locator:
        # No exact locator -> no attestation write at all (j#84865). The action's event
        # projection carries the typed outcome instead of the store carrying an invalid row.
        emit(STAGE_ATTESTATION_WRITE_FAILED, ATTESTATION_REASON_LOCATOR_UNAVAILABLE)
        return None
    record = IdentityAttestationRecord(
        assigned_name=assigned_name,
        workspace_id=_norm(workspace_id),
        role=_norm(role),
        lane_id=_norm(lane) or DEFAULT_LANE,
        locator=locator,
        verdict=verdict,
        detail=detail,
        observed_at=now,
        replacement_action_id=_norm(replacement_action_id),
    )
    persisted = record_identity_attestation(record, home=home)
    if persisted is None:
        emit(STAGE_ATTESTATION_WRITE_FAILED, ATTESTATION_REASON_STORE_WRITE_FAILED)
        return record
    emit(STAGE_ATTESTATION_WRITE_SUCCEEDED)
    return persisted


def _argv0_alias_binds_to_exec_target(argv0_alias: str, exec_target: str) -> bool:
    """True iff ``argv0_alias`` is a trusted absolute alias of ``exec_target`` (#14017).

    The wrapper is a **separate trust boundary** from the resolver: it re-establishes,
    fail-closed at exec time, the exact binding the resolver made at resolve time
    (``exec_target = realpath(alias)``, see ``agent_provider_executable`` R3-F1). Both
    halves must hold:

    - the ``exec_target`` (``provider_argv[0]``, the file actually ``exec``'d) is an
      absolute, existing regular file already pinned to its **own realpath** — the
      resolver's verified realpath, never a symlink an attacker could swing; and
    - the ``argv0_alias``, resolved at THIS moment, names that **same file**.

    An unrelated absolute, nonexistent, relative, or different-target-symlink alias
    fails every one of those checks and returns ``False``; the caller then fails typed
    and value-free instead of letting an unverified value reach the provider's argv[0]
    (the one input that decides whether Claude stays resident or exits). Value-free by
    construction: it returns only a boolean and neither returns nor raises any path.
    """
    if not (argv0_alias and os.path.isabs(argv0_alias)):
        return False
    if not (exec_target and os.path.isabs(exec_target)):
        return False
    # The exec target must be a real file already at its own realpath — the trust anchor
    # the alias is checked against. A missing target or one that is itself a symlink is
    # not a canonical resolver output and cannot anchor the binding.
    if not os.path.isfile(exec_target) or os.path.realpath(exec_target) != exec_target:
        return False
    # The alias must be the SAME file as the exec target when resolved now. A nonexistent
    # / unrelated / different-target alias never satisfies ``samefile`` (a missing path
    # raises ``OSError`` -> not bound), so an unverified value can never become argv[0].
    try:
        return os.path.samefile(argv0_alias, exec_target)
    except OSError:
        return False


def cmd_herdr_agent_attest(args: argparse.Namespace) -> int:
    """CLI entry: self-attest this agent's boot identity env, then exec the provider.

    Reached only as the wrapped managed launch argv
    (``... herdr agent-attest --assigned-name <NAME> --workspace-id <WS>
    --role <PROVIDER> --lane <LANE> -- <provider argv...>``). Writes the
    self-attestation (best-effort) and then ``exec``s the provider argv, replacing this
    process — so the provider is the real herdr pane occupant and inherits exactly the
    env this wrapper observed. A missing provider argv is the only hard error (nothing
    to exec); every attestation step is non-blocking.

    **Exec-target / argv[0] decoupling (Redmine #14017).** ``provider_argv[0]`` is the
    provider's verified absolute exec-target realpath — the file that is run. When the
    launch injected :data:`MOZYO_PROVIDER_ARGV0_ENV` (a provider whose trusted alias
    differs from that realpath, e.g. Claude's stable ``~/.local/bin/claude`` symlink),
    the provider is ``os.execv``'d at that realpath but handed ``argv[0]=<alias>`` — so
    the exec target stays the trust-pinned realpath while Claude's interactive TUI sees
    the stable alias it needs to stay resident instead of exiting into ``shell_residue``.
    The alias is argv[0] DATA only and is never itself executed.

    **Wrapper-side fail-closed re-verification (R3-F1).** This wrapper is a *separate
    trust boundary* from the resolver, so it does not blindly trust the injected value:
    before the alias can reach argv[0] it re-establishes the resolver's binding here, at
    exec time, via :func:`_argv0_alias_binds_to_exec_target` — the exec target must be an
    absolute realpath of its own and the absolute alias must name that same file. A
    set-but-unbound alias (unrelated absolute, nonexistent, relative, or a symlink to a
    different target) is a spoofed / drifted input; it never becomes argv[0] and fails
    typed and value-free (``die``), never a silent successful launch that would re-trigger
    the provider exit and mislead startup health. The var is dropped from the env the
    provider inherits (before validation, so the value never lingers) so, apart from that
    one argv[0] token, the provider's launch is byte-invariant. Without the var (a normal
    / unsymlinked / Codex / older-wrapper launch) the provider is ``os.execvp``'d at
    ``provider_argv[0]`` unchanged — the honest fallback that keeps the realpath on both
    the exec target and argv[0] and never weakens the trust boundary by execing an alias.
    """
    provider_argv = list(getattr(args, "provider_argv", None) or [])
    # argparse REMAINDER keeps a leading ``--`` separator; drop it so argv[0] is the
    # provider executable.
    if provider_argv and provider_argv[0] == "--":
        provider_argv = provider_argv[1:]
    if not provider_argv:
        from mozyo_bridge.shared.errors import die

        die(
            "herdr agent-attest requires a provider command after `--` to exec "
            "(usage: herdr agent-attest --assigned-name ... -- <provider> [args...])"
        )
        raise AssertionError("unreachable")

    env = os.environ
    # Redmine #14231: the reserved startup action_id rides an `--env` key (see
    # MOZYO_STARTUP_ACTION_ID_ENV); absent (an unwrapped / older-launcher / test path) the
    # sink is a no-op and every stage append is skipped, so nothing here can fail a launch.
    append_event = _build_event_appender(_norm(env.get(MOZYO_STARTUP_ACTION_ID_ENV, "")))
    append_event(STAGE_WRAPPER_ENTERED)
    perform_self_attestation(
        assigned_name=_norm(getattr(args, "assigned_name", "")),
        workspace_id=_norm(getattr(args, "workspace_id", "")),
        role=_norm(getattr(args, "role", "")),
        lane=_norm(getattr(args, "lane", "")),
        env=env,
        replacement_action_id=_norm(getattr(args, "replacement_action_id", "")),
        append_event=append_event,
    )
    # Redmine #14017: the exec target is always provider_argv[0] (the verified realpath);
    # the trusted argv[0] alias, if any, arrives out-of-band via MOZYO_PROVIDER_ARGV0. It
    # is a wrapper instruction, not identity the provider should carry, so drop it from the
    # inherited env (keeps the provider's env byte-invariant; only argv[0] differs). It is
    # popped BEFORE it is validated so the value never lingers in the inherited env, even
    # on the fail-closed path below.
    exec_target = provider_argv[0]
    argv0_alias = _norm(os.environ.pop(MOZYO_PROVIDER_ARGV0_ENV, ""))
    if argv0_alias:
        # A wrapped launch declared a trusted argv[0] alias. Re-verify the alias->exec-target
        # binding fail-closed at THIS boundary (R3-F1): a set-but-unbound value (unrelated
        # absolute, nonexistent, relative, or a symlink to a different target) is a spoofed /
        # drifted input. It must never reach argv[0], and it must fail typed and value-free —
        # NOT silently launch with the realpath argv[0], which would both re-trigger the
        # provider exit this fix closes and hide the violation from startup health.
        if not _argv0_alias_binds_to_exec_target(argv0_alias, exec_target):
            from mozyo_bridge.shared.errors import die

            append_event(
                STAGE_PROVIDER_EXEC_REJECTED, EXEC_REASON_ARGV0_ALIAS_UNBOUND
            )
            die(
                "MOZYO_PROVIDER_ARGV0 did not verify as a trusted alias bound to the "
                "provider exec target (an absolute exec-target realpath named by an "
                "absolute same-file alias); refusing to launch with an unverified argv[0]"
            )
            raise AssertionError("unreachable")
        # Exec the verified realpath, but present the trusted alias as argv[0]. os.execv
        # takes an explicit path, so PATH is never consulted and the alias is never run.
        # The `provider_exec_call_reached` event is appended BEFORE the call because a
        # successful exec replaces this process — there is no "after" in which to write it
        # (Redmine #14231: the event proves control flow reached the exec call, never that
        # the provider executed; live confirmation only comes from an inventory join).
        append_event(STAGE_PROVIDER_EXEC_CALL_REACHED)
        try:
            os.execv(exec_target, [argv0_alias, *provider_argv[1:]])
        except OSError:
            append_event(STAGE_PROVIDER_EXEC_FAILED, EXEC_REASON_EXEC_RAISED)
            raise
        raise AssertionError("unreachable")  # pragma: no cover - execv replaces process
    # No alias var — an unwrapped / unsymlinked / Codex launch, or an older wrapper that
    # ignores the key (version skew). Keep the realpath on both the exec target and argv[0]:
    # the honest, byte-invariant fallback that never weakens the trust boundary by execing
    # an alias.
    append_event(STAGE_PROVIDER_EXEC_CALL_REACHED)
    try:
        os.execvp(exec_target, provider_argv)
    except OSError:
        append_event(STAGE_PROVIDER_EXEC_FAILED, EXEC_REASON_EXEC_RAISED)
        raise
    raise AssertionError("unreachable")  # pragma: no cover - execvp replaces process


__all__ = (
    "ATTESTATION_REASON_LOCATOR_UNAVAILABLE",
    "ATTESTATION_REASON_STORE_WRITE_FAILED",
    "EXEC_REASON_ARGV0_ALIAS_UNBOUND",
    "EXEC_REASON_EXEC_RAISED",
    "SELF_LOOKUP_REASON_BINARY_UNRESOLVED",
    "SELF_LOOKUP_REASON_LIST_UNREADABLE",
    "SELF_LOOKUP_REASON_ROW_ABSENT",
    "SELF_LOOKUP_REASON_ROW_AMBIGUOUS",
    "SELF_LOOKUP_TOTAL_BUDGET_SECONDS",
    "Lister",
    "bounded_self_lookup",
    "cmd_herdr_agent_attest",
    "perform_self_attestation",
)
