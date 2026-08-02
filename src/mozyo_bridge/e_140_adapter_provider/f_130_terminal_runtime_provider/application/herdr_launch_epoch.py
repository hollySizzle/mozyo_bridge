"""The lane epoch at the managed-launch boundary (Redmine #14756).

Everything the launch side needs to know about the epoch, in one leaf: how to READ the
lane's minted epoch, whether a given launch therefore carries one, and whether the selected
attestation store can actually hold it. The semantics of the epoch itself — what it proves,
how it is minted, what a token has to look like — belong to
:mod:`mozyo_bridge.core.state.lane_epoch`; this module is only the adapter-side boundary.

It is a **new module rather than three more blocks in the modules that already do this
work**, which is the standing instruction for this feature (#13948 Answer j#80989: "new
module, do not grow the modules already near the ceiling"). That instruction had teeth here:
adding the epoch inline put ``herdr_session_start`` at 1012 lines and
``herdr_launcher_capability`` at 1004, against a 1000-line gate — and ``herdr_session_start``
had been sitting at 999, i.e. one line of head-room. Splitting on the seam the feature
actually has is the fix; an allowlist entry would only record the drift.

Three callers, one fact between them, which is the reason they share a module:

- :func:`resolve_launch_lane_epoch` — the per-slot launch reads the epoch to inject;
- :func:`launch_carries_lane_epoch` — the pre-launch preflight asks the same question as a
  boolean, and must get its answer from the SAME read, or the preflight could admit a launch
  that then injects an epoch (or refuse one that would not have);
- :func:`epoch_store_admission` — the store-compatibility verdict for such a launch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

#: The first attestation-store shape carrying ``lane_epoch`` (#14756). An epoch-bearing
#: launch onto an older shape is refused in the PREFLIGHT, before any herdr side effect —
#: never only in the child, whose attestation write is best-effort and swallows its own
#: refusal so a boot is never blocked. A store-side check that is not in the preflight does
#: not run in any form an operator can observe.
EPOCH_MIN_STORE_VERSION = 3

#: An epoch-bearing launch onto a store whose shape has no ``lane_epoch`` column. The pair
#: would boot live and correctly launched, but with no epoch in its attestation — so
#: ``sublane resume`` could never admit it, and nothing would say why.
STORE_EPOCH_UNSUPPORTED = "attestation_store_epoch_unsupported"

#: The named next action every store-shape refusal points at. It lives here, with the
#: refusals that cite it, and ``herdr_launcher_capability`` imports it — rather than each
#: keeping its own copy — so the launch preflight and the pre-close replacement fence can
#: never end up telling an operator two different commands for the same problem.
MIGRATE_HINT = "`mozyo-bridge herdr attestation-store migrate --write`"


def resolve_launch_lane_epoch(
    workspace_id: str, lane: str, *, store_home: str = ""
) -> str:
    """The lane's minted epoch as a canonical token to inject, or ``""`` (Redmine #14756).

    The launch side of the epoch-bound generation proof, and deliberately the *smallest*
    thing that could work: a **read**. Nothing is minted here — the counter advances only
    inside the hibernate disposition CAS, from the row's own stored value — so a launch adds
    no write, no schema migration and no new failure mode to a path whose whole job is to get
    a pane up. That matters beyond tidiness: the shared home is read concurrently by lanes
    running source CLIs of different schema generations, and a forward migration on a launch
    would fail-close every older concurrent reader (``managed-state-model.md``
    ``#### read-compatible / write-migrating split``). So this goes through the read-only,
    non-migrating ``LaneLifecycleReader``, never the migrating store.

    **Fail-open to ``""``, and only here.** An absent row, an unminted epoch, an unreadable
    or newer-schema store all yield the empty token, which means the launch injects nothing
    and is byte-for-byte the pre-#14756 launch. That is not a relaxed gate: it moves the
    refusal to where the evidence actually is. A store problem must never block an agent boot
    (the same contract the best-effort attestation writer has had since #13637 — blocking a
    boot on a cache failure kills the operator's pane), and a pair launched without an epoch
    simply cannot satisfy the resume proof, which fails closed on
    ``lane_epoch_attestation_absent`` with the reason named. Fail-closed at launch would
    trade a refusable resume for an unstartable lane.

    Rendered with ``str(int)`` so the injected token is exactly the canonical form the
    resume-side classifier accepts (``lane_epoch.parse_attested_epoch``); producer and parser
    therefore agree by construction rather than by convention.
    """
    from mozyo_bridge.core.state.lane_epoch import EPOCH_OK, required_resume_epoch
    from mozyo_bridge.core.state.lane_lifecycle_model import LaneLifecycleKey
    from mozyo_bridge.core.state.lane_lifecycle_readonly import LaneLifecycleReader

    try:
        reader = LaneLifecycleReader(home=Path(store_home) if store_home else None)
        record = reader.get(LaneLifecycleKey(workspace_id, lane))
    except Exception:  # noqa: BLE001 — a store problem must never block an agent boot
        return ""
    epoch, authority = required_resume_epoch(record)
    return str(epoch) if authority == EPOCH_OK else ""


def launch_carries_lane_epoch(
    workspace_id: str, lane: str, *, store_home: str = ""
) -> bool:
    """Whether a launch for this lane will inject an epoch — the preflight's question.

    Deliberately expressed as :func:`resolve_launch_lane_epoch` rather than as its own
    lifecycle read. The preflight and the launch must answer from the SAME predicate, or the
    two can disagree: a preflight with an independent notion of "has an epoch" could admit a
    launch that then injects one (the refusal never fires) or refuse one that would not have
    (a launch blocked for nothing). Two surfaces classifying one stored fact eventually
    classify it two ways — the #14477 R7 lesson, applied before it can happen rather than
    after.
    """
    return bool(resolve_launch_lane_epoch(workspace_id, lane, store_home=store_home))


def epoch_store_admission(
    *, epoch_launch: bool, store_version: int, migrate_hint: str
) -> Optional[tuple[str, str]]:
    """``(reason, detail)`` refusing an epoch-bearing launch onto an old store, else ``None``.

    The exact twin of the ``replacement_action_id`` refusal beside it in
    :func:`...herdr_launcher_capability.decide_store_compatibility`, and it exists for the
    same reason rather than for symmetry: a field that cannot survive the older shape must be
    refused where the operator can see it, not dropped silently.

    Returns ``None`` for every admissible case — an epoch-less launch (the pre-#14756 shape,
    and any lane whose lifecycle row has minted no epoch) loses nothing on an older shape, so
    the mixed-runtime home keeps working exactly as #13882 requires.
    """
    if not epoch_launch or store_version >= EPOCH_MIN_STORE_VERSION:
        return None
    return (
        STORE_EPOCH_UNSUPPORTED,
        f"this launch carries a lane epoch, but the selected attestation store is "
        f"v{store_version}, whose shape has no `lane_epoch` column. Attesting it would "
        f"silently drop the epoch, so the pair would boot live and correctly launched yet "
        f"never be resumable (`sublane resume` requires an attested epoch strictly newer "
        f"than the lane's hibernate epoch). Migrate the store first: {migrate_hint}",
    )


def replacement_store_admission(
    workspace_id: str,
    lane_id: str,
    *,
    lifecycle_home: str = "",
    attestation_home: str = "",
) -> Optional[str]:
    """The reason token refusing an epoch-bearing REPLACEMENT, or ``None`` (Redmine #14756).

    The pre-effect half of :func:`epoch_store_admission`, joined for the one caller that
    cannot wait for the launch preflight to answer: a replacement action closes the old slot
    *before* it launches the new one. Discovering there that the epoch cannot be stored
    leaves the pair destroyed and unrelaunchable — the defect j#96848 measured — so the same
    question is asked here, ahead of the first close, and answered from the SAME two
    predicates the preflight uses (:func:`resolve_launch_lane_epoch` and
    :func:`epoch_store_admission`). No third opinion about the epoch exists.

    **Scoped to the epoch axis, deliberately.** The launcher-capability join
    (``decide_store_compatibility``) also rules on the launcher's advertised shapes and on
    the ``replacement_action_id`` column; those need a launcher probe (a subprocess) and
    would be a second surface classifying facts this one has no business re-deciding. What
    this adds is only the axis whose refusal must arrive before a close.

    The store shape is required to be *knowable* only once an epoch is actually at stake:

    - no epoch minted for the lane -> ``None``. This is the conditional-C rule 1 case
      (j#96844): a true legacy ``lane_epoch=0`` lane keeps its existing v1 side-binding heal
      path byte-for-byte, because refusing it would delete a working recovery rail without
      making any epoch storable.
    - store absent -> ``None``. The first attestation write creates it at the current
      version, which carries ``lane_epoch``.
    - store recognized -> the epoch/version verdict, i.e. conditional-C rule 2.
    - store unreadable / unsupported -> refuse with that state's own token. An unknowable
      shape is not an adequate one, and this is the branch where an epoch is already at
      stake, so folding "cannot read" into "fine" would be exactly the #13682 R1-F1 mistake
      of reading an absence of measurement as a measurement of absence.

    The two homes are separate parameters because the two reads are separate stores — the
    lifecycle DB mints the epoch, the attestation DB has to hold it — and the recovery ops
    that call this already carry them as independent fields. Collapsing them into one
    argument would have silently pointed one of the two reads at the wrong isolated home
    under test, which is the quietest possible way for a fence to appear armed and not be.
    Either one empty means "the ambient home", the production case for both.
    """
    from mozyo_bridge.core.state.herdr_identity_attestation import (
        herdr_identity_attestation_path,
    )
    from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
        STORE_ABSENT,
        STORE_RECOGNIZED,
        probe_store_schema,
    )

    if not launch_carries_lane_epoch(workspace_id, lane_id, store_home=lifecycle_home):
        return None
    home = Path(attestation_home) if attestation_home else None
    try:
        observation = probe_store_schema(herdr_identity_attestation_path(home))
    except Exception:  # noqa: BLE001 — an unprobeable store is an inadequate one, not a crash
        return STORE_EPOCH_UNSUPPORTED
    if observation.state == STORE_ABSENT:
        return None
    if observation.state != STORE_RECOGNIZED:
        return observation.state
    refusal = epoch_store_admission(
        epoch_launch=True,
        store_version=int(observation.version or 0),
        migrate_hint=MIGRATE_HINT,
    )
    return refusal[0] if refusal is not None else None


__all__ = (
    "EPOCH_MIN_STORE_VERSION",
    "MIGRATE_HINT",
    "STORE_EPOCH_UNSUPPORTED",
    "epoch_store_admission",
    "launch_carries_lane_epoch",
    "replacement_store_admission",
    "resolve_launch_lane_epoch",
)
