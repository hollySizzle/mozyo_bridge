"""Full-surface adversarial edge sweep of the dispatch derivation (escalation j#92214).

Two blocking findings in consecutive rounds on the same subsystem (j#92165 F2, j#92213 F3)
escalated review to ``full_surface_adversarial``.  This is the executable form of that
sweep: one mutant per edge of the escalation's ``required_surface``, each injected alone
into a COPY of the tree, asserting the derivation says *something* — a resolved pair, an
unresolved site, or an unresolved flow.  Silence is the failure mode the whole subsystem
exists to prevent.

The controls are load-bearing: a derivation that reported everything would satisfy every
mutant while being useless, so a benign counter read, a probe-free tree, and each class
construct the walk claims to model must stay silent.

**Opt-in.**  53 tree copies and 53 full derivations run for about seven minutes, which does
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
    # -- implicit protocol positions (review j#92326 F6) ---------------------------------
    # These positions do not merely carry the value, they RUN a protocol on it.  Kept
    # distinct from the carrier edges above: "where the value goes" and "what the syntax
    # executes on it" are different questions, and answering both with one node-type
    # allowlist is what F3-2 and F6 both came from.
    # The dunder is injected INSIDE the runner class (the injection must start with the
    # `run = __call__` marker, or it dedents mid-class and the module stops parsing).
    ("protocol: direct `with <runner>` (__enter__)", None,
     TAIL + '\n\n    def __enter__(self):\n        return self(["bin", "probe", "direct-with"])\n\n    def __exit__(self, *exc):\n        return False\n',
     '    def _p(self):\n        with self.runner:\n            return None\n\n', True),
    ("protocol: IfExp test (__bool__)", None,
     TAIL + '\n\n    def __bool__(self):\n        self(["bin", "probe", "ifexp-test"])\n        return True\n',
     '    def _p(self):\n        return 1 if self.runner else 0\n\n', True),
    ("protocol: carrier position without the dunder stays silent", None, None,
     '    def _p(self):\n        chosen = self.runner if self.binary else self.runner\n        return chosen([self.binary, "probe", "ifexp-branch"])\n\n', True),
    ("protocol: truth fallback to __len__", None,
     TAIL + '\n\n    def __len__(self):\n        self(["bin", "probe", "len-fallback"])\n        return 1\n',
     '    def _p(self):\n        r = self.runner or None\n        return 1 if r else 0\n\n', True),
    ("protocol: __aenter__ reached by static over-analysis", None,
     TAIL + '\n\n    async def __aenter__(self):\n        return self(["bin", "probe", "aenter"])\n\n    async def __aexit__(self, *e):\n        return False\n',
     '    def _p(self):\n        with self.runner:\n            return None\n\n', True),
    # The hook must live on the class that OWNS the assignment target.  The first version
    # of this edge put it on the runner class while assigning to the instance, so nothing
    # was reported — and the sweep caught that, which is the point of running it.
    ("target hook: __setattr__ receives the runner", None, None,
     '    def __setattr__(self, name, value):\n'
     '        if name == "_probe_slot" and callable(value):\n'
     '            value(["bin", "probe", "setattr-target"])\n'
     '        object.__setattr__(self, name, value)\n\n'
     '    def _p(self):\n        self._probe_slot = self.runner\n        return None\n\n', True),
    # -- semantic class construction (review j#92480 F8) ---------------------------------
    ("declaration: dunder declared by class-body alias", None,
     TAIL + '\n\n    def _probe_truth(self):\n        self(["bin", "probe", "dunder-alias"])\n        return 1\n\n    __len__ = _probe_truth\n',
     '    def _p(self):\n        return 1 if self.runner else 0\n\n', True),
    ("protocol: REAL async with (async def + async with)", None,
     TAIL + '\n\n    async def __aenter__(self):\n        return self(["bin", "probe", "real-async-with"])\n\n    async def __aexit__(self, *e):\n        return False\n',
     '    async def _p(self):\n        async with self.runner:\n            return None\n\n', True),
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

RUNNER_CLASS = "class EndpointBoundHerdrRunner:"
RECORDER_CLASS = "class RecordingHerdrRunner:"
RECORDER = SEED.rsplit("/", 1)[0] + "/shared_space_smoke_observation.py"

# -- class CONSTRUCTION surface (review j#92541 F9) -------------------------------------
# These rewrite the class statement itself, which the injections above cannot reach: a
# metaclass or a class decorator puts members on the class without appearing in its body,
# and a decorated ``def`` is not the function that ends up bound.  Each expects a REPORT,
# because the walk models none of them; the controls below expect silence, because the
# walk does model those and an inversion that refuses everything would prove nothing.
#
# The anchor names WHICH runner class the row rewrites, and that choice is load-bearing:
# the descriptor row has to sit on ``RecordingHerdrRunner``, because that is the class
# whose ``self._inner = inner`` carries a tainted value.  Siting it on the seed class made
# it silent — correctly, since that ``__init__`` parameter is not on a tainted path — and
# the sweep caught the mis-siting, which is what the sweep is for.
#
# (label, anchor, class-statement replacement, owner-method injection, expect)
CLASS_CONSTRUCTION_EDGES = [
    ("construction: metaclass injects a dunder", RUNNER_CLASS,
     "class _ProbeMeta(type):\n"
     "    def __new__(mcls, name, bases, ns):\n"
     "        def _injected(self):\n"
     '            self(["bin", "probe", "metaclass-len"])\n'
     "            return 1\n"
     '        ns["__len__"] = _injected\n'
     "        return super().__new__(mcls, name, bases, ns)\n\n\n"
     "class EndpointBoundHerdrRunner(metaclass=_ProbeMeta):",
     '    def _p(self):\n        return 1 if self.runner else 0\n\n', True),
    ("construction: class decorator rewrites the members", RUNNER_CLASS,
     "def _probe_class_decorator(cls):\n"
     "    def _injected(self):\n"
     '        self(["bin", "probe", "class-decorator-len"])\n'
     "        return 1\n"
     "    cls.__len__ = _injected\n"
     "    return cls\n\n\n"
     "@_probe_class_decorator\n"
     "class EndpointBoundHerdrRunner:",
     '    def _p(self):\n        return 1 if self.runner else 0\n\n', True),
    ("construction: decorated dunder is not its raw body", RUNNER_CLASS,
     "def _probe_replace(fn):\n"
     "    def _wrapper(self):\n"
     '        self(["bin", "probe", "decorated-len"])\n'
     "        return 1\n"
     "    return _wrapper\n\n\n"
     "class EndpointBoundHerdrRunner:\n"
     "    @_probe_replace\n"
     "    def __len__(self):\n"
     "        return 0\n",
     '    def _p(self):\n        return 1 if self.runner else 0\n\n', True),
    ("construction: annotated descriptor __set__ is analysed", RECORDER_CLASS,
     "class _ProbeDescriptor:\n"
     "    def __set__(self, obj, value):\n"
     "        if callable(value):\n"
     '            value(["bin", "probe", "annotated-descriptor-set"])\n'
     '        obj.__dict__["_inner"] = value\n\n\n'
     "class RecordingHerdrRunner:\n"
     "    _inner: _ProbeDescriptor = _ProbeDescriptor()\n",
     '    def _p(self):\n        return None\n\n', True),
    ("construction: conditional class-body binding", RUNNER_CLASS,
     "class EndpointBoundHerdrRunner:\n"
     "    if True:\n"
     "        def __len__(self):\n"
     '            self(["bin", "probe", "conditional-len"])\n'
     "            return 1\n",
     '    def _p(self):\n        return 1 if self.runner else 0\n\n', True),
    ("CONTROL construction: plain direct-def dunder", RUNNER_CLASS,
     "class EndpointBoundHerdrRunner:\n"
     "    def __len__(self):\n"
     "        return 0\n",
     '    def _p(self):\n        return None\n\n', False),
    ("CONTROL construction: modelled member decorator", RUNNER_CLASS,
     "class EndpointBoundHerdrRunner:\n"
     "    @property\n"
     "    def probe_reading(self):\n"
     "        return 0\n",
     '    def _p(self):\n        return None\n\n', False),
    ("CONTROL construction: annotated class constant", RUNNER_CLASS,
     "class EndpointBoundHerdrRunner:\n"
     "    probe_slot: int = 0\n",
     '    def _p(self):\n        return None\n\n', False),
]

# -- class BINDING resolution (review j#92639 F10) --------------------------------------
# R9 closed the set of statement node types; these close the inside of the statements it
# admitted, the writes that happen after the class statement, and the one decorator still
# called modelled.  Every row here is ordinary Python, not an exotic construct.
#
# (label, recorder-body injection, recorder-tail injection, expect)
CLASS_BINDING_EDGES = [
    ("binding: annotated dunder alias",
     '\n\n    def _probe_ann_len(self):\n        self(["bin", "probe", "annotated-dunder"])\n'
     "        return 1\n\n    __len__: object = _probe_ann_len\n", None, True),
    ("binding: unpacking target",
     '\n\n    def _probe_unpack_len(self):\n        self(["bin", "probe", "unpacked-dunder"])\n'
     "        return 1\n\n    (__len__,) = (_probe_unpack_len,)\n", None, True),
    ("binding: right-hand side writes the namespace",
     '\n\n    def _probe_side_len(self):\n        self(["bin", "probe", "assign-side-effect"])\n'
     '        return 1\n\n    _installed = locals().__setitem__("__len__", _probe_side_len)\n',
     None, True),
    ("mutation: member assigned after the class statement", None,
     '\n\ndef _probe_post_len(self):\n    self(["bin", "probe", "post-class-mutation"])\n'
     "    return 1\n\n\nRecordingHerdrRunner.__len__ = _probe_post_len\n", True),
    ("mutation: setattr spelling", None,
     '\n\ndef _probe_setattr_len(self):\n    self(["bin", "probe", "setattr-mutation"])\n'
     '    return 1\n\n\nsetattr(RecordingHerdrRunner, "__len__", _probe_setattr_len)\n', True),
    ("CONTROL binding: plain alias",
     "\n\n    def _probe_plain(self):\n        return 0\n\n    probe_alias = _probe_plain\n",
     None, False),
    ("CONTROL binding: annotated constant", "\n\n    probe_slot: int = 0\n", None, False),
    ("CONTROL binding: bare annotation", "\n\n    probe_unbound: int\n", None, False),
]

# -- class OBJECT escape (review j#92902 F11) -------------------------------------------
# These are not eight more spellings.  The three mutation rows all pass for one reason —
# the class object left the two positions the walk analyses — and a fourth spelling nobody
# has written down is covered by that same rule.  The controls pin the two modelled
# positions, without which the rule would just mean "everything is unreadable".
CLASS_ESCAPE_EDGES = [
    ("escape: type.__setattr__", None,
     '\n\ndef _probe_type_setattr_len(self):\n    self(["bin", "probe", "type-setattr"])\n'
     '    return 1\n\n\ntype.__setattr__(RecordingHerdrRunner, "__len__", _probe_type_setattr_len)\n',
     True),
    ("escape: alias binding", None,
     '\n\ndef _probe_alias_len(self):\n    self(["bin", "probe", "alias-mutation"])\n'
     "    return 1\n\n\n_ProbeAlias = RecordingHerdrRunner\n_ProbeAlias.__len__ = _probe_alias_len\n",
     True),
    ("escape: passed to a helper", None,
     '\n\ndef _probe_helper_len(self):\n    self(["bin", "probe", "helper-mutation"])\n'
     "    return 1\n\n\ndef _probe_install(target):\n    target.__len__ = _probe_helper_len\n\n\n"
     "_probe_install(RecordingHerdrRunner)\n", True),
    ("construction: class-body constructor side effect",
     "\n\n    _installed = _ProbeInstaller()\n", None, True),
    ("CONTROL escape: construction callee stays modelled", None,
     "\n\n_probe_constructed = RecordingHerdrRunner(None)\n", False),
]

# Injected ahead of the recorder class so the constructor-side-effect row can name it.
INSTALLER = (
    "import sys as _probe_sys\n\n\n"
    "class _ProbeInstaller:\n"
    "    def __init__(self):\n"
    "        def _injected(self):\n"
    '            self(["bin", "probe", "constructor-side-effect"])\n'
    "            return 1\n"
    '        _probe_sys._getframe(1).f_locals["__len__"] = _injected\n\n\n'
    "class RecordingHerdrRunner:"
)

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




def run_binding_edge(body_inj, tail_inj, class_replace=None):
    """Inject a class binding (or a post-definition write) into the recorder module.

    Reported is measured over the whole surface, as in :func:`run_class_edge`: a binding
    defect surfaces where the class is used, which is production code here.
    """
    tmp = Path(tempfile.mkdtemp(prefix="mozyo-binding-edge-"))
    try:
        shutil.copytree(ROOT / "src" / "mozyo_bridge", tmp / "mozyo_bridge",
                        ignore=shutil.ignore_patterns("__pycache__"))
        recorder = tmp / RECORDER
        s = recorder.read_text()
        if class_replace:
            assert RECORDER_CLASS in s, "the recorder class statement moved"
            s = s.replace(RECORDER_CLASS, class_replace, 1)
        if body_inj:
            assert TAIL in s, "the recorder body injection point moved"
            s = s.replace(TAIL, TAIL + body_inj, 1)
        if tail_inj:
            s = s + tail_inj
        recorder.write_text(s)
        d = derive_dispatch_surface(tmp)
        pairs = [p for p in d.pairs if p[0] == "probe"]
        return bool(pairs or d.unresolved_sites or d.unresolved_flows), pairs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_class_edge(anchor, class_stmt, owner_inj):
    """Like :func:`run_edge`, but rewrites the class statement itself.

    "Reported" is measured against the whole surface rather than filtered to probe-named
    functions, because a class-construction defect surfaces where the class is *used* —
    the production truth test in ``_prepare_session_locked`` — and not in the injected
    method.  The live tree derives zero unresolved sites and zero unresolved flows, so any
    non-zero count here is caused by the mutation.
    """
    tmp = Path(tempfile.mkdtemp(prefix="mozyo-class-edge-"))
    try:
        shutil.copytree(ROOT / "src" / "mozyo_bridge", tmp / "mozyo_bridge",
                        ignore=shutil.ignore_patterns("__pycache__"))
        target = tmp / (SEED if anchor == RUNNER_CLASS else RECORDER)
        s = target.read_text()
        assert anchor in s, f"the class statement moved: {anchor}"
        target.write_text(s.replace(anchor, class_stmt, 1))
        seed = tmp / SEED
        body = seed.read_text()
        seed.write_text(body.replace(OWNER, owner_inj + OWNER, 1))
        d = derive_dispatch_surface(tmp)
        pairs = [p for p in d.pairs if p[0] == "probe"]
        return bool(pairs or d.unresolved_sites or d.unresolved_flows), pairs
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
        for label, anchor, class_stmt, owner_inj, expect in CLASS_CONSTRUCTION_EDGES:
            got, _pairs = run_class_edge(anchor, class_stmt, owner_inj)
            if got != expect:
                mismatches.append(f"{label}: expected reported={expect}, got {got}")
        for label, body_inj, tail_inj, expect in CLASS_BINDING_EDGES:
            got, _pairs = run_binding_edge(body_inj, tail_inj)
            if got != expect:
                mismatches.append(f"{label}: expected reported={expect}, got {got}")
        for label, body_inj, tail_inj, expect in CLASS_ESCAPE_EDGES:
            needs_installer = body_inj is not None and "_ProbeInstaller" in body_inj
            got, _pairs = run_binding_edge(
                body_inj, tail_inj, class_replace=INSTALLER if needs_installer else None
            )
            if got != expect:
                mismatches.append(f"{label}: expected reported={expect}, got {got}")
        self.assertEqual(mismatches, [], "\n".join(mismatches))
        self.assertGreaterEqual(
            len(rows)
            + len(CLASS_CONSTRUCTION_EDGES)
            + len(CLASS_BINDING_EDGES)
            + len(CLASS_ESCAPE_EDGES),
            53,
            "the sweep lost edges",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
