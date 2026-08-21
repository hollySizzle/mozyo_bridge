"""Typed ``sublane retire`` application facade shared by CLI and supervisor (#15066).

The command used to own the preflight and destructive-intent dispatch inline.  That made the
workspace supervisor choose between shelling out to the CLI (and parsing prose) or duplicating the
retire contract.  This module is the single programmatic boundary instead: a typed request goes in
and a typed result reports whether the lane retired, was already retired, was blocked, was deferred,
or became uncertain.

No Git cleanup is performed here.  Worktree and branch cleanup remain the operator runbook because
Git has no non-force primitive that atomically checks the lane identity while removing the path/ref.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


RETIRE_INTENT_PREFLIGHT = "preflight"
RETIRE_INTENT_EXECUTE = "execute"
RETIRE_INTENT_MIGRATE_HIBERNATED_LEGACY = "migrate_hibernated_legacy"
RETIRE_INTENT_RECONCILE_HIBERNATED_LIVE = "reconcile_hibernated_live"
RETIRE_INTENT_HIBERNATED_BOUND = "retire_hibernated_bound"
RETIRE_INTENT_ACTIVE_LIVE_ZERO = "retire_active_live_zero"
RETIRE_INTENT_ACTIVE_UNBOUND_LIVE_ZERO = "retire_active_unbound_live_zero"
RETIRE_INTENT_HIBERNATED_UNBOUND_LIVE_ZERO = (
    "retire_hibernated_unbound_live_zero"
)

RETIRE_INTENTS = frozenset(
    {
        RETIRE_INTENT_PREFLIGHT,
        RETIRE_INTENT_EXECUTE,
        RETIRE_INTENT_MIGRATE_HIBERNATED_LEGACY,
        RETIRE_INTENT_RECONCILE_HIBERNATED_LIVE,
        RETIRE_INTENT_HIBERNATED_BOUND,
        RETIRE_INTENT_ACTIVE_LIVE_ZERO,
        RETIRE_INTENT_ACTIVE_UNBOUND_LIVE_ZERO,
        RETIRE_INTENT_HIBERNATED_UNBOUND_LIVE_ZERO,
    }
)

RETIRE_RESULT_RETIRED = "retired"
RETIRE_RESULT_BLOCKED = "blocked"
RETIRE_RESULT_DEFERRED = "deferred"
RETIRE_RESULT_UNCERTAIN = "uncertain"
RETIRE_RESULT_ALREADY_RETIRED = "already_retired"

REASON_PREFLIGHT_ONLY = "preflight_only"
REASON_INTENT_NOT_APPLICABLE = "retire_intent_not_applicable"
REASON_IDENTITY_UNRESOLVED = "retire_identity_unresolved"
REASON_IDENTITY_CHANGED = "retire_identity_changed"
REASON_APPLICATION_ERROR = "retire_application_error"
#: Separator between :data:`REASON_APPLICATION_ERROR` and the failure-kind token (Redmine #15840).
#: The reason stays a prefix match for every pre-#15840 caller: ``retire_application_error``
#: becomes ``retire_application_error:os_error``, never a different token.
REASON_APPLICATION_ERROR_SEPARATOR = ":"

REASON_CLEANUP_ATOMIC_GUARD_UNAVAILABLE = "cleanup_atomic_guard_unavailable"
#: ``--worktree-absent`` (Redmine #15789) was combined with an intent it does not apply to. It
#: modifies exactly the two BOUND terminal retires; the unbound rails never had a checkout in
#: scope to begin with, and the guarded close / migration / reconcile intents genuinely need one.
#: Rejected as a zero-write refusal rather than silently ignored, so the flag never reads as
#: honoured where it changed nothing.
REASON_WORKTREE_ABSENT_NOT_APPLICABLE = "worktree_absent_intent_not_applicable"
#: ``--worktree-absent`` was supplied but its evidence did not verify and named no typed reason
#: of its own. The resolver's own reason is reported verbatim whenever it has one (the #14695
#: j#93807 F2 discipline); this is only the fallback.
REASON_ABSENT_WORKTREE_UNPROVEN = "absent_worktree_unproven"

#: The intents ``--worktree-absent`` modifies: the BOUND metadata-only terminal retires, whose
#: whole effect is a lifecycle CAS and whose only use for the checkout was the two facts
#: :mod:`...sublane_absent_worktree_evidence` re-derives from git's surviving entry.
#: The failure kind when the raised exception matches nothing in the closed table below.
#:
#: Redmine #15840 review j#109671 ``finding_unsafeexceptiontype``: the first attempt appended
#: ``type(exc).__name__``, described as safe because "a class name is an identifier". That was
#: WRONG and the review reproduced it — ``type()`` accepts an ARBITRARY string as the class name,
#: so ``type("SECRET_TOKEN_VALUE_123", (RuntimeError,), {})`` produced
#: ``retire_application_error:SECRET_TOKEN_VALUE_123`` and passed an ``isidentifier()`` pin that
#: claimed to prevent exactly that. A class name is identifier-SHAPED caller data, never a
#: trusted literal. Anything that reaches a durable record must come out of the closed table in
#: this module and nowhere else.
REASON_EXCEPTION_UNCLASSIFIED = "unclassified"

#: ``(trusted exception class, literal token)``, most specific first. The token is the ONLY thing
#: that can reach a durable record, and every one of them is written here — no value derived from
#: the raised object is ever emitted.
#:
#: Kept deliberately small and centred on the distinction the diagnostic exists to make (Redmine
#: #15789 j#109134): did an external command fail, or did our own logic break? Extending it is a
#: deliberate edit of this table, which is the point.
_DURABLE_FAILURE_KINDS: tuple[tuple[type, str], ...] = (
    # Subprocess failures — the class that cost the round trip this issue was opened for.
    (subprocess.TimeoutExpired, "subprocess_timeout"),
    (subprocess.CalledProcessError, "called_process_error"),
    (subprocess.SubprocessError, "subprocess_error"),
    # Filesystem / OS. FileNotFoundError & friends are OSError subclasses and land here.
    (FileNotFoundError, "file_not_found"),
    (PermissionError, "permission_denied"),
    (OSError, "os_error"),
    # Our own logic breaking.
    (KeyError, "key_error"),
    (AttributeError, "attribute_error"),
    (TypeError, "type_error"),
    (ValueError, "value_error"),
)


def _durable_failure_kind(exc: BaseException) -> str:
    """The closed-vocabulary token for ``exc`` — total, and never caller-derived (#15840).

    Two properties are load-bearing, both from review j#109671:

    - **Closed vocabulary** (``finding_unsafeexceptiontype``). Every possible return value is a
      literal from :data:`_DURABLE_FAILURE_KINDS` or :data:`REASON_EXCEPTION_UNCLASSIFIED`. No
      string carried by the exception — name, message, args — can reach the caller. That is what
      makes "this cannot leak a secret" a property of the code rather than an assumption about
      how exception classes happen to be named.

    - **Totality** (``finding_terminalhandlerescape``). This runs inside the retire application's
      broad terminal handler, which is the last line before the caller. The first attempt read
      ``type(exc).__name__`` there; a metaclass can define ``__name__`` as a property that raises,
      and the review reproduced a ``RuntimeError`` escaping the handler instead of an
      ``uncertain`` result — breaking the #15066 contract that unexpected failures reach callers
      as a typed result. So: identity comparison only (``is`` never invokes user code), no
      ``__name__``, no ``isinstance`` (a metaclass can hook ``__instancecheck__``), no dict
      lookup (a metaclass can hook ``__hash__``), and the whole walk wrapped so that even a
      hostile ``__mro__`` degrades to the fixed literal rather than raising.
    """
    try:
        mro = type(exc).__mro__
    except BaseException:  # noqa: BLE001 - a hostile metaclass must not escape the handler
        return REASON_EXCEPTION_UNCLASSIFIED
    try:
        for base in mro:
            for known, token in _DURABLE_FAILURE_KINDS:
                if base is known:
                    return token
    except BaseException:  # noqa: BLE001 - likewise for a hostile __mro__ sequence
        return REASON_EXCEPTION_UNCLASSIFIED
    return REASON_EXCEPTION_UNCLASSIFIED

_WORKTREE_ABSENT_INTENTS = frozenset(
    {RETIRE_INTENT_ACTIVE_LIVE_ZERO, RETIRE_INTENT_HIBERNATED_BOUND}
)


@dataclass(frozen=True)
class RetireAssertions:
    """Durable facts the common preflight enforces; every default fails closed."""

    issue_closed: bool = False
    callbacks_drained: bool = False
    verification_passed: bool = False
    durable_record_recorded: bool = False
    target_identity_known: bool = False
    latest_generation_admissible: bool = False
    latest_generation_blocked_reason: str = ""


@dataclass(frozen=True)
class RetireIdentity:
    """The exact lifecycle identity measured independently of the action request."""

    workspace: str
    issue: str
    lane: str
    lane_generation: int
    revision: int

    @property
    def complete(self) -> bool:
        return bool(
            self.workspace
            and self.issue
            and self.lane
            and isinstance(self.lane_generation, int)
            and not isinstance(self.lane_generation, bool)
            and self.lane_generation > 0
            and isinstance(self.revision, int)
            and not isinstance(self.revision, bool)
            and self.revision > 0
        )


@dataclass(frozen=True)
class RetireApplicationRequest:
    """One exact retire request, independent of argparse and stdout rendering."""

    repo_root: Path
    issue: str
    lane_label: str
    assertions: RetireAssertions
    home: Optional[Path] = None
    intent: str = RETIRE_INTENT_PREFLIGHT
    worktree: Optional[str] = None
    branch: Optional[str] = None
    integration_branch: Optional[str] = None
    journal: Optional[str] = None
    expect_lane_generation: int = 0
    expect_lane_revision: int = 0
    integration_journal: Optional[str] = None
    expected_identity: Optional[RetireIdentity] = None
    #: Redmine #15789: the caller asserts the recorded checkout is GONE and opts the BOUND
    #: terminal retire onto the git-administrative-entry evidence path. Default ``False`` keeps
    #: every existing caller byte-for-byte: the checkout stays in preflight scope and a wiped
    #: path still blocks with ``worktree_missing_after_reboot`` exactly as before.
    worktree_absent: bool = False

    def __post_init__(self) -> None:
        if self.intent not in RETIRE_INTENTS:
            raise ValueError(f"unknown retire intent: {self.intent!r}")

    def as_namespace(self):
        """Compatibility shape for the seven existing, independently reviewed intent rails."""
        import argparse

        selected = self.intent
        return argparse.Namespace(
            repo=str(self.repo_root),
            home=self.home,
            issue=self.issue,
            lane_label=self.lane_label,
            worktree=self.worktree,
            branch=self.branch,
            integration_branch=self.integration_branch,
            journal=self.journal,
            expect_lane_generation=self.expect_lane_generation,
            expect_lane_revision=self.expect_lane_revision,
            integration_journal=self.integration_journal,
            execute=selected == RETIRE_INTENT_EXECUTE,
            migrate_hibernated_legacy=(
                selected == RETIRE_INTENT_MIGRATE_HIBERNATED_LEGACY
            ),
            reconcile_hibernated_live=(
                selected == RETIRE_INTENT_RECONCILE_HIBERNATED_LIVE
            ),
            retire_hibernated_bound=selected == RETIRE_INTENT_HIBERNATED_BOUND,
            retire_active_live_zero=selected == RETIRE_INTENT_ACTIVE_LIVE_ZERO,
            retire_active_unbound_live_zero=(
                selected == RETIRE_INTENT_ACTIVE_UNBOUND_LIVE_ZERO
            ),
            retire_hibernated_unbound_live_zero=(
                selected == RETIRE_INTENT_HIBERNATED_UNBOUND_LIVE_ZERO
            ),
            worktree_absent=self.worktree_absent,
        )


@dataclass(frozen=True)
class RetireApplicationResult:
    """Programmatic retire result; exceptions never masquerade as a deterministic refusal."""

    state: str
    reason: str = ""
    mutated: bool = False
    uncertain: bool = False
    preflight: Optional[object] = None
    intents: Optional[object] = None

    @property
    def retire_ok(self) -> bool:
        return self.state in (RETIRE_RESULT_RETIRED, RETIRE_RESULT_ALREADY_RETIRED)

    @property
    def legacy_cli_ok(self) -> bool:
        """Preserve the historical successful read-only preflight exit status."""
        if self.state == RETIRE_RESULT_DEFERRED and self.reason in (
            REASON_PREFLIGHT_ONLY,
            REASON_INTENT_NOT_APPLICABLE,
        ):
            return bool(self.preflight and self.preflight.preflight.may_retire)
        return self.retire_ok

    def as_payload(self) -> dict[str, object]:
        return {
            "state": self.state,
            "reason": self.reason,
            "mutated": self.mutated,
            "uncertain": self.uncertain,
            "cleanup": {
                "state": "cleanup_blocked" if self.retire_ok else "not_started",
                "reason": (
                    REASON_CLEANUP_ATOMIC_GUARD_UNAVAILABLE if self.retire_ok else ""
                ),
                "worktree_removed": False,
                "local_branch_deleted": False,
                "remote_branch_deleted": False,
            },
        }


def _measured_identity(target, *, issue: str) -> Optional[RetireIdentity]:
    if target is None:
        return None
    measured_issue = getattr(target, "issue", None)
    identity = RetireIdentity(
        workspace=str(getattr(target, "workspace", "") or ""),
        # Production evidence targets carry the lane row's owner issue. The fallback keeps
        # backward-compatible injected test doubles usable; an actual row with a blank owner
        # remains blank and therefore incomplete/fail-closed.
        issue=str(issue if measured_issue is None else measured_issue or ""),
        lane=str(getattr(target, "lane", "") or ""),
        lane_generation=getattr(target, "lane_generation", 0),
        revision=getattr(target, "revision", 0),
    )
    return identity if identity.complete else None


def _is_already_state(value: object) -> bool:
    state = str(getattr(value, "state", "") or "")
    return state in ("already_retired", "verified_noop")


def run_retire_application(request: RetireApplicationRequest) -> RetireApplicationResult:
    """Run common preflight + one intent with an action-time exact-identity fence."""
    # Local imports avoid a command/application import cycle while retaining the already-reviewed
    # use case and intent rails as the single implementation of their respective contracts.
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: E501
        resolve_retire_evidence_target,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (  # noqa: E501
        LiveSublaneGitOperations,
        LiveSublaneLifecycleOps,
        SublaneRetireUseCase,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_retire_intents import (  # noqa: E501
        dispatch_retire_intent,
    )

    args = request.as_namespace()
    # Redmine #15789: the flag is refused ahead of every read and every probe, so a
    # non-applicable combination costs nothing and cannot be mistaken for a run that honoured it.
    if request.worktree_absent and request.intent not in _WORKTREE_ABSENT_INTENTS:
        return RetireApplicationResult(
            state=RETIRE_RESULT_BLOCKED,
            reason=REASON_WORKTREE_ABSENT_NOT_APPLICABLE,
        )
    try:
        target = resolve_retire_evidence_target(
            args, request.repo_root, home=request.home
        )
        measured = _measured_identity(target, issue=request.issue)
        if request.expected_identity is not None:
            if measured is None:
                return RetireApplicationResult(
                    state=RETIRE_RESULT_BLOCKED, reason=REASON_IDENTITY_UNRESOLVED
                )
            if measured != request.expected_identity:
                return RetireApplicationResult(
                    state=RETIRE_RESULT_BLOCKED, reason=REASON_IDENTITY_CHANGED
                )

        # Redmine #15789: the absent-checkout evidence is resolved HERE, before the preflight
        # decides scope, and a refusal ends the run. Deciding scope from the caller's assertion
        # and leaving the proof to the intent rail was measurably wrong: a `--worktree-absent`
        # preflight-only run (or one against a non-herdr repo, where the rail returns "not
        # applicable" before its own guard) reported `retire_ok` for a checkout that was in fact
        # PRESENT and dirty. The checkout only leaves scope once its absence is proven.
        absent_worktree = None
        if request.worktree_absent:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_absent_worktree_evidence import (  # noqa: E501
                resolve_absent_worktree_evidence,
            )

            absent_worktree = resolve_absent_worktree_evidence(
                request.repo_root,
                worktree=request.worktree,
                branch=request.branch,
                lane_label=request.lane_label,
            )
            if not absent_worktree.admissible:
                return RetireApplicationResult(
                    state=RETIRE_RESULT_BLOCKED,
                    reason=absent_worktree.reason or REASON_ABSENT_WORKTREE_UNPROVEN,
                )
        # A proven-absent checkout leaves preflight scope for the same reason the unbound rails'
        # never entered it: a path that is not there can be neither dirty nor cleaned up, so
        # neither probe describes the lane being terminalized.
        checkout_in_scope = (
            request.intent
            not in (
                RETIRE_INTENT_ACTIVE_UNBOUND_LIVE_ZERO,
                RETIRE_INTENT_HIBERNATED_UNBOUND_LIVE_ZERO,
            )
            and absent_worktree is None
        )
        worktree_dirty_override = None
        worktree_missing = False
        if request.worktree and checkout_in_scope:
            try:
                worktree_missing = not Path(request.worktree).expanduser().is_dir()
            except OSError:
                worktree_missing = False
            worktree_dirty_override = LiveSublaneGitOperations(
                repo_root=Path(request.worktree)
            ).worktree_dirty()

        outcome = SublaneRetireUseCase(
            LiveSublaneLifecycleOps(repo_root=request.repo_root)
        ).run(
            issue=request.issue,
            lane_label=request.lane_label,
            worktree_path=request.worktree,
            branch=request.branch,
            integration_branch=request.integration_branch,
            assertions=request.assertions,
            worktree_dirty_override=worktree_dirty_override,
            worktree_missing=worktree_missing,
            checkout_in_scope=checkout_in_scope,
        )
        if not outcome.preflight.may_retire:
            return RetireApplicationResult(
                state=RETIRE_RESULT_BLOCKED,
                reason=str(outcome.preflight.decision.primary_reason or "retire_preflight_blocked"),
                preflight=outcome,
            )
        if request.intent == RETIRE_INTENT_PREFLIGHT:
            return RetireApplicationResult(
                state=RETIRE_RESULT_DEFERRED,
                reason=REASON_PREFLIGHT_ONLY,
                preflight=outcome,
            )

        intents = dispatch_retire_intent(
            args,
            request.repo_root,
            may_retire=True,
            worktree=request.worktree,
            evidence_target=target,
            absent_worktree=absent_worktree,
        )
        verdict = intents.actuated
        if verdict is None:
            return RetireApplicationResult(
                # Preserve the historical CLI contract for an intent outside the repository's
                # backend while making the programmatic result explicitly non-retired.  The
                # supervisor therefore performs no cleanup and retries only after fresh facts.
                state=RETIRE_RESULT_DEFERRED,
                reason=REASON_INTENT_NOT_APPLICABLE,
                preflight=outcome,
                intents=intents,
            )
        if not bool(getattr(verdict, "ok", False)):
            return RetireApplicationResult(
                state=RETIRE_RESULT_BLOCKED,
                reason=str(getattr(verdict, "reason", "") or "retire_intent_blocked"),
                preflight=outcome,
                intents=intents,
            )
        already = _is_already_state(verdict)
        return RetireApplicationResult(
            state=(RETIRE_RESULT_ALREADY_RETIRED if already else RETIRE_RESULT_RETIRED),
            mutated=not already,
            preflight=outcome,
            intents=intents,
        )
    except Exception as exc:  # noqa: BLE001 - an exception may be after a side effect
        # Redmine #15840: the handler's own comment says the exception may land AFTER a side
        # effect, yet the result carried no trace of what was raised — the one outcome an
        # operator most needs to diagnose was the one that recorded nothing. Measured cost
        # (#15789 j#109134): an investigation returned `uncertain / retire_application_error`,
        # the cause was unknowable from the result, and finding it took instrumenting a
        # throwaway harness and re-running; it turned out to be a `git worktree add` refusal
        # whose message was the load-bearing evidence for that issue's whole fix.
        #
        # What crosses is a CLOSED-VOCABULARY token, never anything the exception carries. See
        # `_durable_failure_kind`: an earlier attempt appended `type(exc).__name__` and called it
        # safe, and review j#109671 reproduced both ways that was wrong — a class name is
        # arbitrary caller data (`type("SECRET...", ...)`), and reading it can itself raise out
        # of this handler. `str(exc)` / `repr(exc)` / the traceback stay out for the original
        # reason too: the `git worktree add` refusal above embeds the recorded worktree's
        # ABSOLUTE PATH, which `lane_metadata` declares host-local private state that must never
        # reach a durable Redmine record. Raw belongs in the host-local sink, not in a value that
        # flows into CLI JSON and gets pasted into a journal. Boundary:
        # `vibes/docs/logics/exception-diagnostic-sink-boundary.md`.
        return RetireApplicationResult(
            state=RETIRE_RESULT_UNCERTAIN,
            reason=(
                REASON_APPLICATION_ERROR
                + REASON_APPLICATION_ERROR_SEPARATOR
                + _durable_failure_kind(exc)
            ),
            uncertain=True,
        )


__all__ = (
    "RetireApplicationRequest",
    "RetireApplicationResult",
    "RetireAssertions",
    "RetireIdentity",
    "RETIRE_INTENT_PREFLIGHT",
    "RETIRE_INTENT_EXECUTE",
    "RETIRE_INTENT_MIGRATE_HIBERNATED_LEGACY",
    "RETIRE_INTENT_RECONCILE_HIBERNATED_LIVE",
    "RETIRE_INTENT_HIBERNATED_BOUND",
    "RETIRE_INTENT_ACTIVE_LIVE_ZERO",
    "RETIRE_INTENT_ACTIVE_UNBOUND_LIVE_ZERO",
    "RETIRE_INTENT_HIBERNATED_UNBOUND_LIVE_ZERO",
    "RETIRE_RESULT_RETIRED",
    "RETIRE_RESULT_BLOCKED",
    "RETIRE_RESULT_DEFERRED",
    "RETIRE_RESULT_UNCERTAIN",
    "RETIRE_RESULT_ALREADY_RETIRED",
    "REASON_CLEANUP_ATOMIC_GUARD_UNAVAILABLE",
    "REASON_INTENT_NOT_APPLICABLE",
    "REASON_ABSENT_WORKTREE_UNPROVEN",
    "REASON_APPLICATION_ERROR",
    "REASON_APPLICATION_ERROR_SEPARATOR",
    "REASON_EXCEPTION_UNCLASSIFIED",
    "REASON_WORKTREE_ABSENT_NOT_APPLICABLE",
    "run_retire_application",
)
