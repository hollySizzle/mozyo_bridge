"""The smoke's dispatch allowlist is pinned to a derivation, not to a memory (#14658).

#14185 R3 measured what hand enumeration costs (evidence j#91992).  The production
session-start path runs a launcher preflight probe — ``<launcher> herdr agent-attest
--help`` — whose ``(group, subcommand)`` pair was never in
:data:`CLIENT_CALL_SUBCOMMANDS`, so the endpoint gate refused it *before dispatch*,
twice, and the live smoke never reached acceptance 2/3.  The same hand pass had put five
pairs into the set that no call site can emit.  Three of those five occur
elsewhere in the tree as JSON key / alias tuples rather than as commands:
``("agent","pane")`` is ``herdr_state._GET_OBJECT_KEYS`` and ``("pane","location")`` is
``support.herdr_fake.LOCATOR_ALIASES`` exactly, while ``("agent","target")`` is an
adjacent pair inside ``herdr_state._HANDLE_KEYS`` — which is what a text search for a
tuple, rather than a walk of the call graph, will find.  The other two are real herdr
commands that this runner simply never carries: ``agent get`` is issued by
``HerdrCliAgentStateReader`` (retire / turn-start / hibernate paths), and
``wait agent-status`` goes out through ``popen``, not through the gated runner at all.

So the fix is not the missing literal.  It is that the set now has an external authority:
``support.herdr_dispatch_derivation`` follows the gated runner through the first-party
source and reports every site that can dispatch through it.  These tests pin the set
against that derivation in **both** directions, and every fail-closed property the
derivation relies on is probed rather than assumed:

* an unreadable site must be *reported*, never dropped — otherwise "no findings" and
  "could not look" become the same answer;
* the derivation must actually re-read the source — the mutation probes add a dispatch
  site to a copy of the tree and require the new pair to appear, so a walk that had
  quietly stopped resolving anything could not stay green.

The live-path reproduction at the bottom drives the real production preflight through the
real gate.  With the pre-#14658 allowlist it raises exactly the refusal #14185 hit; that
contrast is what makes it a regression test rather than a smoke test.
"""

from __future__ import annotations

import functools
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from support.herdr_dispatch_derivation import (  # noqa: E402
    derive_dispatch_surface,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E402,E501
    disposable_herdr_instance as live_module,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.disposable_herdr_instance import (  # noqa: E402,E501
    CLIENT_CALL_SUBCOMMANDS,
    DisposableHerdrInstance,
    LAUNCHER_PREFLIGHT_SUBCOMMANDS,
    MINTER_ONLY_SUBCOMMANDS,
    REFUSAL_COMMAND_NOT_ALLOWLISTED,
    SmokeEndpointEscapeError,
)

#: The pair the #14185 R3 live run was refused on, and the sibling probe that was already
#: admitted.  Named here so a change to either one has to come through this file.
ATTEST_PROBE_PAIR = ("herdr", "agent-attest")
CONFIG_PARSE_PROBE_PAIR = ("config", "check-parse")

#: Herdr controls that must never become dispatchable.  ``session stop``/``session
#: delete`` name ``default`` as a target in their own help, so on a shared server they
#: reach the operator's session; the rest reconfigure or replace the server.
FORBIDDEN_CONTROLS = (
    ("session", "stop"),
    ("session", "delete"),
    ("session", "attach"),
    ("server", "reload-config"),
    ("config", "reset-keys"),
    ("channel", "set"),
    ("update", "--handoff"),
    ("pane", "send-text"),
    ("pane", "send-keys"),
)

#: Where the gated runner is allowed to leave its owner.  This is deliberately a small
#: hand-written boundary: it is not the command surface (that is derived), it is the
#: **seed** the derivation starts from, so a new consumer must be looked at by a person
#: before the derivation silently starts covering — or missing — it.
EXPECTED_SEED_FLOWS = {
    ("SharedSpaceSmokeHarness", "runner"),
    ("_run_forked_projects", "gate_runner"),
    ("snapshot", "#0"),
}


@functools.lru_cache(maxsize=1)
def _surface():
    """The derivation over the live tree.

    Cached because it parses the whole first-party package (~3 s) and is pure
    read-only analysis: every test in this file asks the same question of the same
    unchanged source.  The mutation probes deliberately call
    :func:`derive_dispatch_surface` directly on their own copied tree, so they never
    read through this cache.
    """
    return derive_dispatch_surface()


class DerivationReadabilityTests(unittest.TestCase):
    """The derivation may not report a surface it could not fully read."""

    def test_no_dispatch_site_is_left_unresolved(self) -> None:
        surface = _surface()
        unresolved = [
            f"{site.anchor}: {site.unresolved_reason}"
            for site in surface.unresolved_sites
        ]
        self.assertEqual(
            unresolved,
            [],
            "a dispatch site whose argv could not be resolved is an unknown, not an "
            "absence: the allowlist below cannot be called complete while one exists",
        )

    def test_no_runner_flow_is_left_unfollowed(self) -> None:
        surface = _surface()
        self.assertEqual(
            list(surface.unresolved_flows),
            [],
            "the gated runner reaches a callee the walk cannot follow, so there may be "
            "dispatch sites beyond the ones reported",
        )

    def test_the_surface_is_not_empty(self) -> None:
        """Baseline: a walk that resolved nothing would satisfy every ⊆ assertion."""
        surface = _surface()
        self.assertGreaterEqual(len(surface.sites), 10, surface.sites)
        self.assertGreaterEqual(len(surface.pairs), 10, sorted(surface.pairs))


class AllowlistMatchesTheDerivationTests(unittest.TestCase):
    """Both directions.  Either one alone is how #14658 happened."""

    def test_every_dispatchable_pair_is_admitted(self) -> None:
        """The direction #14185 R3 failed on: a real call site the gate refuses."""
        surface = _surface()
        admitted = set(CLIENT_CALL_SUBCOMMANDS) | set(MINTER_ONLY_SUBCOMMANDS)
        missing = sorted(surface.pairs - admitted)
        detail = {
            pair: [site.anchor for site in surface.sites_for(pair)] for pair in missing
        }
        self.assertEqual(
            missing,
            [],
            f"production can dispatch these pairs and the gate would refuse them, "
            f"with zero dispatch, at the call sites shown: {detail}",
        )

    def test_every_admitted_pair_has_a_call_site(self) -> None:
        """The other direction: privilege granted to a command nobody issues."""
        surface = _surface()
        admitted = set(CLIENT_CALL_SUBCOMMANDS) | set(MINTER_ONLY_SUBCOMMANDS)
        undeliverable = sorted(admitted - surface.pairs)
        self.assertEqual(
            undeliverable,
            [],
            "these pairs are allowlisted but no call site reachable from the gated "
            "runner emits them, so they are privilege with no purpose — remove them, or "
            "add the call site that justifies them",
        )

    def test_the_launcher_preflight_probe_is_on_the_surface(self) -> None:
        """The exact #14185 R3 regression, anchored to the site that emits it."""
        surface = _surface()
        anchors = [site.anchor for site in surface.sites_for(ATTEST_PROBE_PAIR)]
        self.assertTrue(
            anchors,
            f"{ATTEST_PROBE_PAIR} is no longer derived from any call site; if the "
            f"production launcher preflight really was removed, remove it from "
            f"LAUNCHER_PREFLIGHT_SUBCOMMANDS in the same change",
        )
        self.assertTrue(
            any("preflight_attest_launcher_capability" in anchor for anchor in anchors),
            f"expected the launcher capability preflight to be the emitter: {anchors}",
        )
        self.assertIn(ATTEST_PROBE_PAIR, CLIENT_CALL_SUBCOMMANDS)

    def test_the_two_launcher_probes_are_named_apart_from_the_server_calls(self) -> None:
        """``argv[0]`` is the launcher for these two, so the record must say so."""
        self.assertEqual(
            set(LAUNCHER_PREFLIGHT_SUBCOMMANDS),
            {ATTEST_PROBE_PAIR, CONFIG_PARSE_PROBE_PAIR},
        )
        self.assertEqual(
            set(live_module.HERDR_SERVER_CLIENT_SUBCOMMANDS)
            & set(LAUNCHER_PREFLIGHT_SUBCOMMANDS),
            set(),
            "a pair cannot be both a Herdr server call and a launcher probe",
        )

    def test_no_lifecycle_control_is_dispatchable_or_admitted(self) -> None:
        """Negative control, on the derivation as well as on the allowlist."""
        surface = _surface()
        admitted = set(CLIENT_CALL_SUBCOMMANDS) | set(MINTER_ONLY_SUBCOMMANDS)
        for pair in FORBIDDEN_CONTROLS:
            self.assertNotIn(pair, surface.pairs, f"{pair} is reachable from the smoke")
            self.assertNotIn(pair, admitted, f"{pair} is allowlisted")

    def test_the_minters_extra_authority_is_still_only_its_own_stop(self) -> None:
        self.assertEqual(set(MINTER_ONLY_SUBCOMMANDS), {("server", "stop")})
        self.assertNotIn(("server", "stop"), CLIENT_CALL_SUBCOMMANDS)


class SeedFlowTests(unittest.TestCase):
    """Where the gated runner leaves its owner is a reviewed boundary, not a discovery."""

    def test_the_seed_flows_are_the_reviewed_ones(self) -> None:
        surface = _surface()
        observed = {(flow.callee, flow.parameter) for flow in surface.seed_flows}
        self.assertEqual(
            observed,
            EXPECTED_SEED_FLOWS,
            "the disposable smoke hands its gated runner somewhere new (or no longer "
            "hands it somewhere it used to). The derivation starts from this boundary, "
            "so a change here has to be read by a person before the derived command "
            "surface can be trusted again",
        )


class ModelledReadTableTests(unittest.TestCase):
    """The guard's exception table is small, justified, and unambiguous.

    Reads are admitted from an explicit table rather than from a syntactic test, because
    three successive syntactic tests were each wrong in a new way (j#92123 / j#92165 /
    j#92213).  A hand-written exception set is only defensible while it stays checkable:
    every key must name a real, unique class, and every entry must say why.
    """

    def test_every_entry_names_exactly_one_real_class(self) -> None:
        from support import herdr_dispatch_derivation as derivation

        index = derivation._index_package(derivation.default_source_root())
        for (class_name, attribute), reason in (
            derivation._MODELLED_ATTRIBUTE_READS.items()
        ):
            with self.subTest(cls=class_name, attr=attribute):
                owners = [
                    module for module, indexed in index.items()
                    if class_name in indexed.classes
                ]
                self.assertEqual(
                    len(owners), 1,
                    f"{class_name!r} resolves to {len(owners)} classes; the key would be "
                    f"ambiguous",
                )
                self.assertTrue(reason.strip(), "every admitted read must record why")

    def test_every_entry_is_actually_consumed_by_the_live_run(self) -> None:
        """Review j#92266 F5, verdict j#92272.

        The table's own tests checked that keys look well-formed, never that anything
        uses them.  A stale key — a read that was removed, or a typo — therefore sat in
        the guard pre-authorising a read nobody performs, which is the opposite of "a new
        read stays red until a person adds it".
        """
        from support import herdr_dispatch_derivation as derivation

        surface = _surface()
        table = set(derivation._MODELLED_ATTRIBUTE_READS)
        used = set(surface.used_read_exceptions)
        self.assertEqual(
            sorted(table - used),
            [],
            "these modelled-read exceptions are never consumed by the derivation; a "
            "stale entry pre-authorises a read that no longer happens",
        )
        self.assertEqual(
            sorted(used - table),
            [],
            "the run consumed an exception key that is not in the table",
        )

    def test_every_entry_names_an_attribute_the_class_really_has(self) -> None:
        """A typo key would otherwise wait in the table for a matching future read."""
        from support import herdr_dispatch_derivation as derivation

        index = derivation._index_package(derivation.default_source_root())
        walker = derivation._Walker(index)
        for class_name, attribute in derivation._MODELLED_ATTRIBUTE_READS:
            with self.subTest(cls=class_name, attr=attribute):
                owner = [
                    module for module, indexed in index.items()
                    if class_name in indexed.classes
                ][0]
                self.assertNotEqual(
                    walker._attribute_kind((owner, class_name), attribute),
                    "unknown",
                    f"{class_name}.{attribute} is not a member of that class",
                )

    def test_the_table_stays_small(self) -> None:
        """A drifting exception table is how a fail-closed guard becomes fail-open."""
        from support import herdr_dispatch_derivation as derivation

        self.assertLessEqual(
            len(derivation._MODELLED_ATTRIBUTE_READS),
            15,
            "the modelled-read exceptions are growing; each one is a place the walk stops "
            "asking questions, so a larger set needs a deliberate decision, not drift",
        )


class DerivationLivenessTests(unittest.TestCase):
    """The oracle must be falsifiable: prove it re-reads the source it is given.

    Every assertion above is of the form "the derived set matches the allowlist".  A
    walk that had stopped resolving anything would satisfy the ⊆ direction trivially, and
    a walk that returned a frozen constant would satisfy both.  These probes mutate a
    *copy* of the tree — never the live source — and require the mutation to show up.
    """

    def _probe_reports(self, surface, function_marker: str) -> list:
        """Everything the derivation said about an injected probe, in any channel.

        A shape may legitimately surface as a resolved pair, an unresolved site, or an
        unresolved flow; what must never happen is all three being empty.
        """
        return (
            [f for f in surface.unresolved_flows if function_marker in f]
            + [s.anchor for s in surface.unresolved_sites if function_marker in s.function]
            + [str(p) for p in surface.pairs if p[0] == "probe"]
        )

    def _mutated_tree(
        self,
        addition: str,
        runner_addition: str = "",
        init_addition: str = "",
        tail_addition: str = "",
        recorder_addition: str = "",
        recorder_replace: tuple = (),
    ) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="mozyo-derivation-probe-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        destination = tmp / "mozyo_bridge"
        shutil.copytree(
            ROOT / "src" / "mozyo_bridge",
            destination,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        seed = (
            destination
            / "e_140_adapter_provider"
            / "f_130_terminal_runtime_provider"
            / "application"
            / "disposable_herdr_instance.py"
        )
        source = seed.read_text(encoding="utf-8")
        marker = "    def _binding_env(self) -> dict[str, str]:"
        self.assertIn(marker, source, "the probe's injection point moved")
        if runner_addition:
            runner_marker = "    run = __call__"
            self.assertIn(runner_marker, source, "the runner injection point moved")
            source = source.replace(runner_marker, runner_addition, 1)
        if init_addition:
            init_marker = "        self.refusal_reasons: set = set()"
            self.assertIn(init_marker, source, "the runner __init__ injection point moved")
            source = source.replace(
                init_marker, init_marker + "\n" + init_addition, 1
            )
        if tail_addition:
            tail_marker = "    run = __call__"
            self.assertIn(tail_marker, source, "the module tail injection point moved")
            source = source.replace(tail_marker, tail_marker + tail_addition, 1)
        seed.write_text(source.replace(marker, addition + marker, 1), encoding="utf-8")
        if recorder_replace:
            recorder = seed.parent / "shared_space_smoke_observation.py"
            body = recorder.read_text(encoding="utf-8")
            self.assertIn(recorder_replace[0], body, "the recorder anchor moved")
            recorder.write_text(
                body.replace(recorder_replace[0], recorder_replace[1], 1),
                encoding="utf-8",
            )
        if recorder_addition:
            recorder = seed.parent / "shared_space_smoke_observation.py"
            body = recorder.read_text(encoding="utf-8")
            recorder_marker = "    run = __call__"
            self.assertIn(recorder_marker, body, "the recorder injection point moved")
            recorder.write_text(
                body.replace(recorder_marker, recorder_marker + recorder_addition, 1),
                encoding="utf-8",
            )
        return tmp

    def test_a_new_dispatch_site_appears_in_the_derivation(self) -> None:
        baseline = _surface()
        self.assertNotIn(("probe", "liveness"), baseline.pairs)
        tree = self._mutated_tree(
            "    def _probe_liveness(self):\n"
            '        return self.runner([self.binary, "probe", "liveness"])\n\n'
        )
        mutated = derive_dispatch_surface(tree)
        self.assertIn(
            ("probe", "liveness"),
            mutated.pairs,
            "the derivation did not notice a dispatch site added to the seed class, so "
            "it cannot be relied on to notice one added to production",
        )
        self.assertEqual(mutated.unresolved_sites, ())

    def test_an_unresolvable_argv_is_reported_rather_than_dropped(self) -> None:
        """The fail-closed property the ⊆ assertions rest on."""
        tree = self._mutated_tree(
            "    def _probe_unreadable(self, chosen):\n"
            "        return self.runner(chosen)\n\n"
        )
        mutated = derive_dispatch_surface(tree)
        # Anchored to the injected site: a bare "something was unresolved" would also
        # pass if an unrelated site had gone unreadable, which is the opposite of what
        # this probe claims to show.
        reported = [
            site
            for site in mutated.unresolved_sites
            if "_probe_unreadable" in site.function
        ]
        self.assertEqual(
            len(reported),
            1,
            f"a dispatch whose argv cannot be resolved was silently omitted; an "
            f"unreadable site must never read as no site. All unresolved: "
            f"{[s.anchor for s in mutated.unresolved_sites]}",
        )
        self.assertTrue(reported[0].unresolved_reason)
        self.assertIsNone(reported[0].pair)

    def test_a_runner_escaping_into_a_container_is_reported(self) -> None:
        """The other half of fail-closed: taint the walk cannot follow must be named.

        A dispatch site the walk resolves wrongly is loud; a runner that leaves through
        a container or a return value is silent, and silence is indistinguishable from
        "there was nothing there".  Each shape below is injected on its own so one of
        them cannot cover for the others.
        """
        shapes = {
            "List": "    def _stash(self):\n        return [self.runner]\n\n",
            "Assign": (
                "    def _stash(self):\n        d = {}\n"
                "        d['r'] = self.runner\n        return d\n\n"
            ),
            "Return": "    def _stash(self):\n        return self.runner\n\n",
        }
        for expected, injection in shapes.items():
            with self.subTest(shape=expected):
                tree = self._mutated_tree(injection)
                flows = [
                    flow
                    for flow in derive_dispatch_surface(tree).unresolved_flows
                    if "_stash" in flow
                ]
                self.assertTrue(
                    flows,
                    f"a runner leaving through {expected} was not reported; the walk "
                    f"would lose it and still call the surface complete",
                )
                self.assertIn(expected, flows[0])

    def test_an_unknown_runner_attribute_call_is_reported(self) -> None:
        """Review j#92123 F1(a), verdict j#92132.

        ``runner.execute(argv)`` was neither a dispatch (only ``run`` / ``__call__``
        count) nor an escape (any ``Attribute`` parent was treated as modelled), so it
        appeared in no output at all — the silent omission this whole check exists to
        prevent, sitting inside the check itself.
        """
        tree = self._mutated_tree(
            "    def _probe_attr(self):\n"
            '        return self.runner.execute([self.binary, "probe", "attribute-call"])\n\n'
        )
        surface = derive_dispatch_surface(tree)
        self.assertTrue(
            self._probe_reports(surface, "_probe_attr"),
            "an unknown attribute call on the gated runner was silently omitted",
        )

    def test_a_bound_method_alias_of_the_runner_is_reported(self) -> None:
        """Review j#92123 F1(b), verdict j#92132.

        Taking ``runner.run`` as a *value* drops the taint, so the later call through the
        alias was not recognised as a dispatch either.  ``run = __call__`` is exactly how
        both runners spell it, so this is the shape a refactor would actually produce.
        """
        tree = self._mutated_tree(
            "    def _probe_attr(self):\n"
            "        forward = self.runner.run\n"
            '        return forward([self.binary, "probe", "bound-alias"])\n\n'
        )
        surface = derive_dispatch_surface(tree)
        self.assertTrue(
            self._probe_reports(surface, "_probe_attr"),
            "a bound-method alias of the gated runner was silently omitted",
        )

    def test_a_benign_attribute_read_stays_modelled(self) -> None:
        """Control: the F1 fix must NARROW the model, not blanket-report every attribute.

        Without this, "report everything under an ``Attribute``" would pass both tests
        above while making the derivation useless — the counter reads that
        ``EndpointGateCounters.snapshot`` performs on the live tree are exactly the reads
        it must keep modelling.
        """
        tree = self._mutated_tree(
            "    def _probe_read(self):\n"
            "        return int(self.runner.escape_refusals)\n\n"
        )
        surface = derive_dispatch_surface(tree)
        self.assertEqual(
            self._probe_reports(surface, "_probe_read"),
            [],
            "a plain data-attribute read was reported; the model narrowed too far",
        )
        # And the real tree, which performs five such reads, stays fully readable.
        self.assertEqual(_surface().unresolved_flows, ())

    def test_a_declared_method_on_the_runner_is_analysed_not_assumed(self) -> None:
        """Review j#92165 F2(A), verdict j#92168.

        Admitting ``runner.execute(argv)`` because "the class declares that method and the
        walk analyses it" was a claim the walk did not honour: propagation only followed
        tainted *arguments*, so a call whose only tainted value was the receiver never
        bound anything and the body was never read.  The receiver is now bound to the
        callee's ``self``, which makes the claim true — so the injected method resolves as
        a real dispatch pair rather than merely being reported.
        """
        tree = self._mutated_tree(
            "    def _probe_declared(self):\n"
            '        return self.runner.execute([self.binary, "probe", "declared-method"])\n\n',
            runner_addition=(
                "    run = __call__\n\n"
                "    def execute(self, argv):\n"
                "        return self(argv)\n"
            ),
        )
        surface = derive_dispatch_surface(tree)
        self.assertIn(
            ("probe", "declared-method"),
            surface.pairs,
            "a declared method that dispatches through the receiver was not analysed",
        )

    def test_a_callable_data_attribute_of_the_runner_is_reported(self) -> None:
        """Review j#92165 F2(B), verdict j#92168.

        ``_inner`` is a plain instance attribute AND the ungated inner runner.  Reading it
        out and calling it bypasses the endpoint gate itself, so "it is a data member" was
        never evidence that the value cannot dispatch — three of this runner's data
        attributes hold callables.
        """
        tree = self._mutated_tree(
            "    def _probe_callable_data(self):\n"
            "        forward = self.runner._inner\n"
            '        return forward([self.binary, "probe", "callable-data"])\n\n'
        )
        surface = derive_dispatch_surface(tree)
        self.assertTrue(
            self._probe_reports(surface, "_probe_callable_data"),
            "reading the ungated inner runner out of the gate was silently omitted",
        )

    def test_a_with_binding_of_a_callable_attribute_is_still_reported(self) -> None:
        """The boundary of the one structural exemption.

        ``with self._lock:`` is modelled because the value is consumed and bound to
        nothing.  Adding an ``as`` target binds it, so the same shape must be reported —
        otherwise the exemption would be a hole shaped like a keyword.
        """
        tree = self._mutated_tree(
            "    def _probe_withas(self):\n"
            "        with self.runner._inner as forward:\n"
            '            return forward([self.binary, "probe", "with-as"])\n\n'
        )
        surface = derive_dispatch_surface(tree)
        self.assertTrue(
            self._probe_reports(surface, "_probe_withas"),
            "an ``as`` binding escaped through the with-consumption exemption",
        )

    def test_a_callable_carried_inside_a_container_is_reported(self) -> None:
        """Review j#92213 F3-1, verdict j#92219.

        A list is not callable; a list *holding* the inner runner still hands one out via
        a subscript.  "The outer node is not callable" was never a statement about the
        contents.
        """
        tree = self._mutated_tree(
            "    def _probe_container(self):\n"
            "        forward = self.runner._forwarders[0]\n"
            '        return forward([self.binary, "probe", "container-element"])\n\n',
            init_addition="        self._forwarders = [inner]",
        )
        surface = derive_dispatch_surface(tree)
        self.assertTrue(
            self._probe_reports(surface, "_probe_container"),
            "a dispatcher extracted from a container attribute was silently omitted",
        )

    def test_a_context_manager_on_a_runner_attribute_is_reported(self) -> None:
        """Review j#92213 F3-2, verdict j#92219.

        ``with <attr>:`` was exempted because the value is bound to nothing.  But ``with``
        *runs* ``__enter__`` / ``__exit__`` — not escaping and not executing are different
        claims, and only the first one was true.
        """
        tree = self._mutated_tree(
            "    def _probe_ctx(self):\n"
            "        with self.runner._ctx:\n"
            "            return None\n\n",
            init_addition="        self._ctx = _ProbeCtx(inner)",
            tail_addition=(
                "\n\nclass _ProbeCtx:\n"
                "    def __init__(self, inner):\n"
                "        self._inner = inner\n\n"
                "    def __enter__(self):\n"
                '        return self._inner(["bin", "probe", "with-enter"])\n\n'
                "    def __exit__(self, *exc):\n"
                "        return False\n"
            ),
        )
        surface = derive_dispatch_surface(tree)
        self.assertTrue(
            self._probe_reports(surface, "_probe_ctx"),
            "a context manager held on the runner was silently omitted",
        )

    def test_a_subclass_inheriting_call_is_treated_as_callable(self) -> None:
        """Review j#92213 F3-3, verdict j#92219.

        The subclass defines its own ``__init__`` deliberately: a subclass without one
        trips a *different* branch and would pass this test for the wrong reason.  What is
        under test is only whether callability follows base classes.
        """
        tree = self._mutated_tree(
            "    def _probe_inherit(self):\n"
            "        sub = _ProbeSub(self.runner, capability_provider=self._current_capability,\n"
            "                        binding_env={}, agent_env={})\n"
            '        return sub([self.binary, "probe", "inherited-wrapper"])\n\n',
            tail_addition=(
                "\n\nclass _ProbeSub(EndpointBoundHerdrRunner):\n"
                "    def __init__(self, inner, **kwargs):\n"
                "        super().__init__(inner, **kwargs)\n"
            ),
        )
        surface = derive_dispatch_surface(tree)
        self.assertIn(
            ("probe", "inherited-wrapper"),
            surface.pairs,
            "a wrapper inheriting __call__ was not recognised as a dispatcher",
        )

    def test_a_runner_stored_in_module_state_via_global_is_reported(self) -> None:
        """Review j#92266 F4, verdict j#92272.

        ``global X; X = self.runner`` looked like an ordinary local binding because the
        walk read the assignment target's node type and never read the scope declaration.
        The runner reached module state, another function dispatched through it, and all
        three channels stayed empty — the same shape as the original defect.
        """
        tree = self._mutated_tree(
            "    def _probe_global_carrier(self):\n"
            "        global _PROBE_GLOBAL_RUNNER\n"
            "        _PROBE_GLOBAL_RUNNER = self.runner\n"
            "        return _probe_global_dispatch()\n\n",
            tail_addition=(
                "\n\n_PROBE_GLOBAL_RUNNER = None\n\n\n"
                "def _probe_global_dispatch():\n"
                '    return _PROBE_GLOBAL_RUNNER(["bin", "probe", "global-carrier"])\n'
            ),
        )
        surface = derive_dispatch_surface(tree)
        self.assertTrue(
            self._probe_reports(surface, "_probe_global_carrier"),
            "a runner escaping into module state through `global` was silently omitted",
        )

    def test_a_runner_rebound_through_nonlocal_is_reported(self) -> None:
        """The other half of the same root cause.

        ``nonlocal`` previously "passed" only because the walk descends into nested
        function bodies and picked the assignment up as an outer local — scope blindness
        producing an accidental catch rather than a modelled one.  It is now reported
        explicitly, because rebinding an enclosing scope is not something this walk
        follows.
        """
        tree = self._mutated_tree(
            "    def _probe_nonlocal_carrier(self):\n"
            "        holder = None\n\n"
            "        def _inner_set():\n"
            "            nonlocal holder\n"
            "            holder = self.runner\n\n"
            "        _inner_set()\n"
            '        return holder([self.binary, "probe", "nonlocal-carrier"])\n\n'
        )
        surface = derive_dispatch_surface(tree)
        self.assertTrue(
            self._probe_reports(surface, "_probe_nonlocal_carrier"),
            "a runner rebound through `nonlocal` was silently omitted",
        )

    def test_a_truth_test_on_the_runner_is_analysed(self) -> None:
        """Review j#92326 F6-1, verdict j#92333.

        ``runner = runner or subprocess.run`` already exists in production
        (``herdr_session_start.py``).  ``BoolOp`` was on the modelled-parent list, so the
        position was waved through — yet ``or`` *runs* ``__bool__`` on the operand.  No
        call site has to change for that to dispatch; only the class does.

        The dunder is injected on ``RecordingHerdrRunner`` because that is the class the
        walk resolves at that site.  Using a class that does not flow there would pass for
        the wrong reason.
        """
        tree = self._mutated_tree(
            "",
            recorder_addition=(
                "\n\n    def __bool__(self):\n"
                '        self(["bin", "probe", "boolop-truth-test"])\n'
                "        return True\n"
            ),
        )
        surface = derive_dispatch_surface(tree)
        self.assertIn(
            ("probe", "boolop-truth-test"),
            surface.pairs,
            "a dispatching __bool__ at the existing production BoolOp was not analysed",
        )

    def test_the_runner_used_directly_as_a_context_manager_is_analysed(self) -> None:
        """Review j#92326 F6-2, verdict j#92333.

        R4 closed ``with runner._ctx:`` — a context held on an *attribute*.  The tainted
        value used directly as the context expression went through ``ast.withitem`` on the
        modelled-parent list instead, so ``__enter__`` ran unexamined.
        """
        tree = self._mutated_tree(
            "    def _probe_direct_with(self):\n"
            "        with self.runner:\n"
            "            return None\n\n",
            runner_addition=(
                "    run = __call__\n\n"
                "    def __enter__(self):\n"
                '        return self(["bin", "probe", "direct-with"])\n\n'
                "    def __exit__(self, *exc):\n"
                "        return False\n"
            ),
        )
        surface = derive_dispatch_surface(tree)
        self.assertIn(("probe", "direct-with"), surface.pairs)

    def test_a_conditional_expression_test_on_the_runner_is_analysed(self) -> None:
        """The third protocol position, closed by contract rather than by literal."""
        tree = self._mutated_tree(
            "    def _probe_ifexp(self):\n"
            "        return 1 if self.runner else 0\n\n",
            runner_addition=(
                "    run = __call__\n\n"
                "    def __bool__(self):\n"
                '        self(["bin", "probe", "ifexp-test"])\n'
                "        return True\n"
            ),
        )
        surface = derive_dispatch_surface(tree)
        self.assertIn(("probe", "ifexp-test"), surface.pairs)

    def test_a_protocol_position_without_the_dunder_stays_modelled(self) -> None:
        """Control, taken from production rather than invented.

        ``herdr_session_start`` really does contain ``runner = runner or
        subprocess.run``, and the class at that site defines no ``__bool__``.  The live
        derivation must therefore stay silent — otherwise the protocol contract has
        degenerated into reporting every carrier position.
        """
        production = (
            ROOT / "src" / "mozyo_bridge" / "e_140_adapter_provider"
            / "f_130_terminal_runtime_provider" / "application" / "herdr_session_start.py"
        )
        self.assertIn(
            "runner = runner or subprocess.run",
            production.read_text(encoding="utf-8"),
            "the production BoolOp this control depends on has moved",
        )
        self.assertEqual(_surface().unresolved_flows, ())

    def test_a_truth_test_falling_back_to_len_is_analysed(self) -> None:
        """Review j#92400 F7-1, verdict j#92403.

        Truth testing is a fallback chain, not one dunder: with no ``__bool__``, ``or``
        and ``if/else`` both call ``__len__``.  The previous round mapped those positions
        to ``("__bool__",)`` and asserted the mapping was complete.  The walk no longer
        names which dunder a position invokes — it binds every dunder the class declares —
        so it does not need to be right about the interpreter's choice.
        """
        tree = self._mutated_tree(
            "",
            recorder_addition=(
                "\n\n    def __len__(self):\n"
                '        self(["bin", "probe", "len-fallback"])\n'
                "        return 1\n"
            ),
        )
        self.assertIn(("probe", "len-fallback"), derive_dispatch_surface(tree).pairs)

    def test_an_assignment_target_hook_receiving_the_runner_is_analysed(self) -> None:
        """Review j#92400 F7-2, verdict j#92403.

        ``self._inner = inner`` runs ``__setattr__`` on the *target*, which receives the
        runner as an argument.  The R6 audit asked only what runs *on the value* and
        recorded "nothing" for assignment — true, and beside the point.  Construction
        alone dispatches here.
        """
        tree = self._mutated_tree(
            "",
            recorder_addition=(
                "\n\n    def __setattr__(self, name, value):\n"
                '        if name == "_inner" and callable(value):\n'
                '            value(["bin", "probe", "setattr-target"])\n'
                "        object.__setattr__(self, name, value)\n"
            ),
        )
        self.assertIn(("probe", "setattr-target"), derive_dispatch_surface(tree).pairs)

    def test_an_async_context_protocol_is_analysed(self) -> None:
        """A residual the reviewer named but did not push to a mutant (j#92400).

        The use site is a real ``async def`` + ``async with``.  The first version of this
        test injected ``async def __aenter__`` and then used a plain ``with``, so its name
        claimed more than it exercised — review j#92480 was right to call that out, and
        the evidence it produced was reported as stronger than it was.
        """
        tree = self._mutated_tree(
            "    async def _probe_async_ctx(self):\n"
            "        async with self.runner:\n"
            "            return None\n\n",
            runner_addition=(
                "    run = __call__\n\n"
                "    async def __aenter__(self):\n"
                '        return self(["bin", "probe", "aenter"])\n\n'
                "    async def __aexit__(self, *exc):\n"
                "        return False\n"
            ),
        )
        self.assertIn(("probe", "aenter"), derive_dispatch_surface(tree).pairs)

    def test_a_data_descriptor_set_hook_is_analysed(self) -> None:
        """Review j#92480 F8-1, verdict j#92484.

        A data descriptor's ``__set__`` lives on the type of the object the class-level
        attribute holds — not on the owner, which is where the previous round looked.
        The descriptor is installed as the real ``_inner``, so the existing
        ``self._inner = inner`` triggers it.
        """
        tree = self._mutated_tree(
            "",
            recorder_replace=(
                "class RecordingHerdrRunner:",
                'class _ProbeDescriptor:\n'
                "    def __set__(self, obj, value):\n"
                "        if callable(value):\n"
                '            value(["bin", "probe", "descriptor-set"])\n'
                '        obj.__dict__["_inner"] = value\n\n\n'
                "class RecordingHerdrRunner:\n"
                "    _inner = _ProbeDescriptor()\n",
            ),
        )
        self.assertIn(("probe", "descriptor-set"), derive_dispatch_surface(tree).pairs)

    def test_an_unresolvable_base_is_not_read_as_having_no_members(self) -> None:
        """Review j#92480 F8-2, verdict j#92484.

        ``_base_classes`` already reported ``saw_unresolved`` and the member scan threw it
        away, so a base the walk cannot read looked like a base with nothing in it.  The
        previous review had asked for exactly this and I recorded it as a residual instead
        of closing it.
        """
        tree = self._mutated_tree(
            "",
            recorder_replace=(
                "class RecordingHerdrRunner:",
                "from probe_external_base import _ProbeExternalBase  # noqa: E402\n\n\n"
                "class RecordingHerdrRunner(_ProbeExternalBase):",
            ),
        )
        surface = derive_dispatch_surface(tree)
        self.assertTrue(
            surface.unresolved_flows or surface.unresolved_sites,
            "an unreadable member surface was reported as an empty one",
        )

    def test_a_dunder_declared_by_alias_is_analysed(self) -> None:
        """Review j#92480 F8-3, verdict j#92484.

        ``__len__ = _probe_truth`` declares the dunder as surely as a ``def`` does.  The
        previous round claimed to bind "every dunder the class declares" while collecting
        only ``def`` forms — the claim was false inside a resolved first-party class.
        """
        tree = self._mutated_tree(
            "",
            recorder_addition=(
                "\n\n    def _probe_truth(self):\n"
                '        self(["bin", "probe", "dunder-alias"])\n'
                "        return 1\n\n"
                "    __len__ = _probe_truth\n"
            ),
        )
        self.assertIn(("probe", "dunder-alias"), derive_dispatch_surface(tree).pairs)

    def test_the_probe_does_not_touch_the_live_source(self) -> None:
        """Probe hygiene: mutating the copy must leave the real tree byte-identical.

        Asserted on the bytes of the live seed module, before and after, because that is
        the actual claim.  Checking that the injected pair is absent from the live
        derivation would pass even if the probe HAD written to ``src`` — it would only
        catch the one mutation that happens to emit that pair.
        """
        live = (
            ROOT
            / "src"
            / "mozyo_bridge"
            / "e_140_adapter_provider"
            / "f_130_terminal_runtime_provider"
            / "application"
            / "disposable_herdr_instance.py"
        )
        before = live.read_bytes()
        self._mutated_tree(
            '    def _probe_hygiene(self):\n'
            '        return self.runner([self.binary, "probe", "hygiene"])\n\n'
        )
        self.assertEqual(live.read_bytes(), before, "the probe wrote to the live source")
        self.assertNotIn(("probe", "hygiene"), _surface().pairs)


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class _Process:
    """The owned server child handle: alive until the lifecycle stops it."""

    def __init__(self) -> None:
        self.pid = os.getpid()
        self._returncode = None

    def poll(self):
        return self._returncode

    def wait(self, timeout=None):
        self._returncode = 0
        return 0

    def terminate(self) -> None:
        self._returncode = 0

    def kill(self) -> None:
        self._returncode = -9


class LauncherPreflightReachesTheRunnerTests(unittest.TestCase):
    """Drive the REAL production preflight through the REAL gate (#14185 R3 repro).

    No Herdr, no launcher process: the inner runner is a fake, and the "launcher" is an
    executable stub that only has to exist for ``resolve_attest_launcher``.  What is real
    is the call site — ``preflight_attest_launcher_capability`` builds the probe argv the
    way production does and hands it to the gate.
    """

    def _instance(self, tmp: Path, dispatched: list) -> DisposableHerdrInstance:
        instance = DisposableHerdrInstance(
            binary="/bin/true",
            root=tmp / "instance",
            base_env={"HOME": str(tmp / "operator")},
            runner=lambda argv, **kwargs: dispatched.append(list(argv))
            or subprocess.CompletedProcess(list(argv), 0, "[]", ""),
            popen_factory=lambda argv, **kwargs: _Process(),
            sleeper=lambda _seconds: None,
            ambient_env={},
        )
        instance.start()
        self.addCleanup(instance.shutdown)
        dispatched.clear()
        return instance

    def _run_preflight(self, instance, launcher: Path, tmp: Path):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
            preflight_attest_launcher_capability,
        )

        return preflight_attest_launcher_capability(
            str(launcher),
            instance.runner,
            5.0,
            {"PATH": "/usr/bin:/bin"},
            repo_root=tmp,
        )

    def test_the_probe_reaches_the_inner_runner(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
            HerdrLauncherIncompatibleError,
        )

        dispatched: list = []
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            instance = self._instance(tmp, dispatched)
            launcher = _executable(tmp / "fake-mozyo-bridge")
            # The capability verdict is decided elsewhere and this stub launcher cannot
            # satisfy it, so the preflight must end in the CAPABILITY verdict — not in a
            # gate refusal.  Asserting the specific type is the point: a bare
            # ``assertRaises(Exception)`` would also pass on the refusal this test exists
            # to rule out.
            with self.assertRaises(HerdrLauncherIncompatibleError):
                self._run_preflight(instance, launcher, tmp)
            self.assertEqual(
                [argv[1:3] for argv in dispatched],
                [["herdr", "agent-attest"]],
                "the production launcher preflight probe was not dispatched",
            )
            self.assertEqual(instance.runner.escape_refusals, 0)
            self.assertEqual(instance.runner.operator_endpoint_requests, 0)

    def test_the_pre_14658_allowlist_reproduces_the_live_refusal(self) -> None:
        """Mutation probe: without the pair, the same call is refused with 0 dispatch."""
        dispatched: list = []
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            instance = self._instance(tmp, dispatched)
            launcher = _executable(tmp / "fake-mozyo-bridge")
            without_the_pair = frozenset(
                set(CLIENT_CALL_SUBCOMMANDS) - {ATTEST_PROBE_PAIR}
            )
            with mock.patch.object(
                live_module, "CLIENT_CALL_SUBCOMMANDS", without_the_pair
            ):
                with self.assertRaises(SmokeEndpointEscapeError) as caught:
                    self._run_preflight(instance, launcher, tmp)
            self.assertEqual(
                caught.exception.reason,
                REFUSAL_COMMAND_NOT_ALLOWLISTED,
                "this is the refusal #14185 R3 recorded in j#91992",
            )
            self.assertEqual(
                dispatched, [], "a refused probe must make zero external requests"
            )
            self.assertEqual(instance.runner.escape_refusals, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
