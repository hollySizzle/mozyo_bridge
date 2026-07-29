"""Shared `os` fault schedule for the legacy mirror family (Redmine #14684).

Four modules inject `os` primitives against a real mirror tree. Between them they
had re-written the same three fakes — an `os.open` that remembers which
descriptor the staging create handed back, an `os.close` that closes for real and
then fails that descriptor, and a primitive that raises instead of running — and
this module owns the three shapes as a set, so a fix to one of them lands once
instead of once per module that had it.

As a set, because no module needed all three and none of them is used alone:

===========================================================  ======  =====  =====
consumer                                                     track   close  raise
===========================================================  ======  =====  =====
integration .../test_legacy_mirror_fault_injection.py        yes     yes    yes
regressions/test_issue_14580_reused_descriptor_number_close  yes     yes    yes
integration .../test_platform_capability_probe_io.py         --      --     yes
unit .../test_platform_capability_probe.py                   --      --     yes
===========================================================  ======  =====  =====

Four, not the five the #14660 characterization marks with a non-zero `os_patch`
(`vibes/docs/logics/legacy-mirror-failure-state-characterization.md`
§5.5 移設先 module の確定). The fifth,
`tests/regressions/test_issue_14651_capability_advertisement.py`, does not import
this and has none of the three shapes: its two `os_patch` replace
`os.supports_dir_fd` / `os.supports_fd` with a `frozenset`, which substitutes an
*advertisement the probe must refuse to read* (#14651) rather than making a
primitive fail. Routing it through a fault schedule would be reuse in name only.
`os_patch` counts tests that patch an `os` *attribute*; the consumer set is the
tests that inject a *fault*, and §5.5 gives the per-module split of the two.

It deliberately does not own a fault whose payload *is* the property under
test — the short write, the ordinary file substituted at the staging name
mid-write, the `scandir` whose failure is deferred to the first step, the
`lstat` keyed on a staging name. Those stay at the call site with the docstring
that explains them; a fake taking an arbitrary callback for each of them would
be indirection rather than sharing.

Two notes on what the schedule patches:

* Every module of the family reaches its primitives through a plain
  ``import os``, so ``patch.object(legacy_mirror_sync.os, "write", ...)``,
  ``patch.object(owned_descriptors.os, "write", ...)`` and
  ``patch.object(os, "write", ...)`` name the same attribute of the same module
  object — measured, not assumed. The schedule patches ``os``: one shared fake
  cannot carry every call site's spelling, and adopting one of them would read
  as a reach it does not have.
* A fault keyed on nothing but a call count lands on the host capability probe
  before it reaches the subject, because the probe calls the same primitives
  (#14651). Such a fault therefore belongs with
  `_MirrorTreeFixture._preflight_already_answered`. Faults keyed on a descriptor
  pick out their own call and do not need it.

What the consumers defend, and what they do not, measured rather than asserted
(Redmine #14684, reviews j#93050 / j#93155 / j#93223):

*Behaviour a consumer catches breaking.* Mutating each of these fails cases, in
the count shown: the faults firing at all (19 / 12 / 15), `staging` matching only
an `O_CREAT` open (10), `close_fired` recording that the close fault fired (9),
the close fault closing for real before it raises (3), `before_raising` running
(2), and `calls` counting what the fault reached (1).

*Fail-loud boundaries no case exercises.* `_name`'s refusal to bind one name to
two different descriptors, and the rejection of an `error` that is not an
exception instance. **Removing either leaves all 40 cases green** — the subject
never produces two distinct matching descriptors, and every call site passes an
instance. They are kept as boundaries rather than promises: breaking one cannot
hand a consumer a wrong answer quietly, only an error. Closing them properly
would take cases built against this module directly, which is outside the paths
this task declared; the gateway ruled that acceptable here (j#93223) rather than
required.

*Behaviour removed because nothing could observe it.* A "fail only the first
call" knob, the close fault firing at most once, an exception *instance* being
re-raised as the same object rather than rebuilt, and the rule for choosing
between two descriptors matching one name — that last one deleted by making the
ambiguity raise instead. A shared fake that promises what nothing checks lets a
later consumer build on behaviour no case defends. Anything added back needs the
case that would notice, in the same change.
"""

from __future__ import annotations

import contextlib
import os
import unittest.mock
from collections import Counter
from types import TracebackType

#: The descriptors `track_descriptors` names, and nothing else. A fault keyed on
#: a descriptor spells one of these rather than a number, because the number is
#: not known until the subject asks for it.
DESCRIPTORS = ("staging", "walk_root")


class FaultSchedule:
    """The `os` faults one run injects, declared up front and applied together.

    Declare, then run inside the schedule::

        schedule = FaultSchedule().track_descriptors()
        schedule.raise_after_closing("staging", RuntimeError("injected close unwind"))
        with schedule:
            self._service(repo).sync()
        self.assertTrue(schedule.close_fired, "the staging close was never reached")

    Nothing is patched until the ``with``, and every fault calls the real
    primitive unless the case is about it not running — so what a test observes
    is the syscall happening rather than a flag the fake set.
    """

    def __init__(self) -> None:
        #: How many times each patched primitive was reached. A test that has to
        #: prove its injection fired reads this instead of keeping its own list.
        self.calls: Counter[str] = Counter()
        #: The descriptor of this run's create (`O_CREAT`) — its staging file —
        #: and every one of them, for a fault that must recognise any.
        self.staging: int | None = None
        self.staging_descriptors: set[int] = set()
        #: The descriptor opened without a `dir_fd`: where the component walk
        #: starts.
        self.walk_root: int | None = None
        #: Whether the descriptor-keyed close fault fired.
        self.close_fired = False
        self._builders: list[tuple[str, object]] = []
        self._scheduled: set[str] = set()
        self._stack: contextlib.ExitStack | None = None

    # --- declaring ----------------------------------------------------------

    def track_descriptors(self) -> FaultSchedule:
        """Record which descriptor the subject's `os.open` handed back.

        A name binds to **the** descriptor matching its predicate — `O_CREAT`
        for `staging`, no `dir_fd` for `walk_root`. There is no rule for
        choosing between two of them, because two distinct matches raise
        (`_name`): a schedule that silently picked one would name the wrong
        descriptor with nothing to notice.

        The earlier versions did have such a rule — `staging` kept the most
        recent match and `walk_root` the first — and no case could catch either
        breaking. `audit()` opens twice without a `dir_fd`, but the first is
        closed before the second asks, so both calls return the same number
        (measured: `[3, 3]`) and every choice rule computes the same answer.
        Removing the choice is what closes that, not documenting it
        (review j#93155 F2).

        The predicates themselves are observed: recording any open as a create
        instead of only an `O_CREAT` one fails ten cases.

        Both are read from the call the subject actually makes. Naming a
        descriptor any other way would be guessing at a number, and the numbers
        are reused the moment they are freed.
        """

        def build(real_open):  # type: ignore[no-untyped-def]
            def tracking_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
                fd = real_open(path, flags, *args, **kwargs)
                self.calls["open"] += 1
                if flags & os.O_CREAT:
                    self._name("staging", fd)
                    self.staging_descriptors.add(fd)
                if "dir_fd" not in kwargs:
                    self._name("walk_root", fd)
                return fd

            return tracking_open

        return self._schedule("open", build)

    def _name(self, descriptor: str, fd: int) -> None:
        """Bind `descriptor` to `fd`, refusing to choose between two answers."""
        current = getattr(self, descriptor)
        if current is not None and current != fd:
            raise AssertionError(
                f"two descriptors match {descriptor} in one run ({current} and {fd}); "
                "the schedule will not pick one for you — key the fault differently"
            )
        setattr(self, descriptor, fd)

    def raise_on(self, primitive: str, error: BaseException) -> FaultSchedule:
        """``os.<primitive>`` raises `error` instead of running — on every call.

        `error` is the exception object, raised as it is. There was briefly a
        second mode taking a callable to build a fresh exception per fire; no
        case could tell the two apart, so the choice is gone rather than
        documented (review j#93155 F2).

        There is also deliberately no "only the first call" variant. One case
        needs a failure that does not persist, and it writes that fake itself: a
        fault that stops after n calls has a *schedule* which is the property
        under test, so it belongs with the case that asserts it (review j#93050
        F2).
        """
        if not isinstance(error, BaseException):
            raise TypeError(f"expected an exception instance, got {error!r}")

        def build(_real):  # type: ignore[no-untyped-def]
            def failing(*args, **kwargs):  # type: ignore[no-untyped-def]
                self.calls[primitive] += 1
                raise error

            return failing

        return self._schedule(primitive, build)

    def raise_after_closing(
        self,
        descriptor: str,
        error: BaseException,
        *,
        before_raising=None,  # type: ignore[no-untyped-def]
    ) -> FaultSchedule:
        """`os.close` closes for real, then raises — whenever `descriptor` closes.

        Every close of that descriptor raises, the same way `raise_on` fails
        every call. It used to fire at most once; nothing could catch that guard
        breaking (measured — removing it leaves all 40 cases green, because no
        case closes the named descriptor twice), and one rule for both faults
        beats two rules with one of them undefended (review j#93155 F2).

        Closing for real first is the point rather than an implementation
        detail: every defect these cases pin is about what the subject does
        *after* the descriptor is gone, and a close that never ran would leak it
        instead.

        `descriptor` is one of `DESCRIPTORS`, resolved when the close arrives —
        `track_descriptors` has to be scheduled too, or nothing is ever named.
        `before_raising` runs between the close and the raise, for the cases
        whose payload is itself the property (#14580 takes the freed number
        before anyone else can).
        """
        if descriptor not in DESCRIPTORS:
            raise ValueError(f"unknown descriptor {descriptor!r}; expected one of {DESCRIPTORS}")
        if not isinstance(error, BaseException):
            raise TypeError(f"expected an exception instance, got {error!r}")

        def build(real_close):  # type: ignore[no-untyped-def]
            def closing_then_failing(fd: int) -> None:
                self.calls["close"] += 1
                real_close(fd)
                if fd == getattr(self, descriptor):
                    self.close_fired = True
                    if before_raising is not None:
                        before_raising()
                    raise error

            return closing_then_failing

        return self._schedule("close", build)

    def _schedule(self, primitive: str, build) -> FaultSchedule:  # type: ignore[no-untyped-def]
        if primitive in self._scheduled:
            raise ValueError(
                f"os.{primitive} already has a fault scheduled; "
                "the second one would silently replace the first"
            )
        if self._stack is not None:
            raise RuntimeError("the schedule is already applied; declare every fault first")
        self._scheduled.add(primitive)
        self._builders.append((primitive, build))
        return self

    # --- applying -----------------------------------------------------------

    def __enter__(self) -> FaultSchedule:
        if self._stack is not None:
            raise RuntimeError("the schedule is already applied")
        stack = contextlib.ExitStack()
        try:
            for primitive, build in self._builders:
                replacement = build(getattr(os, primitive))  # type: ignore[operator]
                stack.enter_context(unittest.mock.patch.object(os, primitive, replacement))
        except BaseException:
            stack.close()
            raise
        self._stack = stack
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        stack, self._stack = self._stack, None
        if stack is not None:
            stack.close()
