"""Generation-bound turn-start authority for the gateway refresh (Redmine #14203).

The pure-ish decision half of ``sublane recover-gateway``'s turn-start classification, split
out of :mod:`.sublane_gateway_recovery_live` to keep that module under the module-health line
ceiling. Given an exact anchor delivery record + the request pins + the recovery's stores
(repo root, state home), it answers ONE question fail-closed: did the delivery's own
queue-enter rail OBSERVE the turn start on a process generation that is COHERENT with — and
IDENTICAL to — the gateway the refresh is about to close?

The generation authority is the home-scoped :mod:`~mozyo_bridge.core.state.herdr_launch_generation`
store (design consultation answer j#87472): a single ``attested`` row per ``assigned_name``
holding the whole generation as one atomic fact — the collision-free per-launch token
(``startup_action_id``, the reserved startup-transaction action id) plus the exact identity.
Never the seconds-precision ``observed_at`` (two same-second launches share it, j#87445 F1),
and never a token read separately from its identity (a torn pair could compose two
generations, j#87472).

The authority chain (each tightened by a review round, all fail-closed):

* the observed start must sit on the v2 queue-enter observation (``event_wait_kind ==
  "changed"`` or the #13292 snapshot's WORKING read) — the standard-rail ``turn_start_outcome``
  branch is unreachable and removed (j#87418 F3);
* the record's persisted gateway binding must match the request pins AND the provider
  (j#87424 F1);
* the binding's ``startup_action_id`` token must be non-empty AND exactly equal the LIVE
  current-generation token, read now at recovery time from the launch-generation store under
  a single verified identity join — and that token's startup action must itself be a
  terminally-successful startup transaction whose participant is this exact gateway (j#87472).

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

    (j#87397 / design j#87409 / j#87418 / j#87424 / j#87445 / j#87472.) SCOPE: the herdr
    queue-enter rail only — the design decision's observation-only pre-Enter wait lives there.
    Two requirements, BOTH on the exact anchor delivery record:

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
    non-empty, j#87424 F1), AND its ``startup_action_id`` token must be non-empty and exactly
    equal the LIVE current-generation token (j#87472) — never the ``observed_at`` timestamp
    (two same-second launches share it). A same-second recycle, an ABA relaunch, a foreign
    provider, or a tokenless legacy record never binds.
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
    SINGLE VERIFIED IDENTITY JOIN + a terminally-successful startup transaction (j#87472).
    (read-only, fail-closed)

    Read at recovery time (the failed gateway is still live, pre-close) from the home-scoped
    launch-generation store. The collision-free per-launch token (``startup_action_id``) is
    the generation authority the record binding must equal; the whole generation is ONE
    atomic row, so identity and token can never be torn apart. Required, all exact:

    * an ``attested`` generation row for ``request.assigned_name`` with a non-empty token;
    * ``verdict == present``; ``role`` (== the request provider) / ``lane_id`` / ``locator``
      equal the request pins; ``workspace_id`` equals the repo workspace;
    * that token names a startup transaction that reached ``completed_success`` whose
      participant for this provider is exactly this gateway (assigned_name + locator, not
      closed) — so a rolled-back or foreign generation never lends its token.

    Returns ``""`` on an unreadable / absent store, a pending / absent row, a tokenless row,
    ANY mismatched axis, or a startup transaction that is not this gateway's terminal success.
    """
    from mozyo_bridge.core.state.herdr_launch_generation import (
        verified_generation_token,
    )

    try:
        repo_workspace = repo_scope_workspace_id(repo_root)
    except Exception:  # noqa: BLE001 - unresolvable workspace => no verified join
        return ""
    if not _norm(repo_workspace):
        return ""
    return verified_generation_token(
        attestation_home,
        assigned_name=request.assigned_name,
        workspace_id=repo_workspace,
        role=request.provider,
        lane_id=request.lane,
        locator=request.locator,
        norm=_norm,
        norm_lane=_norm_lane,
    )


__all__ = (
    "record_observed_turn_start",
    "record_generation_bound",
    "current_request_generation_token",
)
