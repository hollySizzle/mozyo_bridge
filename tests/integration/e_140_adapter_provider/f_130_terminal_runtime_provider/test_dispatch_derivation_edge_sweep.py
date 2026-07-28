"""Full-surface adversarial edge sweep of the dispatch derivation (escalation j#92214).

Two blocking findings in consecutive rounds on the same subsystem (j#92165 F2, j#92213 F3)
escalated review to ``full_surface_adversarial``.  This is the executable form of that
sweep: one mutant per edge of the escalation's ``required_surface``, each injected alone
into a COPY of the tree, asserting the derivation says *something* — a resolved pair, an
unresolved site, or an unresolved flow.  Silence is the failure mode the whole subsystem
exists to prevent.

The two controls are load-bearing: a derivation that reported everything would satisfy
every mutant while being useless, so a benign counter read and a probe-free tree must stay
silent.

**Opt-in.**  23 tree copies and 23 full derivations run for about two minutes, which does
not belong in every ``unittest discover``.  Set ``MOZYO_DERIVATION_EDGE_SWEEP=1`` to run
it.  The per-edge regressions that must never regress silently live in
``tests/unit/.../test_disposable_smoke_command_surface.py`` and run unconditionally.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from support.herdr_dispatch_derivation import derive_dispatch_surface  # noqa: E402

OPT_IN = "MOZYO_DERIVATION_EDGE_SWEEP"

SEED = (
    "mozyo_bridge/e_140_adapter_provider/f_130_terminal_runtime_provider"
    "/application/disposable_herdr_instance.py"
)
OWNER = "    def _binding_env(self) -> dict[str, str]:"
TAIL = "    run = __call__"
INIT = "        self.refusal_reasons: set = set()"

CTX = '''

class _ProbeCtx:
    def __init__(self, inner):
        self._inner = inner

    def __enter__(self):
        return self._inner(["bin", "probe", "with-enter"])

    def __exit__(self, *exc):
        return False
'''

SUB = '''

class _ProbeSub(EndpointBoundHerdrRunner):
    def __init__(self, inner, **kwargs):
        super().__init__(inner, **kwargs)
'''

# (label, runner-__init__ injection, module-tail injection, owner-method injection, expect)
EDGES = [
    # -- taint seed -------------------------------------------------------------
    ("seed: direct call on the seed attribute", None, None,
     '    def _p(self):\n        return self.runner([self.binary, "probe", "seed"])\n\n', True),
    # -- local / parameter / attribute propagation --------------------------------
    ("propagation: local rebind", None, None,
     '    def _p(self):\n        r = self.runner\n        return r([self.binary, "probe", "local"])\n\n', True),
    ("propagation: through a helper parameter", None,
     "\n\ndef _probe_helper(runner, argv):\n    return runner(argv)\n",
     '    def _p(self):\n        return _probe_helper(self.runner, [self.binary, "probe", "param"])\n\n', True),
    ("propagation: **kwargs dict forwarding", None,
     "\n\ndef _probe_kw(runner=None, argv=None):\n    return runner(argv)\n",
     '    def _p(self):\n        call = dict(runner=self.runner, argv=[self.binary, "probe", "kw"])\n        return _probe_kw(**call)\n\n', True),
    # -- callable wrapper construction / inheritance ------------------------------
    ("wrapper: constructed callable wrapper", None,
     "\n\nclass _ProbeWrap:\n    def __init__(self, inner):\n        self._inner = inner\n\n    def __call__(self, argv):\n        return self._inner(argv)\n",
     '    def _p(self):\n        w = _ProbeWrap(self.runner)\n        return w([self.binary, "probe", "wrapper"])\n\n', True),
    ("inheritance: subclass inherits __call__", None, SUB,
     '    def _p(self):\n        sub = _ProbeSub(self.runner, capability_provider=self._current_capability,\n                       binding_env={}, agent_env={})\n        return sub([self.binary, "probe", "inherited"])\n\n', True),
    # -- attribute call / read ----------------------------------------------------
    ("attribute call: unknown method", None, None,
     '    def _p(self):\n        return self.runner.execute([self.binary, "probe", "unknown"])\n\n', True),
    ("attribute call: declared method dispatching", None,
     None, None, None),  # placed below via runner-tail variant
    ("attribute read: bound method alias", None, None,
     '    def _p(self):\n        f = self.runner.run\n        return f([self.binary, "probe", "alias"])\n\n', True),
    ("attribute read: callable data (_inner)", None, None,
     '    def _p(self):\n        f = self.runner._inner\n        return f([self.binary, "probe", "inner"])\n\n', True),
    ("attribute read: undeclared new attribute", INIT + "\n        self._novel = inner", None,
     '    def _p(self):\n        f = self.runner._novel\n        return f([self.binary, "probe", "novel"])\n\n', True),
    # -- callable carrier container ------------------------------------------------
    ("container: list element extraction", INIT + "\n        self._forwarders = [inner]", None,
     '    def _p(self):\n        f = self.runner._forwarders[0]\n        return f([self.binary, "probe", "container"])\n\n', True),
    ("container: dict value extraction", INIT + '\n        self._by_name = {"i": inner}', None,
     '    def _p(self):\n        f = self.runner._by_name["i"]\n        return f([self.binary, "probe", "dict"])\n\n', True),
    # -- context protocol ------------------------------------------------------------
    ("context: with on a runner attribute", INIT + "\n        self._ctx = _ProbeCtx(inner)", CTX,
     '    def _p(self):\n        with self.runner._ctx:\n            return None\n\n', True),
    ("context: with ... as binding", None, None,
     '    def _p(self):\n        with self.runner._inner as f:\n            return f([self.binary, "probe", "withas"])\n\n', True),
    # -- escapes ----------------------------------------------------------------------
    ("escape: return the runner", None, None,
     '    def _p(self):\n        return self.runner\n\n', True),
    ("escape: container literal", None, None,
     '    def _p(self):\n        return [self.runner]\n\n', True),
    ("escape: subscript store", None, None,
     '    def _p(self, d):\n        d[0] = self.runner\n        return d\n\n', True),
    ("escape: comprehension", None, None,
     '    def _p(self):\n        return [r for r in [self.runner]]\n\n', True),
    ("escape: yield", None, None,
     '    def _p(self):\n        yield self.runner\n\n', True),
    # -- module / enclosing-scope carriers (review j#92266 F4) ---------------------------
    ("carrier: global module state", None,
     "\n\n_PROBE_GLOBAL_RUNNER = None\n\n\ndef _probe_global_dispatch():\n    return _PROBE_GLOBAL_RUNNER([\"bin\", \"probe\", \"global-carrier\"])\n",
     '    def _p(self):\n        global _PROBE_GLOBAL_RUNNER\n        _PROBE_GLOBAL_RUNNER = self.runner\n        return _probe_global_dispatch()\n\n', True),
    ("carrier: nonlocal rebinding", None, None,
     '    def _p(self):\n        holder = None\n\n        def _set():\n            nonlocal holder\n            holder = self.runner\n\n        _set()\n        return holder([self.binary, "probe", "nonlocal-carrier"])\n\n', True),
    # -- Process boundary (escalation j#92214 required_surface) ---------------------------
    # An INDEPENDENT mutant, not "the live tree happens to exercise it" — that substitute
    # is exactly what review j#92266 rejected.  Both directions are pinned: a worker that
    # dispatches must be found, and a worker whose argv cannot be resolved must be
    # reported rather than dropped at the fork boundary.
    ("process: worker dispatches through the forked runner", None,
     "\n\ndef _probe_worker(index, gate_runner):\n    return gate_runner([\"bin\", \"probe\", \"process-worker\"])\n",
     '    def _p(self):\n        import multiprocessing\n        ctx = multiprocessing.get_context("fork")\n        proc = ctx.Process(target=_probe_worker, args=(0, self.runner))\n        proc.start()\n        return proc\n\n', True),
    ("process: worker argv unresolvable", None,
     "\n\ndef _probe_worker_opaque(index, gate_runner, chosen):\n    return gate_runner(chosen)\n",
     '    def _p(self, chosen):\n        import multiprocessing\n        ctx = multiprocessing.get_context("fork")\n        proc = ctx.Process(target=_probe_worker_opaque, args=(0, self.runner, chosen))\n        proc.start()\n        return proc\n\n', True),
    # -- argv resolution ----------------------------------------------------------------
    ("argv: unresolvable argv", None, None,
     '    def _p(self, chosen):\n        return self.runner(chosen)\n\n', True),
    # -- CONTROLS (must stay silent) -----------------------------------------------------
    ("CONTROL: declared counter read", None, None,
     '    def _p(self):\n        return int(self.runner.escape_refusals)\n\n', False),
    ("CONTROL: no probe at all", None, None,
     '    def _p(self):\n        return None\n\n', False),
]

DECLARED_METHOD = (
    "attribute call: declared method dispatching",
    None,
    TAIL + "\n\n    def execute(self, argv):\n        return self(argv)\n",
    '    def _p(self):\n        return self.runner.execute([self.binary, "probe", "declared"])\n\n',
    True,
)


def run_edge(init_inj, tail_inj, owner_inj):
    tmp = Path(tempfile.mkdtemp(prefix="mozyo-edge-"))
    try:
        shutil.copytree(ROOT / "src" / "mozyo_bridge", tmp / "mozyo_bridge",
                        ignore=shutil.ignore_patterns("__pycache__"))
        seed = tmp / SEED
        s = seed.read_text()
        if init_inj:
            s = s.replace(INIT, init_inj, 1)
        if tail_inj:
            s = s.replace(TAIL, tail_inj if tail_inj.startswith(TAIL) else TAIL + tail_inj, 1)
        s = s.replace(OWNER, owner_inj + OWNER, 1)
        seed.write_text(s)
        d = derive_dispatch_surface(tmp)
        pairs = [p for p in d.pairs if p[0] == "probe"]
        sites = [x for x in d.unresolved_sites if "_p" == x.function.split(".")[-1] or "_Probe" in x.function]
        flows = [
            f for f in d.unresolved_flows
            if ":_p:" in f or "._p:" in f or "_Probe" in f or "_probe_" in f
        ]
        sites = sites + [
            x for x in d.unresolved_sites if "_probe_" in x.function
        ]
        return bool(pairs or sites or flows), pairs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)




@unittest.skipUnless(
    os.environ.get(OPT_IN) == "1",
    f"the full-surface edge sweep is opt-in; set {OPT_IN}=1 to run it",
)
class DerivationEdgeSweepTests(unittest.TestCase):
    def test_every_edge_of_the_required_surface_is_observed(self) -> None:
        rows = [e for e in EDGES if e[3] is not None]
        rows.insert(8, DECLARED_METHOD)
        mismatches = []
        for label, init_inj, tail_inj, owner_inj, expect in rows:
            got, _pairs = run_edge(init_inj, tail_inj, owner_inj)
            if got != expect:
                mismatches.append(f"{label}: expected reported={expect}, got {got}")
        self.assertEqual(mismatches, [], "\n".join(mismatches))
        self.assertGreaterEqual(len(rows), 24, "the sweep lost edges")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
