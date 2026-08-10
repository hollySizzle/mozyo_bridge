"""macOS LaunchAgent lifecycle for the workspace callback supervisor (Redmine #13683 Phase B1).

Phase A shipped the ``workflow supervisor`` command **contract** with the three mutating verbs
(``--install`` / ``--restart`` / ``--uninstall``) fail-closed (a bare "no host mutation" refusal).
This module is the Phase B1 realization of that contract: the bounded LaunchAgent lifecycle that a
host service manager (launchd) would run — and **nothing more**.
It is deliberately *not* a general daemon manager (the OTel receiver's ``otel_launchd`` module is the
safe-pattern reference, j#78995); it manages exactly one owned label / plist / log for this supervisor.

**ONE owned LaunchAgent** (Redmine #15192). Between #14150 and #15192 this adapter registered TWO
agents — a coarse ``--run-once`` reconcile agent and a finer ``--drain-only`` agent — and installed
them as an atomic pair. That second registration is retired: a ``--run-once`` tick is a *superset* of
a drain tick (it does the local drain leg and, when the body's watermark is due, the provider leg), so
the drain agent bought latency, not capability, at the price of a second thing for an operator to see
in Login Items and a second lifecycle to keep consistent. macOS now registers exactly one agent
running the same bounded ``workflow supervisor --run-once`` the Linux systemd timer runs, at the same
shared portable cadence (:data:`DEFAULT_OS_TICK_INTERVAL_SECONDS`). ``--drain-only`` and ``--watch``
remain available as manual / event-driven entry points; neither is registered with an OS scheduler.

Upgrading a host that still carries the retired drain agent is a **migration, not a leftover**: see
:func:`classify_legacy_drain` / :func:`remove_legacy_drain` and the ordering note on
:func:`install`.

Design boundary (design preflight j#78995 / Implementation Request j#79005):

- **One-shot scheduled cadence, never KeepAlive.** ``workflow supervisor --run-once`` is a *bounded*
  sweep that exits; the plist schedules it with ``RunAtLoad`` (run once at load) + ``StartInterval``
  (re-run every N seconds), and carries **no** ``KeepAlive`` key. Mapping a one-shot command onto
  ``KeepAlive`` would be a tight restart loop, so KeepAlive is structurally absent — not merely false.
- **No secret ever reaches the plist.** The rendered plist has **no** ``EnvironmentVariables`` key at
  all, so no code path can serialize a credential into it. A launchd-started supervisor inherits no
  shell environment; the Redmine key/URL reach it through the daemon-trusted home-scoped credential
  file (``resolve_redmine_credentials``), never the plist. ``ProgramArguments`` is the exact
  PATH-resolved ``mozyo-bridge`` executable + structured argv — never a shell string.
- **Structured launchctl only.** Every ``launchctl`` invocation is structured argv
  (``bootstrap`` / ``bootout`` / ``kickstart -k`` / ``print``) — no shell. Install is idempotent
  (bootout-then-bootstrap), restart acts only on a *loaded* service, uninstall removes exactly the
  owned label / plist and touches nothing else.
- **Fail-closed, zero-mutation refusals.** ``install`` / ``restart`` refuse — *before* writing any
  file or invoking launchctl — on a non-darwin host, a missing executable, or a retired drain plist
  that cannot be identified as ours. A non-ready Redmine credential is **not** among them since
  #15192 (review j#102151 Finding 4): readiness is projected, not gated, matching the Linux adapter,
  because a tick does useful local work with no provider at all. ``uninstall`` and status stay
  usable with no credential at all.
- **Redacted status projection.** Status reports plist existence / loaded / pid / scheduled interval /
  executable-match / credential-readiness as booleans, counts, and fixed-vocabulary tokens only — no
  credential value, no request header, no repo-local path, no pane text.

This module performs **no** Redmine fetch, gate progression, route resolution, or callback delivery:
installing / restarting / uninstalling the agent is orthogonal to what the agent does when it runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    DEFAULT_OS_TICK_INTERVAL_SECONDS,
    DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_context import (
    normalize_base_url,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_credentials import (
    resolve_redmine_credentials,
)
# The pure layer (owned identity, path / argv resolution, plist rendering and read-back, and the
# vocabularies those produce) lives in the sibling module so neither side exceeds the module-health
# line budget — the same split the Linux adapter carries (review j#102069 F7). Everything is
# re-exported here, so this module remains the single import for the whole macOS adapter and no
# caller or test had to change.
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_launchd_agent import (  # noqa: E501
    DRAIN_LOG_RELATIVE,
    DRAIN_PLIST_RELATIVE,
    HOME_PIN_DUPLICATE,
    HOME_PIN_MALFORMED,
    HOME_PIN_MISSING,
    HOME_PIN_NO_ARGV,
    HOME_PIN_NOT_ABSOLUTE,
    HOME_PIN_NOT_INSTALLED,
    HOME_PIN_OK,
    HOME_PIN_UNREADABLE,
    LEGACY_DRAIN_AGENT,
    LOG_RELATIVE,
    PLIST_RELATIVE,
    SUPERVISOR_AGENT,
    SUPERVISOR_AGENTS,
    SUPERVISOR_ARGV_TAIL,
    SUPERVISOR_DRAIN_ARGV_TAIL,
    SUPERVISOR_DRAIN_LAUNCHD_LABEL,
    SUPERVISOR_EXECUTABLE_NAME,
    SUPERVISOR_HOME_FLAG,
    SUPERVISOR_LAUNCHD_LABEL,
    SupervisorAgent,
    extract_pinned_home as _extract_pinned_home,
    log_path,
    plist_path,
    read_installed_plist as _read_installed_plist,
    render_plist,
    # The pure launchctl-message layer: what the manager's wording says, parsed without running it.
    LAUNCHCTL_NOT_FOUND_CODES as _LAUNCHCTL_NOT_FOUND_CODES,
    LAUNCHCTL_UNREADABLE_PHRASES as _LAUNCHCTL_UNREADABLE_PHRASES,
    names_exactly as _names_exactly,
    has_not_found_clause as _has_not_found_clause,
    not_found_operand as _not_found_operand,
    quoted_names as _quoted_names,
    resolve_mozyo_home,
    resolve_supervisor_command,
)

# ---------------------------------------------------------------------------
# Fixed-vocabulary reason tokens (machine-readable; secret-safe; UI-language-independent).
# ---------------------------------------------------------------------------

#: A mutating verb (install/restart/uninstall) was refused because the host is not macOS.
REASON_UNSUPPORTED_PLATFORM = "launchd_unsupported_platform"
#: install/restart refused: the `mozyo-bridge` executable is not resolvable on PATH.
REASON_EXECUTABLE_NOT_FOUND = "supervisor_executable_not_found"
#: restart refused: the service is not currently loaded (restart acts only on a loaded service).
REASON_SERVICE_NOT_LOADED = "service_not_loaded"
#: restart refused: no owned plist is installed (nothing to restart; run install first).
REASON_NOT_INSTALLED = "service_not_installed"
#: restart/status: the installed plist's ``--home`` pin is missing / malformed / duplicated / not an
#: absolute canonical path, so the daemon-effective root cannot be trusted (fail-closed for restart;
#: unhealthy for status). Also used when the owned plist file exists but is unreadable / non-mapping.
REASON_HOME_PIN_UNHEALTHY = "home_pin_unhealthy"
#: restart refused: the requested mozyo home differs from the installed plist pin (a home change
#: must go through ``install``, which rewrites the plist — restart never silently re-points).
REASON_HOME_PIN_MISMATCH = "home_pin_mismatch"
#: restart refused: the installed ``ProgramArguments`` no longer match the command an install would
#: write now (executable moved / argv drift). An executable / home change must reinstall (rewrite the
#: plist); restart never kickstarts a drifted command (j#79136 R4-F2).
REASON_INSTALLED_COMMAND_DRIFT = "installed_command_drift"
#: A launchctl bootstrap failed (message redacted to a fixed token; no host detail leaks).
REASON_BOOTSTRAP_FAILED = "launchctl_bootstrap_failed"
#: A launchctl kickstart failed (message redacted to a fixed token).
REASON_KICKSTART_FAILED = "launchctl_kickstart_failed"
#: install/uninstall refused: a plist sits at the retired drain agent's owned path but does NOT carry
#: our retired drain label, so it belongs to someone else. Removing it would be deleting a stranger's
#: LaunchAgent; refuse with zero mutation and let the operator resolve the collision (#15192).
REASON_LEGACY_DRAIN_FOREIGN_LABEL = "legacy_drain_foreign_label"
#: install/uninstall refused: a file sits at the retired drain agent's owned path but cannot be
#: parsed, so its identity is unknowable — distinct from absence, and never guessed (#15192).
REASON_LEGACY_DRAIN_UNREADABLE = "legacy_drain_unreadable"
#: install refused: the retired drain plist is ours and removable in principle, but unlinking it
#: failed. Reported instead of proceeding, because proceeding would leave TWO registrations — the
#: exact state #15192 exists to end.
REASON_LEGACY_DRAIN_REMOVAL_FAILED = "legacy_drain_removal_failed"

#: install refused: the retired agent's run state could not be READ (permission denied, a broken
#: service manager, an unrecognized launchctl failure). Distinct from ``still_loaded`` because the
#: facts differ — one says "it is running", this one says "I cannot tell" — and identical only in
#: consequence: neither may authorize deleting a registration or adding a second (j#102180 F1).
REASON_LEGACY_DRAIN_STATE_UNREADABLE = "legacy_drain_state_unreadable"
#: restart refused: the owned service's run state could not be READ. Distinct from
#: ``service_not_loaded`` because the facts differ — one says the service is not running, this one
#: says we cannot tell — and **shared verbatim with the Linux adapter**: the backend declares one
#: operator-visible meaning per verb, so the refusal vocabulary is a common contract and carries no
#: OS-specific manager noun (review j#102398 finding r9f2).
REASON_SERVICE_STATE_UNREADABLE = "service_state_unreadable"

#: ``launchctl print`` probe outcomes (see :func:`_probe`). Three values, not a boolean: "I could
#: not read it" is a different answer from "it is not there", and only the latter is safe.
PROBE_LOADED = "loaded"
PROBE_CONFIRMED_ABSENT = "confirmed_absent"
PROBE_UNREADABLE = "unreadable"

#: Retired-drain classification vocabulary (see :func:`classify_legacy_drain`).
LEGACY_DRAIN_ABSENT = "absent"  # nothing at the retired path: a clean or already-migrated host
LEGACY_DRAIN_OWNED = "owned"  # our retired registration, safe to remove
LEGACY_DRAIN_FOREIGN = "foreign"  # a plist at that path carrying someone else's Label
LEGACY_DRAIN_UNREADABLE = "unreadable"  # present but unparseable / non-mapping / no Label

#: The install/uninstall refusal reason for each non-removable retired-drain state.
_LEGACY_DRAIN_REFUSAL_REASON = {
    LEGACY_DRAIN_FOREIGN: REASON_LEGACY_DRAIN_FOREIGN_LABEL,
    LEGACY_DRAIN_UNREADABLE: REASON_LEGACY_DRAIN_UNREADABLE,
}

#: ``next_elapse_basis`` when the host manager publishes no next-fire time. launchd schedules a
#: ``StartInterval`` agent internally and exposes no "next run" anywhere in ``launchctl print``, so
#: this adapter answers the shared 次回起動 question with an explicit unknown rather than omitting the
#: key (an absent key reads as "no next run"; the CLI renders this token as ``unknown``). Same
#: literal the systemd adapter publishes for the same state — the drift guard is a test, not an
#: import, so neither OS adapter has to import the other (#15192).
NEXT_ELAPSE_UNKNOWN = ""

#: ``last_result`` vocabulary, borrowed verbatim from systemd's so 直近の終了結果 means the same thing
#: on both hosts: a clean exit, a non-zero exit, or nothing recorded yet.
LAST_RESULT_SUCCESS = "success"
LAST_RESULT_EXIT_CODE = "exit-code"

#: Credential-readiness tokens (the exact readiness the live supervisor needs to reach Redmine).
CREDENTIAL_READY = "ready"  # api key + usable base url present
CREDENTIAL_INCOMPLETE = "incomplete"  # exactly one of key / usable url present
CREDENTIAL_MISSING = "missing"  # neither present, and nothing unsafe (the plain unconfigured case)
CREDENTIAL_UNSAFE = "unsafe"  # a present credential file is unsafe/malformed (permission / YAML)

# A launchctl "print" for an unknown label exits non-zero — but a non-zero exit alone says only
# that the read failed, NOT that the service is gone. `_probe` classifies it three ways; see
# `_says_not_found` for the evidence a removal requires (reviews j#102180 / j#102200 / j#102309).
_LAUNCHCTL = "launchctl"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


# ---------------------------------------------------------------------------
# Credential readiness (the exact readiness the live supervisor needs; secret-safe token only).
# ---------------------------------------------------------------------------


def classify_credential_readiness(*, mozyo_home: Optional[Path] = None) -> str:
    """Classify **daemon-effective** Redmine credential readiness into a fixed, secret-safe token.

    Judges what the *launchd-managed* supervisor will actually have at run time, not what the
    installer's interactive shell happens to hold. Two independent leaks are closed:

    - **shell key/URL** — the plist carries no ``EnvironmentVariables`` and launchd inherits no
      shell environment, so readiness resolves with an **empty environ**: an installer's exported
      ``MOZYO_REDMINE_*`` can never produce a false ``ready`` (Redmine #13683 review j#79059 F1).
    - **shell home root** — the credential file's root is the resolved **mozyo home**
      (:func:`resolve_mozyo_home`), the exact root pinned onto the daemon argv, not whatever
      ``mozyo_bridge_home()`` a later launchd process (with no ``MOZYO_BRIDGE_HOME``) would
      re-derive (j#79092 R2-F1).

    Ready needs an api key **and** a normalizable base URL from that home file; a present-but-unsafe
    / malformed file surfaces as :data:`CREDENTIAL_UNSAFE` (the resolver refuses to read it and
    returns a redacted warning), so a fail-closed refusal is visibly deliberate. Returns only a
    token — never the key, the URL, or the warning text.
    """
    creds = resolve_redmine_credentials(resolve_mozyo_home(mozyo_home), environ={})
    if creds.warnings:
        return CREDENTIAL_UNSAFE
    has_key = bool(creds.api_key)
    has_url = bool(normalize_base_url(creds.base_url))
    if has_key and has_url:
        return CREDENTIAL_READY
    if has_key or has_url:
        return CREDENTIAL_INCOMPLETE
    return CREDENTIAL_MISSING


# ---------------------------------------------------------------------------
# launchctl seam (structured argv only; no shell).
# ---------------------------------------------------------------------------


def _default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def _running_on_darwin() -> bool:
    return sys.platform == "darwin"


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


def _service_target(agent: SupervisorAgent = SUPERVISOR_AGENT) -> str:
    return f"{_gui_domain()}/{agent.label}"


#: The widest process id ``launchctl`` can print. DERIVED, not chosen: POSIX ``pid_t`` is a signed
#: 32-bit integer on Darwin, so ten digits covers every value the kernel can assign. Deliberately
#: NOT unified with the Redmine-id / lifecycle-revision widths this lane also bounds — a pid is
#: the OS's counter and answers to a different authority (Redmine #14753).
_MAX_PID_DIGITS = len(str(2**31 - 1))


def _launchctl(runner: Runner, args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return runner([_LAUNCHCTL, *args])


#: The ``launchctl print`` line prefixes carrying the last bounded sweep's exit status. Both
#: spellings are accepted because the wording is not stable across macOS releases and an unmatched
#: prefix silently costs the whole 直近の終了結果 projection (#15192).
_LAST_EXIT_PREFIXES = ("last exit code = ", "last exit status = ")

def _probe(runner: Runner, agent: SupervisorAgent = SUPERVISOR_AGENT) -> dict:
    """Read-only ``launchctl print`` → ``{state, loaded, pid, last_exit_status}``. Never raises.

    ``state`` is the THREE-valued answer the migration fence needs (review j#102180 finding 1):
    :data:`PROBE_LOADED`, :data:`PROBE_CONFIRMED_ABSENT`, or :data:`PROBE_UNREADABLE`. Collapsing
    every non-zero exit into "not loaded" is what let a permission-denied / manager-error read pass
    as a verified stop — "I could not see it" is not "it is not there", and only the second one may
    authorize deleting a registration.

    A non-zero exit is classified as *confirmed absent* ONLY when launchctl positively says the
    service is unknown (:data:`_LAUNCHCTL_NOT_FOUND_CODES` / :data:`_LAUNCHCTL_NOT_FOUND_PHRASES`).
    Anything else — an unrecognized code, an unreadable message, a missing launchctl binary, an OS
    error — is :data:`PROBE_UNREADABLE`, so the caller fails closed rather than guessing.

    ``loaded`` is kept as the boolean the status projection and ``restart`` already consume; it is
    true only for :data:`PROBE_LOADED`, so an unreadable probe never reads as "running".

    Every integer here is read as an ASCII decimal inside POSIX ``pid_t`` width, NOT via
    ``str.isdigit()``, which does not mean "a number ``int()`` can read": measured (Redmine #14753),
    a ``pid = ²`` line raised a raw ``ValueError`` out of :func:`service_status`, breaking both this
    function's "never raises" promise and the typed status dict its callers consume. An unreadable
    value reads as ``None`` — the same value returned when launchctl reports none at all.
    """
    def _result(state: str) -> dict:
        return {"state": state, "loaded": False, "pid": None, "last_exit_status": None}

    try:
        result = _launchctl(runner, ["print", _service_target(agent)])
    except (FileNotFoundError, OSError):  # launchctl absent / not executable — unknowable, not absent
        return _result(PROBE_UNREADABLE)
    if result.returncode != 0:
        return _result(
            PROBE_CONFIRMED_ABSENT
            if _says_not_found(result, _service_target(agent))
            else PROBE_UNREADABLE
        )
    pid: Optional[int] = None
    last_exit: Optional[int] = None
    seen_pid = False
    for line in (result.stdout or "").splitlines():
        stripped = line.strip()
        if not seen_pid and stripped.startswith("pid = "):
            pid = _small_int_or_none(stripped.split("=", 1)[1])
            seen_pid = True
            continue
        for prefix in _LAST_EXIT_PREFIXES:
            if stripped.startswith(prefix):
                last_exit = _small_int_or_none(stripped[len(prefix):])
                break
    return {
        "state": PROBE_LOADED, "loaded": True, "pid": pid, "last_exit_status": last_exit,
    }


def _says_not_found(result, service_target: str) -> bool:
    """Whether a non-zero ``launchctl print`` positively reports THIS service as unknown.

    This is the only path that can authorize deleting a registration on the strength of an *error*,
    so the evidence is a **conjunction**, not a choice (review j#102200 finding r3f1). All of:

    1. the exit code is one launchctl uses for an unknown label, **and**
    2. the output carries a recognized "no such service" phrase, **and**
    3. the output names the exact service target we asked about, **and**
    4. the output carries no signal that the read failed for some *other* reason.

    The earlier version accepted the code **or** the phrase, which is how ``113`` +
    ``Operation not permitted`` — a permission failure — read as absence and deleted an owned plist.
    Either signal alone is too weak to carry that consequence: launchctl's man page documents only
    "0 on success, non-zero on failure", so 113 is not a not-found contract, and it states that
    ``print`` output is not an API, so the wording may change. Requiring both, bound to our own
    domain/label, is what makes the reading specific enough to act on; requiring the *absence* of a
    permission signal is what stops "the reason we could not look" from passing as "nothing to see".

    A miss on any conjunct yields :data:`PROBE_UNREADABLE`: the install refuses with a typed reason
    and keeps the plist. That is the deliberate failure direction — **over-refusal is recoverable
    and visible, a second live registration is the defect this migration exists to prevent** — and
    until the contract can be confirmed against a real launchd, refusing is the honest answer.
    """
    if result.returncode not in _LAUNCHCTL_NOT_FOUND_CODES:
        return False
    # The two streams are read SEPARATELY, as the distinct texts launchctl actually wrote (review
    # j#102417 finding r10f1). Concatenating them into one string handed the position-aware parser a
    # sentence that never existed: `stderr="Could not find service"` with `stdout='"<owned>"'` put a
    # phrase and an operand on either side of the joining newline, which satisfied "separated by
    # whitespace only" and authorized unlinking the owned plist. Hardening the parser is worth
    # nothing if its caller can manufacture the very adjacency the parser checks.
    streams = [
        getattr(result, "stderr", "") or "",
        getattr(result, "stdout", "") or "",
    ]
    # Wording is prose whose capitalization is not a contract, so phrases are matched case-folded.
    # Identity is the label launchd keys the job off, matched exactly as launchctl wrote it (review
    # j#102327 finding r6f1). A denial signal anywhere disqualifies the whole read: it names why we
    # could not look, which is the opposite of evidence that there is nothing to look at.
    if any(
        phrase in stream.lower()
        for stream in streams
        for phrase in _LAUNCHCTL_UNREADABLE_PHRASES
    ):
        return False
    target = service_target or ""
    label = target.rsplit("/", 1)[-1]
    if not label:
        return False
    bound = False
    for stream in streams:
        operand = _not_found_operand(stream)
        if operand is None:
            # A stream that says nothing about absence is normal (an empty stdout, say). A stream
            # that DOES carry recognized wording but yields no operand is ambiguity, and ambiguity is
            # not resolved by whatever the other stream happens to say.
            if _has_not_found_clause(stream):
                return False
            continue
        if operand not in (target, label):
            return False  # this stream reports a DIFFERENT service missing: contradictory
        bound = True
    return bound


def _small_int_or_none(token: str) -> Optional[int]:
    """A small signed decimal, or ``None``. Never raises (see :data:`_MAX_PID_DIGITS`)."""
    raw = (token or "").strip()
    negative = raw.startswith("-")
    digits = raw[1:] if negative else raw
    if not (digits.isascii() and digits.isdigit() and len(digits) <= _MAX_PID_DIGITS):
        return None
    return -int(digits) if negative else int(digits)


def _is_loaded(
    runner: Runner, agent: SupervisorAgent = SUPERVISOR_AGENT
) -> tuple[bool, Optional[int]]:
    """``(loaded, pid)`` — the narrow view :func:`restart` needs. See :func:`_probe`."""
    probe = _probe(runner, agent)
    return bool(probe["loaded"]), probe["pid"]


# ---------------------------------------------------------------------------
# Retired-drain migration (Redmine #15192).
#
# A host installed before #15192 carries a SECOND LaunchAgent. Leaving it would break the very
# acceptance this change exists for ("macOS manages exactly one LaunchAgent") and would keep running
# a `--drain-only` tick the single agent already subsumes. So install removes it — but only when it
# is provably OURS.
# ---------------------------------------------------------------------------


def classify_legacy_drain(os_home: Optional[Path] = None) -> str:
    """Classify what sits at the retired drain agent's owned plist path (read-only; never raises).

    Path ownership alone is not identity. A LaunchAgent plist carries its own ``Label``, and launchd
    keys the running service off *that*, not off the filename — so a file at our retired path whose
    ``Label`` is someone else's is someone else's agent, and unlinking it would remove a service this
    module never installed. The four outcomes are therefore kept apart and only
    :data:`LEGACY_DRAIN_OWNED` is removable:

    - :data:`LEGACY_DRAIN_ABSENT` — nothing there (a clean host, or one already migrated);
    - :data:`LEGACY_DRAIN_OWNED` — parses, and ``Label`` is exactly our retired drain label;
    - :data:`LEGACY_DRAIN_FOREIGN` — parses, but the ``Label`` is not ours;
    - :data:`LEGACY_DRAIN_UNREADABLE` — present but unparseable / non-mapping / no ``Label`` string,
      so identity is unknowable and is never guessed.
    """
    target = plist_path(os_home, agent=LEGACY_DRAIN_AGENT)
    if not target.exists():
        return LEGACY_DRAIN_ABSENT
    parsed = _read_installed_plist(target)
    if parsed is None:
        return LEGACY_DRAIN_UNREADABLE
    label = parsed.get("Label")
    if not isinstance(label, str) or not label:
        return LEGACY_DRAIN_UNREADABLE
    return LEGACY_DRAIN_OWNED if label == LEGACY_DRAIN_AGENT.label else LEGACY_DRAIN_FOREIGN


def remove_legacy_drain(
    *, os_home: Optional[Path] = None, runner: Runner = _default_runner
) -> dict:
    """Boot out and unlink the retired drain agent when — and only when — it is ours.

    ``{"state": <classification>, "removed": bool, "reason": <token>}``. An absent legacy agent is a
    no-op success (the normal steady state). A foreign / unreadable one mutates **nothing** and
    reports the refusal token, so the caller can fail closed rather than delete something it cannot
    identify. Its owned log is deliberately left alone: a log is evidence of what the retired agent
    did, and this migration retires a *registration*, not an audit trail.

    **The stop is verified, not assumed** (review j#102151 Finding 1). Unlinking the plist does not
    unregister anything: launchd keys a bootstrapped job off its *label*, so a job whose file is gone
    keeps running until logout. The removal therefore proceeds only on **positive evidence that the
    retired job is gone**, and there are exactly two ways to obtain it:

    1. ``launchctl bootout`` **succeeds** — we just unloaded it ourselves. This is the strongest
       evidence available and it depends on nothing but the exit status of the action we took.
    2. bootout fails, and a follow-up ``launchctl print`` **positively reports an unknown service**
       — it was never loaded, which is the ordinary state of an already stopped retired agent.

    The bootout return code alone is deliberately not the test, because it also exits non-zero for a
    never-loaded label; treating that as failure would refuse every clean migration. But its
    *success* is a fact worth using, and reading it first means the common path never depends on
    interpreting an error at all.

    Anything else refuses with :data:`REASON_LEGACY_DRAIN_STATE_UNREADABLE`. There is deliberately
    no separate "still loaded" answer any more: distinguishing a running job from an unreadable one
    required interpreting launchctl's wording, and that interpretation is no longer permitted to
    influence a deletion, so a token claiming the distinction would assert more than this code can
    establish. The retired plist is kept on purpose: it is the operator's only durable trace of a
    registration that may still be live, and removing it would hide the very thing they need to act
    on. ``service_status`` still reports it via ``legacy_drain``.
    """
    state = classify_legacy_drain(os_home)
    if state == LEGACY_DRAIN_ABSENT:
        return {"state": state, "removed": False, "reason": ""}
    if state != LEGACY_DRAIN_OWNED:
        return {"state": state, "removed": False, "reason": _LEGACY_DRAIN_REFUSAL_REASON[state]}
    # Unload before unlinking: removing the file leaves a bootstrapped service running until logout.
    #
    # THE ONLY AUTHORITY TO UNLINK IS A SUCCEEDING BOOTOUT. A non-zero result ends the decision here
    # — the wording launchctl printed is never read, so it cannot authorize anything (owner
    # delegation j#102452, gateway disposition j#102458).
    #
    # This is structural, not another rule about strings. Six review rounds tried to make the
    # message safe to interpret: an exit code treated as a contract, a substring match, an invented
    # character class, an open negation, a phrase never bound to its operand, a position rule the
    # caller could forge across two streams, and finally an unparseable stream read as silence and a
    # newline read as a space. Each fix was locally right and rested on an unverified premise about
    # output nobody here has observed. The defect is not any one of those premises — it is that a
    # destructive action depends on parsing text whose grammar is undocumented and unavailable to
    # check. Removing the dependency removes the class.
    #
    # `launchctl bootout` returning 0 means *this process just unloaded that job*. That is a fact
    # about an action we took, not an inference from prose, and it is the whole authority now.
    try:
        booted_out = _launchctl(runner, ["bootout", _service_target(LEGACY_DRAIN_AGENT)])
        unloaded_by_us = booted_out.returncode == 0
    except (FileNotFoundError, OSError):
        unloaded_by_us = False
    if not unloaded_by_us:
        # Keep the plist. It is the operator's only durable trace of a registration that may still
        # be live, and `--run-once` already performs the drain leg, so leaving it costs no
        # capability. `service_status` reports it as a pending migration via `legacy_drain`.
        return {
            "state": state, "removed": False, "reason": REASON_LEGACY_DRAIN_STATE_UNREADABLE,
        }
    try:
        plist_path(os_home, agent=LEGACY_DRAIN_AGENT).unlink()
    except OSError:
        return {"state": state, "removed": False, "reason": REASON_LEGACY_DRAIN_REMOVAL_FAILED}
    return {"state": state, "removed": True, "reason": ""}


# ---------------------------------------------------------------------------
# Lifecycle verbs (structured results; fail-closed, zero-mutation refusals).
# ---------------------------------------------------------------------------


def install(
    *,
    os_home: Optional[Path] = None,
    mozyo_home: Optional[Path] = None,
    interval_seconds: int = DEFAULT_OS_TICK_INTERVAL_SECONDS,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
    agent: SupervisorAgent = SUPERVISOR_AGENT,
) -> dict:
    """Write the owned plist and (re)bootstrap the single agent. Idempotent; fail-closed.

    Refuses — before any filesystem write or launchctl call — on a non-darwin host, a missing
    executable, or a retired drain plist that cannot be identified as ours. The mozyo home is
    resolved **once** and used for both the readiness projection and the pinned ``--home`` argv, so
    the daemon reads the exact root the preflight validated. The plist / log live under the OS user
    home (``os_home``).

    **An unconfigured Redmine does not block the install** (review j#102151 Finding 4). Readiness is
    resolved against the pinned home and *reported* as ``credential_readiness``, never used as a
    gate — the same contract the Linux adapter has carried since #15183. It used to refuse here, a
    rule inherited from #13683 when a supervisor tick meant nothing but a Redmine reconciliation.
    Since #14150 gave the sweep a local drain leg, a tick does useful work from SQLite + Herdr with
    no provider at all, so refusing to schedule anything left the local work unrun and made the
    *operator-visible* meaning of ``install`` differ per host — which is what #15192 exists to end.
    Nothing about credential handling loosens: values are still read only by
    ``resolve_redmine_credentials``, an unsafe file still yields a redacted warning and no value, and
    no credential reaches the plist, the status projection, or a log.

    **Ordering: the retired drain agent is removed BEFORE the owned agent is written** (#15192).
    That is the ordering that makes the acceptance invariant hold under partial failure. Installing
    first and migrating second would, on a mid-sequence failure, leave the host with *two* running
    registrations — precisely the state this change exists to end. Removing first can only ever leave
    zero or one: a failure after the removal leaves the owned agent's install to be retried (it is
    idempotent), and the removed drain leg is not a capability loss, since a ``--run-once`` tick
    already does the drain leg's work.

    Every *preflight* refusal — platform, executable, and an unidentifiable retired plist — is
    evaluated before **either** mutation, so a refused install is zero-mutation. Three refusals are
    not, and all three stop **before the owned agent is written or bootstrapped**:
    ``legacy_drain_state_unreadable`` (the bootout did not succeed, so nothing authorizes a
    removal) and
    ``legacy_drain_removal_failed`` (the unlink failed after the job was confirmed gone). These are
    reported honestly rather than described as zero-mutation.
    """
    if not _running_on_darwin():
        return _refused("install", REASON_UNSUPPORTED_PLATFORM, label=agent.label)
    resolved_mozyo = resolve_mozyo_home(mozyo_home)
    command = resolve_supervisor_command(mozyo_home=resolved_mozyo, which=which, agent=agent)
    if command is None:
        return _refused("install", REASON_EXECUTABLE_NOT_FOUND, label=agent.label)
    # Projected, NOT gated: an unconfigured Redmine must not stop the agent being installed, or the
    # local work a tick can safely do never runs (j#102151 Finding 4; matches the Linux adapter).
    readiness = classify_credential_readiness(mozyo_home=resolved_mozyo)
    # Classified (read-only) as part of the preflight, so an unidentifiable legacy plist refuses with
    # zero mutation instead of being discovered halfway through the install.
    legacy_state = classify_legacy_drain(os_home)
    if legacy_state not in (LEGACY_DRAIN_ABSENT, LEGACY_DRAIN_OWNED):
        return _refused(
            "install", _LEGACY_DRAIN_REFUSAL_REASON[legacy_state],
            credential_readiness=readiness, label=agent.label, legacy_drain=legacy_state,
        )
    migration = remove_legacy_drain(os_home=os_home, runner=runner)
    if migration["reason"]:
        # The removal itself failed (an unlink error). Proceeding would install a second live
        # registration alongside it, so stop here with the owned agent untouched.
        return _refused(
            "install", migration["reason"],
            credential_readiness=readiness, label=agent.label, legacy_drain=migration["state"],
        )

    target = plist_path(os_home, agent=agent)
    target.parent.mkdir(parents=True, exist_ok=True)
    log_path(os_home, agent=agent).parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        render_plist(command, interval_seconds=interval_seconds, os_home=os_home, agent=agent)
    )
    # A previously loaded agent must be booted out before bootstrap or launchd rejects the
    # duplicate label; a not-loaded bootout is fine to ignore (idempotent install).
    _launchctl(runner, ["bootout", _service_target(agent)])
    result = _launchctl(runner, ["bootstrap", _gui_domain(), str(target)])
    if result.returncode != 0:
        return {
            "action": "install",
            "performed": False,
            "reason": REASON_BOOTSTRAP_FAILED,
            "credential_readiness": readiness,
            "label": agent.label,
            "legacy_drain": migration["state"],
            "legacy_drain_removed": migration["removed"],
        }
    return {
        "action": "install",
        "performed": True,
        "reason": "",
        "credential_readiness": readiness,
        "scheduled_interval_seconds": max(1, int(interval_seconds)),
        "label": agent.label,
        # What the migration did, so an upgrade is observable rather than silent.
        "legacy_drain": migration["state"],
        "legacy_drain_removed": migration["removed"],
    }


def restart(
    *,
    os_home: Optional[Path] = None,
    mozyo_home: Optional[Path] = None,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
    agent: SupervisorAgent = SUPERVISOR_AGENT,
) -> dict:
    """Kickstart (kill + relaunch) the *loaded* agent. Fail-closed zero-mutation.

    The **installed plist** — not the caller's current shell — is the authority on the daemon's
    mozyo home: restart reads the ``--home`` pin from the owned plist and checks *that* exact root's
    credential readiness, so it never reports a false-ready restart when the current shell resolves a
    different (ready) home than the one the loaded service actually runs with (j#79125 R3-F1).

    Refuses — before any launchctl mutation — on a non-darwin host, no installed plist (file
    absent), an owned plist that exists but is unreadable / non-mapping, an unhealthy ``--home`` pin
    (missing / malformed / duplicated / not an absolute canonical path), a requested ``mozyo_home``
    that differs from the pin, installed ``ProgramArguments`` that no longer match the command an
    install would write now (executable / argv drift — reinstall to change), a missing executable, or
    a service that is not loaded.

    A non-ready credential does **not** block a restart, for the same reason it no longer blocks an
    install (j#102151 Finding 4): readiness is reported, not gated, and matches the Linux adapter.
    """
    if not _running_on_darwin():
        return _refused("restart", REASON_UNSUPPORTED_PLATFORM, label=agent.label)
    target = plist_path(os_home, agent=agent)
    if not target.exists():
        return _refused("restart", REASON_NOT_INSTALLED, label=agent.label)
    installed = _read_installed_plist(target)
    if installed is None:
        # File present but unreadable / non-mapping — unhealthy, NOT absence (j#79136 R4-F3).
        return _refused(
            "restart", REASON_HOME_PIN_UNHEALTHY, home_pin=HOME_PIN_UNREADABLE, label=agent.label
        )
    installed_argv = installed.get("ProgramArguments")
    pinned, pin_status = _extract_pinned_home(installed_argv)
    if pin_status != HOME_PIN_OK:
        return _refused("restart", REASON_HOME_PIN_UNHEALTHY, home_pin=pin_status, label=agent.label)
    # A requested home that disagrees with the installed pin is a re-point attempt — refuse; a home
    # change must rewrite the plist via install, not silently kickstart the old pin.
    if mozyo_home is not None and str(resolve_mozyo_home(mozyo_home)) != pinned:
        return _refused("restart", REASON_HOME_PIN_MISMATCH, home_pin=pin_status, label=agent.label)
    pinned_home = Path(pinned)
    expected = resolve_supervisor_command(mozyo_home=pinned_home, which=which, agent=agent)
    if expected is None:
        return _refused("restart", REASON_EXECUTABLE_NOT_FOUND, label=agent.label)
    # The installed command must still be exactly what an install would write (same authority as
    # `service_status`'s executable_matches): a moved executable or any argv drift means the loaded
    # service runs a stale command — reinstall to change it, never kickstart the drift (j#79136 R4-F2).
    if installed_argv != expected:
        return _refused("restart", REASON_INSTALLED_COMMAND_DRIFT, label=agent.label)
    # Projected, NOT gated (j#102151 Finding 4).
    readiness = classify_credential_readiness(mozyo_home=pinned_home)
    # The THREE-valued probe is carried through, not collapsed to a bool (review j#102398 r9f2).
    # `_is_loaded` reduced "I could not read the manager" and "the manager says it is not there" to
    # one `service_not_loaded`, dropping `probe_state` — so a permission-denied read was reported as
    # an established fact, and the same envelope meant different things on the two hosts even though
    # the backend declares the verbs identical. Both still refuse; they no longer claim to be the
    # same refusal.
    probe_state = _probe(runner, agent=agent)["state"]
    if probe_state != PROBE_LOADED:
        return _refused(
            "restart",
            REASON_SERVICE_NOT_LOADED
            if probe_state == PROBE_CONFIRMED_ABSENT
            else REASON_SERVICE_STATE_UNREADABLE,
            credential_readiness=readiness,
            label=agent.label,
            probe_state=probe_state,
        )
    result = _launchctl(runner, ["kickstart", "-k", _service_target(agent)])
    if result.returncode != 0:
        return {
            "action": "restart",
            "performed": False,
            "reason": REASON_KICKSTART_FAILED,
            "credential_readiness": readiness,
            "label": agent.label,
        }
    return {
        "action": "restart",
        "performed": True,
        "reason": "",
        "credential_readiness": readiness,
        "label": agent.label,
    }


def uninstall(
    *,
    os_home: Optional[Path] = None,
    runner: Runner = _default_runner,
    agent: SupervisorAgent = SUPERVISOR_AGENT,
) -> dict:
    """Boot the agent out and remove exactly the owned plist. No credential required.

    Refuses only on a non-darwin host (there is no launchd to bootout). On darwin, tears down the
    agent even when credentials are absent — you must be able to remove a service without them. The
    plist lives under the OS user home (``os_home``); no mozyo home is needed to remove it.

    A retired ``--drain-only`` registration from before #15192 is torn down too, under the same
    identity fence :func:`install` uses: "remove exactly the owned artifacts" has to include the
    artifacts this adapter used to own, or uninstalling would leave a live agent behind on exactly
    the hosts that most need cleaning. A foreign / unreadable plist at that path is reported and left
    untouched — it never blocks removing the agent this adapter *does* own, because refusing to
    uninstall over a stranger's file would strand our own registration.
    """
    if not _running_on_darwin():
        return _refused("uninstall", REASON_UNSUPPORTED_PLATFORM, label=agent.label)
    _launchctl(runner, ["bootout", _service_target(agent)])
    target = plist_path(os_home, agent=agent)
    existed = target.exists()
    if existed:
        target.unlink()
    migration = remove_legacy_drain(os_home=os_home, runner=runner)
    return {
        "action": "uninstall",
        "performed": True,
        "reason": "",
        "removed": existed,
        "label": agent.label,
        "legacy_drain": migration["state"],
        "legacy_drain_removed": migration["removed"],
        "legacy_drain_reason": migration["reason"],
    }


def service_status(
    *,
    os_home: Optional[Path] = None,
    mozyo_home: Optional[Path] = None,
    interval_hint: int = DEFAULT_OS_TICK_INTERVAL_SECONDS,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
    agent: SupervisorAgent = SUPERVISOR_AGENT,
) -> dict:
    """A read-only, redacted projection of the host service state. Mutates nothing.

    Reports plist existence (under the OS user home ``os_home``), loaded/pid, the *scheduled*
    interval, the ``--home`` pin health, whether the installed argv still matches the one an install
    would write now, and **daemon-effective** credential readiness — as booleans / counts / fixed
    tokens only. When a plist is installed and readable, ``credential_readiness`` is that of the
    **pinned** mozyo home (the root the loaded daemon actually runs with), not the caller's current
    shell, so the projection reflects the *installed daemon*, not a would-be re-point (j#79125 R3-F1).
    An unhealthy pin — or an owned plist that exists but is unreadable / non-mapping (``home_pin`` =
    ``unreadable_plist``; distinct from absence, which is ``not_installed`` — j#79136 R4-F3) —
    surfaces as ``home_pin`` != ``ok`` with an empty readiness (unknowable). Only when nothing is
    installed is ``credential_readiness`` the would-be root's (``mozyo_home`` / default). Never emits
    a credential value, a request header, a repo-local path, or pane text.
    """
    target = plist_path(os_home, agent=agent)
    plist_exists = target.exists()
    probe = _probe(runner, agent=agent)
    loaded, pid = bool(probe["loaded"]), probe["pid"]
    probe_state = probe["state"]

    installed = _read_installed_plist(target) if plist_exists else None
    # Three distinct states: absent (not_installed), present-but-unreadable (unreadable_plist), and
    # present + parsed (judged by its --home pin) — j#79136 R4-F3.
    scheduled_interval = installed.get("StartInterval") if installed else None
    run_at_load = bool(installed.get("RunAtLoad")) if installed else None
    keep_alive_present = ("KeepAlive" in installed) if installed else False
    no_environment_block = ("EnvironmentVariables" not in installed) if installed else True

    installed_argv = installed.get("ProgramArguments") if installed else None
    if installed is not None:
        pinned, pin_status = _extract_pinned_home(installed_argv)
        # The installed daemon's authority is its own pin; readiness is unknowable if the pin is bad.
        credential_readiness = (
            classify_credential_readiness(mozyo_home=Path(pinned))
            if pin_status == HOME_PIN_OK
            else ""
        )
        # "still what an install would write" is judged against the pinned home, not the caller's.
        expected = (
            resolve_supervisor_command(mozyo_home=Path(pinned), which=which, agent=agent)
            if pin_status == HOME_PIN_OK
            else None
        )
    elif plist_exists:
        # File present but unparseable / non-mapping — unhealthy, NOT absence (j#79136 R4-F3).
        pin_status = HOME_PIN_UNREADABLE
        credential_readiness = ""
        expected = None
    else:
        pin_status = HOME_PIN_NOT_INSTALLED
        credential_readiness = classify_credential_readiness(
            mozyo_home=resolve_mozyo_home(mozyo_home)
        )
        expected = None

    executable_matches = bool(
        expected is not None
        and isinstance(installed_argv, list)
        and installed_argv == expected
    )

    last_exit_status = probe["last_exit_status"]
    return {
        "action": "service-status",
        "label": agent.label,
        "platform_supported": _running_on_darwin(),
        "installed": plist_exists,
        "plist_exists": plist_exists,
        "loaded": loaded,
        "pid": pid,
        "scheduled_interval_seconds": (
            int(scheduled_interval)
            if isinstance(scheduled_interval, int)
            else (int(interval_hint) if not plist_exists else None)
        ),
        "run_at_load": run_at_load,
        "keep_alive_present": keep_alive_present,
        "no_environment_block": no_environment_block,
        "home_pin": pin_status,
        "executable_matches": executable_matches,
        "credential_readiness": credential_readiness,
        # ---- the cross-adapter contract keys (#15192) --------------------------------------
        # 次回起動: launchd runs a ``StartInterval`` agent off an internal timer and publishes no
        # next-fire time anywhere in ``launchctl print``. The key is emitted with an explicit
        # unknown basis rather than omitted, because an ABSENT key reads as "nothing scheduled"
        # while the agent is in fact scheduled — the same reason the systemd side always pairs a
        # value with its basis. The cadence an operator can act on is the interval + last trigger.
        "next_elapse": "",
        "next_elapse_basis": NEXT_ELAPSE_UNKNOWN,
        # 直近の終了結果, in systemd's vocabulary so the word means one thing on both hosts.
        # launchd publishes the status but not when it happened, so the timestamp stays empty.
        "last_result": (
            ""
            if last_exit_status is None
            else (LAST_RESULT_SUCCESS if last_exit_status == 0 else LAST_RESULT_EXIT_CODE)
        ),
        "last_exit_status": last_exit_status,
        "last_exit_at": "",
        # Whether the host manager's answer could be READ, in the shared fixed vocabulary (review
        # j#102200 finding r3f2). Without it, "confirmed stopped" and "I could not tell" produced
        # byte-identical projections — both `loaded: False`, `pid: None` — so neither an operator
        # nor the common CLI could distinguish a verified state from an unreadable one. A fixed
        # token only: no raw launchctl text and no secret ever reaches this projection.
        "probe_state": probe_state,
        # 実行内容: the exact argv the scheduled agent runs. Non-secret by construction — a
        # PATH-resolved executable, fixed flags, and a config directory; never an environment block.
        "installed_command": list(installed_argv) if isinstance(installed_argv, list) else [],
        # The provider cadence the supervisor body enforces internally, surfaced so an operator can
        # see that the OS tick is not a Redmine poll. This adapter does not set or enforce it.
        "provider_reconcile_interval_seconds": DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
        # A pre-#15192 registration still present is a pending migration, not a second owned agent:
        # ``install`` / ``uninstall`` remove it, and status makes it visible in the meantime.
        "legacy_drain": classify_legacy_drain(os_home),
    }


def _refused(action: str, reason: str, **extra: object) -> dict:
    """A fail-closed, zero-mutation refusal result (fixed vocabulary; no host detail)."""
    return {"action": action, "performed": False, "reason": reason, **extra}


__all__ = (
    "SUPERVISOR_LAUNCHD_LABEL",
    "SUPERVISOR_EXECUTABLE_NAME",
    "SUPERVISOR_ARGV_TAIL",
    "SUPERVISOR_HOME_FLAG",
    "REASON_UNSUPPORTED_PLATFORM",
    "REASON_EXECUTABLE_NOT_FOUND",
    "REASON_SERVICE_NOT_LOADED",
    "REASON_NOT_INSTALLED",
    "REASON_HOME_PIN_UNHEALTHY",
    "REASON_HOME_PIN_MISMATCH",
    "REASON_INSTALLED_COMMAND_DRIFT",
    "REASON_BOOTSTRAP_FAILED",
    "REASON_KICKSTART_FAILED",
    "REASON_LEGACY_DRAIN_FOREIGN_LABEL",
    "REASON_LEGACY_DRAIN_UNREADABLE",
    "REASON_LEGACY_DRAIN_REMOVAL_FAILED",
    "REASON_LEGACY_DRAIN_STATE_UNREADABLE",
    "REASON_SERVICE_STATE_UNREADABLE",
    "PROBE_LOADED",
    "PROBE_CONFIRMED_ABSENT",
    "PROBE_UNREADABLE",
    "LEGACY_DRAIN_ABSENT",
    "LEGACY_DRAIN_OWNED",
    "LEGACY_DRAIN_FOREIGN",
    "LEGACY_DRAIN_UNREADABLE",
    "NEXT_ELAPSE_UNKNOWN",
    "LAST_RESULT_SUCCESS",
    "LAST_RESULT_EXIT_CODE",
    "HOME_PIN_OK",
    "HOME_PIN_MISSING",
    "HOME_PIN_DUPLICATE",
    "HOME_PIN_MALFORMED",
    "HOME_PIN_NOT_ABSOLUTE",
    "HOME_PIN_NO_ARGV",
    "HOME_PIN_UNREADABLE",
    "HOME_PIN_NOT_INSTALLED",
    "CREDENTIAL_READY",
    "CREDENTIAL_INCOMPLETE",
    "CREDENTIAL_MISSING",
    "CREDENTIAL_UNSAFE",
    "plist_path",
    "log_path",
    "resolve_mozyo_home",
    "resolve_supervisor_command",
    "render_plist",
    "classify_credential_readiness",
    "install",
    "restart",
    "uninstall",
    "service_status",
    "SupervisorAgent",
    "SUPERVISOR_AGENT",
    "SUPERVISOR_AGENTS",
    "LEGACY_DRAIN_AGENT",
    "SUPERVISOR_DRAIN_LAUNCHD_LABEL",
    "classify_legacy_drain",
    "remove_legacy_drain",
)
