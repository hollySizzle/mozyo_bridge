"""Render-based ghost-composer empty gate (Redmine #14065 Phase 2, pure).

#14064 proved that a plain-text composer observation cannot tell a provider *ghost*
idle placeholder from an exact-same-text real unsent input: both render byte-identical
text, so ``observe_composer_text`` reported ``has_pending=True`` for a ghost and every
public rail (converge-bound-pair / repair-pins / hibernate) preserved it — blocking the
#13846 drain. Phase 1 built a redacted render observation; the live item-7 diagnostic
(j#82180) admitted exactly one positive discriminator: a ghost renders ``dim``, real
input renders ``normal``.

This module is the pure gate that lets that render signal empty a *text* pending
candidate — and nothing else. It is deliberately fail-closed and content-free:

- it never sees pane body / hash / length / raw ANSI — only the closed render facts
  the e140 adapter hands across the boundary (:class:`RenderGhostFacts`);
- a text candidate may be emptied ONLY when the render authority positively says the
  composer is a readable, prompt-present ghost whose ``style_provenance`` the resolved
  provider *declares* as a ghost signal (:class:`GhostComposerRenderPolicy`, built from
  the v3 provider profile schema — ``dim`` today). ``normal`` / ``mixed`` / ``unknown``,
  an unreadable / ambiguous render, an unresolved provider, or a missing observation all
  preserve (:func:`render_admits_empty` returns ``False``);
- the empty vocabulary stays ``dim``-only because the *policy* carries the admitted set
  (from e140), so this module holds no render-vocabulary literal to drift.

Dependency direction: this is core (e110). It receives already-closed facts and an
injected policy value; it never imports the e140 provider registry / render adapter
(IR j#82181 item 2). The e140 side builds the policy (from the profile registry) and the
facts (from the authority-resolved render read) and injects both.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class GhostComposerRenderPolicy:
    """Which ``style_provenance`` each provider declares as a ghost-composer signal.

    A frozen projection of the v3 provider profiles' ``ghost_composer_signals`` (built by
    the e140 factory and injected). ``admits`` is fail-closed: a non-string provider /
    provenance, a provider not in the map, or a provenance the provider did not declare
    all return ``False``. :meth:`empty` is the default a caller uses when no policy was
    injected — it admits nothing, so the gate degrades to pure preserve (Phase-1 behaviour).
    """

    _admitted: Mapping[str, frozenset[str]]

    def admits(self, provider_id: object, style_provenance: object) -> bool:
        if not isinstance(provider_id, str) or not isinstance(style_provenance, str):
            return False
        return style_provenance in self._admitted.get(provider_id, frozenset())

    def admitted_for(self, provider_id: object) -> frozenset[str]:
        if not isinstance(provider_id, str):
            return frozenset()
        return self._admitted.get(provider_id, frozenset())

    @classmethod
    def from_pairs(
        cls, pairs: "Mapping[str, frozenset[str]] | dict[str, frozenset[str]]"
    ) -> "GhostComposerRenderPolicy":
        """Freeze a ``{provider_id: admitted style_provenances}`` mapping into a policy."""
        frozen = {
            str(provider): frozenset(str(s) for s in signals)
            for provider, signals in dict(pairs).items()
        }
        return cls(_admitted=frozen)

    @classmethod
    def empty(cls) -> "GhostComposerRenderPolicy":
        """The fail-closed default: admits no ghost signal for any provider."""
        return cls(_admitted={})


@dataclass(frozen=True)
class RenderGhostFacts:
    """The closed, content-free render facts the e140 adapter hands to the gate.

    Carries no body / hash / length / excerpt / raw ANSI — only the closed enums / bools
    the redacted render observation exposes, plus the authority-resolved provider. Built
    on the e140 side from an authority-resolved render read; :meth:`unobserved` is the
    fail-closed value when no render could be authority-resolved (foreign / non-herdr /
    unreadable target), which always preserves.
    """

    observed: bool
    readable: bool
    prompt_present: bool
    style_provenance: str
    provider_id: str
    reason: str = ""

    @classmethod
    def unobserved(cls, *, reason: str = "") -> "RenderGhostFacts":
        return cls(
            observed=False,
            readable=False,
            prompt_present=False,
            style_provenance="unknown",
            provider_id="",
            reason=reason,
        )


def render_admits_empty(
    *,
    text_has_pending: object,
    facts: RenderGhostFacts,
    policy: GhostComposerRenderPolicy,
) -> bool:
    """Whether a text pending candidate may be emptied as a render-confirmed ghost.

    Fail-closed conjunction (IR j#82181 item 3): returns ``True`` ONLY when the text
    observation actually reported a pending composer AND the authority-resolved render
    was observed, readable (which, by the render observation's invariant, means
    ``reason == "ok"``), prompt-present, and its ``style_provenance`` is one the resolved
    provider declares as a ghost signal. Any other combination — no text pending, no
    observation, unreadable / ambiguous render, no prompt, an unadmitted provenance
    (``normal`` / ``mixed`` / ``unknown``), or an unresolved provider — returns ``False``
    (preserve).
    """
    if text_has_pending is not True:
        return False
    if not facts.observed or not facts.readable or not facts.prompt_present:
        return False
    return policy.admits(facts.provider_id, facts.style_provenance)


__all__ = (
    "GhostComposerRenderPolicy",
    "RenderGhostFacts",
    "render_admits_empty",
)
