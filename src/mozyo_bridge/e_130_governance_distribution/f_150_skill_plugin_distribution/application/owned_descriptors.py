"""Descriptor ownership and teardown-channel discipline (Redmine #14580).

Split out of ``legacy_mirror_sync`` when that module crossed the module-health
threshold. These are the primitives the mirror sync's failure handling is built
on, and they are the part several review rounds kept finding defects in, so they
are worth reading as a unit:

- :class:`_OwnedDescriptor` — a descriptor closed at most once, with ownership
  released *before* the close syscall, because the close can itself unwind
  (j#90477 R11-F1 / j#90482 R12-F1).
- :func:`_teardown_during` — runs teardown actions independently during an
  unwind, keeping three outcome channels distinct: a returned failure, an
  ordinary exception, and a control-flow exception that outranks the primary
  (j#90482 R12-F2 / j#90487 R13-F1/F2/F3 / j#90492 R14-F1/F2).
- :func:`_attach_secondary` — records a teardown failure without ever becoming
  the reason the caller sees a different exception. Recording it is routed
  through :func:`_run_teardown_action` so that an interrupt arriving *while*
  recording cannot skip the teardown that has not run yet (R14-F1).

Keeping them here means the ordering and channel rules have one home rather than
being restated at each call site.
"""

from __future__ import annotations

import os

from ..domain.legacy_mirror_contract import Violation


def _close_quietly(fd: int) -> bool:
    """Close a descriptor, reporting failure rather than raising.

    A failing `close` is not a fact about the mirror, but letting it escape
    turned every caller — the CLI, `release check drift` — back into a
    traceback (j#90458 R8-F2). Callers that care fold the ``False`` into their
    typed result; the rest simply must not crash on teardown.
    """
    try:
        os.close(fd)
    except OSError:
        return False
    return True




def _describe_teardown_result(outcome: object) -> str | None:
    """Turn a teardown action's *returned* failure into a note, if it is one.

    Teardown actions report failure two ways, and only one of them is an
    exception: :meth:`_OwnedDescriptor.close` returns ``False`` for a close
    ``OSError``, and the staging release returns a non-empty violation tuple
    for a cleanup ``OSError``. Discarding those return values meant a typed
    cleanup failure vanished — the primary surfaced with no notes while
    residue stayed on disk (j#90487 R13-F2).
    """
    if outcome is False:
        return "close reported a failure"
    if isinstance(outcome, tuple) and outcome:
        return "; ".join(
            violation.message() if isinstance(violation, Violation) else str(violation)
            for violation in outcome
        )
    return None


def _record_secondary(primary: BaseException, secondary: BaseException) -> BaseException | None:
    """Attach ``secondary`` to ``primary``; return control-flow raised doing so.

    :func:`_attach_secondary` deliberately lets a control-flow exception out —
    an interrupt outranks a note (R13-F3) — but it must not leave the loop that
    called it, because the actions after this one would never run (j#90492
    R14-F1). Returning it puts it on the same channel as an interrupt from the
    action itself, so exactly one rule decides what happens next.
    """
    try:
        _attach_secondary(primary, secondary)
    except Exception:  # noqa: BLE001 - `_attach_secondary` already absorbs these
        return None
    except BaseException as interrupt:  # noqa: BLE001 - routed, not raised here
        return interrupt
    return None


def _record_returned_failure(primary: BaseException, outcome: object) -> BaseException | None:
    """Record a teardown action's *returned* failure, if it reported one."""
    try:
        note = _describe_teardown_result(outcome)
    except Exception:  # noqa: BLE001 - describing a failure cannot itself fail loudly
        note = "a teardown result could not be described"
    except BaseException as interrupt:  # noqa: BLE001 - routed, not raised here
        return interrupt
    if note is None:
        return None
    return _record_secondary(primary, RuntimeError(note))


def _run_teardown_action(primary: BaseException, action) -> BaseException | None:
    """Run one action, record what it reports, and never raise.

    Every way this can go wrong — the action raising, the action *returning* a
    failure, and the recording of either — funnels into one return value: a
    control-flow exception, or ``None``. That is what keeps
    :func:`_teardown_during`'s loop intact no matter which step fails.
    """
    try:
        outcome = action()
    except Exception as failure:  # noqa: BLE001 - recorded, not raised
        return _record_secondary(primary, failure)
    except BaseException as interrupt:  # noqa: BLE001 - routed, not raised here
        return interrupt
    else:
        return _record_returned_failure(primary, outcome)


def _teardown_during(primary: BaseException, *actions) -> BaseException | None:
    """Run each teardown action independently, preserving ``primary``.

    Chaining them meant one failure skipped the rest (j#90482 R12-F2), so each
    runs on its own. Three outcome channels are kept distinct, because
    collapsing them is what several rounds kept finding:

    - a **returned** failure (``False`` / violation tuple) is recorded as a
      note; it is not an exception and must not be dropped (R13-F2);
    - an ordinary :class:`Exception` is recorded as a note; the caller's
      exception stays the one that actually unwound the operation;
    - a **control-flow** ``BaseException`` (``KeyboardInterrupt``,
      ``SystemExit``, ``GeneratorExit``) outranks the primary. Demoting one to
      a note swallowed an interrupt the descriptor helper explicitly promises
      never to swallow (R13-F3). The first such exception is returned for the
      caller to raise instead of ``primary``.

    Two properties this loop guarantees, both of which it once failed to:

    - **The remaining actions always run.** Not just when the action itself
      fails: an interrupt arriving while a secondary was being *recorded* used
      to escape the loop, so a staging release never ran and residue stayed on
      disk (j#90492 R14-F1). Recording is now on the same channel as the
      action, via :func:`_run_teardown_action`.
    - **No teardown failure is dropped.** Later control-flow exceptions used to
      vanish once one was held (j#90492 R14-F2); they are recorded on
      ``primary``, which is the single ledger of everything that went wrong
      during teardown and stays reachable as the ``__context__`` of whatever
      the caller raises.

    The one bounded exception: if recording a *later* control-flow exception is
    itself interrupted, that innermost interrupt is dropped rather than chased,
    since the regress has no natural end. The remaining actions still run.
    """
    control_flow: BaseException | None = None
    for action in actions:
        arrived = _run_teardown_action(primary, action)
        if arrived is None:
            continue
        if control_flow is None:
            control_flow = arrived
        else:
            _record_secondary(primary, arrived)
    return control_flow


class _OwnedDescriptor:
    """A descriptor this run owns, closed at most once.

    Ownership is released **before** the close syscall runs.
    :func:`_close_quietly` deliberately re-raises anything that is not an
    ``OSError`` so an interrupt is never swallowed, which means the close can
    unwind — and if the sentinel were set afterwards, a later ``finally`` would
    close the same descriptor *number* again. Under number reuse that closed an
    unrelated descriptor: a measured run closed a `/dev/null` handle that had
    just been assigned the freed number (j#90477 R11-F1).

    Both the staging descriptor and the verification descriptor go through this
    one structure, so the ordering cannot be right in one place and wrong in
    the other.
    """

    __slots__ = ("_fd",)

    def __init__(self, fd: int) -> None:
        self._fd = fd

    @property
    def held(self) -> bool:
        return self._fd != -1

    @property
    def fileno(self) -> int:
        return self._fd

    def detach(self) -> int:
        """Give the descriptor to someone else; this object stops owning it."""
        fd = self._fd
        self._fd = -1
        return fd

    def close(self) -> bool:
        """Close once. ``False`` when the close reported an ``OSError``."""
        fd = self._fd
        if fd == -1:
            return True
        self._fd = -1  # detach first: a raising close must not close it twice
        return _close_quietly(fd)


def _attach_secondary(primary: BaseException, secondary: BaseException) -> None:
    """Keep a teardown failure visible without replacing the primary one.

    The exception the caller sees must stay the one that actually unwound the
    write; a close or cleanup failure is additional information, not a
    replacement (j#90477 R11-F1 condition 3).
    """
    try:
        note = f"{type(secondary).__name__}: {secondary}"
        add_note = getattr(primary, "add_note", None)
        if add_note is not None:
            add_note(f"secondary failure during teardown: {note}")
        elif primary.__context__ is None:
            primary.__context__ = secondary
    except Exception:  # noqa: BLE001 - best effort by design
        # Recording a secondary failure must never become the reason the caller
        # sees a different exception — a raising `add_note` replaced the primary
        # and skipped the release entirely (j#90482 R12-F2). The region is a
        # format plus an attribute set, so nothing meaningful is hidden.
        #
        # Control-flow exceptions are deliberately NOT caught: an interrupt
        # arriving here outranks the note (j#90487 R13-F3).
        pass
