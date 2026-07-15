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
from typing import Optional

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


__all__ = (
    "FENCE_OK",
    "FENCE_PAIR_ALREADY_SPLIT",
    "FENCE_PROVENANCE_UNKNOWN",
    "FENCE_RUNTIME_LACKS_CONTRACT",
    "HealFenceVerdict",
    "PLACEMENT_CONTRACT_SAME_TAB_PAIR",
    "RUNTIME_PLACEMENT_CAPABILITIES",
    "RuntimePlacementFingerprint",
    "evaluate_heal_runtime_fence",
    "production_placement_fingerprint",
)
