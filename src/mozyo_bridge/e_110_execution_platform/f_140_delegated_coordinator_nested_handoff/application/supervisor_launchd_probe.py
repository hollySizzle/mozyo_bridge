"""Reading launchd's answer about the owned service — the READ-ONLY half (Redmine #15192).

Split out of :mod:`...application.supervisor_launchd` to keep every module inside the module-health
line budget, and because the boundary is a real one: nothing here mutates a host. It runs
``launchctl print``, classifies the answer three ways, and parses the manager's wording.

The classification is a conservative load-state prerequisite for install/restart, but never proves
loaded argv, file identity, or permission to unlink.  Those authorities stay in the lifecycle
adapter: restart uses verified bootout/fresh-plist/bootstrap, and a failed bootout never permits
deletion (j#102458; j#103093).

That sentence is deliberately narrower than the one it replaces (review j#102590 r14f4), which said
a destructive verb importing from this module would be a defect — while the mutating modules were
importing the process seam from it, and that seam is what runs ``bootout``. The claim worth making is
the one the code keeps: what this module *concludes* never grants permission to change the host.
Process execution now lives in ``supervisor_launchd_process``; this module imports that narrow seam
only to perform the read-only ``launchctl print`` request (review j#102843 r15f4).

Every name is re-exported from ``supervisor_launchd``, so that module remains the single import for
the whole macOS adapter and no caller or test had to change.
"""

from __future__ import annotations

from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_launchd_agent import (  # noqa: E501
    LAUNCHCTL_NOT_FOUND_CODES,
    LAUNCHCTL_UNREADABLE_PHRASES,
    SUPERVISOR_AGENT,
    SupervisorAgent,
    has_not_found_clause,
    names_exactly,
    not_found_operand,
    quoted_names,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_launchd_process import (  # noqa: E501
    Runner,
    launchctl,
    service_target,
)

#: ``launchctl print`` probe outcomes (see :func:`_probe`). Three values, not a boolean: "I could
#: not read it" is a different answer from "it is not there", and only the latter is safe.
PROBE_LOADED = "loaded"
PROBE_CONFIRMED_ABSENT = "confirmed_absent"
PROBE_UNREADABLE = "unreadable"


#: The widest process id ``launchctl`` can print. DERIVED, not chosen: POSIX ``pid_t`` is a signed
#: 32-bit integer on Darwin, so ten digits covers every value the kernel can assign. Deliberately
#: NOT unified with the Redmine-id / lifecycle-revision widths this lane also bounds — a pid is
#: the OS's counter and answers to a different authority (Redmine #14753).
_MAX_PID_DIGITS = len(str(2**31 - 1))


#: The ``launchctl print`` line prefixes carrying the last bounded sweep's exit status. Both
#: spellings are accepted because the wording is not stable across macOS releases and an unmatched
#: prefix silently costs the whole 直近の終了結果 projection (#15192).
_LAST_EXIT_PREFIXES = ("last exit code = ", "last exit status = ")

def probe(runner: Runner, agent: SupervisorAgent = SUPERVISOR_AGENT) -> dict:
    """Read-only ``launchctl print`` → ``{state, loaded, pid, last_exit_status}``. Never raises.

    ``state`` is THREE-valued (review j#102180 finding 1): :data:`PROBE_LOADED`,
    :data:`PROBE_CONFIRMED_ABSENT`, or :data:`PROBE_UNREADABLE`. Collapsing every non-zero exit into
    "not loaded" reported a permission-denied / manager-error read as an established fact — "I could
    not see it" is not "it is not there".

    This proves only the load-state token. It cannot prove manager-loaded argv and never authorizes
    unlink; lifecycle code combines it with owned exact bytes and verified manager actions.

    A non-zero exit is classified as *confirmed absent* ONLY when launchctl positively says the
    service is unknown (:data:`LAUNCHCTL_NOT_FOUND_CODES` / :data:`_LAUNCHCTL_NOT_FOUND_PHRASES`).
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
        result = launchctl(runner, ["print", service_target(agent)])
    except (FileNotFoundError, OSError):  # launchctl absent / not executable — unknowable, not absent
        return _result(PROBE_UNREADABLE)
    if result.returncode != 0:
        return _result(
            PROBE_CONFIRMED_ABSENT
            if says_not_found(result, service_target(agent))
            else PROBE_UNREADABLE
        )
    pid: Optional[int] = None
    last_exit: Optional[int] = None
    seen_pid = False
    for line in (result.stdout or "").splitlines():
        stripped = line.strip()
        if not seen_pid and stripped.startswith("pid = "):
            pid = small_int_or_none(stripped.split("=", 1)[1])
            seen_pid = True
            continue
        for prefix in _LAST_EXIT_PREFIXES:
            if stripped.startswith(prefix):
                last_exit = small_int_or_none(stripped[len(prefix):])
                break
    return {
        "state": PROBE_LOADED, "loaded": True, "pid": pid, "last_exit_status": last_exit,
    }


def says_not_found(result, service_target: str) -> bool:
    """Whether a non-zero ``launchctl print`` positively reports THIS service as unknown.

    **No deletion depends on this** (j#102458, review j#102496 r12f4). It once decided whether a
    failed bootout could still delete a plist; it now only sharpens a read-only projection. The
    conjunction below is kept because a wrong *status* is still worth avoiding, and because relaxing
    it would invite the same reasoning back into a destructive path.

    The evidence is a **conjunction**, not a choice (review j#102200 finding r3f1). All of:

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

    A miss yields :data:`PROBE_UNREADABLE`: status projects unknown and install/restart refuse before
    manager mutation.  The wording still never authorizes unlink or substitutes for exact plist
    identity; only a bootout rc 0 carries verified-stop authority.
    """
    if result.returncode not in LAUNCHCTL_NOT_FOUND_CODES:
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
        for phrase in LAUNCHCTL_UNREADABLE_PHRASES
    ):
        return False
    target = service_target or ""
    label = target.rsplit("/", 1)[-1]
    if not label:
        return False
    bound = False
    for stream in streams:
        operand = not_found_operand(stream)
        if operand is None:
            # A stream that says nothing about absence is normal (an empty stdout, say). A stream
            # that DOES carry recognized wording but yields no operand is ambiguity, and ambiguity is
            # not resolved by whatever the other stream happens to say.
            if has_not_found_clause(stream):
                return False
            continue
        if operand not in (target, label):
            return False  # this stream reports a DIFFERENT service missing: contradictory
        bound = True
    return bound


def small_int_or_none(token: str) -> Optional[int]:
    """A small signed decimal, or ``None``. Never raises (see :data:`_MAX_PID_DIGITS`)."""
    raw = (token or "").strip()
    negative = raw.startswith("-")
    digits = raw[1:] if negative else raw
    if not (digits.isascii() and digits.isdigit() and len(digits) <= _MAX_PID_DIGITS):
        return None
    return -int(digits) if negative else int(digits)


def is_loaded(
    runner: Runner, agent: SupervisorAgent = SUPERVISOR_AGENT
) -> tuple[bool, Optional[int]]:
    """``(loaded, pid)`` — the narrow view :func:`restart` needs. See :func:`probe`."""
    read = probe(runner, agent)
    return bool(read["loaded"]), read["pid"]


__all__ = (
    "PROBE_LOADED",
    "PROBE_CONFIRMED_ABSENT",
    "PROBE_UNREADABLE",
    "probe",
    "says_not_found",
    "small_int_or_none",
    "is_loaded",
)
