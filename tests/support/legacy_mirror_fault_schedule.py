"""Shared `os` fault schedule for the legacy mirror family (Redmine #14684).

The five modules the #14660 characterization marks with a non-zero `os_patch`
(`vibes/docs/logics/legacy-mirror-failure-state-characterization.md`
§5.5 移設先 module の確定) inject `os` primitives against a real mirror tree,
and each of them had re-written the same three fakes: an `os.open` that
remembers which descriptor the staging create handed back, an `os.close` that
closes for real and then fails exactly that descriptor once, and a primitive
that raises instead of running. This module owns those three shapes, so a fix
to one of them lands once instead of five times.

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
  (#14651). ``only_first`` therefore belongs with
  `_MirrorTreeFixture._preflight_already_answered`, exactly as the hand-written
  fake it replaces did. Faults keyed on a descriptor pick out their own call and
  do not need it.
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


def _exception_factory(error):  # type: ignore[no-untyped-def]
    """Turn an `error` argument into something that yields an exception.

    An instance is re-raised as it is, which is what a fault firing once wants.
    A callable — an exception class, or a lambda — is called per fire, for a
    fault that can fire more than once and whose cases must not share a
    traceback.
    """
    if isinstance(error, BaseException):
        return lambda: error
    if callable(error):
        return error
    raise TypeError(f"expected an exception or a callable returning one, got {error!r}")


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
        #: The descriptor of the latest create (`O_CREAT`) — this run's staging
        #: file — and every one of them, for a fault that must recognise any.
        self.staging: int | None = None
        self.staging_descriptors: set[int] = set()
        #: The first descriptor opened without a `dir_fd`: where the component
        #: walk starts.
        self.walk_root: int | None = None
        #: Whether the descriptor-keyed close fault fired.
        self.close_fired = False
        self._builders: list[tuple[str, object]] = []
        self._scheduled: set[str] = set()
        self._stack: contextlib.ExitStack | None = None

    # --- declaring ----------------------------------------------------------

    def track_descriptors(self) -> FaultSchedule:
        """Record which descriptor the subject's `os.open` handed back.

        Both names are read from the call the subject actually makes. Naming a
        descriptor any other way would be guessing at a number, and the numbers
        are reused the moment they are freed.
        """

        def build(real_open):  # type: ignore[no-untyped-def]
            def tracking_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
                fd = real_open(path, flags, *args, **kwargs)
                self.calls["open"] += 1
                if flags & os.O_CREAT:
                    self.staging = fd
                    self.staging_descriptors.add(fd)
                if self.walk_root is None and "dir_fd" not in kwargs:
                    self.walk_root = fd
                return fd

            return tracking_open

        return self._schedule("open", build)

    def raise_on(self, primitive: str, error, *, only_first: bool = False) -> FaultSchedule:  # type: ignore[no-untyped-def]
        """``os.<primitive>`` raises `error` instead of running.

        ``only_first`` lets the real primitive run from the second call on, for
        the cases about a failure that does not persist.
        """
        make = _exception_factory(error)

        def build(real):  # type: ignore[no-untyped-def]
            def failing(*args, **kwargs):  # type: ignore[no-untyped-def]
                self.calls[primitive] += 1
                if only_first and self.calls[primitive] > 1:
                    return real(*args, **kwargs)
                raise make()

            return failing

        return self._schedule(primitive, build)

    def raise_after_closing(
        self,
        descriptor: str,
        error,  # type: ignore[no-untyped-def]
        *,
        before_raising=None,  # type: ignore[no-untyped-def]
    ) -> FaultSchedule:
        """`os.close` closes for real, then raises once for `descriptor`.

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
        make = _exception_factory(error)

        def build(real_close):  # type: ignore[no-untyped-def]
            def closing_then_failing(fd: int) -> None:
                self.calls["close"] += 1
                real_close(fd)
                if fd == getattr(self, descriptor) and not self.close_fired:
                    self.close_fired = True
                    if before_raising is not None:
                        before_raising()
                    raise make()

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
