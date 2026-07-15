"""Provider startup-screen signature schema (Redmine #13760).

The closed ``startup_blockers[{id, all_of}]`` schema a provider profile carries: the
pre-composer **startup screens** (a trust confirmation, a first-run theme picker, a login
prompt) that render as a live pane but cannot accept a handoff body. A fresh worktree's
managed Claude worker sat on one, so every readiness projection called it ready and the
queue-enter rail typed an Implementation Request into a screen with no composer (#13582
j#77917). The pre-send admission gate reads this schema to refuse such a send.

Kept in its own module (out of the oversized ``agent_provider_profile_config`` — the
module-health gate) because it is a cohesive, self-contained schema: a pure fold, a few
bounds, and one frozen record with the whole invariant on it. ``agent_provider_profile_config``
imports these back and re-exports them, so every existing importer of ``StartupBlocker`` /
``fold_startup_text`` is unchanged.

Dependency direction: this is a **leaf**. It borrows the shared
:class:`AgentProviderProfileError` and the ``_reject_forbidden_token`` guard from
``agent_provider_profile_config`` lazily, inside the two methods that raise — so there is
no import cycle (the config module imports this one at top level). The lazy import is a
cached ``sys.modules`` lookup; a ``StartupBlocker`` is built only from packaged profile
data (a handful) and tests, never on a hot path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

#: Keys a ``startup_blockers`` entry may carry. Closed like every other profile block: an
#: unknown key is rejected, so a blocker can never grow a field a future reader might honor
#: as an action ("accept", "keys", "answer") — the profile describes a screen, it never
#: authorises a response to one.
_STARTUP_BLOCKER_KEYS: frozenset[str] = frozenset({"id", "all_of"})

#: How many startup blockers one provider may declare. A provider has a handful of
#: pre-composer screens; a large list is a data mistake (or an attempt to make the
#: pre-send classifier scan an unbounded corpus on every send), not a real contract.
MAX_STARTUP_BLOCKERS = 8

#: The AND-arity bounds of one blocker's ``all_of``. The lower bound is the
#: false-positive guard j#77947 requires: a single generic phrase ("Yes", "continue")
#: could appear in a perfectly ready composer, so a blocker must be pinned by at
#: least two co-located signatures from the SAME screen. The upper bound keeps a
#: blocker falsifiable — a long AND chain is one re-word away from silently never
#: matching (a fail-OPEN drift, which is the failure this whole gate exists to stop).
MIN_STARTUP_SIGNATURES = 2
MAX_STARTUP_SIGNATURES = 6

#: Length bounds on one signature, measured on the *folded* form (the classifier's
#: match key). A too-short signature matches too much; a too-long one is brittle
#: against re-wording and pane truncation.
MIN_STARTUP_SIGNATURE_FOLDED_LEN = 6
MAX_STARTUP_SIGNATURE_LEN = 160


def fold_startup_text(text: object) -> str:
    """The classifier's match key: lowercased, alphanumerics only (pure, never raises).

    Everything that is not alphanumeric is dropped — whitespace, punctuation, and the
    box-drawing / border glyphs a TUI dialog frames its lines with. A rendered pane
    hard-wraps a long line to the pane width (even mid-token) and may reprint a border
    glyph at the wrap, so a raw substring test against the on-screen text is fragile in
    exactly the way that would make this gate fail OPEN. Folding both sides to
    alphanumerics makes the wrap, the frame, and the punctuation vanish, so a signature
    matches however the TUI folded the screen. Unicode-aware (:meth:`str.isalnum`), so a
    non-Latin provider UI folds the same way.

    This is the same "normalise away the rendering, then substring-match" posture as the
    turn-start rail's :func:`~...f_130_terminal_runtime_provider.domain.turn_start_rail.composer_retains_body`,
    widened from whitespace to all non-alphanumerics because a dialog — unlike a composer
    body — is framed.
    """
    if not isinstance(text, str):
        return ""
    return "".join(ch for ch in text.lower() if ch.isalnum())


@dataclass(frozen=True)
class StartupBlocker:
    """One provider startup screen that cannot accept a handoff body (Redmine #13760).

    A blocker is matched when **every** signature in ``all_of`` appears in the folded
    visible pane content (:func:`fold_startup_text`) — an AND, never an any-match, so a
    single generic phrase cannot classify a ready composer as blocked (j#77947
    correction 1). ``blocker_id`` is a fixed token: it is the ONLY thing about the
    screen that is allowed to reach a structured outcome / journal — the pane's own text
    is never carried out of the classifier (j#77947 invariant 3).

    Purely mechanical description. A blocker names a screen; it does not say what to do
    about it, and it carries no keys, no answer, and no authority to dismiss it. Clearing
    a trust / login prompt stays an operator action in the provider's own UI.
    """

    blocker_id: str
    all_of: tuple[str, ...]

    def __post_init__(self) -> None:
        # Redmine #13760 review j#78481 finding 1: the FULL invariant lives here, in the
        # single validator EVERY construction path runs — not split between `__post_init__`
        # (count only) and `from_record` (elements). A directly-built
        # `StartupBlocker("trust", ("phrase", None))` used to keep its malformed `all_of`,
        # and because `fold_startup_text(None) == ""` and `"" in folded` is always true,
        # the AND silently degraded to a single-signature match — the exact false positive
        # the AND exists to prevent. So the object itself now cannot hold a malformed
        # signature set, whoever builds it.
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (  # noqa: E501
            AgentProviderProfileError,
            _reject_forbidden_token,
        )

        if not isinstance(self.blocker_id, str) or not self.blocker_id.strip():
            raise AgentProviderProfileError(
                f"startup blocker 'id' must be a non-empty string, got {self.blocker_id!r}"
            )
        _reject_forbidden_token(
            self.blocker_id, field="startup_blockers.id", provider_id=self.blocker_id
        )
        if isinstance(self.all_of, (str, bytes)) or not isinstance(self.all_of, tuple):
            # A bare string is iterated character-by-character; a list is unhashable on a
            # frozen dataclass. Require the tuple contract explicitly rather than coercing
            # a wrong shape into a passing one.
            raise AgentProviderProfileError(
                f"startup blocker {self.blocker_id!r} 'all_of' must be a tuple of "
                f"signature strings, got {type(self.all_of).__name__}"
            )
        count = len(self.all_of)
        if count < MIN_STARTUP_SIGNATURES or count > MAX_STARTUP_SIGNATURES:
            raise AgentProviderProfileError(
                f"startup blocker {self.blocker_id!r} must declare between "
                f"{MIN_STARTUP_SIGNATURES} and {MAX_STARTUP_SIGNATURES} 'all_of' "
                f"signatures, got {count}: one generic phrase would false-positive a "
                f"ready composer, so a blocker is pinned by co-located signatures from "
                f"the same screen (Redmine #13760 j#77947)"
            )
        seen: list[str] = []
        for signature in self.all_of:
            if not isinstance(signature, str) or not signature.strip():
                raise AgentProviderProfileError(
                    f"startup blocker {self.blocker_id!r} 'all_of' entries must be "
                    f"non-empty strings; got {signature!r}. A blank / non-string signature "
                    f"folds to '' and would match every screen, collapsing the AND"
                )
            if signature in seen:
                raise AgentProviderProfileError(
                    f"startup blocker {self.blocker_id!r} lists signature {signature!r} "
                    f"more than once; a duplicate does not strengthen the AND"
                )
            seen.append(signature)
            if len(signature) > MAX_STARTUP_SIGNATURE_LEN:
                raise AgentProviderProfileError(
                    f"startup blocker {self.blocker_id!r} signature is {len(signature)} "
                    f"chars; the bound is {MAX_STARTUP_SIGNATURE_LEN} (a long signature is "
                    f"one re-word away from silently never matching — a fail-open drift)"
                )
            folded = fold_startup_text(signature)
            if len(folded) < MIN_STARTUP_SIGNATURE_FOLDED_LEN:
                raise AgentProviderProfileError(
                    f"startup blocker {self.blocker_id!r} signature {signature!r} folds to "
                    f"{len(folded)} alphanumeric char(s); at least "
                    f"{MIN_STARTUP_SIGNATURE_FOLDED_LEN} are required so a punctuation-only "
                    f"/ near-empty signature cannot match every screen"
                )

    def matches(self, content: object) -> bool:
        """True iff every signature appears in ``content`` (pure; never raises).

        ``__post_init__`` guarantees every ``all_of`` entry folds to a non-empty key, so
        this AND can no longer collapse to a single-signature match on a blank element.
        """
        folded = fold_startup_text(content)
        if not folded:
            return False
        return all(fold_startup_text(sig) in folded for sig in self.all_of)

    @classmethod
    def from_record(cls, record: object, *, provider_id: str) -> "StartupBlocker":
        """Validate one ``startup_blockers`` entry's STRUCTURE, then delegate to the type.

        Only the mapping/key structure is checked here (an artifact-shaped concern);
        every value invariant — id, signature strings, count, length, folded length,
        duplicates, forbidden tokens — is enforced by :meth:`__post_init__`, so a directly
        constructed object cannot be weaker than a loaded one (review j#78481 finding 1).
        """
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (  # noqa: E501
            AgentProviderProfileError,
        )

        if not isinstance(record, Mapping):
            raise AgentProviderProfileError(
                f"agent provider profile {provider_id!r} 'startup_blockers' entries must "
                f"be mappings, got {type(record).__name__}"
            )
        unknown = set(record) - _STARTUP_BLOCKER_KEYS
        if unknown:
            raise AgentProviderProfileError(
                f"unknown 'startup_blockers' key(s) {sorted(map(repr, unknown))} in agent "
                f"provider profile {provider_id!r}; allowed: "
                f"{sorted(_STARTUP_BLOCKER_KEYS)}. A blocker describes a screen; it never "
                f"carries an action, a key sequence, or an authority to dismiss it."
            )
        missing = _STARTUP_BLOCKER_KEYS - set(record)
        if missing:
            raise AgentProviderProfileError(
                f"agent provider profile {provider_id!r} startup blocker is missing "
                f"{sorted(missing)}"
            )
        raw_all_of = record["all_of"]
        if isinstance(raw_all_of, (str, bytes)) or not isinstance(raw_all_of, Sequence):
            raise AgentProviderProfileError(
                f"agent provider profile {provider_id!r} startup blocker 'all_of' must be "
                f"a list of signature strings, got {type(raw_all_of).__name__}"
            )
        try:
            return cls(blocker_id=record["id"], all_of=tuple(raw_all_of))
        except AgentProviderProfileError as exc:
            # Re-frame the type-level message with the artifact provider context so a
            # malformed packaged profile still names which provider failed to load.
            raise AgentProviderProfileError(
                f"agent provider profile {provider_id!r}: {exc}"
            ) from exc


__all__ = (
    "MAX_STARTUP_BLOCKERS",
    "MAX_STARTUP_SIGNATURES",
    "MAX_STARTUP_SIGNATURE_LEN",
    "MIN_STARTUP_SIGNATURES",
    "MIN_STARTUP_SIGNATURE_FOLDED_LEN",
    "StartupBlocker",
    "fold_startup_text",
)
