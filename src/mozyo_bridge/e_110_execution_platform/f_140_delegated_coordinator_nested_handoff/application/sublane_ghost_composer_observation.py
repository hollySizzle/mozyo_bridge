"""Default render-ghost facts reader for the pending-composer gate (Redmine #14065 Phase 2).

The e110 pending-composer seams (quarantine inspect, hibernated reconcile) decide with the
pure gate in ``domain.sublane_ghost_composer_gate`` but need the closed render facts to feed
it. This thin application helper is the default source of those facts: it calls the e140
authority-resolved render read (``read_composer_render`` — herdr-backend-only, foreign-target
fail-closed, redacted) and maps its :class:`ComposerRenderView` onto the domain
:class:`RenderGhostFacts`. It carries no pane body across — only the closed enums / bools and
the authority-resolved provider id.

The e140 import stays lazy inside the function (the seams' other provider reads are lazy too),
and the seams accept an injected reader so a hermetic test never spawns herdr. A non-herdr
backend or an unresolved / unreadable target yields an ``unobserved`` facts value, which the
gate treats as preserve.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Mapping, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_ghost_composer_gate import (  # noqa: E501
    GhostComposerRenderPolicy,
    RenderGhostFacts,
    render_admits_empty,
)


def apply_ghost_empty(
    has_pending: object,
    *,
    policy: Optional[GhostComposerRenderPolicy],
    repo_root: Path,
    env: Optional[Mapping[str, str]],
    locator: str,
    facts_reader: Optional[Callable[[str], RenderGhostFacts]] = None,
) -> object:
    """The effective ``has_pending`` after the Phase 2 dim-ghost render gate.

    The single shared seam both pending-composer observers use (quarantine inspect,
    hibernated reconcile). Returns ``has_pending`` unchanged unless a policy is injected
    AND the text observation reported pending — then it authority-resolves the render at
    action time and returns ``False`` iff the render is a dim ghost the provider declares
    (:func:`render_admits_empty`). Everything else, and any read error, preserves.
    ``facts_reader`` lets a hermetic test supply facts; ``None`` uses the live read.
    """
    if has_pending is not True or policy is None:
        return has_pending
    reader = facts_reader or (
        lambda loc: read_render_ghost_facts(repo_root, loc, env=env)
    )
    try:
        facts = reader(locator)
    except Exception:  # noqa: BLE001 - a failed render read preserves (never empties)
        return has_pending
    if render_admits_empty(text_has_pending=True, facts=facts, policy=policy):
        return False
    return has_pending


def default_ghost_policy() -> GhostComposerRenderPolicy:
    """The injected ghost policy the public rails use, built from the v3 profiles (e140).

    Lazily reaches the e140 factory so the e110 rails inject the policy without importing
    the provider registry / singleton themselves (IR j#82181 item 2). A load failure fails
    closed to the empty policy — admits nothing, so the gate degrades to pure preserve.
    """
    try:
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_ghost_policy import (  # noqa: E501
            build_ghost_composer_policy,
        )

        return build_ghost_composer_policy()
    except Exception:  # noqa: BLE001 - an unbuildable policy preserves (never empties)
        return GhostComposerRenderPolicy.empty()


def read_render_ghost_facts(
    repo_root: Path,
    locator: str,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> RenderGhostFacts:
    """Authority-resolved, redacted render facts for ``locator`` (fail-closed, no raise)."""
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
        read_composer_render,
    )

    try:
        view = read_composer_render(
            repo_root, locator, env=env if env is not None else os.environ
        )
    except Exception:  # noqa: BLE001 - a failed render read is fail-soft to unobserved (preserve)
        return RenderGhostFacts.unobserved(reason="read_error")
    observation = view.observation
    if not view.backend_selected or observation is None:
        return RenderGhostFacts.unobserved(
            reason="backend_not_selected" if not view.backend_selected else "no_observation"
        )
    return RenderGhostFacts(
        observed=True,
        readable=observation.readable,
        prompt_present=observation.prompt_present,
        style_provenance=observation.style_provenance,
        provider_id=view.provider,
        reason=observation.reason,
    )


__all__ = ("apply_ghost_empty", "default_ghost_policy", "read_render_ghost_facts")
