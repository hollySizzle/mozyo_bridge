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

The authority split is the root cause, and it is decidable **before any side effect** —
but only from the right two facts: where the managed launch points, and where the
provider's own updater actually *writes*. This module is the pure, total classifier for
that question, plus the second axis the re-launch needs: whether the exact executable
identity a lane was bound to is still the one that is there.

**Review j#95741 F2 correction.** The first cut answered the second question with the
distinct realpaths the provider's *command* resolved to on the trusted PATH. That is a
proxy, and an unsound one: an update runs the package manager, which writes to *its*
global prefix, and where the binary sits on PATH is an independently determined fact. A
host with one matching ``codex`` on PATH but a PATH ``npm`` owning a different prefix was
classified ``aligned`` — and before the update the second install does not exist, so no
enumeration of the provider command could have seen it. The proxy is gone: only a
positively resolved updater write target is accepted, and its absence is ``unknown``.

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

import os
from dataclasses import dataclass
from typing import Sequence

# --- Axis 1: does the managed exec target agree with the updater's reach? -------------

#: The caller did not supply authority facts. The gate is not armed — byte-invariant
#: with the pre-#14741 behavior. Never a claim that the authority is sound.
AUTHORITY_NOT_EVALUATED = "not_evaluated"
#: The managed exec target lies inside the single install root the provider's own updater
#: was positively resolved to write to: an update reaches what the managed launch runs.
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

    ``provider`` is the profile id. ``updater_targets`` is the number of DISTINCT install
    roots the provider's own updater was **positively resolved** to write to — a small
    count, not a path list, so the record stays safe for a durable journal. Zero is the
    honest common case: nothing established where the updater writes. Two or more means
    the updater's own target is itself ambiguous, which is a split on its face.

    (It was ``reachable_installs`` in the first cut, when it counted provider-command PATH
    resolutions. That input was the j#95741 F2 proxy and is gone; the name went with it,
    because a count of the wrong thing is worse than no count at all.)

    There is deliberately no field carrying a path, a version string, or an env value.
    """

    provider: str
    authority: str = AUTHORITY_NOT_EVALUATED
    binding: str = BINDING_NOT_EVALUATED
    updater_targets: int = 0

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
        if not isinstance(self.updater_targets, int) or isinstance(
            self.updater_targets, bool
        ):
            raise UpdateAuthorityError(
                f"updater_targets must be an int, got "
                f"{type(self.updater_targets).__name__}"
            )
        if self.updater_targets < 0:
            raise UpdateAuthorityError(
                f"updater_targets cannot be negative, got {self.updater_targets}"
            )

    @property
    def proven_wrong_binary(self) -> bool:
        """True only when a POSITIVE finding says this lane runs the wrong binary.

        ``split`` (the updater writes somewhere else) and ``drifted`` (the executable is
        not the one this lane bound) are both positive, demonstrated findings.
        ``unknown`` is deliberately NOT one: it says the question is undecided.

        This is the predicate the **pre-send** fence uses, and the split from
        :attr:`admits_relaunch` is the #14741 j#95741 F1/F2 resolution. Once the F2 proxy
        was removed, ``unknown`` became the common and honest verdict for any host whose
        package-manager prefix nobody has positively resolved. Treating ``unknown`` as a
        send refusal would have taken the entire workspace offline to guard against a
        possibility, which is not fail-closed — it is a different outage. Acceptance 3
        asks for zero-send on "drift/split", and that is exactly what this predicate is.
        """
        return self.authority == AUTHORITY_SPLIT or self.binding == BINDING_DRIFTED

    @property
    def admits_relaunch(self) -> bool:
        """True only when neither axis withholds admission — the STRICT predicate.

        An un-evaluated axis admits (the caller did not arm it); an aligned / matched
        axis admits (it was armed and passed). ``split`` / ``drifted`` / ``unknown``
        never admit — including ``unknown``, because a re-launch is precisely the moment
        the #14741 loop re-armed itself, and re-starting a binary whose authority nobody
        could establish is how that loop stayed invisible. A send to a live pane is a
        weaker action than resurrecting a dead one, which is why the pre-send fence uses
        :attr:`proven_wrong_binary` instead.
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
            "updater_targets": self.updater_targets,
        }


#: Fixed operator sentences, keyed by token so the text can never disagree with the
#: verdict it explains and no observed value can leak into one.
AUTHORITY_DETAIL: dict[str, str] = {
    AUTHORITY_NOT_EVALUATED: (
        "update authority was not evaluated for this action; this is not a claim that "
        "the managed executable and the provider's updater agree"
    ),
    AUTHORITY_ALIGNED: (
        "the managed exec target lies inside the single install root the provider's own "
        "updater was positively resolved to write to, so an update reaches the install "
        "this lane runs"
    ),
    AUTHORITY_SPLIT: (
        "the managed exec target and the provider's own updater target are different "
        "installs: updating the provider cannot change what this lane runs, and an "
        "update that exits 0 is not evidence the lane was fixed. Re-point the trusted "
        "override at the install the updater owns, or remove the extra install — mozyo "
        "never relaxes to PATH first-match and never accepts an update prompt for you"
    ),
    AUTHORITY_UNKNOWN: (
        "the managed exec target, or the install root the provider's own updater writes "
        "to, could not be positively established, so an authority split can be neither "
        "shown nor ruled out. Establishing an updater's target means asking its package "
        "manager; mozyo does not infer it from where the provider's binary sits on PATH, "
        "because those are independently determined facts. An undecidable authority is "
        "never admitted as an aligned one"
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


def _within(exec_target: str, root: str) -> bool:
    """True iff ``exec_target`` is the root itself or a path underneath it.

    Separator-anchored on purpose: a plain ``startswith`` would read ``/opt/nodes/x`` as
    living under ``/opt/node``, which would turn a genuine split into a false alignment —
    the one direction this classifier must never fail in.
    """
    if exec_target == root:
        return True
    prefix = root if root.endswith(os.sep) else root + os.sep
    return exec_target.startswith(prefix)


def classify_update_authority(
    *,
    exec_target: str,
    updater_write_roots: Sequence[str],
    updater_roots_readable: bool,
) -> str:
    """Classify the managed-exec vs updater-write authority (pure, total, fail-closed).

    ``exec_target`` is the verified realpath the managed launch runs.
    ``updater_write_roots`` are the **positively resolved install roots the provider's
    own updater would write to** — the package manager's / installer's own target.
    ``updater_roots_readable`` is False when that target could not be established.

    **Redmine #14741 review j#95741 F2 — why this input is what it is.** The first cut
    passed the distinct realpaths the provider's *command* resolves to on the trusted
    PATH, and treated a single match against ``exec_target`` as alignment. That is a
    **proxy, not the fact**: an update runs ``npm install -g <package>`` (or its pnpm /
    bun / brew / installer equivalent), which writes to the *package manager's* global
    prefix. Where the provider's binary happens to sit on PATH and where its package
    manager writes are two independently determined facts. A host with exactly one
    ``codex`` on PATH matching the override, but a PATH ``npm`` owning a different global
    prefix, returned ``aligned`` — and the second install does not even exist yet before
    the update, so no enumeration of the provider command could ever have seen it. The
    proxy therefore promoted an unverified authority to a positive alignment, which is
    the same class of defect this issue exists to close. Only a positively resolved
    updater write target is accepted now; nothing is inferred from the provider command.

    Precedence, and why:

    1. an unresolved updater target decides nothing -> :data:`AUTHORITY_UNKNOWN`. This is
       the common case, and it is the honest one: establishing where a package manager
       writes requires asking that package manager, which this module does not do;
    2. a missing / blank ``exec_target`` likewise -> :data:`AUTHORITY_UNKNOWN`;
    3. **no** write root -> :data:`AUTHORITY_UNKNOWN`, NOT "aligned". "The updater has
       nowhere to write" is a guess, not an observation;
    4. more than one distinct write root -> :data:`AUTHORITY_SPLIT`: the updater's own
       target is ambiguous, so at most one of them can be the managed one;
    5. exactly one, and the managed exec target is NOT inside it ->
       :data:`AUTHORITY_SPLIT`. This is the measured #14741 shape;
    6. exactly one, and the managed exec target IS inside it -> :data:`AUTHORITY_ALIGNED`.

    Both sides are expected to be :func:`os.path.realpath`-resolved by the caller, so a
    symlinked alias and its target compare equal rather than reading as a false split.
    """
    if not updater_roots_readable:
        return AUTHORITY_UNKNOWN
    if not isinstance(exec_target, str) or not exec_target.strip():
        return AUTHORITY_UNKNOWN
    distinct: list[str] = []
    for candidate in updater_write_roots:
        if isinstance(candidate, str) and candidate.strip() and candidate not in distinct:
            distinct.append(candidate)
    if not distinct:
        return AUTHORITY_UNKNOWN
    if len(distinct) > 1:
        return AUTHORITY_SPLIT
    return AUTHORITY_ALIGNED if _within(exec_target, distinct[0]) else AUTHORITY_SPLIT


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
