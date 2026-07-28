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




_LEDGER_KEY = object()
"""Key for the ledger inside a primary's instance dictionary.

An identity no caller can name. A string key — however obscure — is still in
the attribute namespace: ``setattr(exc, key, value)`` and ``getattr`` both work
on it whatever it is spelled like, so a caller's binding could be replaced
(j#90517 R17-F1). An identity key removes the collision rather than making it
unlikely.

The cost, stated because it is real: an instance dictionary is restored by
attribute name, so an exception carrying a ledger raises ``TypeError`` on
``pickle.loads``. ``pickle.dumps`` gets that far only when the retained entries
are themselves picklable — a failure object whose ``__reduce__`` raises fails
the dump, since the ledger holds the objects rather than a rendering of them
(j#90529 R18-F2). A string key was chosen once to keep the exception
unpicklable-free and it was the wrong trade: the ledger has never survived a
pickle anyway, and not overwriting a caller's binding is the requirement.
"""

_ABSENT = object()
"""Distinguishes "no ledger yet" from "something else is bound at the key"."""

_instance_state = BaseException.__dict__["__dict__"].__get__
"""Reach a primary's real instance dictionary, ignoring its class.

``object.__getattribute__(exc, "__dict__")`` still runs a ``__dict__`` data
descriptor defined by a subclass. Bypassing ``__setattr__`` is not the same as
bypassing the type: a subclass whose ``__dict__`` property raised lost the
retention outright, and one that raised ``KeyboardInterrupt`` escaped the rail
and skipped an action that had not run (j#90508 R16-F1). Binding the descriptor
that ``BaseException`` itself defines cannot dispatch to a subclass at all.
"""


class _Ledger:
    """The container the implementation owns, and the only one it will trust.

    A module-private type, checked by exact identity, so nothing a caller can
    construct is ever adopted as the record — and so iterating and appending
    are always plain built-in ``list`` operations.
    """

    __slots__ = ("entries",)

    def __init__(self) -> None:
        self.entries: list[object] = []


def teardown_failures(primary: BaseException) -> tuple[object, ...]:
    """Every failure seen while tearing down ``primary``, as objects.

    This is the machine-readable half of the record. Notes are for humans and
    are best effort; this is what a caller — or a test — reads to find out that
    a cleanup returned ``CLEANUP_FAILED`` and residue is on disk.

    A read, and only a read. Routing it through the creating path meant asking
    an exception what went wrong *wrote to that exception* — replacing whatever
    was bound at the carrier key even when nothing had failed at all (j#90517
    R17-F1).
    """
    ledger = _existing_ledger(primary)
    if ledger is None:
        return ()
    return tuple(entry.failure for entry in ledger.entries)


def _existing_ledger(primary: BaseException) -> _Ledger | None:
    """The ledger already on ``primary``, or ``None``. Never writes."""
    state = _instance_state(primary)
    if type(state) is not dict:
        return None
    ledger = state.get(_LEDGER_KEY)
    return ledger if type(ledger) is _Ledger else None


def _ledger(primary: BaseException) -> _Ledger | None:
    """The ledger every teardown failure is appended to, created on first use.

    Appending to a list cannot fail, and that is the entire point: the failure
    has to be *retained* before anything fallible runs on it. Making
    ``add_note`` the ledger meant that an interrupt during recording lost the
    failure being recorded, not just the interrupt — a cleanup that reported
    ``CLEANUP_FAILED`` left residue on disk and nothing reachable said so
    (j#90503 R15-F1) — and a secondary whose ``__str__`` raised vanished
    outright (R15-F2).

    Four earlier carriers were each written, measured, and discarded, which is
    why this one is so deliberate: ``setattr`` and ``__context__`` assignment
    both route through a type's ``__setattr__`` (j#90503),
    ``object.__getattribute__`` still dispatches to a subclass ``__dict__``
    descriptor (j#90508 R16-F1), and an obscure string key is still an
    attribute name a caller can bind (j#90517 R17-F1). What is left — a
    descriptor bound from ``BaseException``, an identity key, and a private
    container type — is reachable only through this module.

    Returns ``None`` rather than writing when there is no instance dictionary,
    or when something that is not ours already occupies the key: refusing to
    retain is always better than overwriting a binding that belongs to someone
    else.
    """
    state = _instance_state(primary)
    if type(state) is not dict:
        return None
    ledger = state.get(_LEDGER_KEY, _ABSENT)
    if type(ledger) is _Ledger:
        return ledger
    if ledger is not _ABSENT:
        return None
    ledger = _Ledger()
    state[_LEDGER_KEY] = ledger
    return ledger


class _Occurrence:
    """One arrival of one failure, distinct from every other arrival.

    The ledger holds these rather than the failures themselves, because
    "already recorded" has to be answerable independently of the failure
    object: two actions may report the same object — ``False`` is a singleton —
    and those are two occurrences (j#90517 R17-F2), while a retry of *this*
    occurrence is not a second one (j#90561 R19-F1).
    """

    __slots__ = ("failure",)

    def __init__(self, failure: object) -> None:
        self.failure = failure


_RETENTION_ATTEMPTS = 4
"""How many times one retention retries a carrier that keeps interrupting.

Bounded so a carrier that never recovers still terminates; the queue survives
the call anyway and later retentions try again.
"""


class _Retention:
    """One unwind's retention, with the occurrences the carrier has not taken.

    ``_remember`` used to return the control flow it hit and nothing else, so
    the caller could not tell an append that *happened* from one that did not.
    A carrier that interrupted once and then recovered lost both the failure it
    was recording and the interrupt itself — the notes still showed the failure,
    but the ledger was empty (j#90529 R18-F1). Notes are a rendering; losing the
    objects is losing the record.

    The fix for that kept a queue and popped an entry once its append returned.
    "After the append returned" is an ordering, not an acknowledgement: control
    flow arrives at bytecode boundaries, so an interrupt between the append and
    the pop left the occurrence queued *and* recorded — a retry duplicated it —
    and the pop itself sat outside the guard, so an interrupt there escaped the
    rail and skipped a cleanup that had not run (j#90561 R19-F1).

    There is no commit step now. Nothing is ever removed from the queue; the
    ledger *is* the record of what has been taken, and the next pass simply
    skips occurrences already in it. Every instruction that touches either lives
    inside the guard, so an interrupt anywhere leaves a state the next pass
    reads correctly.

    If the carrier never recovers the record is unreachable — the same boundary
    as refusing to overwrite a foreign binding (j#90517 R17-F1), and stated for
    the same reason: this is not a case this code can create.
    """

    __slots__ = ("primary", "_queued")

    def __init__(self, primary: BaseException) -> None:
        self.primary = primary
        self._queued: list[_Occurrence] = []

    def remember(self, failure: object) -> BaseException | None:
        """Retain one occurrence, and anything still queued before it."""
        return self._retain(_Occurrence(failure))

    def flush(self) -> BaseException | None:
        """Retain whatever earlier attempts could not."""
        return self._retain(None)

    def _retain(self, arriving: _Occurrence | None) -> BaseException | None:
        """Queue ``arriving`` if given, then take everything the carrier will.

        Returns the first control flow raised while trying, rather than raising
        it: the carrier itself broke the "remaining actions always run" rule by
        unwinding out of the loop (j#90508 R16-F1/F2, j#90561 R19-F1). Each
        interrupt is an occurrence in its own right, so it joins the queue and
        the loop tries again — bounded, because a carrier that keeps
        interrupting must not spin.

        Arrivals wait in a list, not a slot. Holding the incoming occurrence in
        a single local made the queue's *entrance* the lossy part of an
        otherwise lossless machine: an ordinary exception dropped it, an
        interrupt overwrote it with the interrupt's own occurrence, and the last
        interrupt of an exhausted retry was never queued at all — even when the
        carrier recovered on the very next call (j#90620 R20-F1). Admission is
        idempotent, so re-admitting the whole list costs nothing and an arrival
        can only be added, never replaced.
        """
        unadmitted: list[_Occurrence] = [] if arriving is None else [arriving]
        first: BaseException | None = None
        for _ in range(_RETENTION_ATTEMPTS):
            try:
                self._admit(unadmitted)
                self._drain()
                return first
            except Exception:  # noqa: BLE001 - nothing to route; admitted below
                break
            except BaseException as interrupt:  # noqa: BLE001 - routed, not raised
                if first is None:
                    first = interrupt
                unadmitted.append(_Occurrence(interrupt))
        self._admit_before_leaving(unadmitted)
        return first

    def _admit_before_leaving(self, unadmitted: list[_Occurrence]) -> None:
        """Get the leftovers into the queue, so a later retention can see them.

        Whatever is still unadmitted when this call gives up would otherwise
        exist only in a local that is about to go out of scope. Bounded, and
        the last unavoidable point: a signal can land on the instruction that
        appends to the queue, and Python has no way to make that atomic. The
        window is one instruction wide, retried, and no wider than this.
        """
        for _ in range(_RETENTION_ATTEMPTS):
            try:
                self._admit(unadmitted)
                return
            except Exception:  # noqa: BLE001 - the queue is a plain list; retry
                continue
            except BaseException:  # noqa: BLE001 - priority is already decided
                continue

    def _admit(self, unadmitted: list[_Occurrence]) -> None:
        """Queue every arrival that is not queued yet."""
        for occurrence in unadmitted:
            self._enqueue(occurrence)

    def _enqueue(self, occurrence: _Occurrence) -> None:
        """Queue an occurrence unless it is already there. Idempotent."""
        for queued in self._queued:
            if queued is occurrence:
                return
        self._queued.append(occurrence)

    def _drain(self) -> None:
        """Append every queued occurrence the ledger does not already hold."""
        while True:
            ledger = _ledger(self.primary)
            if ledger is None:
                return  # no carrier; the queue waits for a later attempt
            occurrence = self._unrecorded(ledger)
            if occurrence is None:
                return
            ledger.entries.append(occurrence)

    def _unrecorded(self, ledger: _Ledger) -> _Occurrence | None:
        """The first queued occurrence the ledger has not taken, in order."""
        for occurrence in self._queued:
            for entry in ledger.entries:
                if entry is occurrence:
                    break
            else:
                return occurrence
        return None


def _is_reported_failure(outcome: object) -> bool:
    """Whether a teardown action *returned* a failure rather than raising one.

    Teardown actions report failure two ways, and only one of them is an
    exception: :meth:`_OwnedDescriptor.close` returns ``False`` for a close
    ``OSError``, and the staging release returns a non-empty violation tuple
    for a cleanup ``OSError``. Discarding those return values meant a typed
    cleanup failure vanished — the primary surfaced with no notes while
    residue stayed on disk (j#90487 R13-F2). This is a pure type test, so
    classifying an outcome cannot itself lose it.
    """
    return outcome is False or (isinstance(outcome, tuple) and bool(outcome))


def _describe_failure(failure: object) -> str:
    """Name a failure for a human, without trusting its ``__str__``.

    A secondary whose ``__str__`` raised used to disappear entirely (j#90503
    R15-F2). Retention no longer depends on this function at all, but a note
    that degrades to a type name is still better than no note.
    """
    if failure is False:
        return "close reported a failure"
    if isinstance(failure, tuple):
        parts = []
        for violation in failure:
            try:
                parts.append(
                    violation.message() if isinstance(violation, Violation) else str(violation)
                )
            except Exception:  # noqa: BLE001 - degrade to identity
                parts.append(f"<unprintable {type(violation).__name__}>")
        return "; ".join(parts) or "a teardown result could not be described"
    name = type(failure).__name__
    try:
        return f"{name}: {failure}"
    except Exception:  # noqa: BLE001 - degrade to identity
        return f"{name}: <unprintable>"


def _present(retention: _Retention, failure: object) -> BaseException | None:
    """Render an already-retained failure as a note; never raise.

    :func:`_attach_secondary` deliberately lets a control-flow exception out —
    an interrupt outranks a note (R13-F3) — but it must not leave the loop that
    called it, because the actions after this one would never run (j#90492
    R14-F1). That interrupt is a teardown failure in its own right, so this is
    where its single retention happens.
    """
    try:
        _attach_secondary(retention.primary, failure)
    except Exception:  # noqa: BLE001 - `_attach_secondary` already absorbs these
        return None
    except BaseException as interrupt:  # noqa: BLE001 - routed, not raised here
        retention.remember(interrupt)
        return interrupt
    return None


def _record_secondary(retention: _Retention, secondary: object) -> BaseException | None:
    """Retain ``secondary``, then present it; return control-flow raised doing so.

    The order is the fix: retention first and unconditionally, presentation
    second and best effort. Every occurrence passing through here is retained
    exactly once, which is what lets :class:`_Retention` drop de-duplication
    (j#90517 R17-F2).
    """
    retaining = retention.remember(secondary)
    presenting = _present(retention, secondary)
    return retaining if retaining is not None else presenting


def _run_teardown_action(retention: _Retention, action) -> BaseException | None:
    """Run one action, record what it reports, and never raise.

    Every way this can go wrong — the action raising, the action *returning* a
    failure, and the recording of either — funnels into one return value: a
    control-flow exception, or ``None``. That is what keeps
    :func:`_teardown_during`'s loop intact no matter which step fails.
    """
    try:
        outcome = action()
    except Exception as failure:  # noqa: BLE001 - recorded, not raised
        return _record_secondary(retention, failure)
    except BaseException as interrupt:  # noqa: BLE001 - routed, not raised here
        # Retained here rather than by the caller: whether it goes on to be
        # raised or merely noted, it belongs on the ledger. The action's own
        # interrupt keeps precedence over anything the retention hit.
        retention.remember(interrupt)
        return interrupt
    else:
        if _is_reported_failure(outcome):
            return _record_secondary(retention, outcome)
        return None


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
    - **No teardown failure is dropped.** Every one of them — raised, returned,
      or arriving while another was being recorded — is appended to
      :func:`_ledger` as an *object*, before anything fallible touches it.
      Notes are a rendering of that ledger, not the ledger itself. Making
      ``add_note`` the record meant an interrupt mid-recording lost the failure
      being recorded (j#90503 R15-F1), and a secondary whose ``__str__`` raised
      was swallowed as a best-effort no-op (R15-F2).

    Precedence and retention are separate questions, which is what R14-F2 got
    wrong in the other direction: the **first** control-flow exception is the
    one the caller raises, and every later one is still on the ledger.

    Retention is on the same channel as everything else. The carrier broke both
    properties twice by raising out of the loop instead (j#90508 R16-F1/F2), so
    :meth:`_Retention.flush` returns control flow rather than raising it, and no
    step of the record can cost an action that has not run. A carrier that
    refuses an occurrence does not lose it either: it stays queued and is
    retried, including once after the last action (j#90529 R18-F1).

    Each occurrence is retained at exactly one place — where it arises — so the
    ledger counts occurrences rather than distinct objects. Two actions that
    each return the same singleton ``False`` are two entries (j#90517 R17-F2).
    """
    retention = _Retention(primary)
    control_flow: BaseException | None = None
    for action in actions:
        arrived = _run_teardown_action(retention, action)
        if arrived is None:
            continue
        if control_flow is None:
            control_flow = arrived
        else:
            # Retained already, wherever it arose; this only adds the note. An
            # interrupt raised by the note itself is retained by `_present` —
            # only its *priority* is bounded, because the regress has no
            # natural end.
            _present(retention, arrived)
    # A carrier that recovered after refusing an occurrence still gets to keep
    # it, even if no later retention came along to carry the retry. The flush is
    # part of the retention channel, so its control flow goes on the same rail:
    # discarding it made an interrupt vanish outright — worse than the demotion
    # to a note that R13-F3 was about (j#90561 R19-F2).
    late = retention.flush()
    if control_flow is None:
        control_flow = late
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


def _attach_secondary(primary: BaseException, secondary: object) -> None:
    """Render a teardown failure into a note, without replacing the primary one.

    The exception the caller sees must stay the one that actually unwound the
    write; a close or cleanup failure is additional information, not a
    replacement (j#90477 R11-F1 condition 3).

    This is presentation only. The record itself is :func:`_ledger`, which was
    already written before this runs — so everything below may fail without
    losing anything (j#90503 R15-F1/F2).
    """
    try:
        note = _describe_failure(secondary)
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
