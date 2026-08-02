"""Refspec safety and push-outcome classification for the #13686 live adapter.

Split out of :mod:`...application.auto_integration_live_ops` when review j#96516's arbitration
asked that the typed push vocabulary and its durable record not be paid for by growing that
module further — it was two lines from the health threshold. These three are a coherent unit
rather than an arbitrary slice: **what a ref name is allowed to be, and what git's answer about
a push means.** Both are questions about *strings the process boundary carries*, and neither
needs the adapter's repository, remote or sandbox.

Every name here is internal to the subsystem. Nothing is re-exported: ``_UnsafeRefspecError``
in particular is a signal that must not leave the adapter (j#96516 finding 2 — R21 documented
it as internal while ``__all__`` published it).
"""
from __future__ import annotations

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (
    PUSH_OPERATIONAL_ERROR,
    PUSH_REMOTE_MOVED,
    PUSH_REMOTE_REFUSED,
)


class _UnsafeRefspecError(ValueError):
    """A ref name could not be turned into a provably non-force refspec.

    A branch name carrying ``+``, whitespace, or a leading ``-`` would change what the
    constructed ``git push`` argv *means* — ``+`` spells a force inside a refspec, and a
    leading ``-`` turns the value into an option — so the argv is never built at all.

    **This exception does not leave the adapter.** All three call sites catch it and answer in
    that operation's own fail-closed vocabulary: a read returns ``""`` / ``False``, and both
    the merge and the push return their vocabulary's ``invalid_input`` — the same status the
    push already gives an unusable source head. Three rounds went into defending an escape out
    of one of them (j#96461 finding 2, j#96492 finding 4, j#96499 finding 1); an unusable
    input is a refusal, and this adapter has one way of saying so.
    """


def _checked_branch(ref: str) -> str:
    """Return ``ref`` as a bare branch name, or raise :class:`_UnsafeRefspecError`.

    Accepts either ``<branch>`` or ``refs/heads/<branch>`` and normalizes to the bare name, so
    that one spelling choice — how the caller qualifies the ref — does not decide whether the
    refspec is safe. That is the ONLY normalization performed here; everything else about the
    name is judged as written.
    """
    # No `.strip()`. R18 trimmed first and checked afterwards, so `'ma in'` was refused while
    # `' main '` and `'main\n'` were silently rewritten to `main` — the same character
    # accepted or rejected depending on where in the name it sat (j#96461 finding 2). This
    # function's job is to answer whether the ref AS SPELLED can be handed to git, so a
    # spelling it would have to be repaired to be usable is not one it can vouch for. Trimming
    # a configured value is a separate, deliberate step that happens once, upstream, in
    # `normalized_branch` when the action record is formed.
    candidate = ref or ""
    if candidate.startswith("refs/heads/"):
        candidate = candidate[len("refs/heads/") :]
    if not candidate:
        raise _UnsafeRefspecError("target ref is empty")
    if candidate.startswith("-"):
        raise _UnsafeRefspecError(
            f"target ref {ref!r} starts with '-' and would be read as an option"
        )
    # NUL and friends never reach git: `subprocess.run` raises `ValueError` before spawning,
    # which is not an `OSError` and so escaped `_run` entirely (j#96453 finding 2). A ref the
    # process boundary cannot carry is invalid input, not an exception.
    if any(character < " " or character == "\x7f" for character in candidate):
        raise _UnsafeRefspecError(
            f"target ref {ref!r} contains a control character that cannot be passed to a "
            "process; refusing to construct the command"
        )
    forbidden = set("+ \t\n:^~?*[\\")
    if any(character in forbidden for character in candidate):
        raise _UnsafeRefspecError(
            f"target ref {ref!r} contains a character that would change the refspec's "
            "meaning ('+' spells a force); refusing to construct the push"
        )
    return candidate


def _push_status(porcelain: str, *, refspec: str) -> str:
    """Classify a failed push from ``git push --porcelain``'s line for ``refspec``.

    The format is ``<flag><TAB><src>:<dst><TAB><summary>``. Only the line naming OUR refspec
    is consulted — an ``--atomic`` push of one ref should produce exactly one, and a run that
    produced none for it (unreachable remote, a git that never spawned) has said nothing about
    the remote's ref and must not be reported as though it had.

    ``[rejected]`` and ``[remote rejected]`` are different strings for different things and
    are matched as whole tokens, not by substring: the second contains the first.
    """
    for line in porcelain.splitlines():
        fields = line.split("\t")
        if len(fields) < 3 or fields[1].strip() != refspec:
            continue
        summary = fields[2].strip()
        if summary.startswith("[remote rejected]"):
            return PUSH_REMOTE_REFUSED
        if summary.startswith("[rejected]"):
            return PUSH_REMOTE_MOVED
        # A flagged line we do not recognise says the push did not land, and nothing more.
        return PUSH_OPERATIONAL_ERROR
    return PUSH_OPERATIONAL_ERROR
