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
    launch_updater_target_resolver,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_update_authority_preflight import (  # noqa: E501
    evaluate_update_authority,
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
    updater_targets: Optional[Callable[[str], Any]] = None,
    bound_identities: Optional[Mapping[str, str]] = None,
    observed_identities: Optional[Mapping[str, str]] = None,
    registry: Optional[Any] = None,
) -> RelaunchAuthorityVerdict:
    """Decide whether an update-caused relaunch of ``providers`` may proceed (never raises).

    ``observations`` are the ``(provider_id, blocker_id)`` facts
    :func:`observe_lane_update_screens` read. With none of them update-derived the cause is
    ``generic_fresh`` and this returns admitted **without resolving anything** — no package
    manager is consulted, which is what keeps an ordinary heal byte-invariant and hermetic.

    ``updater_targets`` overrides the resolver (tests, and any caller arming deliberately);
    by default the built-in one is composed for the plan, and a plan with no provider
    carrying a trusted updater binding resolves to ``None`` — those providers stay
    ``not_evaluated`` and admit, per D2 j#96288 item 1. An unbound provider is out of this
    ticket's scope even under ``update_relaunch``; it is never promoted to ``unknown``,
    which is what refused every Claude send on every host in R3.

    ``bound_identities`` / ``observed_identities`` are
    :func:`...agent_provider_update_authority_preflight.executable_identity` tokens keyed by
    provider — ``<realpath>@<version>``, the exact-generation executable identity.

    **Under ``update_relaunch`` both are REQUIRED** (clarification j#96847). An independent
    audit found the previous cut's hole: the typed cause alone armed the fence, so an armed
    relaunch whose identity was simply *absent* still passed on the authority axis, and a
    lane could be restarted with nothing at all known about which binary it had been running.
    A missing / blank identity on either side is therefore not "the axis was not armed" here
    — it is an unproven generation, and it refuses. The asymmetry with the caller-optional
    axis elsewhere is deliberate: an update-derived relaunch is precisely the moment the
    identity matters, so it is the one moment its absence cannot be waived.

    A drifted identity refuses for the same reason (path OR version): a relaunch would
    inherit a pin that no longer describes what it starts. A same-version reinstall at the
    same realpath is a MATCH, not a drift — nothing this lane runs changed.
    """
    cause = launch_cause_for_observed_blockers(observations)
    if cause != LAUNCH_CAUSE_UPDATE_RELAUNCH:
        return RelaunchAuthorityVerdict(
            ok=True,
            reason=RELAUNCH_NOT_EVALUATED,
            detail=(
                "no update-derived startup screen was observed on this lane, so the "
                "relaunch is not tied to a provider update and the update-authority "
                "fence does not apply to it"
            ),
            launch_cause=cause,
        )

    resolver = (
        launch_updater_target_resolver(providers)
        if updater_targets is None
        else updater_targets
    )
    for provider in providers or ():
        per_provider = resolver(provider) if callable(resolver) else None
        if per_provider is None:
            # D2 j#96288 item 1, and the one ordering that has to be got right here. The
            # plan-wide resolver answers ``None`` for a provider with no trusted updater
            # binding, and that must SKIP the evaluation — not be handed to the classifier,
            # which reads a probe returning ``None`` as a failed probe and answers
            # ``unknown``, i.e. refuses. A lane is a (codex, claude) pair, so passing the
            # resolver straight through would have let codex's update screen refuse the
            # relaunch on CLAUDE's unbound authority: the R3 shape that refused every
            # Claude send on every host, rebuilt one layer down. The launch preflight
            # (:func:`...agent_provider_executable.preflight_launch_providers`) applies the
            # same skip for the same reason; both are tested separately so neither masks
            # the other's loss.
            continue
        # Exact-generation identity is mandatory once armed (j#96847). Checked BEFORE the
        # authority classifier, because "we do not know which binary this lane was running"
        # is a stronger and earlier defect than anything the authority axis can report, and
        # reporting an authority verdict for an unidentified generation would be answering
        # a different question than the one asked. Scoped per provider, so a bound provider
        # missing its identity never refuses on an unbound sibling's behalf.
        bound = str((bound_identities or {}).get(provider, "") or "").strip()
        observed = str((observed_identities or {}).get(provider, "") or "").strip()
        if not bound or not observed:
            return RelaunchAuthorityVerdict(
                ok=False,
                reason=RELAUNCH_AUTHORITY_REFUSED,
                detail=(
                    f"provider {provider} update-derived relaunch has no exact-generation "
                    f"executable identity (bound={'present' if bound else 'absent'}, "
                    f"observed={'present' if observed else 'absent'}); an update-caused "
                    "relaunch is not admitted on an unproven generation"
                ),
                launch_cause=cause,
            )
        authority = evaluate_update_authority(
            provider,
            env,
            registry=registry,
            updater_targets=lambda _pid, _r=per_provider: _r,
            bound_identity=bound,
            observed_identity=observed,
        )
        if authority.admits_actuation:
            continue
        return RelaunchAuthorityVerdict(
            ok=False,
            reason=RELAUNCH_AUTHORITY_REFUSED,
            detail=(
                f"provider {authority.provider} update authority="
                f"{authority.authority}, executable binding={authority.binding}; "
                "restarting it would start the same binary the update could not reach"
            ),
            launch_cause=cause,
        )
    return RelaunchAuthorityVerdict(
        ok=True,
        reason=RELAUNCH_AUTHORITY_ALIGNED,
        detail=(
            "every bound provider in the plan runs the install its own updater writes to"
        ),
        launch_cause=cause,
    )


def fence_update_relaunch_or_die(
    providers: Sequence[str],
    env: Optional[Mapping[str, str]] = None,
    *,
    slots: Optional[Mapping[str, Any]] = None,
    read_visible_factory: Callable[[], Callable[[str], object]],
    registry: Optional[Any] = None,
    bound_identities: Optional[Mapping[str, str]] = None,
    observed_identities: Optional[Mapping[str, str]] = None,
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
        bound_identities=bound_identities,
        observed_identities=observed_identities,
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
