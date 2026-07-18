"""Mutating-actuation runtime / placement-contract fence (Redmine #13705).

A lane's gateway (Codex) and worker (Claude) must always live as a **same-tab
pair** (Redmine #13411 lane=tab). The measured #13705 incident: a lane created
under the #13411 same-tab heal contract (source ``c4a999e``) was *healed* by an
older installed runtime (pipx ``mozyo-bridge 0.10.0``) that lacked that contract,
so the replacement gateway landed in a **different tab** from the surviving worker
— a silently-tolerated ``pair_split`` that ``sublane list`` still reported
``active``. The direct cause was a runtime/source skew, but the product defect is
that mutating ``sublane start/heal`` actuation performs pane side effects **without
first proving the running runtime can honour the placement contract the lane was
built under**.

This module is the pure fence that a mutating heal evaluates **before any herdr
write** (workspace / tab / agent). It is deliberately backend-neutral and
side-effect free: it decides from a :class:`RuntimePlacementFingerprint` (the
running build's version + advertised placement capabilities) and an optional
already-observed pair co-location fact. The adapter (``sublane_actuator_herdr_ops``)
builds the production fingerprint, reads the live inventory, and raises on a
blocked verdict so the use case fails closed with zero side effect — exactly like
every other fail-closed session-start condition.

Capability model (self-attestation, not cryptographic): the running runtime
*advertises* which placement contracts it implements. A runtime that ships the
#13411 same-tab heal contract advertises :data:`PLACEMENT_CONTRACT_SAME_TAB_PAIR`.
An incompatible older runtime advertises a set that lacks it (or, with no
resolvable build version, cannot attest its provenance at all). The fence refuses
to mutate an existing lane from a runtime that cannot attest the contract — so a
skewed runtime fails closed *before* it can split the pair, and a compatible
runtime proceeds and is held to the same-tab postcondition the caller verifies
after the launch. The fence never repairs anything and never reads a live process
environment (herdr cannot); recovery from a blocked heal is an owner decision
(verify the runtime with ``mozyo-bridge doctor runtime``, then heal / recreate
from a runtime whose source and installed fingerprints agree — Redmine #13524).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    _norm,
)

#: The placement-contract capability token a runtime advertises when it ships the
#: Redmine #13411 lane=tab same-tab pair placement + heal contract (the surviving
#: slot's tab is read and rejoined instead of minting a fresh split tab).
PLACEMENT_CONTRACT_SAME_TAB_PAIR = "same_tab_pair_v13411"

#: The placement contracts THIS runtime build implements. The production
#: fingerprint advertises exactly this set; a test / simulated older runtime
#: injects a narrower one to exercise the incompatible-runtime fence.
RUNTIME_PLACEMENT_CAPABILITIES = frozenset({PLACEMENT_CONTRACT_SAME_TAB_PAIR})

#: Fence verdict reason vocabulary (fail-closed reasons are non-``ok``).
FENCE_OK = "ok"
#: The running runtime has no resolvable build version, so its placement
#: provenance cannot be attested at all — a mutating heal refuses to guess.
FENCE_PROVENANCE_UNKNOWN = "provenance_unknown"
#: The running runtime's version is known but it does not advertise the required
#: placement contract (the incompatible-older-runtime case, Redmine #13705).
FENCE_RUNTIME_LACKS_CONTRACT = "runtime_lacks_placement_contract"
#: The lane's live gateway/worker pair is ALREADY split across tabs / workspaces,
#: which a heal cannot repair in place (herdr forbids live same-tab re-split);
#: the split is surfaced as a degraded ``pair_split`` state, not healed over.
FENCE_PAIR_ALREADY_SPLIT = "pair_already_split"


@dataclass(frozen=True)
class RuntimePlacementFingerprint:
    """The running build's placement provenance (version + advertised contracts).

    ``version`` is the runtime's ``mozyo_bridge.__version__`` (empty / blank means
    provenance is unknown). ``capabilities`` is the set of placement contracts the
    build attests it implements. Both are self-reported by the running code, so
    the fence is a self-attestation gate, not a cryptographic proof — an older
    runtime that lacked #13411 would (had it carried this fence) advertise a set
    without :data:`PLACEMENT_CONTRACT_SAME_TAB_PAIR`.
    """

    version: str
    capabilities: frozenset = RUNTIME_PLACEMENT_CAPABILITIES


@dataclass(frozen=True)
class HealFenceVerdict:
    """The fence decision: ``ok`` proceeds, otherwise a fail-closed reason + detail."""

    ok: bool
    reason: str
    detail: str


def production_placement_fingerprint() -> RuntimePlacementFingerprint:
    """The running build's real placement fingerprint (``__version__`` + capabilities).

    Read lazily so the module imports without pulling the package version at import
    time. In production the version is the installed / source ``__version__`` and the
    capabilities are :data:`RUNTIME_PLACEMENT_CAPABILITIES` (this build ships #13411).
    """
    from mozyo_bridge import __version__

    return RuntimePlacementFingerprint(
        version=str(__version__ or ""),
        capabilities=RUNTIME_PLACEMENT_CAPABILITIES,
    )


def evaluate_heal_runtime_fence(
    fingerprint: RuntimePlacementFingerprint,
    *,
    required_capability: str = PLACEMENT_CONTRACT_SAME_TAB_PAIR,
    existing_pair_colocated: Optional[bool] = None,
) -> HealFenceVerdict:
    """Decide whether a mutating heal may proceed under ``fingerprint`` (pure).

    Fail-closed order (any blocked verdict must be raised BEFORE a side effect):

    1. **provenance unknown** — no resolvable build version, so the runtime cannot
       attest which placement contract it honours (:data:`FENCE_PROVENANCE_UNKNOWN`);
    2. **runtime lacks the contract** — the version is known but the build does not
       advertise ``required_capability``, i.e. an incompatible older runtime that
       would split the pair (:data:`FENCE_RUNTIME_LACKS_CONTRACT`) — the direct
       #13705 incident shape;
    3. **pair already split** — the lane's live pair is already split across tabs /
       workspaces (``existing_pair_colocated is False``); a heal cannot repair a live
       split, so it fails closed and the split stays visible as a degraded state
       (:data:`FENCE_PAIR_ALREADY_SPLIT`). ``None`` (fewer than two live slots — the
       ordinary single-provider heal) is not applicable and never blocks.

    Otherwise the runtime is compatible and the heal proceeds; the caller then
    verifies the same-tab postcondition after the launch.
    """
    version = (fingerprint.version or "").strip()
    if not version:
        return HealFenceVerdict(
            ok=False,
            reason=FENCE_PROVENANCE_UNKNOWN,
            detail=(
                "the running runtime has no resolvable build version, so its "
                "placement-contract provenance cannot be attested; refuse to mutate "
                "an existing lane from a runtime of unknown provenance (verify with "
                "`mozyo-bridge doctor runtime`)"
            ),
        )
    if required_capability not in fingerprint.capabilities:
        return HealFenceVerdict(
            ok=False,
            reason=FENCE_RUNTIME_LACKS_CONTRACT,
            detail=(
                f"the running runtime (version {version}) does not advertise the "
                f"{required_capability!r} placement contract required to heal this "
                "lane without splitting its gateway/worker pair across tabs; the "
                "runtime is incompatible with the lane's placement contract "
                "(source/installed runtime skew — Redmine #13705). Fail-closed with "
                "no pane side effect; heal from a compatible runtime whose "
                "`doctor runtime` fingerprint matches the source"
            ),
        )
    if existing_pair_colocated is False:
        return HealFenceVerdict(
            ok=False,
            reason=FENCE_PAIR_ALREADY_SPLIT,
            detail=(
                "the lane's live gateway/worker pair is already split across tabs / "
                "workspaces; a heal cannot repair a live split in place (herdr "
                "forbids same-tab re-split of live panes). The split is a degraded "
                "`pair_split` state — surface it and recover by an owner-approved "
                "retire + recreate, not a heal"
            ),
        )
    return HealFenceVerdict(
        ok=True,
        reason=FENCE_OK,
        detail=(
            f"runtime version {version} advertises {required_capability!r}; the heal "
            "may proceed under the same-tab pair placement contract"
        ),
    )


# -- same-tab postcondition (verified AFTER the relaunch) -----------------------
#
# The preflight fence above proves the runtime CAN honour same-tab placement; this
# postcondition proves the relaunch DID (Redmine #13705). Redmine #13933 R11 (j#81429)
# scopes it to a single owed participant so a pair-level launcher driven for ONE
# replacement leg converges an approved partial pair — without ever bypassing the
# same-tab placement contract (a live split still fails closed).

#: Both slots are live but in different placement containers — a live pair_split. Never
#: bypassed: a target-scoped heal still fails closed on a live split.
HEAL_REASON_PAIR_SPLIT = "pair_split"
#: A full-pair heal did not leave BOTH slots live and co-located.
HEAL_REASON_PAIR_INCOMPLETE = "pair_incomplete"
#: A target-scoped heal's own owed participant slot did not come up live.
HEAL_REASON_TARGET_ABSENT = "launch_target_absent"


class SublaneHealError(RuntimeError):
    """A mutating lane heal was fenced (preflight or same-tab postcondition, #13705).

    Subclasses :class:`RuntimeError` so every caller catching ``RuntimeError`` /
    ``Exception`` and every test asserting the fence's message substring is unchanged.
    It additionally carries a stable, credential/path-free ``reason`` token so a
    single-participant convergence launch surfaces WHY the launcher fenced in its public
    outcome instead of a bare ``effect_failed / launch`` (Redmine #13933 R11 j#81429 #2).
    """

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


def enforce_heal_postcondition(
    healed: "Mapping[str, tuple[str, str]]",
    pair: "tuple[str, str]",
    *,
    target_provider: "Optional[str]" = None,
) -> None:
    """Verify the same-tab pair placement after a relaunch, or raise (pure, #13705).

    ``healed`` maps provider -> the live ``(locator, placement_key)`` on the post-heal
    read-back; ``pair`` is the binding-resolved ``(gateway, worker)`` providers.
    ``target_provider`` selects the postcondition contract:

    - ``None`` — a **full-pair** heal (#13378 / #13705 self-heal): BOTH slots must be
      live and share one placement container. A missing slot or a split fails closed.
      The byte-identical historical contract.
    - a provider — a **single-participant** convergence launch (Redmine #13933 R11
      j#81429 #3): the pair-level launcher is driven for ONE owed replacement leg while
      its sibling may still be a stale participant awaiting its own leg or, for an
      approved partial pair, legitimately absent. That provider's slot must be live and
      — whenever the sibling is ALSO live — the pair must still be co-located (a live
      split is NEVER tolerated, so same-tab placement is not bypassed); an absent sibling
      is a partial state a later leg converges, not a launch failure.

    Raises :class:`SublaneHealError` with a stable ``reason`` token on any failure; the
    message names the observed slots for the journal (locators only — no path / credential).
    """
    gateway = healed.get(pair[0])
    worker = healed.get(pair[1])
    colocation = None if gateway is None or worker is None else gateway[1] == worker[1]
    if target_provider is None:
        target_present = None
        ok = colocation is True
    else:
        target_present = any(_norm(role) == _norm(target_provider) for role in healed)
        ok = target_present and colocation is not False
    if ok:
        return
    if colocation is False:
        reason = HEAL_REASON_PAIR_SPLIT
    elif target_present is False:
        reason = HEAL_REASON_TARGET_ABSENT
    else:
        reason = HEAL_REASON_PAIR_INCOMPLETE
    raise SublaneHealError(
        "lane heal postcondition failed: the gateway "
        f"{gateway[0] if gateway else '<none>'} and worker "
        f"{worker[0] if worker else '<none>'} are not confirmed in one "
        f"placement container after the relaunch (gateway placement "
        f"{gateway[1] if gateway else None}, worker placement "
        f"{worker[1] if worker else None}); the pair is split or incomplete "
        "(Redmine #13705) — fail-closed",
        reason=reason,
    )


__all__ = (
    "FENCE_OK",
    "FENCE_PAIR_ALREADY_SPLIT",
    "FENCE_PROVENANCE_UNKNOWN",
    "FENCE_RUNTIME_LACKS_CONTRACT",
    "HEAL_REASON_PAIR_INCOMPLETE",
    "HEAL_REASON_PAIR_SPLIT",
    "HEAL_REASON_TARGET_ABSENT",
    "HealFenceVerdict",
    "PLACEMENT_CONTRACT_SAME_TAB_PAIR",
    "RUNTIME_PLACEMENT_CAPABILITIES",
    "RuntimePlacementFingerprint",
    "SublaneHealError",
    "enforce_heal_postcondition",
    "evaluate_heal_runtime_fence",
    "production_placement_fingerprint",
)
