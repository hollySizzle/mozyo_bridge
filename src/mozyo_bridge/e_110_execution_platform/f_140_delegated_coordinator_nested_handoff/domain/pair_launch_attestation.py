"""Post-launch pair self-attestation decision (Redmine #13847, pure).

``sublane create/start`` launches a gateway + worker pair, each wrapped in the #13637
managed-launch self-attestation. A launch that returns a live locator is NOT proof the
pair attested: an incompatible launcher (schema skew) or a failed env injection leaves a
slot **live but unattested / stale**, and promoting that to ``executed`` is exactly the
false success #13847 closes (the live evidence: gateway ``unattested``, worker
``stale_named_slot``). The launcher capability preflight (item 2) prevents the
incompatible-launcher cause *before* launch; this decision is the **action-time
confirmation after launch** — the belt to that suspenders — so a pair that still fails to
self-attest for any reason is never reported as a started lane.

This is the pure decision, shared by two orchestrations so they can never drift:

- the create/start post-launch gate (item 1) — a fresh launch that does not confirm both
  slots' self-attestation returns ``partial_pair_recovery_required`` with a durable
  recovery pointer instead of ``executed``;
- the hibernated exact-pair recovery (items 3–5) — the resume CAS proceeds only after both
  post-hibernate slots re-attest, decided here too.

Fail-closed: a slot is attested only on the positive :data:`ATTEST_OK` join
(:func:`...herdr_identity_attestation.evaluate_attestation`); every other state (absent,
stale, missing, conflict) and any slot the caller could not observe blocks the pair. There
is no partial success — an attested gateway with an unattested worker is
``partial_pair_recovery_required``, naming which slots are bad so the recovery surface can
relaunch exactly the bad generation.

Pure: no I/O, no store access. The caller performs the live observation (read the
attestation store, resolve the live locator, run ``evaluate_attestation``) and passes the
per-slot join results in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

# The two lane roles the pair is composed of, in a stable order (gateway first). Duplicated
# as literals so this domain leaf imports no provider module; the provider binding
# (gateway=codex / worker=claude by default) is resolved by the caller, never here.
#
# These are the LAUNCH-ATTESTATION slot labels. They agree with the declared-pin vocabulary
# by value, but that vocabulary's owner is `mozyo_bridge.core.state.lane_pin_role` (Redmine
# #13920) — pin writers/readers import PIN_ROLE_GATEWAY / PIN_ROLE_WORKER from there. Keep
# these two in step with it; do NOT "align" them onto the legacy `codex` / `claude` spelling
# that `domain.sublane_lifecycle` exports under these same NAMES for provider work.
GATEWAY_ROLE = "gateway"
WORKER_ROLE = "worker"

#: Both slots produced a fresh, locator-matched self-attestation — the pair is started.
PAIR_ATTESTED = "pair_attested"
#: At least one slot is not freshly attested (absent / stale / missing / conflict / not
#: observed). The pair booted partially; a public exact-pair recovery is required. Never a
#: success.
PARTIAL_PAIR_RECOVERY_REQUIRED = "partial_pair_recovery_required"


@dataclass(frozen=True)
class SlotAttestation:
    """One slot's post-launch self-attestation join result (pure input).

    ``role`` is :data:`GATEWAY_ROLE` / :data:`WORKER_ROLE`. ``ok`` is True only for the
    positive :data:`...herdr_identity_attestation.ATTEST_OK` join. ``state`` is the join
    state token (``attested`` / ``absent`` / ``stale`` / ``missing`` / ``conflict`` / a
    caller ``unobserved`` sentinel). ``detail`` is a value-free operator explanation.
    ``locator`` is the live locator the slot was observed at (empty when unobserved) — a
    pointer segment for the durable recovery record, never a secret.
    """

    role: str
    assigned_name: str
    ok: bool
    state: str
    detail: str = ""
    locator: str = ""


@dataclass(frozen=True)
class PairAttestationVerdict:
    """The fail-closed pair verdict + which slots blocked it."""

    ok: bool
    reason: str
    gateway: SlotAttestation
    worker: SlotAttestation
    #: The roles that failed to attest (empty iff ``ok``). Ordered gateway-before-worker.
    blocked_roles: Tuple[str, ...]

    def blocked_summary(self) -> str:
        """A value-free one-line summary of the blocked slots (for the durable pointer)."""
        parts = []
        for slot in (self.gateway, self.worker):
            if not slot.ok:
                parts.append(f"{slot.role}={slot.state}")
        return "; ".join(parts)


def decide_pair_launch_attestation(
    gateway: SlotAttestation, worker: SlotAttestation
) -> PairAttestationVerdict:
    """Decide whether a launched pair confirmed both slots' self-attestation (pure).

    Fail-closed: both slots must be :data:`SlotAttestation.ok` (the positive
    ``ATTEST_OK`` join) for :data:`PAIR_ATTESTED`. Any non-ok slot — including a slot the
    caller could not observe — yields :data:`PARTIAL_PAIR_RECOVERY_REQUIRED`, listing the
    bad roles so a recovery relaunches exactly the bad generation and preserves the good
    one. There is no partial success.
    """
    blocked = tuple(
        slot.role for slot in (gateway, worker) if not slot.ok
    )
    if blocked:
        return PairAttestationVerdict(
            ok=False,
            reason=PARTIAL_PAIR_RECOVERY_REQUIRED,
            gateway=gateway,
            worker=worker,
            blocked_roles=blocked,
        )
    return PairAttestationVerdict(
        ok=True,
        reason=PAIR_ATTESTED,
        gateway=gateway,
        worker=worker,
        blocked_roles=(),
    )


__all__ = (
    "GATEWAY_ROLE",
    "WORKER_ROLE",
    "PAIR_ATTESTED",
    "PARTIAL_PAIR_RECOVERY_REQUIRED",
    "SlotAttestation",
    "PairAttestationVerdict",
    "decide_pair_launch_attestation",
)
