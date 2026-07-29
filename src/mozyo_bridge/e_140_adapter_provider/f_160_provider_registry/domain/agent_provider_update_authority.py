"""Managed-exec vs provider-updater authority split (Redmine #14741).

The defect this closes (#14741, live evidence #14725 j#94108). A managed lane pinned
its Codex exec target with the profile's trusted-env override, so
:func:`...agent_provider_executable.resolve_agent_launch` returned that exact realpath
and — because an override short-circuits the search — **never looked at the trusted
PATH at all**. The provider's own in-TUI updater does not share that pin: it shells out
to a package manager (``npm install -g @openai/codex``), which writes to whatever
install the ambient PATH's package manager owns. Those were two different installs. So:

- the managed launch kept starting the pinned (older) executable;
- the updater kept updating the *other* one and exiting 0;
- the lane read the clean exit as "the launch finished", self-healed by re-launching
  the same pinned older binary, and looped — while the Implementation Request the
  queue-enter rail had typed was consumed by the update prompt's default option.

The authority split is the root cause, and it is decidable **before any side effect**
from facts the launch resolver already computes: where the managed launch points, and
where the trusted PATH — the updater's reach — points. This module is the pure, total
classifier for that question, plus the second axis the re-launch needs: whether the
exact executable identity a lane was bound to is still the one that is there.

Design constraints this module deliberately honors
--------------------------------------------------
- **Fail closed, never guess.** Facts that cannot be established do not decay to
  "aligned". They become :data:`AUTHORITY_UNKNOWN`, which does not admit a launch.
  Absence of a split proof is not proof of alignment (the #13845 discipline).
- **Opt-in, byte-invariant when unused.** :data:`AUTHORITY_NOT_EVALUATED` is the
  default on every axis, exactly like the #14231 ``EVIDENCE_NOT_APPLICABLE`` tri-state.
  A caller that does not supply the facts gets the pre-#14741 behavior unchanged; this
  module never silently arms a gate on a call site that was not built for it.
- **No host paths, no versions, no env values on the outcome.** A verdict is put on a
  structured outcome and a pasteable durable record, so it carries fixed tokens and
  small counts only — the same invariant as ``StartupBlocker`` (#13760 j#77947
  invariant 3) and ``SlotHealth``. The paths are *compared* here; they never leave.
- **It describes, it never repairs.** A split is reported to the operator. This module
  does not relax to PATH first-match, does not rewrite the override, and does not run
  or accept an update — the #14741 guardrails.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

# --- Axis 1: does the managed exec target agree with the updater's reach? -------------

#: The caller did not supply authority facts. The gate is not armed — byte-invariant
#: with the pre-#14741 behavior. Never a claim that the authority is sound.
AUTHORITY_NOT_EVALUATED = "not_evaluated"
#: The managed exec target is exactly the executable the trusted PATH resolves — the
#: provider's own updater writes to the install the managed launch runs.
AUTHORITY_ALIGNED = "aligned"
#: The managed launch and the provider's updater target different installs. Updating
#: cannot change what the managed lane runs, so an update "success" is not a launch fix.
AUTHORITY_SPLIT = "split"
#: The facts needed to decide are missing or unusable. Fail closed: an undecidable
#: authority is never admitted as an aligned one.
AUTHORITY_UNKNOWN = "unknown"

UPDATE_AUTHORITIES: frozenset[str] = frozenset(
    {
        AUTHORITY_NOT_EVALUATED,
        AUTHORITY_ALIGNED,
        AUTHORITY_SPLIT,
        AUTHORITY_UNKNOWN,
    }
)

# --- Axis 2: is the exact executable identity a lane was bound to still there? --------
#
# Separate from axis 1 on purpose. A lane can be perfectly aligned (one install, one
# PATH entry) and still have been *re-written underneath it* by an update that ran
# between the bind and the re-launch. Collapsing the two would make "we updated it"
# indistinguishable from "we are pointed at the wrong one", which is precisely the
# confusion that made the #14741 loop unreadable.

#: No binding was supplied to re-verify. Not a claim that the binding holds.
BINDING_NOT_EVALUATED = "not_evaluated"
#: The observed executable identity is exactly the bound one.
BINDING_MATCHED = "matched"
#: The observed identity differs from the bound one: the file or its version changed
#: under the lane. A re-launch must re-bind explicitly, never inherit the old pin.
BINDING_DRIFTED = "drifted"
#: The identity could not be observed. Fail closed — unobserved is never unchanged.
BINDING_UNKNOWN = "unknown"

EXECUTABLE_BINDINGS: frozenset[str] = frozenset(
    {
        BINDING_NOT_EVALUATED,
        BINDING_MATCHED,
        BINDING_DRIFTED,
        BINDING_UNKNOWN,
    }
)


class UpdateAuthorityError(ValueError):
    """An update-authority record violates the closed contract (fail-closed)."""


@dataclass(frozen=True)
class UpdateAuthority:
    """One provider's two-axis update-authority verdict (validated on build).

    ``provider`` is the profile id. ``reachable_installs`` is the number of DISTINCT
    executables the trusted PATH resolves the provider command to — a small count, not
    a path list, so the record stays safe for a durable journal. Zero means the PATH
    resolved none (which is why an override was needed, and why the updater's reach
    cannot be described); two or more means the updater's own target is itself
    ambiguous, which is a split on its face.

    There is deliberately no field carrying a path, a version string, or an env value.
    """

    provider: str
    authority: str = AUTHORITY_NOT_EVALUATED
    binding: str = BINDING_NOT_EVALUATED
    reachable_installs: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise UpdateAuthorityError(
                f"update authority must name a provider, got {self.provider!r}"
            )
        if self.authority not in UPDATE_AUTHORITIES:
            raise UpdateAuthorityError(
                f"update authority {self.authority!r} is not recognised; "
                f"allowed: {sorted(UPDATE_AUTHORITIES)}"
            )
        if self.binding not in EXECUTABLE_BINDINGS:
            raise UpdateAuthorityError(
                f"executable binding {self.binding!r} is not recognised; "
                f"allowed: {sorted(EXECUTABLE_BINDINGS)}"
            )
        if not isinstance(self.reachable_installs, int) or isinstance(
            self.reachable_installs, bool
        ):
            raise UpdateAuthorityError(
                f"reachable_installs must be an int, got "
                f"{type(self.reachable_installs).__name__}"
            )
        if self.reachable_installs < 0:
            raise UpdateAuthorityError(
                f"reachable_installs cannot be negative, got {self.reachable_installs}"
            )

    @property
    def admits_launch(self) -> bool:
        """True only when neither axis withholds admission.

        An un-evaluated axis admits (the caller did not arm it); an aligned / matched
        axis admits (it was armed and passed). ``split`` / ``drifted`` / ``unknown``
        never admit — including ``unknown``, because the whole point is that an
        undecidable authority must not be spent as an aligned one.
        """
        return self.authority in (
            AUTHORITY_NOT_EVALUATED,
            AUTHORITY_ALIGNED,
        ) and self.binding in (BINDING_NOT_EVALUATED, BINDING_MATCHED)

    def as_payload(self) -> dict:
        return {
            "provider": self.provider,
            "authority": self.authority,
            "binding": self.binding,
            "reachable_installs": self.reachable_installs,
        }


#: Fixed operator sentences, keyed by token so the text can never disagree with the
#: verdict it explains and no observed value can leak into one.
AUTHORITY_DETAIL: dict[str, str] = {
    AUTHORITY_NOT_EVALUATED: (
        "update authority was not evaluated for this action; this is not a claim that "
        "the managed executable and the provider's updater agree"
    ),
    AUTHORITY_ALIGNED: (
        "the managed exec target is the executable the trusted PATH resolves, so the "
        "provider's own updater writes to the install the managed lane runs"
    ),
    AUTHORITY_SPLIT: (
        "the managed exec target and the provider's own updater target are different "
        "installs: updating the provider cannot change what this lane runs, and an "
        "update that exits 0 is not evidence the lane was fixed. Re-point the trusted "
        "override at the install the updater owns, or remove the extra install — mozyo "
        "never relaxes to PATH first-match and never accepts an update prompt for you"
    ),
    AUTHORITY_UNKNOWN: (
        "the managed exec target or the provider's updater reach could not be "
        "established, so an authority split can be neither shown nor ruled out; an "
        "undecidable authority is never admitted as an aligned one"
    ),
}

#: Fixed operator sentences for the executable-binding axis.
BINDING_DETAIL: dict[str, str] = {
    BINDING_NOT_EVALUATED: (
        "no executable binding was supplied to re-verify; this is not a claim that the "
        "executable is unchanged"
    ),
    BINDING_MATCHED: (
        "the observed executable identity is exactly the one this lane was bound to"
    ),
    BINDING_DRIFTED: (
        "the executable identity changed under this lane (the file or its version is "
        "not the bound one); a re-launch must re-bind explicitly rather than inherit a "
        "pin that no longer describes what is there"
    ),
    BINDING_UNKNOWN: (
        "the executable identity could not be observed, so the binding can be neither "
        "confirmed nor refuted; unobserved is never read as unchanged"
    ),
}


def classify_update_authority(
    *,
    exec_target: str,
    path_exec_targets: Sequence[str],
    path_readable: bool,
) -> str:
    """Classify the managed-exec vs updater-target authority (pure, total, fail-closed).

    ``exec_target`` is the verified realpath the managed launch runs.
    ``path_exec_targets`` are the DISTINCT verified realpaths the provider's command
    resolves to on the **trusted** PATH — the reach of the provider's own updater,
    which runs through a package manager on that PATH rather than through mozyo's pin.
    ``path_readable`` is False when the trusted PATH could not be enumerated at all (it
    was absent, or a component was empty / relative and the whole PATH was refused).

    Precedence, and why:

    1. an unreadable PATH decides nothing -> :data:`AUTHORITY_UNKNOWN`. It is exactly
       the state in which the updater's target is undescribable;
    2. a missing / blank ``exec_target`` likewise -> :data:`AUTHORITY_UNKNOWN`;
    3. **no** PATH resolution -> :data:`AUTHORITY_UNKNOWN`, NOT "aligned". A provider
       the trusted PATH cannot resolve is the shape an override exists to rescue, and
       "the updater has nowhere to write" is a guess, not an observation;
    4. more than one distinct PATH resolution -> :data:`AUTHORITY_SPLIT`. The updater's
       own target is ambiguous, so at most one of them can be the managed one;
    5. exactly one, and it is not the managed exec target -> :data:`AUTHORITY_SPLIT`.
       This is the measured #14741 shape;
    6. exactly one, and it IS the managed exec target -> :data:`AUTHORITY_ALIGNED`.

    Comparison is on the already-realpath-resolved strings the resolver produced: both
    sides come from :func:`os.path.realpath`, so a symlinked alias on one side and its
    target on the other compare equal rather than reading as a false split.
    """
    if not path_readable:
        return AUTHORITY_UNKNOWN
    if not isinstance(exec_target, str) or not exec_target.strip():
        return AUTHORITY_UNKNOWN
    distinct = []
    for candidate in path_exec_targets:
        if isinstance(candidate, str) and candidate.strip() and candidate not in distinct:
            distinct.append(candidate)
    if not distinct:
        return AUTHORITY_UNKNOWN
    if len(distinct) > 1:
        return AUTHORITY_SPLIT
    return AUTHORITY_ALIGNED if distinct[0] == exec_target else AUTHORITY_SPLIT


def classify_executable_binding(
    *,
    bound_identity: str,
    observed_identity: str,
) -> str:
    """Re-verify one lane's exact executable identity (pure, total, fail-closed).

    An *identity* is the caller's exact binding token — in practice the verified
    realpath joined with the version that realpath reported when the lane was bound. It
    is compared, never parsed and never emitted: this function returns a token, and the
    identities themselves stay with the caller.

    An empty ``bound_identity`` means nothing was bound, so there is nothing to
    re-verify -> :data:`BINDING_NOT_EVALUATED`. An empty ``observed_identity`` against a
    non-empty binding means the observation failed -> :data:`BINDING_UNKNOWN`, never
    "matched": an update that rewrote the executable is exactly the case in which the
    observation is the thing that breaks, so silence must not read as sameness.
    """
    bound = bound_identity.strip() if isinstance(bound_identity, str) else ""
    observed = observed_identity.strip() if isinstance(observed_identity, str) else ""
    if not bound:
        return BINDING_NOT_EVALUATED
    if not observed:
        return BINDING_UNKNOWN
    return BINDING_MATCHED if observed == bound else BINDING_DRIFTED


__all__ = (
    "AUTHORITY_ALIGNED",
    "AUTHORITY_DETAIL",
    "AUTHORITY_NOT_EVALUATED",
    "AUTHORITY_SPLIT",
    "AUTHORITY_UNKNOWN",
    "BINDING_DETAIL",
    "BINDING_DRIFTED",
    "BINDING_MATCHED",
    "BINDING_NOT_EVALUATED",
    "BINDING_UNKNOWN",
    "EXECUTABLE_BINDINGS",
    "UPDATE_AUTHORITIES",
    "UpdateAuthority",
    "UpdateAuthorityError",
    "classify_executable_binding",
    "classify_update_authority",
)
