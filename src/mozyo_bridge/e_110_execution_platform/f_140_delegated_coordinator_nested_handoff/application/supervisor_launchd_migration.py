"""The retired ``--drain-only`` LaunchAgent migration (Redmine #15192).

A host installed between #14150 and #15192 carries a SECOND LaunchAgent. Leaving it would break the
acceptance this change exists for ("macOS manages exactly one LaunchAgent") and would keep running a
``--drain-only`` tick the single agent already subsumes — so ``install`` and ``uninstall`` remove it.
But only when it is provably OURS.

Split out of :mod:`...application.supervisor_launchd` to keep both modules inside the module-health
line budget, and because this is a genuinely separable concern: a one-way, time-limited migration off
a registration shape that no longer exists, sitting beside the lifecycle of the one that does. It
uses the dedicated process and pinned-filesystem seams; policy remains here, so the lifecycle verbs
can call it without a cycle.

Every name is re-exported from ``supervisor_launchd``, so that module remains the single import for
the whole macOS adapter and no caller or test had to change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_launchd_agent import (  # noqa: E501
    LEGACY_DRAIN_AGENT,
    SUPERVISOR_AGENT,
    SupervisorAgent,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_launchd_process import (  # noqa: E501
    Runner,
    default_runner as _default_runner,
    launchctl,
    service_target,
)
# Every read and write of an owned plist goes through the filesystem seam, which pins each directory
# component no-follow rather than re-walking a path string (review j#102590 r14f1).
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_launchd_fs import (  # noqa: E501
    PLIST_ABSENT,
    PLIST_FOREIGN,
    PLIST_OWNED,
    PLIST_UNREADABLE,
    classify,
    unlink_owned,
)

# ---------------------------------------------------------------------------
# Fixed-vocabulary reason tokens for this migration (secret-safe; UI-language-independent).
# ---------------------------------------------------------------------------

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
#: install refused: nothing established that the retired job stopped. Only a succeeding ``bootout``
#: does (gateway disposition j#102458); a non-zero one ends the decision without reading a word of
#: what launchctl printed.
REASON_LEGACY_DRAIN_STATE_UNREADABLE = "legacy_drain_state_unreadable"

#: Retired-drain classification vocabulary (see :func:`classify_legacy_drain`). The same four values
#: as the shared :func:`classify_plist` — the retired path was simply the first to get an identity
#: test, and for a while the only one (review j#102496 r12f2).
LEGACY_DRAIN_ABSENT = PLIST_ABSENT  # nothing at the retired path: a clean or already-migrated host
LEGACY_DRAIN_OWNED = PLIST_OWNED  # our retired registration, safe to remove
LEGACY_DRAIN_FOREIGN = PLIST_FOREIGN  # a plist at that path carrying someone else's Label
LEGACY_DRAIN_UNREADABLE = PLIST_UNREADABLE  # present but unparseable / non-mapping / no Label

#: The install/uninstall refusal reason for each non-removable retired-drain state.
_LEGACY_DRAIN_REFUSAL_REASON = {
    LEGACY_DRAIN_FOREIGN: REASON_LEGACY_DRAIN_FOREIGN_LABEL,
    LEGACY_DRAIN_UNREADABLE: REASON_LEGACY_DRAIN_UNREADABLE,
}


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
    return classify_agent_plist(os_home, agent=LEGACY_DRAIN_AGENT)


def classify_agent_plist(
    os_home: Optional[Path] = None, *, agent: SupervisorAgent = SUPERVISOR_AGENT
) -> str:
    """Classify what sits at ``agent``'s own plist path (read-only; never raises).

    The single identity test every destructive verb shares, for the current agent and the retired one
    alike. Before #15192 review j#102496 only the retired path had one, which meant ``install``
    overwrote and ``uninstall`` deleted whatever occupied the *current* agent's path — a stranger's
    LaunchAgent included (r12f2).
    """
    return classify(os_home, agent=agent)


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
    retired job is gone**, and there is exactly **one** source of it: ``launchctl bootout``
    **succeeding**, which means this process just unloaded that job. Anything else refuses with
    :data:`REASON_LEGACY_DRAIN_STATE_UNREADABLE` and keeps the plist.

    A follow-up ``launchctl print`` used to serve as a second authority ("it reports an unknown
    service, so it was never loaded"). That authority is **retired** (owner delegation j#102452,
    gateway disposition j#102458): it required interpreting manager wording, and no deletion may
    depend on that any more. The three-valued probe survives only on the *non-destructive*
    ``service_status`` projection. There is likewise no separate "still loaded" answer: distinguishing
    a running job from an unreadable one was derivable only from the same wording, so a token claiming
    it would assert more than this code can establish.

    The retired plist is kept on purpose: it is the operator's only durable trace of a registration
    that may still be live, and removing it would hide the very thing they need to act on.
    ``service_status`` still reports it via ``legacy_drain``.

    **Identity is re-established at unlink time, not merely at entry** (review j#102496 r12f1). The
    entry classification and the unlink are separated by a subprocess call, so the file that gets
    unlinked is not necessarily the file that was classified; a plist replaced in that window was
    deleted while the result still read ``state: owned, removed: true``. Note the limit honestly:
    re-checking narrows the window, it does not close it. ``unlink`` targets a *path*, not the inode
    that was just validated, so a replacement landing between the re-check and the call would still
    be removed. The guarantee offered is the one that can actually be kept — identity is revalidated
    at action time and any mismatch this adapter can observe refuses with zero further mutation.
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
        booted_out = launchctl(runner, ["bootout", service_target(LEGACY_DRAIN_AGENT)])
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
    # Re-establish identity at ACTION time (j#102496 r12f1). The classification above is now stale by
    # one subprocess call, and what gets unlinked is decided here, not there.
    at_unlink = classify_agent_plist(os_home, agent=LEGACY_DRAIN_AGENT)
    if at_unlink == LEGACY_DRAIN_ABSENT:
        # Someone else removed it while we were booting out. The goal state holds; we did not do it,
        # and reporting otherwise would credit this call with a mutation it never performed.
        return {"state": at_unlink, "removed": False, "reason": ""}
    if at_unlink != LEGACY_DRAIN_OWNED:
        # A different file is there now. Report what is actually on disk, not what used to be.
        return {
            "state": at_unlink, "removed": False, "reason": _LEGACY_DRAIN_REFUSAL_REASON[at_unlink],
        }
    try:
        unlink_owned(os_home, agent=LEGACY_DRAIN_AGENT)
    except OSError:
        return {"state": state, "removed": False, "reason": REASON_LEGACY_DRAIN_REMOVAL_FAILED}
    return {"state": state, "removed": True, "reason": ""}


__all__ = (
    "Runner",
    "REASON_LEGACY_DRAIN_FOREIGN_LABEL",
    "REASON_LEGACY_DRAIN_UNREADABLE",
    "REASON_LEGACY_DRAIN_REMOVAL_FAILED",
    "REASON_LEGACY_DRAIN_STATE_UNREADABLE",
    "LEGACY_DRAIN_ABSENT",
    "LEGACY_DRAIN_OWNED",
    "LEGACY_DRAIN_FOREIGN",
    "LEGACY_DRAIN_UNREADABLE",
    "classify_legacy_drain",
    "classify_agent_plist",
    "remove_legacy_drain",
)
