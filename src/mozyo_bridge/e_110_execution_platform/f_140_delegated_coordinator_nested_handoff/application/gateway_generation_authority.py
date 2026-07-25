"""Generation-bound turn-start authority for the gateway refresh (Redmine #14203).

The pure-ish decision half of ``sublane recover-gateway``'s turn-start classification, split
out of :mod:`.sublane_gateway_recovery_live` to keep that module under the module-health line
ceiling. Given an exact anchor delivery record + the request pins + the recovery's stores
(repo root, attestation home), it answers ONE question fail-closed: did the delivery's own
queue-enter rail OBSERVE the turn start on a process generation that is COHERENT with — and
IDENTICAL to — the gateway the refresh is about to close?

The authority chain (each tightened by a review round, all fail-closed):

* the observed start must sit on the v2 queue-enter observation (``event_wait_kind ==
  "changed"`` or the #13292 snapshot's WORKING read) — the standard-rail ``turn_start_outcome``
  branch is unreachable and removed (j#87418 F3);
* the record's persisted gateway binding must match the request pins AND the provider
  (j#87424 F1);
* the binding's collision-free per-launch generation token (``startup_action_id``) must be
  non-empty AND exactly equal the LIVE current-generation attestation's token, read now at
  recovery time under a single verified identity join — never the ``observed_at`` timestamp
  (two same-second launches share it, j#87445 F1).

These are module-level functions taking the request + stores explicitly (no ``self``), so the
recovery ops delegate to them and the whole chain stays independently testable.
"""

from __future__ import annotations

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
    repo_scope_workspace_id,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
    RUNTIME_BUSY,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
    _norm_lane,
)


def record_observed_turn_start(rec, *, request, repo_root, attestation_home) -> bool:
    """Did the ANCHOR delivery's QUEUE-ENTER rail OBSERVE the turn start, GENERATION-BOUND?

    (j#87397 / design j#87409 / j#87418 / j#87424 / j#87445.) SCOPE: the herdr queue-enter
    rail only — the design decision's observation-only pre-Enter wait lives there. Two
    requirements, BOTH on the exact anchor delivery record:

    1. an observed start on the v2 queue-enter observation (``event_wait_kind == "changed"``
       OR the #13292 snapshot's ``read_ok`` + ``runtime_state == busy``);
    2. the record's persisted gateway binding is generation-coherent with — and identical to
       — THIS request's live gateway (:func:`record_generation_bound`).
    """
    qe = getattr(rec, "queue_enter_observation", None)
    if not isinstance(qe, dict):
        return False
    observed = _norm(str(qe.get("event_wait_kind") or "")) == "changed" or (
        qe.get("read_ok") is True
        and _norm(str(qe.get("runtime_state") or "")) == RUNTIME_BUSY
    )
    if not observed:
        return False
    return record_generation_bound(
        rec, request=request, repo_root=repo_root, attestation_home=attestation_home
    )


def record_generation_bound(rec, *, request, repo_root, attestation_home) -> bool:
    """The record's persisted binding is the SAME generation as THIS request's LIVE gateway.

    Fail-closed generation authority: the binding must be present and its ``assigned_name`` /
    ``locator`` / ``row_revision`` / ``provider`` must all exactly equal the request pins (all
    non-empty, j#87424 F1), AND its collision-free ``startup_action_id`` token must be
    non-empty and exactly equal the LIVE current-generation attestation's token (j#87445 F1) —
    never the ``observed_at`` timestamp (two same-second launches share it). A same-second
    recycle, an ABA relaunch, a foreign provider, or a tokenless legacy record never binds.
    """
    qe = getattr(rec, "queue_enter_observation", None)
    if not isinstance(qe, dict):
        return False
    binding = qe.get("gateway_binding")
    if not isinstance(binding, dict):
        return False
    pin_rev = _norm(getattr(request, "gateway_revision", ""))
    if not (
        _norm(str(binding.get("assigned_name") or "")) == _norm(request.assigned_name)
        and _norm(str(binding.get("locator") or "")) == _norm(request.locator)
        and pin_rev
        and _norm(str(binding.get("row_revision") or "")) == pin_rev
        and _norm(str(binding.get("provider") or "")) == _norm(request.provider)
    ):
        return False
    binding_token = _norm(str(binding.get("startup_action_id") or ""))
    if not binding_token:
        return False
    return binding_token == current_request_generation_token(
        request=request, repo_root=repo_root, attestation_home=attestation_home
    )


def current_request_generation_token(*, request, repo_root, attestation_home) -> str:
    """The LIVE startup GENERATION TOKEN for THIS request's pinned gateway, gated by a
    SINGLE VERIFIED IDENTITY JOIN (j#87424 F1 + j#87445 F1). (read-only, fail-closed)

    Read at recovery time (the failed gateway is still live, pre-close). The collision-free
    per-launch token (``startup_action_id``) is the generation authority the record binding
    must equal; EVERY identity axis of the attestation must be verified AND the token
    non-empty. Required, all exact: ``verdict == present`` + a non-empty
    ``startup_action_id``; ``assigned_name`` / ``role`` (== the request provider) / ``lane_id``
    / ``locator`` equal the request pins; ``workspace_id`` equals the repo workspace. ``""`` on
    an unreadable store, an absent record, a tokenless (legacy / v2-store) row, or ANY
    mismatched axis.
    """
    from mozyo_bridge.core.state.herdr_identity_attestation import (
        HerdrIdentityAttestationStore,
        VERDICT_PRESENT,
    )

    try:
        repo_workspace = repo_scope_workspace_id(repo_root)
    except Exception:  # noqa: BLE001 - unresolvable workspace => no verified join
        return ""
    if not _norm(repo_workspace):
        return ""
    try:
        record = HerdrIdentityAttestationStore(home=attestation_home).read(
            _norm(request.assigned_name)
        )
    except Exception:  # noqa: BLE001 - unreadable attestation => no live generation
        return ""
    if record is None:
        return ""
    token = _norm(str(getattr(record, "startup_action_id", "") or ""))
    if not (
        _norm(getattr(record, "verdict", "")) == VERDICT_PRESENT
        and token
        and _norm(getattr(record, "assigned_name", "")) == _norm(request.assigned_name)
        and _norm(getattr(record, "role", "")) == _norm(request.provider)
        and _norm_lane(getattr(record, "lane_id", "")) == _norm_lane(request.lane)
        and _norm(getattr(record, "locator", "")) == _norm(request.locator)
        and _norm(getattr(record, "workspace_id", "")) == _norm(repo_workspace)
    ):
        return ""
    return token


__all__ = (
    "record_observed_turn_start",
    "record_generation_bound",
    "current_request_generation_token",
)
