"""Owned-descriptor teardown / retention machine unit tests (Redmine #14580).

Behavior-preserving move out of the 3,865-line
`tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py`
per the #14660 characterization (§5.5 移設先 module の確定) and the placement
ruling in `vibes/docs/logics/tests-placement-discovery-policy.md`
`## #14660 legacy mirror family 裁定`. Test bodies are unchanged; only the
module frame and import paths moved (Redmine #14666, T1 move-only).
"""

from __future__ import annotations

import contextlib
import inspect
import pickle
import sys
import unittest
import unittest.mock
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution.application import (  # noqa: E402
    owned_descriptors,
)
from tests.support.legacy_mirror_tree_fixture import (  # noqa: E402
    _MirrorTreeFixture,
)


_Acquire = Callable[[BaseException], "owned_descriptors.TeardownRecord | None"]


class _ScheduledCarrier(owned_descriptors.RetentionCarrier):
    """A carrier whose every acquisition is answered by a test function.

    The retention machine's hardest properties are about a carrier that *fails*,
    so reaching them needs one that misbehaves on a schedule. Until Redmine
    #14683 that meant `unittest.mock.patch.object(owned_descriptors, "_ledger")`
    — replacing a module-private global for the duration of the call. The seam
    is on the public surface now, so the schedule is handed in instead and
    nothing about the module is mutated.

    The fake is written the way the port expects one to be written (review
    j#93039 F2): every signature is annotated, no `type: ignore` is needed, and
    nothing private is named. `TeardownRecord` is passed straight through — the
    schedule never looks inside it, which is the whole contract.
    """

    __slots__ = ("_acquire",)

    def __init__(self, acquire: _Acquire) -> None:
        self._acquire = acquire

    def ledger(
        self, primary: BaseException
    ) -> owned_descriptors.TeardownRecord | None:
        return self._acquire(primary)


_REAL_CARRIER = owned_descriptors.RetentionCarrier()
"""The unmodified seam, so a schedule can defer to it without naming `_ledger`."""


class OwnedDescriptorTeardownTest(_MirrorTreeFixture):
    """The retention machine (`owned_descriptors`): teardown ordering, the
    failure ledger, and the carrier. No `os` primitive is injected here."""

    def test_the_module_exports_exactly_the_surface_it_documents(self) -> None:
        """The seam is only narrow if the module's bindings say so.

        There is no `__all__`, so every non-underscore module binding is
        reachable — an `import x` as much as a `def`. A typing helper imported
        for the module's own annotations joined the API that way and nobody
        noticed, because the surface was being read off the `def`s and classes
        rather than off the namespace (review j#93181 R2-F1).

        So the namespace is the oracle here, and the set is exact rather than a
        subset: a new import has to be either deliberately listed or bound
        privately. `os` and `Violation` predate this seam and are named as the
        exceptions they are, not quietly tolerated.
        """
        documented = {
            # The seam this Task introduced.
            "teardown_during",
            "RetentionCarrier",
            "TeardownRecord",
            "RETENTION_ATTEMPTS",
            # The read that was already public.
            "teardown_failures",
            # Predating the seam: the `os` module and the violation type.
            "os",
            "Violation",
            # `from __future__ import annotations` binds this.
            "annotations",
        }
        actual = {name for name in vars(owned_descriptors) if not name.startswith("_")}

        self.assertEqual(
            documented,
            actual,
            "the module's public bindings drifted from the surface it documents",
        )

    def test_teardown_continues_when_recording_a_secondary_is_interrupted(self) -> None:
        """The rail's own property, stated directly: whichever step fails — the
        action, or the *recording* of what it reported — every remaining action
        still runs (j#90492 R14-F1)."""

        class Primary(Exception):
            def add_note(self, note: str) -> None:  # type: ignore[override]
                raise KeyboardInterrupt("interrupt while recording")

        ran: list[str] = []

        def failing() -> None:
            ran.append("failing")
            raise RuntimeError("ordinary teardown failure")

        def second() -> None:
            ran.append("second")

        def third() -> None:
            ran.append("third")

        primary = Primary("write failed")
        control = owned_descriptors.teardown_during(primary, failing, second, third)

        self.assertIsInstance(control, KeyboardInterrupt)
        self.assertEqual(["failing", "second", "third"], ran)

    def test_control_flow_priority_keeps_the_first_and_records_the_rest(self) -> None:
        """j#90492 R14-F2, stated directly: the first control-flow exception is
        the one the caller raises, later ones land on the primary's ledger, and
        neither decision may cost a remaining action."""

        ran: list[str] = []

        def first() -> None:
            ran.append("first")
            raise KeyboardInterrupt("first")

        def second() -> None:
            ran.append("second")
            raise SystemExit("second")

        def third() -> None:
            ran.append("third")

        primary = Exception("write failed")
        control = owned_descriptors.teardown_during(primary, first, second, third)

        self.assertIsInstance(control, KeyboardInterrupt)
        self.assertEqual("first", str(control))
        self.assertEqual(["first", "second", "third"], ran)
        self.assertTrue(
            any(
                isinstance(entry, SystemExit)
                for entry in owned_descriptors.teardown_failures(primary)
            ),
            "the second control-flow failure was dropped",
        )

    def test_a_secondary_that_cannot_be_stringified_is_still_retained(self) -> None:
        """j#90503 R15-F2. `_attach_secondary` swallows an ordinary exception as
        best effort, so a secondary whose `__str__` raised was reported as
        recorded and then dropped — no special interrupt needed."""

        class UnprintableFailure(Exception):
            def __str__(self) -> str:
                raise RuntimeError("this failure cannot be stringified")

        class UnprintableExit(SystemExit):
            def __str__(self) -> str:
                raise RuntimeError("nor can this one")

        ran: list[str] = []

        def first() -> None:
            ran.append("first")
            raise KeyboardInterrupt("first")

        def second() -> None:
            ran.append("second")
            raise UnprintableFailure()

        def third() -> None:
            ran.append("third")
            raise UnprintableExit()

        def fourth() -> None:
            ran.append("fourth")

        primary = Exception("write failed")
        control = owned_descriptors.teardown_during(primary, first, second, third, fourth)

        self.assertIsInstance(control, KeyboardInterrupt)
        self.assertEqual(["first", "second", "third", "fourth"], ran)

        kinds = {type(entry) for entry in owned_descriptors.teardown_failures(primary)}
        self.assertIn(UnprintableFailure, kinds, "the unprintable ordinary failure was dropped")
        self.assertIn(UnprintableExit, kinds, "the unprintable control-flow failure was dropped")

        # Python 3.11+ also exposes the best-effort human presentation through
        # exception notes.  On 3.10 the public ledger above is the authority;
        # BaseException.add_note does not exist there.
        if hasattr(primary, "add_note"):
            notes = "\n".join(getattr(primary, "__notes__", []))
            self.assertIn("UnprintableFailure", notes)
            self.assertIn("UnprintableExit", notes)

    def test_an_interrupt_while_recording_a_later_failure_is_retained(self) -> None:
        """The innermost case: a later control-flow failure arrives, and the
        recording of *that* is interrupted too. Only its priority is bounded —
        both it and what it was recording stay on the ledger (j#90503)."""

        class Primary(Exception):
            def add_note(self, note: str) -> None:  # type: ignore[override]
                raise GeneratorExit("interrupt while recording the later failure")

        ran: list[str] = []

        def first() -> None:
            ran.append("first")
            raise KeyboardInterrupt("first")

        def second() -> None:
            ran.append("second")
            raise SystemExit("second")

        def third() -> None:
            ran.append("third")

        primary = Primary("write failed")
        control = owned_descriptors.teardown_during(primary, first, second, third)

        self.assertIsInstance(control, KeyboardInterrupt)
        self.assertEqual(["first", "second", "third"], ran)
        kinds = {type(entry) for entry in owned_descriptors.teardown_failures(primary)}
        self.assertIn(SystemExit, kinds, "the later control-flow failure was dropped")
        self.assertIn(GeneratorExit, kinds, "the interrupt that broke the recording was dropped")

    def _run_teardown_actions(self, primary, actions, label: str) -> None:
        """Run the actions and require that every one of them ran.

        One helper because the carrier has to hold under every hostile primary,
        not just the one that was fashionable that round.
        """
        ran: list[str] = []

        def tracked(index: int, action):  # type: ignore[no-untyped-def]
            def run() -> None:
                ran.append(f"a{index}")
                action()

            return run

        wrapped = [tracked(i, action) for i, action in enumerate(actions, start=1)]
        owned_descriptors.teardown_during(primary, *wrapped)

        self.assertEqual(
            [f"a{i}" for i in range(1, len(actions) + 1)],
            ran,
            f"{label}: the carrier skipped an action that had not run",
        )

    def _assert_ledger_holds_the_failure(self, primary, actions, label: str) -> None:
        """As above, and the failure is on the ledger afterwards."""
        self._run_teardown_actions(primary, actions, label)
        self.assertTrue(
            any(
                isinstance(entry, RuntimeError)
                for entry in owned_descriptors.teardown_failures(primary)
            ),
            f"{label}: the failure was not retained",
        )

    def test_the_ledger_survives_a_hostile_dict_descriptor(self) -> None:
        """j#90508 R16-F1. `object.__getattribute__(exc, "__dict__")` still runs
        a `__dict__` data descriptor defined by a subclass — bypassing
        `__setattr__` is not the same as bypassing the type.

        Measured before the fix: the property raising an ordinary exception lost
        the retention silently, and the property raising `KeyboardInterrupt`
        escaped the rail so the second action never ran.
        """

        class DictRaises(Exception):
            @property
            def __dict__(self):  # type: ignore[override]
                raise RuntimeError("this exception has no usable instance dict")

        class DictInterrupts(Exception):
            @property
            def __dict__(self):  # type: ignore[override]
                raise KeyboardInterrupt("interrupt from the carrier itself")

        def failing() -> None:
            raise RuntimeError("teardown failure")

        def quiet() -> None:
            return None

        for label, primary in (
            ("ordinary", DictRaises("write failed")),
            ("control flow", DictInterrupts("write failed")),
        ):
            with self.subTest(descriptor=label):
                self._assert_ledger_holds_the_failure(primary, (failing, quiet), label)

    def test_the_carrier_key_is_not_an_attribute_name(self) -> None:
        """j#90517 R17-F1. An obscure string key is still an attribute name:
        `setattr`/`getattr` work on any string however it is spelled, so a
        caller's binding could be replaced. I claimed such a key was "outside
        the caller's namespace"; it was not. An identity key removes the
        collision instead of making it unlikely.

        The pickle cost is stated rather than hidden, and
        `test_the_pickle_boundary_depends_on_the_entries` says where it lands.
        """

        def failing() -> None:
            raise RuntimeError("teardown failure")

        self.assertNotIsInstance(
            owned_descriptors._LEDGER_KEY, str, "a string key is in the attribute namespace"
        )

        primary = RuntimeError("write failed")
        owned_descriptors.teardown_during(primary, failing)
        self.assertNotEqual((), owned_descriptors.teardown_failures(primary))
        string_keys = {
            key
            for key in object.__getattribute__(primary, "__dict__")
            if isinstance(key, str)
        }
        expected_presentation_keys = {"__notes__"} if hasattr(primary, "add_note") else set()
        self.assertEqual(
            expected_presentation_keys,
            string_keys,
            "the carrier took a name in the caller's namespace",
        )

    def test_the_pickle_boundary_depends_on_the_entries(self) -> None:
        """j#90529 R18-F2. I wrote the limitation down as "`dumps` succeeds,
        `loads` fails" — which is only true when what the ledger holds can be
        pickled. The ledger holds the failure objects rather than a rendering of
        them, so one whose `__reduce__` raises fails the dump. Stating a
        limitation is not the same as stating it accurately.
        """

        class Unpicklable(Exception):
            def __reduce__(self):  # type: ignore[override]
                raise TypeError("this failure cannot be pickled")

        def ordinary_failure() -> None:
            raise RuntimeError("teardown failure")

        def unpicklable_failure() -> None:
            raise Unpicklable("teardown failure")

        # A module-level primary type: a class defined in a test body is not
        # picklable for reasons that have nothing to do with the ledger.
        picklable_entries = RuntimeError("write failed")
        owned_descriptors.teardown_during(picklable_entries, ordinary_failure)
        with self.assertRaises(TypeError):
            pickle.loads(pickle.dumps(picklable_entries))

        unpicklable_entries = RuntimeError("write failed")
        owned_descriptors.teardown_during(unpicklable_entries, unpicklable_failure)
        with self.assertRaises(TypeError):
            pickle.dumps(unpicklable_entries)

    def test_a_value_at_the_carrier_key_is_never_replaced(self) -> None:
        """j#90508 R16-F2 and j#90517 R17-F1. The ledger was any `list` found at
        a public key, so a caller's own list was adopted and mutated and a
        `list` subclass with a hostile `__iter__` escaped the rail. Checking the
        value's type stopped the adoption but still *replaced* the binding, and
        the regression only looked at the foreign list's contents — so it went
        green without showing the binding was preserved.

        Refusing to retain is the right answer here: the record is worth less
        than someone else's data.
        """

        class Plain(Exception):
            pass

        class HostileList(list):
            def __iter__(self):  # type: ignore[override]
                raise KeyboardInterrupt("iterating this is not safe")

        def failing() -> None:
            raise RuntimeError("teardown failure")

        def quiet() -> None:
            return None

        callers_own = ["caller data"]
        for label, value in (("plain list", callers_own), ("list subclass", HostileList())):
            with self.subTest(value=label):
                primary = Plain("write failed")
                state = object.__getattribute__(primary, "__dict__")
                state[owned_descriptors._LEDGER_KEY] = value

                # (a) the read accessor alone must not touch the binding.
                self.assertEqual((), owned_descriptors.teardown_failures(primary))
                self.assertIs(
                    value, state[owned_descriptors._LEDGER_KEY], f"{label}: a read replaced it"
                )

                # (b) nor may a full teardown.
                self._run_teardown_actions(primary, (failing, quiet), label)
                self.assertIs(
                    value,
                    state[owned_descriptors._LEDGER_KEY],
                    f"{label}: the teardown replaced the binding",
                )

        self.assertEqual(["caller data"], callers_own, "the caller's own list was mutated")

    def test_reading_the_ledger_does_not_create_one(self) -> None:
        """j#90517 R17-F1. `teardown_failures` looked like a read accessor and
        was not: it went through the creating path, so asking an exception what
        went wrong wrote to that exception even when nothing had failed."""

        primary = RuntimeError("write failed")
        state = object.__getattribute__(primary, "__dict__")
        before = dict(state)

        self.assertEqual((), owned_descriptors.teardown_failures(primary))
        self.assertEqual(before, dict(state), "reading the ledger modified the exception")

    def test_each_occurrence_is_one_ledger_entry(self) -> None:
        """j#90517 R17-F2. The ledger de-duplicated by object identity, so two
        independent actions returning the same singleton `False` — the whole
        returned-failure channel — collapsed into one entry while the
        best-effort notes correctly showed two on runtimes that support them.
        Occurrences in the machine-readable ledger are the authority."""

        def returns_false() -> bool:
            return False

        shared = RuntimeError("the same instance, raised twice")

        def raises_shared() -> None:
            raise shared

        for label, action in (("returned False", returns_false), ("raised", raises_shared)):
            with self.subTest(channel=label):
                primary = Exception("write failed")
                owned_descriptors.teardown_during(primary, action, action)

                self.assertEqual(
                    2,
                    len(owned_descriptors.teardown_failures(primary)),
                    f"{label}: two occurrences collapsed into one ledger entry",
                )
                if hasattr(primary, "add_note"):
                    self.assertEqual(
                        2,
                        len(getattr(primary, "__notes__", [])),
                        f"{label}: the notes and the ledger disagree",
                    )

    def test_a_carrier_failure_never_skips_a_remaining_action(self) -> None:
        """j#90508 R16-F1, second condition: acquiring or writing the record is
        on the same channel as everything else.

        Pinned at the seam deliberately. Three carriers in a row were escaped
        by a hostile primary, and each fix made the previous hostile input
        unreachable — so asserting through an input would only pin whichever
        attack happened to still work. Retention not propagating is the
        property; this asserts that directly.

        It asserts what survives too, which the first version of this test did
        not: it checked the actions and the return value only, so it stayed
        green while a carrier that interrupted once and then recovered dropped
        both the failure it was recording and the interrupt (j#90529 R18-F1).
        """
        ran: list[str] = []
        failure = RuntimeError("teardown failure")

        def failing() -> None:
            ran.append("a1")
            raise failure

        def quiet() -> None:
            ran.append("a2")

        real_ledger = _REAL_CARRIER.ledger
        fired: list[bool] = []

        def interrupts_once(
            primary: BaseException,
        ) -> owned_descriptors.TeardownRecord | None:
            if not fired:
                fired.append(True)
                raise KeyboardInterrupt("interrupt from inside the carrier")
            return real_ledger(primary)

        primary = Exception("write failed")
        control = owned_descriptors.teardown_during(
            primary, failing, quiet, carrier=_ScheduledCarrier(interrupts_once)
        )

        self.assertEqual(["a1", "a2"], ran, "a carrier failure skipped a remaining action")
        self.assertIsInstance(control, KeyboardInterrupt)

        ledger = owned_descriptors.teardown_failures(primary)
        self.assertEqual(
            1,
            sum(1 for entry in ledger if entry is failure),
            "the failure the carrier refused was not retained exactly once on recovery",
        )
        self.assertEqual(
            1,
            sum(1 for entry in ledger if entry is control),
            "the carrier's own interrupt was not retained exactly once",
        )

    @staticmethod
    def _source_line(function, match: str) -> int:
        """The line in `function` containing `match`.

        Found by source text, not written down: a literal line number would go
        stale the moment the module is edited, and an injection that quietly
        stops firing is exactly the kind of test that reports green for nothing.
        """
        lines, start = inspect.getsourcelines(function)
        for offset, line in enumerate(lines):
            if match in line:
                return start + offset
        raise AssertionError(
            f"no line matching {match!r} in {function.__name__}; the probe is stale"
        )

    @classmethod
    def _drain_line(cls, match: str) -> int:
        return cls._source_line(owned_descriptors._Retention._drain, match)

    def _interrupt_the_queue_append(self, failure: BaseException):
        """Raise `failure` once, on the instruction that admits to the queue."""
        line = self._source_line(
            owned_descriptors._Retention._enqueue, "self._queued.append("
        )
        code = owned_descriptors._Retention._enqueue.__code__
        fired: list[bool] = []

        def local(frame, event, arg):  # type: ignore[no-untyped-def]
            if event == "line" and frame.f_lineno == line and not fired:
                fired.append(True)
                raise failure
            return local

        def tracer(frame, event, arg):  # type: ignore[no-untyped-def]
            return local if frame.f_code is code else None

        return tracer, fired

    def test_an_arrival_survives_a_failure_before_it_reaches_the_queue(self) -> None:
        """j#90620 R20-F1. Making ledger membership the commit authority fixed
        the far end of the machine and left the entrance lossy: an arrival lived
        in a single local until it was queued, so an ordinary exception dropped
        it and an interrupt *replaced* it with the interrupt's own occurrence.

        Measured before the fix, injecting at the queue append: `MemoryError`
        left an empty ledger, and `KeyboardInterrupt` left a ledger holding the
        interrupt and not the failure it arrived with.
        """
        for label, injected in (
            ("ordinary", MemoryError("no room to queue it")),
            ("control flow", KeyboardInterrupt("interrupt while queueing")),
        ):
            with self.subTest(failure=label):
                primary = Exception("write failed")
                retention = owned_descriptors._Retention(primary)
                original = RuntimeError("the original teardown failure")

                tracer, fired = self._interrupt_the_queue_append(injected)
                sys.settrace(tracer)
                try:
                    first = retention.remember(original)
                finally:
                    sys.settrace(None)
                retention.flush()

                self.assertTrue(fired, f"{label}: the injection never fired")
                ledger = owned_descriptors.teardown_failures(primary)
                self.assertEqual(
                    1,
                    sum(1 for entry in ledger if entry is original),
                    f"{label}: the arrival was lost before it reached the queue",
                )
                if label == "control flow":
                    self.assertIs(first, injected, "the interrupt did not take priority")
                    self.assertEqual(
                        1,
                        sum(1 for entry in ledger if entry is injected),
                        "the interrupt was not retained exactly once",
                    )
                else:
                    self.assertIsNone(first, "an ordinary failure is not control flow")

    @staticmethod
    def _helper_lines() -> dict[str, object]:
        """Classify every executable line of `_took_the_interrupt`.

        Enumerated from the code object rather than named, because naming is
        how the last two gaps got through: the injections sat after the
        priority assignment (j#90839 R23-F1), and then the enumeration started
        *after* the `try:` header and so skipped the two lines that were
        actually unprotected (j#90882 R24-F1). Everything the function can
        execute is classified here, and the residual is asserted rather than
        assumed.
        """
        source, start = inspect.getsourcelines(owned_descriptors._took_the_interrupt)
        code = owned_descriptors._took_the_interrupt.__code__
        executable = sorted({line for _, _, line in code.co_lines() if line})

        def text(line: int) -> str:
            return source[line - start].strip()

        entry = next(line for line in executable if text(line) == "try:")
        handler = next(
            line
            for line in executable
            if text(line).startswith("except BaseException as nested")
        )
        exit_line = next(line for line in executable if text(line).startswith("return "))

        body = [line for line in executable if entry < line < handler]
        inner = [line for line in executable if handler <= line < exit_line]
        if not body or not inner:
            raise AssertionError("the helper no longer has the shape this probe assumes")

        # The residual, spelled out. Classifying by how a line is *spelled* —
        # anything that looked like a `try:`/`except`/`return` — meant every
        # region added to the helper silently widened the escape surface it
        # approved, and two nested headers rode in that way (j#90918 R25-F1).
        # This is the sequence the helper is allowed to have, in order; if it
        # gains a region, or loses one, resolution fails here rather than
        # quietly permitting more.
        expected_roles = (
            "try:",
            "except BaseException as nested:",
            "try:",
            "except BaseException:",
            "pass",
            "return interrupt if first is None else first",
        )
        residual: list[int] = []
        remaining = list(expected_roles)
        for line in executable:
            if remaining and text(line).startswith(remaining[0]):
                remaining.pop(0)
                residual.append(line)
        if remaining:
            raise AssertionError(
                f"the helper no longer has the pinned residual shape; unmatched: {remaining}"
            )

        return {
            "executable": executable,
            "entry": entry,
            "exit": exit_line,
            "body": body,
            "inner": inner,
            "residual": set(residual),
        }

    def _interrupt_while_taking_an_interrupt(self, steps):  # type: ignore[no-untyped-def]
        """Raise each `(line, exception)` in `steps`, in order, once each."""
        code = owned_descriptors._took_the_interrupt.__code__
        pending = list(steps)

        def local(frame, event, arg):  # type: ignore[no-untyped-def]
            if event == "line" and pending and frame.f_lineno == pending[0][0]:
                _, failure = pending.pop(0)
                raise failure
            return local

        def tracer(frame, event, arg):  # type: ignore[no-untyped-def]
            return local if frame.f_code is code else None

        return tracer, pending

    def _interrupt_the_main_rail(self, reached: list[bool] | None = None):
        """Interrupt the carrier once, so the main retention rail handles it.

        Driven through the public carrier seam, so nothing is patched at all.
        """
        real_ledger = _REAL_CARRIER.ledger
        fired: list[bool] = []
        interrupt = KeyboardInterrupt("the carrier was interrupted")

        def interrupts_once(
            primary: BaseException,
        ) -> owned_descriptors.TeardownRecord | None:
            if not fired:
                fired.append(True)
                if reached is not None:
                    reached.append(True)
                raise interrupt
            return real_ledger(primary)

        return (
            contextlib.nullcontext(),
            _ScheduledCarrier(interrupts_once),
            interrupt,
        )

    def _interrupt_the_final_rail(self, reached: list[bool] | None = None):
        """Fail the main admission, then interrupt the exit rail.

        Still a patch of a private method, and it has to be: the exit rail
        (`_admit_before_leaving`) only ever admits to the queue — it never
        consults the carrier — so the seam #14683 lifted out cannot reach this
        rail. The queue is the retention's own retry buffer rather than a
        collaborator, and publishing it to make this patch go away would put the
        machine's internal state on the public surface for one test's benefit.
        """
        real_enqueue = owned_descriptors._Retention._enqueue
        calls: list[int] = []
        interrupt = KeyboardInterrupt("the final admission was interrupted")

        def scheduled(retention, occurrence):  # type: ignore[no-untyped-def]
            calls.append(1)
            if len(calls) == 1:
                raise MemoryError("the main admission failed")
            if len(calls) == 2:
                if reached is not None:
                    reached.append(True)
                raise interrupt
            return real_enqueue(retention, occurrence)

        return (
            unittest.mock.patch.object(
                owned_descriptors._Retention, "_enqueue", scheduled
            ),
            None,
            interrupt,
        )

    @staticmethod
    def _fail_occurrences(reached: list[bool], count: int):
        """Fail the helper's first `count` occurrence constructions.

        The handler cannot be reached by a traced injection: raising from a
        trace function turns tracing off for that frame, so a second injection
        inside the handler would never fire — the kind of probe that reports
        green having done nothing. Failing the construction instead leaves the
        tracer armed for the line actually under test. One failure reaches the
        handler; two reach the handler's own absorbing branch.
        """
        real = owned_descriptors._Occurrence
        seen: list[int] = []

        def raising(failure):  # type: ignore[no-untyped-def]
            if reached and len(seen) < count:
                seen.append(1)
                raise GeneratorExit(f"occurrence construction {len(seen)}")
            return real(failure)

        return unittest.mock.patch.object(
            owned_descriptors, "_Occurrence", raising
        ), seen

    def test_a_nested_interrupt_never_skips_a_remaining_action(self) -> None:
        """j#90807 R22-F1, and what the two rounds after it turned up.

        Catching a control-flow exception is not the same as handling it: the
        `except` body is ordinary code outside the `try` that caught it, so a
        second interrupt arriving while the first was being turned into an
        occurrence escaped the retention and skipped a cleanup.

        Every executable line of the helper, on both rails, under three
        schedules — because each round fixed one rail or one line and left its
        twin the same shape, and because lines the schedule never reached were
        silently skipped rather than measured (j#90918 R25-F1).

        Two things are asserted, and the second is the one that kept slipping:

        - wherever the helper does not escape, every action runs and the first
          control flow is the one returned;
        - the set of lines that *do* escape is exactly the pinned residual —
          the two guards' headers, the absorbing handler and its body, and the
          return. Approving whatever looked like a `try:` let two avoidable
          headers in, so the shape is spelled out in `_helper_lines` and a new
          region fails there instead.
        """
        shape = self._helper_lines()
        # Keyed by rail. Sharing one line-number set across rails meant a rail
        # that never reached the helper at all was covered by the other one —
        # measured: replacing the final rail with a no-op left this test green
        # (j#90948 R26-F2). Each rail now has to stand on its own.
        escaped: dict[str, set[int]] = {}
        exercised: dict[str, set[int]] = {}

        for rail, drive in (
            ("main", self._interrupt_the_main_rail),
            ("final", self._interrupt_the_final_rail),
        ):
            escaped.setdefault(rail, set())
            exercised.setdefault(rail, set())
            for schedule, failures in (("plain", 0), ("handler", 1), ("absorb", 2)):
                for line in shape["executable"]:  # type: ignore[union-attr]
                    with self.subTest(rail=rail, schedule=schedule, line=line):
                        reached: list[bool] = []
                        patch, carrier, interrupt = drive(reached)
                        occurrences, _ = self._fail_occurrences(reached, failures)
                        injected = GeneratorExit("a second interrupt while recording")
                        tracer, pending = self._interrupt_while_taking_an_interrupt(
                            [(line, injected)]
                        )

                        ran: list[str] = []

                        def failing() -> None:
                            ran.append("failing")
                            raise RuntimeError("teardown failure")

                        def quiet() -> None:
                            ran.append("quiet")

                        primary = Exception("write failed")
                        left = None
                        with contextlib.ExitStack() as stack:
                            stack.enter_context(patch)
                            stack.enter_context(occurrences)
                            sys.settrace(tracer)
                            try:
                                control = owned_descriptors.teardown_during(
                                    primary, failing, quiet, carrier=carrier
                                )
                            except BaseException as out:  # noqa: BLE001 - the point
                                control, left = None, out
                            finally:
                                sys.settrace(None)

                        if pending:
                            continue  # this schedule does not reach that line
                        exercised[rail].add(line)
                        if left is not None:
                            escaped[rail].add(line)
                            continue

                        self.assertEqual(
                            ["failing", "quiet"], ran, "a remaining action was skipped"
                        )
                        self.assertIs(
                            control, interrupt, "the first interrupt lost priority"
                        )

                        if schedule == "plain" and line in shape["body"]:  # type: ignore[operator]
                            # The recoverable case: the handler is reached by the
                            # injection alone, so both occurrences are keepable.
                            # Inside the handler nothing more is attempted — the
                            # regress ends there by design — which is stated here
                            # rather than left as an untested gap.
                            ledger = owned_descriptors.teardown_failures(primary)
                            self.assertEqual(
                                1,
                                sum(1 for entry in ledger if entry is interrupt),
                                "the interrupt being recorded was lost",
                            )
                            self.assertEqual(
                                1,
                                sum(1 for entry in ledger if entry is injected),
                                "the nested interrupt was not retained",
                            )

        required = set(shape["executable"]) - {shape["executable"][0]}  # type: ignore[index]
        self.assertEqual({"main", "final"}, set(exercised), "a rail did not run at all")
        for rail in ("main", "final"):
            self.assertEqual(
                set(),
                required - exercised[rail],
                f"{rail}: a line of the helper was never executed, so nothing measured it",
            )
            self.assertEqual(
                shape["residual"],
                escaped[rail],
                f"{rail}: the lines that escape the helper are not the pinned residual",
            )

    def test_an_interrupt_during_the_final_admission_still_counts(self) -> None:
        """j#90779 R21-F1. The exit rail added for R20-F1 swallowed control flow
        with a `continue`, under a comment claiming priority was already
        decided. It is not: when the main loop leaves on an *ordinary* failure
        nothing has been chosen yet, so an interrupt during the final admission
        was neither raised by the caller nor recorded — while the very next
        attempt admitted successfully, so this is not the never-recovers
        boundary either.

        The schedule is the point: an ordinary failure first, so the exit rail
        is reached with no control flow chosen, then the interrupt on it, then
        recovery. Neither earlier regression composes those.
        """
        real_enqueue = owned_descriptors._Retention._enqueue
        interrupt = KeyboardInterrupt("the final admission was interrupted")
        calls: list[int] = []

        def scheduled(retention, occurrence):  # type: ignore[no-untyped-def]
            calls.append(1)
            if len(calls) == 1:
                raise MemoryError("the main admission failed")
            if len(calls) == 2:
                raise interrupt
            return real_enqueue(retention, occurrence)

        ran: list[str] = []

        def failing() -> bool:
            ran.append("failing")
            return False

        def quiet() -> None:
            ran.append("quiet")

        primary = Exception("write failed")
        with unittest.mock.patch.object(
            owned_descriptors._Retention, "_enqueue", scheduled
        ):
            control = owned_descriptors.teardown_during(primary, failing, quiet)

        self.assertGreaterEqual(len(calls), 3, "the schedule never reached recovery")
        self.assertEqual(["failing", "quiet"], ran)
        self.assertIs(control, interrupt, "the interrupt did not reach the caller")

        ledger = owned_descriptors.teardown_failures(primary)
        self.assertEqual(
            1,
            sum(1 for entry in ledger if entry is False),
            "the returned failure was lost",
        )
        self.assertEqual(
            1,
            sum(1 for entry in ledger if entry is interrupt),
            "the interrupt was not retained exactly once",
        )

    def test_an_exhausted_retry_still_reaches_the_queue(self) -> None:
        """j#90620 R20-F1, the far end of the same defect. The last interrupt of
        an exhausted retry sat in the local that was about to go out of scope,
        so it was never queued — and this is not the documented never-recovers
        boundary, because the carrier works again on the very next call.

        Driven entirely through the public entry point (review j#93039 F1). It
        used to construct the retention directly, which was the only thing
        keeping the machine on the public surface; the schedule reaches the same
        exhausted-retry boundary as an argument, and asserting the remaining
        action ran comes free with going through the rail.
        """
        real_ledger = _REAL_CARRIER.ledger
        attempts = owned_descriptors.RETENTION_ATTEMPTS
        raised: list[BaseException] = []

        def interrupts_then_recovers(
            primary: BaseException,
        ) -> owned_descriptors.TeardownRecord | None:
            if len(raised) < attempts:
                interrupt = KeyboardInterrupt(f"interrupt-{len(raised) + 1}")
                raised.append(interrupt)
                raise interrupt
            return real_ledger(primary)

        ran: list[str] = []
        original = RuntimeError("the original teardown failure")

        def failing() -> None:
            ran.append("failing")
            raise original

        def quiet() -> None:
            ran.append("quiet")

        primary = Exception("write failed")
        first = owned_descriptors.teardown_during(
            primary,
            failing,
            quiet,
            carrier=_ScheduledCarrier(interrupts_then_recovers),
        )

        self.assertEqual(attempts, len(raised), "the schedule did not exhaust the retries")
        self.assertIs(first, raised[0], "the first interrupt did not take priority")
        self.assertEqual(["failing", "quiet"], ran, "an exhausted retry skipped an action")

        ledger = owned_descriptors.teardown_failures(primary)
        self.assertEqual(1, sum(1 for entry in ledger if entry is original))
        for index, interrupt in enumerate(raised, start=1):
            self.assertEqual(
                1,
                sum(1 for entry in ledger if entry is interrupt),
                f"interrupt-{index} was not retained exactly once",
            )

    def _interrupt_after_a_commit(self, primary, line: int):
        """Trace `_drain` and interrupt once at `line`, after an append landed.

        "After the append returned" is an ordering, not a commit
        acknowledgement — control flow arrives between bytecodes. Conditioning
        on the ledger being non-empty is what makes this the post-commit
        boundary rather than some earlier one.
        """
        code = owned_descriptors._Retention._drain.__code__
        fired: list[bool] = []

        def local(frame, event, arg):  # type: ignore[no-untyped-def]
            if (
                event == "line"
                and frame.f_lineno == line
                and not fired
                and owned_descriptors.teardown_failures(primary)
            ):
                fired.append(True)
                raise KeyboardInterrupt("interrupt at an instruction boundary")
            return local

        def tracer(frame, event, arg):  # type: ignore[no-untyped-def]
            return local if frame.f_code is code else None

        return tracer, fired

    def test_retention_survives_an_interrupt_at_a_commit_boundary(self) -> None:
        """j#90561 R19-F1. The queue popped an entry once its append returned,
        which is an ordering and not an acknowledgement: an interrupt between
        the append and the pop left the occurrence queued *and* recorded, so a
        retry duplicated it, and the pop sat outside the guard, so an interrupt
        there escaped the rail and skipped a cleanup that had not run.

        Measured before the fix at two boundaries: `actions=['failing']` with
        the `KeyboardInterrupt` escaping `_teardown_during`, and a ledger
        holding the same failure twice.
        """
        for label, match in (
            ("loop head", "while True:"),
            ("the unrecorded scan", "_unrecorded("),
        ):
            with self.subTest(boundary=label):
                ran: list[str] = []
                failure = RuntimeError("the teardown failure")

                def failing() -> None:
                    ran.append("failing")
                    raise failure

                def quiet() -> None:
                    ran.append("quiet")

                primary = Exception("write failed")
                tracer, fired = self._interrupt_after_a_commit(
                    primary, self._drain_line(match)
                )
                sys.settrace(tracer)
                try:
                    control = owned_descriptors.teardown_during(primary, failing, quiet)
                finally:
                    sys.settrace(None)

                self.assertTrue(fired, f"{label}: the injection never fired")
                self.assertEqual(["failing", "quiet"], ran, f"{label}: an action was skipped")
                self.assertIsInstance(control, KeyboardInterrupt)

                ledger = owned_descriptors.teardown_failures(primary)
                self.assertEqual(
                    1,
                    sum(1 for entry in ledger if entry is failure),
                    f"{label}: the failure was recorded more than once",
                )
                self.assertEqual(
                    1,
                    sum(1 for entry in ledger if entry is control),
                    f"{label}: the interrupt was not recorded exactly once",
                )

    def test_the_final_flush_surfaces_the_control_flow_it_hits(self) -> None:
        """j#90561 R19-F2. The flush after the last action returned control flow
        that was thrown away, so an interrupt there vanished entirely — worse
        than the demotion to a note R13-F3 was about, and not the documented
        never-recovers boundary either, since the carrier recovers next call."""
        ran: list[str] = []
        failure = RuntimeError("teardown failure")

        def failing() -> None:
            ran.append("failing")
            raise failure

        def quiet() -> None:
            ran.append("quiet")

        real_ledger = _REAL_CARRIER.ledger
        schedule = ["ordinary", "interrupt"]

        def scheduled(primary: BaseException) -> owned_descriptors.TeardownRecord | None:
            if schedule:
                if schedule.pop(0) == "ordinary":
                    raise MemoryError("the carrier failed, leaving the queue")
                raise KeyboardInterrupt("the carrier interrupted the final flush")
            return real_ledger(primary)

        primary = Exception("write failed")
        control = owned_descriptors.teardown_during(
            primary, failing, quiet, carrier=_ScheduledCarrier(scheduled)
        )

        self.assertEqual([], schedule, "the schedule never reached the final flush")
        self.assertEqual(["failing", "quiet"], ran)
        self.assertIsInstance(control, KeyboardInterrupt)

        ledger = owned_descriptors.teardown_failures(primary)
        self.assertEqual(1, sum(1 for entry in ledger if entry is failure))
        self.assertEqual(1, sum(1 for entry in ledger if entry is control))

    def test_a_carrier_that_never_recovers_gives_up_the_record_only(self) -> None:
        """The stated boundary, held to: if the carrier never takes anything,
        the record is unreachable — but the actions still all run, the first
        control flow still surfaces, and nothing is duplicated or escapes."""
        ran: list[str] = []

        def failing() -> None:
            ran.append("a1")
            raise RuntimeError("teardown failure")

        def quiet() -> None:
            ran.append("a2")

        def never_recovers(_primary: BaseException) -> owned_descriptors.TeardownRecord | None:
            raise KeyboardInterrupt("the carrier is gone for good")

        primary = Exception("write failed")
        control = owned_descriptors.teardown_during(
            primary, failing, quiet, carrier=_ScheduledCarrier(never_recovers)
        )

        self.assertEqual(["a1", "a2"], ran)
        self.assertIsInstance(control, KeyboardInterrupt)
        self.assertEqual((), owned_descriptors.teardown_failures(primary))

    def test_a_record_the_module_did_not_create_is_never_written_to(self) -> None:
        """The seam replaces *when* the carrier answers, not *what* the record
        is (review j#93039 F2).

        `TeardownRecord` is public so an override has a return type to name, and
        subclassing it is therefore something a caller can do. It must not be a
        way to become the record: admission is exact-type identity, the same
        rule `_existing_ledger` applies on the way out (j#90517 R17-F1).
        Measured before the narrow was added: the drain appended occurrences
        into the substitute, building a second ledger that no reader could ever
        reach — a silent record is worse than a refused one.

        Refusing is the never-recovers boundary, so the channel discipline is
        unchanged: every remaining action still runs.
        """

        class Substitute(owned_descriptors.TeardownRecord):
            __slots__ = ("entries",)

            def __init__(self) -> None:
                self.entries: list[object] = []

        substitute = Substitute()

        def hands_back_a_substitute(
            _primary: BaseException,
        ) -> owned_descriptors.TeardownRecord | None:
            return substitute

        ran: list[str] = []

        def failing() -> None:
            ran.append("a1")
            raise RuntimeError("teardown failure")

        def quiet() -> None:
            ran.append("a2")

        primary = Exception("write failed")
        control = owned_descriptors.teardown_during(
            primary, failing, quiet, carrier=_ScheduledCarrier(hands_back_a_substitute)
        )

        self.assertEqual(
            [], substitute.entries, "a record the module did not create was written to"
        )
        self.assertEqual((), owned_descriptors.teardown_failures(primary))
        self.assertEqual(["a1", "a2"], ran, "refusing the record skipped an action")
        self.assertIsNone(control, "refusing the record invented control flow")

    def test_the_ledger_survives_a_primary_that_refuses_attributes(self) -> None:
        """The carrier has to be the instance dictionary, not `setattr`.

        A `__context__`-chained second carrier was written for exactly this
        case and measured to fail: `__context__` assignment routes through the
        same `__setattr__` that refuses the attribute, so both carriers died
        together. One carrier that works beats two that do not.
        """

        class Frozen(Exception):
            def __setattr__(self, name: str, value: object) -> None:
                raise AttributeError("this exception refuses attributes")

            def __getattr__(self, name: str) -> object:
                raise AttributeError("and refuses unknown reads")

        def failing() -> None:
            raise RuntimeError("teardown failure")

        primary = Frozen("write failed")
        owned_descriptors.teardown_during(primary, failing)

        ledger = owned_descriptors.teardown_failures(primary)
        self.assertTrue(
            any(isinstance(entry, RuntimeError) for entry in ledger),
            "the ledger did not survive an exception that refuses attributes",
        )


if __name__ == "__main__":
    unittest.main()
