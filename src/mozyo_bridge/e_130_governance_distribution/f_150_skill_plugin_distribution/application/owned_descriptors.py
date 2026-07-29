"""Descriptor ownership and teardown-channel discipline (Redmine #14580).

Split out of ``legacy_mirror_sync`` when that module crossed the module-health
threshold. These are the primitives the mirror sync's failure handling is built
on, and they are the part several review rounds kept finding defects in, so they
are worth reading as a unit:

- :class:`_OwnedDescriptor` — a descriptor closed at most once, with ownership
  released *before* the close syscall, because the close can itself unwind
  (j#90477 R11-F1 / j#90482 R12-F1).
- :class:`_StagingOwnership` — the one answer to "does this name still refer to
  the file this run created?", which is only answerable while the creating
  descriptor is still open (Redmine #14652).
- :func:`teardown_during` — runs teardown actions independently during an
  unwind, keeping three outcome channels distinct: a returned failure, an
  ordinary exception, and a control-flow exception that outranks the primary
  (j#90482 R12-F2 / j#90487 R13-F1/F2/F3 / j#90492 R14-F1/F2).
- :func:`_attach_secondary` — records a teardown failure without ever becoming
  the reason the caller sees a different exception. Recording it is routed
  through :func:`_run_teardown_action` so that an interrupt arriving *while*
  recording cannot skip the teardown that has not run yet (R14-F1).

Keeping them here means the ordering and channel rules have one home rather than
being restated at each call site.

The teardown API this module intends is :func:`teardown_failures`,
:func:`teardown_during`, :class:`RetentionCarrier`, :class:`TeardownRecord` and
the :data:`RETENTION_ATTEMPTS` bound. Everything else stays module-private,
because the safety argument of this module rests on callers *not* being able to
name it: the ledger key is an identity, the ledger container is checked by exact
type, and the descriptor that reaches a primary's instance dictionary is bound
from ``BaseException`` itself.

Intending an API is not the same as having one, though: there is no ``__all__``,
so *every* non-underscore module binding is reachable and an ``import x`` is as
reachable as a ``def``. Imports this module needs for itself are therefore bound
privately (``Callable as _Callable``), which is why a typing helper does not
quietly join the API (review j#93181 R2-F1).

Three reachable bindings predate this seam and are left alone: ``os``,
``Violation``, and ``annotations`` — the last from ``from __future__ import
annotations``, which reads like a declaration and binds a name like any other
import. Missing exactly that one is what review j#93216 R3-F1 caught here — so
the authoritative list lives in the unit tests, where it is asserted against the
live namespace rather than against this paragraph.

:class:`RetentionCarrier` is the one seam that had to be lifted out (Redmine
#14683): "what happens when the carrier fails" is a property worth pinning, and
pinning it used to mean monkeypatching :func:`_ledger` over the module — a
private name, and a global one. The retention *machine* is not part of that seam
and stays private (review j#93039 F1): a caller replaces where the record goes,
never how the queue and the rails behave. :class:`TeardownRecord` exists only so
the seam's return type can be named without naming a private one (F2).
"""

from __future__ import annotations

import os
from collections.abc import Callable as _Callable

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


class TeardownRecord:
    """A primary's retained record, named but not opened.

    This exists so :meth:`RetentionCarrier.ledger` has a *public* return type to
    declare. It has no members and nothing useful can be done with an instance:
    the real container is :class:`_Ledger`, which stays private because the
    admission rule is exact-type identity — anything a caller can construct must
    not be adopted as the record (j#90517 R17-F1). Subclassing this does not
    change that; ``type(x) is _Ledger`` is still false.

    A carrier override therefore annotates ``TeardownRecord | None`` and passes
    the value through without inspecting it. Naming the type is all the public
    surface needs; reading the record is what :func:`teardown_failures` is for.
    """

    __slots__ = ()


class _Ledger(TeardownRecord):
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


class RetentionCarrier:
    """Where a :class:`_Retention` puts the occurrences it has taken.

    The seam exists because the interesting property is what the retention does
    when the carrier *fails*: the record has to be acquired and written on the
    same channel as everything else, so a carrier that raises — ordinarily or
    with control flow — must not cost a teardown action that has not run
    (j#90508 R16-F1/F2). Reaching that state needs a carrier that misbehaves on
    a schedule, and until Redmine #14683 the only way to get one was to patch
    the module-private :func:`_ledger` over the module itself: a private name,
    and a process-global one, replaced for the duration of a call.

    Overriding :meth:`ledger` is the supported way to schedule those failures.
    An override may raise whatever it likes; when it wants the real behaviour it
    calls ``super().ledger(primary)`` and returns the result untouched.

    **The returned value is opaque**: :class:`TeardownRecord` names it without
    opening it. So this seam replaces *when the carrier answers*, never *what
    the record is* — the drain admits only the container this module created,
    by exact type, exactly as :func:`_existing_ledger` does. A fabricated return
    value is refused rather than written to, so it cannot become a second record
    that nothing reads back (j#90517 R17-F1).

    That is the narrow shape the #14660 characterization §3.2(a) asks for, and
    the reason the seam is not simply ":func:`_ledger`, renamed".
    """

    __slots__ = ()

    def ledger(self, primary: BaseException) -> TeardownRecord | None:
        """Acquire the record for ``primary``, creating it on first use."""
        return _ledger(primary)


_DEFAULT_CARRIER = RetentionCarrier()
"""The carrier every retention uses unless a caller hands one in.

A module-level singleton rather than a per-retention construction: a
:class:`_Retention` is built at the top of :func:`teardown_during`, outside any
guard, and an allocation there would be one more thing that can fail before the
rails exist.
"""


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


RETENTION_ATTEMPTS = 4
"""How many times one retention retries a carrier that keeps interrupting.

Bounded so a carrier that never recovers still terminates; the queue survives
the call anyway and later retentions try again.

Public alongside :class:`RetentionCarrier`, because it is the half of the seam's
contract a replacement carrier is written against: exercising the exhausted-retry
boundary means knowing how many attempts there are, and deriving the schedule
from this bound is what keeps such a test from silently stopping short of it if
the bound ever changes.
"""


_MISSING = object()
"""Stands for "no occurrence yet" in :func:`_took_the_interrupt`.

A default rather than a local, so the binding exists before the function's
first instruction and costs the body neither a statement nor a region.
"""


def _took_the_interrupt(
    unadmitted: list[_Occurrence],
    interrupt: BaseException,
    first: BaseException | None,
    occurrence: object = _MISSING,
) -> BaseException | None:
    """Take priority for ``interrupt`` and queue it, without letting one out.

    Catching a control-flow exception is not the same as handling it: the
    ``except`` body is ordinary code outside the ``try`` that caught it, so a
    *second* interrupt arriving while the first was being turned into an
    occurrence escaped the retention entirely and skipped a cleanup that had
    not run yet (j#90807 R22-F1). Both retention rails route their handler
    through here, so the rule is decided once rather than per call site.

    Priority is decided **only** in the return expression, from the arguments.
    Deciding it inside the guard meant a nested interrupt arriving on the
    ``if first is None`` line left the parameter untouched, so the first
    control-flow exception vanished from both the return rail and the ledger —
    while ``interrupt`` sat right there as an argument (j#90839 R23-F1). Nothing
    the body does can change the answer now, whatever it is interrupted at.

    A nested interrupt gets one attempt at retaining *both* — the interrupt it
    landed on, rebuilt from the argument if its occurrence does not exist yet,
    and itself — and is then absorbed. Surfacing it would cost the remaining
    teardown actions, and those outrank a second interrupt whose arrival is
    already represented by the first.

    Nothing runs before the ``try``. Initialising a local first looked
    harmless and was not: that line, like the ``try`` header itself, is outside
    the protected range, so an arrival on it left the helper — taking the
    remaining cleanup and both occurrences with it (j#90882 R24-F1).
    ``occurrence`` is a parameter with a default instead, bound as part of the
    call, so the handler can ask whether it was replaced without a statement or
    a region of its own. Asking the *binding* — a nested ``try`` around an
    ``UnboundLocalError`` — worked, and cost two more escapable headers for no
    reason (j#90918 R25-F1). It is never passed by a caller.

    What is left is six lines, and they are the ones the regression pins:

    1. this ``try`` header,
    2. its ``except`` header,
    3. the inner ``try`` header,
    4. the absorbing ``except`` header,
    5. that handler's ``pass``,
    6. the ``return``.

    Five are region boundaries, which sit between protected ranges by
    construction. The ``pass`` is a statement, and it escapes too — a handler
    body has to contain something, and nothing encloses it. Saying "no
    statement escapes" was therefore wrong, as was counting four boundaries
    (j#90948 R26-F1); a residual described more narrowly than the code has is
    how two earlier defects stayed hidden here. That they cannot be brought
    inside a guard is an argument for keeping them few, not for adding more.
    The regression injects into every executable line of this function on both
    rails, so the set stays measured rather than approved by how a line is
    spelled.
    """
    try:
        occurrence = _Occurrence(interrupt)
        unadmitted.append(occurrence)
    except BaseException as nested:  # noqa: BLE001 - absorbed, never raised
        try:
            if occurrence is _MISSING:
                # Interrupted before the occurrence existed; the argument is
                # bound at call time and cannot have been lost.
                occurrence = _Occurrence(interrupt)
            # Identity, so a nested arrival *after* the append does not queue
            # the same occurrence twice — the commit-boundary lesson again.
            if not any(queued is occurrence for queued in unadmitted):
                unadmitted.append(occurrence)
            unadmitted.append(_Occurrence(nested))
        except BaseException:  # noqa: BLE001 - the regress ends here, by design
            pass
    return interrupt if first is None else first


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

    __slots__ = ("primary", "_carrier", "_queued")

    def __init__(
        self, primary: BaseException, carrier: RetentionCarrier | None = None
    ) -> None:
        self.primary = primary
        self._carrier = _DEFAULT_CARRIER if carrier is None else carrier
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
        for _ in range(RETENTION_ATTEMPTS):
            try:
                self._admit(unadmitted)
                self._drain()
                return first
            except Exception:  # noqa: BLE001 - nothing to route; admitted below
                break
            except BaseException as interrupt:  # noqa: BLE001 - routed, not raised
                first = _took_the_interrupt(unadmitted, interrupt, first)
        return self._admit_before_leaving(unadmitted, first)

    def _admit_before_leaving(
        self, unadmitted: list[_Occurrence], first: BaseException | None
    ) -> BaseException | None:
        """Get the leftovers into the queue, so a later retention can see them.

        Whatever is still unadmitted when this call gives up would otherwise
        exist only in a local that is about to go out of scope.

        This runs on the same two rails as everything else, which it did not
        when it was first written: it swallowed control flow with a ``continue``
        under the comment "priority is already decided". That is false whenever
        the main loop left on an *ordinary* failure — ``first`` is still
        ``None`` then, so an interrupt here was neither raised by the caller nor
        recorded, even though the very next attempt admitted successfully
        (j#90779 R21-F1). An interrupt is a first-class occurrence at the exit
        too, and it is an addition, never a replacement.

        The last unavoidable point is one instruction wide: a signal can land on
        the append itself, which Python cannot make atomic. It is retried, and
        it is no wider than that.
        """
        for _ in range(RETENTION_ATTEMPTS):
            try:
                self._admit(unadmitted)
                return first
            except Exception:  # noqa: BLE001 - the queue is a plain list; retry
                continue
            except BaseException as interrupt:  # noqa: BLE001 - routed, not raised
                first = _took_the_interrupt(unadmitted, interrupt, first)
        return first

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
        """Append every queued occurrence the ledger does not already hold.

        The carrier's answer is admitted by exact type, the same rule
        :func:`_existing_ledger` applies on the way out. A carrier that hands
        back something this module did not create is refused rather than written
        to: appending into it would build a record no reader can reach, which is
        a second, silent ledger — the failure mode the identity key exists to
        prevent (j#90517 R17-F1).
        """
        while True:
            ledger = self._carrier.ledger(self.primary)
            if type(ledger) is not _Ledger:
                return  # no carrier, or not ours; the queue waits for later
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


def _run_teardown_action(
    retention: _Retention, action: _Callable[[], object]
) -> BaseException | None:
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


def teardown_during(
    primary: BaseException,
    *actions: _Callable[[], object],
    carrier: RetentionCarrier | None = None,
) -> BaseException | None:
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

    ``carrier`` replaces where the retention puts what it takes; see
    :class:`RetentionCarrier`. It defaults to the real one, and the mirror sync
    never passes it — it exists so the properties above can be exercised against
    a carrier that fails, which is the only way to reach several of them.
    """
    retention = _Retention(primary, carrier)
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


_teardown_during = teardown_during
"""The in-package spelling, kept because renaming it is not this Task's to make.

``legacy_mirror_sync`` imports this name, and that module is the exclusive
changed path of Redmine #14682 (T2) — editing it here would break the
changed-path ownership the #14660 characterization §7 sets up, which is what
keeps T2 and T3 dispatchable in parallel. It is one binding to one function, not
a second implementation, and T2 drops it when it updates its own import.
"""


class _OwnedDescriptor:
    """A descriptor this run owns, closed at most once.

    Ownership is released **before** the close syscall runs.
    :func:`_close_quietly` deliberately re-raises anything that is not an
    ``OSError`` so an interrupt is never swallowed, which means the close can
    unwind — and if the sentinel were set afterwards, a later ``finally`` would
    close the same descriptor *number* again. Under number reuse that closed an
    unrelated descriptor: a measured run closed a `/dev/null` handle that had
    just been assigned the freed number (j#90477 R11-F1).

    Every descriptor the mirror sync owns — the directory walk's, the leaf
    reads', the staging file's — goes through this one structure, so the
    ordering cannot be right in one place and wrong in the other.
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


#: What a staging name refers to now, relative to the file this run created.
#:
#: The set is exhaustive and each caller handles every kind explicitly. Falling
#: through to the unlink on an answer nobody recognised would be fail-open, and
#: this is the value the release consults before deleting anything.
_OWNERSHIP_CONFIRMED = "confirmed"
_OWNERSHIP_ABSENT = "absent"
_OWNERSHIP_FOREIGN = "foreign"
_OWNERSHIP_UNREADABLE = "unreadable"
_OWNERSHIP_UNPROVEN = "unproven"


class _StagingOwnership:
    """Whether a name still refers to the file this run created.

    ``(st_dev, st_ino)`` is not an identity on its own. A filesystem may hand
    the same inode number straight back out once the inode is released, and
    Linux does: on the overlayfs ``/tmp`` of ``python:3.12-slim``, unlinking
    this run's staging entry and creating an ordinary file at the same name
    reused the number **20 times out of 20**. The substituted file therefore
    compared equal to the one this run had created, passed as owned, and
    ``os.replace`` installed it as a pinned reference (Redmine #14652). The
    same measurement on tmpfs and on APFS reused it 0 times out of 20, which is
    why the case stayed green on macOS — and why it only surfaced once #14651
    stopped a mistaken capability probe from failing the whole module closed on
    Linux first.

    What turns the number back into an identity is an open descriptor: an inode
    is not released while a descriptor still refers to it, so its number cannot
    be handed out again. Measured the same way on the same overlayfs, with the
    creating descriptor still open: 0 reuses out of 20.

    So this object holds that descriptor and refuses to answer once it is gone.
    Refusing is the point. An unpinned comparison is not a weaker proof of
    ownership, it is not a proof at all, and both callers act on the answer —
    one installs the entry, the other deletes it.
    """

    __slots__ = ("_descriptor", "_identity")

    def __init__(self, descriptor: _OwnedDescriptor) -> None:
        self._descriptor = descriptor
        self._identity: os.stat_result | None = None

    def prove(self) -> None:
        """Capture the pinned file's identity. Raises whatever ``fstat`` does.

        Read from the descriptor rather than accepted from a caller, so what is
        compared later cannot have come from anywhere but the file this object
        pins.
        """
        self._identity = os.fstat(self._descriptor.fileno)

    def resolve(self, dir_fd: int, name: str) -> str:
        """What ``name`` refers to now, as one of the ``_OWNERSHIP_*`` kinds.

        Absence is answered before the pin is consulted, because it is true
        either way: there is nothing at the name to install or to delete.
        Claiming surviving residue that was not there was its own defect
        (j#90467 R9-F3). Every answer that asserts an *identity* needs the pin.
        """
        try:
            present = os.lstat(name, dir_fd=dir_fd)
        except FileNotFoundError:
            return _OWNERSHIP_ABSENT
        except OSError:
            return _OWNERSHIP_UNREADABLE
        identity = self._identity
        if identity is None or not self._descriptor.held:
            return _OWNERSHIP_UNPROVEN
        if (present.st_dev, present.st_ino) != (identity.st_dev, identity.st_ino):
            return _OWNERSHIP_FOREIGN
        return _OWNERSHIP_CONFIRMED


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
