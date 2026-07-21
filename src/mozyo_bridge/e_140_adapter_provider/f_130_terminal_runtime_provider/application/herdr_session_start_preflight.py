"""Argument-level fail-closed validation for a managed session start (Redmine #14242 R4).

The three checks ``_prepare_session_locked`` performs on its own arguments **before any side
effect** — an unknown coordinator placement mode, a duplicate ``(provider, lane)`` slot, and an
invalid managed permission policy. They share a shape: pure functions of the request that either
return or raise, producing no state the caller threads onward.

Extracted verbatim from the launch module as a leaf so that module stays inside the
module-health budget without an allowlist entry (integration disposition j#85316: the #14242
transplant onto the latest ``origin/main-next`` composition pushed it to 1009 lines). The
behaviour, the order, and every message are unchanged — this is a boundary move, not a rewrite.

Why these three and not a bigger slice: everything after them either resolves identity, reads
the store, or touches Herdr, so it is not argument validation and does not belong in a leaf that
promises "no side effect". Keeping the extraction to the pure prefix is what makes it reviewable
as behaviour-preserving.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (  # noqa: E501
    InvalidPermissionMode,
    permission_mode_argv,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.coordinator_placement_mode import (  # noqa: E501
    COORDINATOR_PLACEMENT_MODES,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
)


def validate_session_request(
    *,
    providers: Sequence[str],
    lane_id: str,
    coordinator_placement_mode: str,
    claude_permission_mode_default,
    env: Mapping[str, str],
    error_type: type,
) -> None:
    """Reject a malformed session request BEFORE any side effect (pure; raises ``error_type``).

    - **Unknown coordinator placement mode** (Redmine #14139). The composition roots pass a
      value the config loader already validated; this makes the pure entry point reject a bad
      string directly too, so an unknown mode can never silently degrade to per-project.
    - **Duplicate ``(provider, lane)`` slot** (spec §5 slot-uniqueness). Every requested provider
      shares this run's lane, so a repeated provider is a repeated slot: it would mint the SAME
      ``mzb1_<ws>_<role>_<lane>`` name twice (two launches / two renames), and the read side then
      fails closed with ``multiple_matches``, leaving the session unusable. Fail-closed rejection
      (not silent de-dup) matches the spec wording, so the CLI can keep its repeatable
      ``--agent`` flag.
    - **Invalid managed permission policy** (review j#73404). The lane chokepoint requests
      (codex, claude), so a validation that only fired inside the claude slot's launch would
      leave the codex gateway already started — a partial lane — when the env override is
      invalid. Applicability is data-driven (#13441 R1-F2): every requested provider is asked,
      and one answers only if its profile declares the managed permission concept. Validating
      here (rather than only in the launch preflight) keeps an invalid override fail-closed even
      on an adopt-only run.
    """
    if coordinator_placement_mode not in COORDINATOR_PLACEMENT_MODES:
        raise error_type(
            f"unknown coordinator placement mode {coordinator_placement_mode!r}; "
            f"expected one of {sorted(COORDINATOR_PLACEMENT_MODES)}"
        )
    seen_slots: set = set()
    lane_norm = _norm(lane_id)
    for provider in providers:
        slot = (provider, lane_norm)
        if slot in seen_slots:
            raise error_type(
                f"duplicate requested slot for provider {provider!r} in lane "
                f"{lane_norm or 'default'!r}; each (provider, lane) may be prepared "
                "once — remove the duplicate `--agent` argument"
            )
        seen_slots.add(slot)
    for provider in providers:
        try:
            permission_mode_argv(
                provider, policy_default=claude_permission_mode_default, env=env
            )
        except InvalidPermissionMode as exc:
            raise error_type(str(exc)) from exc


__all__ = ("validate_session_request",)
