"""The default agent launch topology — a contract SEPARATE from the provider registry
(Redmine #13569 Increment 2A, Design Answer j#76964 / Coordinator Answer j#76969).

Registering an agent provider profile makes a provider *expressible* and
*recognizable* (the registry / runtime snapshot vocabulary), but never *launched*.
What ``mozyo`` actually starts — and therefore which agents a session is *expected*
to have — is this separate, deliberately literal contract. The status / doctor /
launch consumers must not conflate the two: they classify observed windows against
the **known** providers (the registry snapshot) but judge *missing* / *ready*
against the **expected** topology here, so a profile-only provider added for a test
or a future rebind is recognizable yet never reported missing (acceptance j#76969
condition 2).

This is intentionally NOT derived from the registry (the hard fence of j#76969:
"default pair / topology は registry から導出しない"). It is the single source the
launch topology (``herdr_launch_command.LAUNCH_PROVIDERS``) and the tmux
status/doctor/launch "expected" judgment both reference, so the two can never drift.
"""

from __future__ import annotations

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (
    default_expected_provider_order,
)

#: The built-in default agent pair a ``mozyo`` / herdr session launches and is expected
#: to carry. It is a PROJECTION of the one canonical role-canonical default topology
#: (:data:`~mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding.DEFAULT_EXPECTED_PROFILE_ORDER`,
#: Redmine #14148 finding 1) — the *expected* launch pair, deliberately NOT derived from
#: the provider registry (known != expected). Order is the launch/creation order
#: (``claude`` first, then ``codex``). Consumers that judge missing / ready take this as an
#: overridable input so a test (or a future topology binding) can supply a different
#: expected set without editing a consumer. Byte-identical to the previous literal.
DEFAULT_EXPECTED_AGENTS: tuple[str, ...] = default_expected_provider_order()


__all__ = ("DEFAULT_EXPECTED_AGENTS",)
