"""Update-authority fence for the lane self-heal RELAUNCH (Redmine #14741).

The relaunch-side sibling of
:mod:`...e_110_execution_platform.f_130_handoff_routing.application.startup_admission_composition`,
and the place Design Answer j#96374 items 2-3 actually land. The send side refuses typing
into a lane that runs a binary no update can reach; this side refuses *restarting* one.

Why the relaunch needs its own fence
------------------------------------
The #14741 loop is a relaunch loop. The measured shape (#14725 j#94108): the lane's Codex
slot rendered its update prompt, the queue-enter rail's Enter selected the prompt's default
``1. Update now``, the update ran against a DIFFERENT install than the managed override
pinned, the process exited 0, and :meth:`...HerdrSublaneActuatorOps.heal_lane_column`
restarted the same old binary a few seconds later — forever. Blocking the send stops the
lane from eating another Implementation Request; only blocking the *relaunch* stops the
loop, because the relaunch is what keeps re-creating the state the send is refused on.

What arms it, and what deliberately does not (j#96374 items 1-2)
----------------------------------------------------------------
**Not every heal.** A heal fires whenever a slot vanished, and a vanished slot is a clean
exit — which the ruling excludes as a derivation source, for the concrete reason that
arming on it reads host state (a package manager) on a path that never asked for one. R5
armed every launch that involved a bound provider and regressed 210 tests; arming every
*heal* would be the same mistake one layer down. A heal with no update observation is
therefore byte-invariant with the pre-#14741 heal: this module resolves nothing, runs no
package-manager query, and returns admitted.

**Only an observed update screen.** The signal is the #13760 admission classifier's own
typed verdict for the lane's live slots, matched against signatures the provider profile
declares and that were verified by rendering the real screen. It is read here, from the
lane being healed, inside the same fail-closed pre-side-effect window the heal already
uses for its inventory and runtime-placement fences — so the observation is of the
generation being replaced, not a stale fact carried from somewhere else. No pane text is
re-interpreted, no exit code is promoted, no ambient host state is consulted.

Once armed, ``aligned`` is the only verdict that relaunches. ``split``, ``drifted`` and
``unknown`` all refuse with ZERO relaunch — the refusal is returned before the heal's
first ``workspace`` / ``tab`` / ``agent`` write, so a refused relaunch has started nothing.
``unknown`` refusing is the ruled cost (j#96167 item 4): "we could not establish which
binary an update would reach" is exactly the state the loop was invisible in.

Nothing about the host leaves: the verdict carries fixed tokens only — no path, no version,
no pane content, no env value — so it is safe on a durable record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_launch_composition import (  # noqa: E501
    LAUNCH_CAUSE_GENERIC_FRESH,
    LAUNCH_CAUSE_UPDATE_RELAUNCH,
    launch_cause_for_observed_blockers,
)

#: The heal was not tied to a provider update, so this fence does not apply. The
#: overwhelming majority of heals, and byte-invariant with the pre-#14741 behaviour.
RELAUNCH_NOT_EVALUATED = "relaunch_authority_not_evaluated"
#: Armed and positively passed: the lane's providers run what their own updaters write to.
RELAUNCH_AUTHORITY_ALIGNED = "relaunch_authority_aligned"
#: Armed and refused. One fixed token for every non-positive verdict; which axis failed is
#: carried in ``detail`` as the classifier's own tokens, never re-derived here.
RELAUNCH_AUTHORITY_REFUSED = "relaunch_authority_refused"


@dataclass(frozen=True)
class RelaunchAuthorityVerdict:
    """Whether a mutating relaunch may proceed (pure value, durable-record safe)."""

    ok: bool
    reason: str
    detail: str = ""
    #: The typed cause this verdict was decided under. ``generic_fresh`` means the fence
    #: was never armed, which is an absence of claim about the lane's authority — never a
    #: statement that it was checked and passed.
    launch_cause: str = LAUNCH_CAUSE_GENERIC_FRESH


def observe_lane_update_screens(
    slots: Mapping[str, Any],
    *,
    read_visible: Callable[[str], object],
    registry: Optional[Any] = None,
) -> Tuple[Tuple[str, str], ...]:
    """Read the lane's live slots and return the ``(provider, blocker_id)`` screens seen.

    ``slots`` is the heal's already-resolved ``{role: (locator, placement_key)}`` map, so
    this reads exactly the slots the heal itself keys on — never a re-derived pair.

    Only BLOCKED admissions contribute. An unreadable pane, an unprofiled provider, and a
    clear composer all contribute nothing, and therefore leave the cause ``generic_fresh``
    (unarmed). That is the correct direction here and only here: failing to read a pane
    must not start refusing relaunches on every host with a slow herdr, and the fence it
    would arm protects against a fault this observation has not shown.
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_admission import (  # noqa: E501
        ADMISSION_BLOCKED,
        evaluate_startup_admission,
    )

    observed: list[Tuple[str, str]] = []
    for role, slot in (slots or {}).items():
        provider = str(role or "").strip()
        locator = ""
        if isinstance(slot, (tuple, list)) and slot:
            locator = str(slot[0] or "").strip()
        if not provider or not locator:
            continue
        admission = evaluate_startup_admission(
            provider_id=provider,
            # `evaluate_startup_admission` never raises and treats any read failure as
            # unreadable, so the read is handed over as-is rather than pre-guarded here.
            read_visible=lambda loc=locator: read_visible(loc),
            registry=registry,
        )
        if admission.outcome == ADMISSION_BLOCKED and admission.blocker_id:
            observed.append((admission.provider_id, admission.blocker_id))
    return tuple(observed)


def evaluate_relaunch_authority(
    providers: Sequence[str],
    env: Optional[Mapping[str, str]] = None,
    *,
    observations: Sequence[Any] = (),
    identity_resolver: Optional[Callable[..., Any]] = None,
    exec_target_resolver: Optional[Callable[..., str]] = None,
    registry: Optional[Any] = None,
) -> RelaunchAuthorityVerdict:
    """Decide whether an update-caused relaunch of ``providers`` may proceed (never raises).

    ``observations`` are the ``(provider_id, blocker_id)`` facts an evidence producer read.
    With none of them update-derived the cause is ``generic_fresh`` and this returns
    admitted **without resolving anything** — no package manager is consulted and no
    manifest is opened, which is what keeps an ordinary heal byte-invariant and hermetic.

    Only the providers an observation actually condemns are evaluated (j#96872: mixed
    plans scope to the update target). A sibling in the same lane is untouched, because a
    screen on one slot says nothing about the other — evaluating it is the R3 shape that
    refused every Claude send.

    The two identities, and why they are BOTH resolved here rather than supplied
    (Design Answer j#96872 item 5)
    ------------------------------------------------------------------------------
    - **bound** — the exact package bin realpath + manifest version the updater target
      *currently owns*;
    - **observed** — the exact exec realpath + that same manifest's version the managed
      resolver is *about to launch*.

    A relaunch is admitted only when both resolve and are equal. Note what this deliberately
    does NOT do: it never compares a *stored* identity from the old generation against the
    new one. The previous cut did, and it was wrong in a way that would have been ugly in
    production — a provider that updated in place gets a new version, so a pre/post equality
    test reads every legitimate update as ``drifted`` and refuses the relaunch **forever**,
    turning the fence into a permanent outage exactly when the operator has done the right
    thing. The old generation's digest is *provenance* — which process showed the update
    screen — and it is used as provenance only.

    So a version advance on the same aligned path relaunches on the new version, and a
    same-version reinstall matches. What still refuses is what should: the managed lane
    running a file the updater's package does not own (the #14741 split), a manifest that
    cannot be corresponded (unknown), and any unresolved half.
    """
    from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_launch_composition import (  # noqa: E501
        update_derived_providers,
    )

    cause = launch_cause_for_observed_blockers(observations)
    if cause != LAUNCH_CAUSE_UPDATE_RELAUNCH:
        return RelaunchAuthorityVerdict(
            ok=True,
            reason=RELAUNCH_NOT_EVALUATED,
            detail=(
                "no update-derived evidence applies to this relaunch, so it is not tied "
                "to a provider update and the update-authority fence does not apply"
            ),
            launch_cause=cause,
        )

    resolve_identity = identity_resolver
    if resolve_identity is None:
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.infrastructure.update_manager_adapter import (  # noqa: E501
            resolve_provider_identity as resolve_identity,
        )
    resolve_exec = exec_target_resolver
    if resolve_exec is None:
        resolve_exec = _managed_exec_target

    in_plan = tuple(providers or ())
    for provider in update_derived_providers(observations):
        if provider not in in_plan:
            # The screen was read on a slot this relaunch is not starting. Out of scope.
            continue

        bound = resolve_identity(provider, env)
        if not getattr(bound, "resolved", False):
            return _refuse(
                provider,
                cause,
                f"the install its own updater owns could not be established "
                f"({getattr(bound, 'reason', 'unknown')})",
            )

        exec_target = resolve_exec(provider, env, registry)
        if not exec_target:
            return _refuse(
                provider, cause, "the managed launch target could not be resolved"
            )

        observed = resolve_identity(provider, env, exec_target=exec_target)
        if not getattr(observed, "resolved", False):
            return _refuse(
                provider,
                cause,
                f"what this lane would launch is not the install its updater owns "
                f"({getattr(observed, 'reason', 'unknown')})",
            )
        if observed.digest != bound.digest:
            # Defence in depth: an exec target matching the package bin already forces the
            # digests equal, so a difference here means the two resolutions disagreed
            # between calls (a concurrent install). Refuse rather than pick one.
            return _refuse(
                provider, cause, "the install changed while it was being evaluated"
            )

    return RelaunchAuthorityVerdict(
        ok=True,
        reason=RELAUNCH_AUTHORITY_ALIGNED,
        detail=(
            "the update-target provider will launch exactly the executable its own "
            "updater owns, at that package's stated version"
        ),
        launch_cause=cause,
    )


def _refuse(provider: str, cause: str, why: str) -> RelaunchAuthorityVerdict:
    """One refusal shape. Fixed tokens plus a fixed clause — no path, version, or env."""
    return RelaunchAuthorityVerdict(
        ok=False,
        reason=RELAUNCH_AUTHORITY_REFUSED,
        detail=f"provider {provider}: {why}; refusing to relaunch it",
        launch_cause=cause,
    )


def _managed_exec_target(provider: str, env, registry) -> str:
    """The realpath the managed launch would run, or ``""`` when it cannot be resolved."""
    from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (  # noqa: E501
        AgentProviderProfileError,
        resolve_agent_launch,
    )

    try:
        return resolve_agent_launch(provider, env, registry=registry).exec_target
    except AgentProviderProfileError:
        # Includes the executable-resolution failures. This classifier runs beside the
        # launch resolver's own fail-closed raise, so it reports rather than raises.
        return ""


def fence_update_relaunch_or_die(
    providers: Sequence[str],
    env: Optional[Mapping[str, str]] = None,
    *,
    slots: Optional[Mapping[str, Any]] = None,
    read_visible_factory: Callable[[], Callable[[str], object]],
    registry: Optional[Any] = None,
    identity_resolver: Optional[Callable[..., Any]] = None,
) -> None:
    """Observe, decide, and refuse a relaunch the lane may not have — or return.

    The whole fence as ONE call, so the heal's composition root keeps a single neutral
    dependency and names no provider, no package manager, and no screen. Same split, and
    the same reason, as :mod:`...startup_admission_composition` on the send side: the
    adapter that owns the relaunch is already at the module-health ceiling, and the
    provider-specific composition does not belong in it anyway.

    The reader is a **factory**, and the observation is best-effort. Both matter, and both
    are corrections to the first cut of this function, which took a built reader and let it
    throw. Constructing the reader resolves the herdr binary, so building it eagerly gave
    EVERY heal — including one with no live slot to read — a new way to fail: a lane whose
    heal previously succeeded now died resolving a transport it was never going to use.
    That is the same defect as arming a package-manager query on a launch that never asked
    for one, one layer down. So: no slots, no factory call; and any failure to observe is
    simply "no observation", which leaves the cause ``generic_fresh`` and the heal exactly
    as it was before this ticket. The fence exists to refuse a relaunch a *positive*
    observation condemns — never to convert an unreadable pane into a refusal.

    Raises :class:`RuntimeError` on refusal — the heal's own fail-closed idiom, so a
    refusal reaches the use case exactly like the runtime-placement fence's does. Called
    inside the pre-side-effect window, so a raise has relaunched nothing.
    """
    observations: Tuple[Tuple[str, str], ...] = ()
    if slots:
        try:
            observations = observe_lane_update_screens(
                slots, read_visible=read_visible_factory(), registry=registry
            )
        except Exception:  # noqa: BLE001 - transport construction / read may fail any way
            observations = ()
    verdict = evaluate_relaunch_authority(
        providers,
        env,
        observations=observations,
        registry=registry,
        identity_resolver=identity_resolver,
    )
    if verdict.ok:
        return
    raise RuntimeError(
        f"lane heal fenced ({verdict.reason}): {verdict.detail}. This lane showed a "
        "provider update screen, so the relaunch is update-derived and must prove, for "
        "this exact generation, that the provider runs the install its own updater writes "
        "to. Re-point the trusted override at that install, or remove the extra install — "
        "mozyo never relaxes to PATH first-match, never rewrites the override for you, and "
        "never answers an update prompt on your behalf."
    )


__all__ = (
    "RELAUNCH_AUTHORITY_ALIGNED",
    "RELAUNCH_AUTHORITY_REFUSED",
    "RELAUNCH_NOT_EVALUATED",
    "RelaunchAuthorityVerdict",
    "evaluate_relaunch_authority",
    "fence_update_relaunch_or_die",
    "observe_lane_update_screens",
)
