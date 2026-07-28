"""Machine derivation of the argv pairs the disposable smoke can dispatch (#14658).

Why this exists
---------------
:data:`...disposable_herdr_instance.CLIENT_CALL_SUBCOMMANDS` is an allowlist keyed on
``argv[1:3]``.  Its first version was assembled by reading the code by hand, and the
#14185 R3 live run proved that hand enumeration is not a method: the production
session-start path issues the launcher preflight probe ``<launcher> herdr agent-attest
--help``, the pair was never listed, and the gate fail-closed the run before a single
workspace existed (evidence j#91992).  The same hand pass *added* pairs no call site
emits.

Adding the one missing literal would leave the method unchanged, so this module replaces
the method.  It walks the first-party source, follows the gated runner from the one
object that mints it, and reports **every call site that can dispatch through it** plus
the ``(group, subcommand)`` pair each site emits.  The allowlist is then pinned against
that derivation in both directions, and drift on either side fails closed.

What it is, precisely
---------------------
A small inter-procedural taint walk over ``ast``:

* the **seed** is the runner attribute :class:`DisposableHerdrInstance` exposes — the
  object the endpoint gate wraps.  Nothing else is a seed, and
  :func:`derive_seed_flows` re-measures where that object is handed on, so a new
  consumer in the smoke driver cannot be silently absorbed;
* taint propagates through parameters, local bindings, ``with`` bindings, wrapper
  constructors (``RecordingHerdrRunner(runner)``) and instance attributes assigned in
  ``__init__``;
* **calling** a tainted value is a dispatch site.  Its argv expression is resolved to a
  head sequence — list literals, locally-bound lists, starred module constants, and
  values that arrive as a parameter (resolved at every caller).

Every step is fail-closed.  A tainted value the walk cannot follow, or an argv
expression it cannot resolve, is reported as an unresolved finding rather than dropped —
an unreadable site must never read as "no site there".  The walk deliberately knows only
the shapes this code base actually uses; a refactor into an unrecognised shape turns the
oracle red instead of quietly narrowing it.

That claim was once overstated in one specific place, and the correction is worth stating
because it is the same defect this module exists to remove (review j#92123 F1, verdict
j#92132).  The escape check listed ``ast.Attribute`` as a modelled parent *wholesale*
while :meth:`_Walker._is_dispatch` recognised only ``run`` / ``__call__``.  Between those
two, ``runner.execute(argv)`` and ``forward = runner.run`` were neither a dispatch nor an
escape — they appeared nowhere.  A guard against enumeration gaps had an enumeration gap
in its own allow-list.  The correction was then made twice, because the first
attempt replaced one proxy with another (review j#92165 F2, verdict j#92168): judging the
access by *member kind* — "a data member or a property is safe to read" — is no more a
proof than judging it by node type was.  Three of ``EndpointBoundHerdrRunner``'s own data
attributes hold callables, and one of them, ``_inner``, is the **ungated inner runner**:
reading it out and calling it bypasses the endpoint gate entirely.  And admitting a call
because "the class declares that method and the walk analyses it" was a claim the walk did
not honour, since propagation followed tainted arguments only and a receiver-only call
bound nothing.

That correction was itself wrong in three further ways (review j#92213): a container is
not callable but *holds* callables and gives them up to a subscript; ``with <attr>:`` binds
nothing but *executes* ``__enter__``; and a subclass that inherits ``__call__`` is callable
while its own body says nothing.  Three rounds, three syntactic tests for "this value
cannot dispatch", three fail-open guards.

The conclusion drawn — and it is the important part of this module's history — is that a
local syntactic check cannot establish that semantic property, and that continuing to try
was the actual defect.  So the read side no longer tries:

* a **call** is modelled only when the receiver taint is actually bound into the callee's
  ``self`` (:meth:`_Walker._propagate`), which is what makes the body genuinely analysed.
  Method lookup and callability both follow base classes; an unresolvable base is treated
  as callable, the direction that keeps taint flowing rather than dropping it;
* a **read** is modelled only when the attribute is one the walk already tracks, or when
  the ``(class, attribute)`` pair appears in :data:`_MODELLED_ATTRIBUTE_READS` with a
  written justification.  There is no third reason, and in particular no inference from
  the shape of the assigned value;
* everything else — unknown attributes, containers, context managers, unresolvable
  classes — is reported.

Narrowing the wrapper rule to *callable* classes was needed alongside this: a container
that merely holds the runner (the smoke harness) is not the runner, and treating it as one
made every one of its attributes look runner-carrying.  That narrowing then exposed a
sweep gate that analysed a function only when a *local* was tainted, which had been masked
because the over-broad rule happened to create such a local.  Two errors had been
cancelling; the pair is worth remembering when a change here makes the numbers move.

One more assumption was hiding underneath all of that: the walk read assignment *targets*
without reading **scope declarations** (review j#92266 F4).  ``global X; X = self.runner``
therefore looked like an ordinary local binding while actually publishing the runner to
module state for any other function to dispatch through — silent in all three channels,
the original defect's exact shape.  ``global`` / ``nonlocal`` names are now excluded from
local binding and an assignment of a tainted value to one is reported.  ``nonlocal`` had
appeared to work, but only because :func:`ast.walk` descends into nested definitions and
picked the inner assignment up as an outer local: the same scope blindness, surfacing as
an accidental catch instead of a miss.

One question remained conflated after all of that, and it was the same one (review
j#92326 F6).  ``_MODELLED_PARENTS`` answers *where the value may travel*; it was also
being used to answer *what the syntax executes on the value*.  Those are different, and
three of its entries run a protocol: ``with <value>:`` runs ``__enter__`` / ``__exit__``,
and a ``BoolOp`` operand or an ``IfExp`` test runs ``__bool__``.  ``runner = runner or
subprocess.run`` already exists in production, so a ``__bool__`` appearing on the class at
that site would have dispatched with no call site changing at all.
:data:`_Walker._PROTOCOL_POSITIONS` now answers the second question separately, keyed by
``(parent node type, child field)``, and a protocol position is modelled only when the
receiver is bound into the dunder and analysed — the same rule as a declared method call.
The three positions were established by auditing every modelled parent by child position;
every other implicit-effect position in the language has no modelled parent and so already
reports.

:data:`_MODELLED_ATTRIBUTE_READS` is likewise checked against use rather than trusted:
:attr:`Derivation.used_read_exceptions` records which keys a run actually consumed, and a
test requires the two sets to match exactly.  Without that, a stale or mistyped key sat in
the guard pre-authorising a read nobody performs (review j#92266 F5).

It is analysis-only: nothing here imports the modules it reads, executes production
code, or touches a Herdr endpoint.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

#: Package root of the first-party source the walk is allowed to read.
_PACKAGE = "mozyo_bridge"

#: The attribute of this class is the gated runner every dispatch must pass through.
SEED_MODULE = (
    "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider"
    ".application.disposable_herdr_instance"
)
SEED_CLASS = "DisposableHerdrInstance"
SEED_ATTRIBUTE = "runner"

#: The module that composes the smoke: where the gated runner leaves its owner.
DRIVER_MODULE = (
    "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider"
    ".application.disposable_shared_space_smoke"
)

#: Guard against a pathological recursion in a mutually recursive call graph.  Reaching
#: it is reported as unresolved (fail-closed), never silently truncated.
_MAX_DEPTH = 12


class DerivationError(RuntimeError):
    """The walk could not be performed at all (missing source, unparseable module)."""


@dataclass(frozen=True)
class DispatchSite:
    """One call site that dispatches an argv through the gated runner."""

    module: str
    function: str
    lineno: int
    #: ``(group, subcommand)`` — i.e. ``argv[1:3]`` — or ``None`` when unresolved.
    pair: Optional[tuple] = None
    #: Non-empty exactly when ``pair`` is ``None``: why the argv could not be resolved.
    unresolved_reason: str = ""

    @property
    def anchor(self) -> str:
        return f"{self.module}:{self.function}:{self.lineno}"


@dataclass(frozen=True)
class SeedFlow:
    """One place the gated runner object is handed to another consumer."""

    module: str
    function: str
    lineno: int
    #: The callee the runner is passed to, and under which keyword / position.
    callee: str
    parameter: str


@dataclass
class Derivation:
    """The whole measurement: sites, pairs, and everything that stayed unreadable."""

    sites: tuple = ()
    seed_flows: tuple = ()
    #: Tainted values the walk could not follow (fail-closed findings, not pairs).
    unresolved_flows: tuple = ()
    #: Which :data:`_MODELLED_ATTRIBUTE_READS` keys this run actually consumed.  Compared
    #: against the table so an entry nobody uses cannot linger (review j#92266 F5).
    used_read_exceptions: frozenset = frozenset()

    @property
    def pairs(self) -> frozenset:
        """Every ``(group, subcommand)`` the derivation could resolve."""
        return frozenset(site.pair for site in self.sites if site.pair is not None)

    @property
    def unresolved_sites(self) -> tuple:
        return tuple(site for site in self.sites if site.pair is None)

    def sites_for(self, pair: tuple) -> tuple:
        return tuple(site for site in self.sites if site.pair == pair)


# --------------------------------------------------------------------------- index


@dataclass
class _Module:
    dotted: str
    path: Path
    tree: ast.Module
    #: ``qualname -> FunctionDef`` for every function, including methods.
    functions: dict = field(default_factory=dict)
    #: ``local name -> (module dotted, original name)`` for first-party imports.
    imports: dict = field(default_factory=dict)
    #: Module-level ``name -> tuple/list of str`` constants (for starred expansion).
    constants: dict = field(default_factory=dict)
    #: ``class name -> ClassDef``.
    classes: dict = field(default_factory=dict)
    #: ``local alias -> dotted module`` for ``from pkg import module as alias``.  The
    #: production session-start call is spelled ``_session.prepare_session(...)``, so
    #: without this the walk stops at the very entry point it exists to follow.
    module_aliases: dict = field(default_factory=dict)


def _dotted_for(path: Path, source_root: Path) -> str:
    relative = path.relative_to(source_root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _const_str_sequence(node: ast.AST) -> Optional[tuple]:
    """``("a", "b")`` / ``["a", "b"]`` of plain string constants, else ``None``."""
    if not isinstance(node, (ast.Tuple, ast.List)):
        return None
    values = []
    for element in node.elts:
        if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
            return None
        values.append(element.value)
    return tuple(values)


def _index_module(dotted: str, path: Path) -> _Module:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:  # pragma: no cover - broken checkout
        raise DerivationError(f"could not read {dotted}: {exc}") from exc
    module = _Module(dotted=dotted, path=path, tree=tree)

    def _walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = f"{prefix}{child.name}"
                module.functions[qualname] = child
                _walk(child, f"{qualname}.")
            elif isinstance(child, ast.ClassDef):
                if not prefix:
                    module.classes[child.name] = child
                _walk(child, f"{prefix}{child.name}.")

    _walk(tree, "")

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module.split(".")[0] != _PACKAGE:
                continue
            for alias in node.names:
                module.imports[alias.asname or alias.name] = (node.module, alias.name)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            sequence = _const_str_sequence(node.value)
            if sequence is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    module.constants[target.id] = sequence
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            sequence = _const_str_sequence(node.value) if node.value else None
            if sequence is not None:
                module.constants[node.target.id] = sequence
    return module


def _index_package(source_root: Path) -> dict:
    root = source_root / _PACKAGE
    if not root.is_dir():
        raise DerivationError(f"first-party source root not found: {root}")
    index: dict = {}
    for path in sorted(root.rglob("*.py")):
        dotted = _dotted_for(path, source_root)
        index[dotted] = _index_module(dotted, path)
    # Second pass: an import target is a *module* alias only once the whole package is
    # indexed, so it cannot be classified while reading a single file.
    for module in index.values():
        for local, (origin, original) in module.imports.items():
            candidate = f"{origin}.{original}"
            if candidate in index:
                module.module_aliases[local] = candidate
    return index


# ----------------------------------------------------------------------- taint walk

#: A value the walk is following.  Attribute taint is keyed on the *defining* class, so
#: ``self.recorder`` inside a method and ``harness.recorder`` outside it are the same
#: fact.
_ParamKey = tuple  # (module, function qualname, parameter name)
_AttrKey = tuple  # (module, class name, attribute name)


def _parameter_names(function: ast.AST) -> list:
    args = function.args
    return [
        argument.arg
        for argument in [*args.posonlyargs, *args.args, *args.kwonlyargs]
    ]


def _bind_arguments(
    function: ast.AST, call: ast.Call, *, method: bool = False
) -> dict:
    """Map a call's arguments onto the callee's parameter names (best effort).

    Only the shapes this code base uses are bound: plain positionals and keywords.
    ``*args`` / ``**kwargs`` forwarding is deliberately NOT bound — an unbound argument
    simply does not propagate taint here, and the caller reports the flow it could not
    follow rather than inventing a binding.

    ``method`` drops the implicit receiver.  Without it every ``Wrapper(runner)`` bound
    the runner to ``self`` and every ``Counters.snapshot(runner)`` bound it to ``cls`` —
    the receiver, not the argument, and the walk then followed a value nobody passed.
    """
    args = function.args
    positional = [*args.posonlyargs, *args.args]
    if method and positional and positional[0].arg in {"self", "cls"}:
        positional = positional[1:]
    bound: dict = {}
    for index, value in enumerate(call.args):
        if isinstance(value, ast.Starred) or index >= len(positional):
            continue
        bound[positional[index].arg] = value
    known = set(_parameter_names(function))
    for keyword in call.keywords:
        if keyword.arg and keyword.arg in known:
            bound[keyword.arg] = keyword.value
    return bound


def _enclosing_class(qualname: str) -> str:
    parts = qualname.split(".")
    return parts[-2] if len(parts) >= 2 else ""


#: The ONLY attribute reads on a tainted value the walk models, each with the reason it
#: is admitted.  Everything else is reported.
#:
#: This is a hand-written set, deliberately, after three rounds in which a *syntactic*
#: test for "this value cannot dispatch" was wrong in a new way each time (review j#92123
#: node type, j#92165 member kind, j#92213 container contents / context protocol /
#: inheritance).  A local syntax check cannot establish a semantic property, and pretending
#: otherwise produced three fail-open guards in a row.  What it CAN do is fail closed on
#: everything it was not told about, which is what this table does: it is small, each entry
#: carries its justification, it covers only the reads this smoke actually performs, and a
#: new read — including a legitimate one — turns the oracle red until a person adds it.
#:
#: Keyed by ``(class name, attribute)``.  A test asserts every class named here resolves to
#: exactly one class in the index, so the key cannot quietly match the wrong type.
_MODELLED_ATTRIBUTE_READS: dict = {
    ("EndpointBoundHerdrRunner", "dispatched_calls"): "gate counter (int) read into evidence",
    ("EndpointBoundHerdrRunner", "bound_calls"): "gate counter (int) read into evidence",
    ("EndpointBoundHerdrRunner", "escape_refusals"): "gate counter (int) read into evidence",
    ("EndpointBoundHerdrRunner", "operator_endpoint_requests"): (
        "gate counter (int) read into evidence"
    ),
    ("EndpointBoundHerdrRunner", "refusal_reasons"): "closed refusal tokens (set of str)",
    ("RecordingHerdrRunner", "launched_locators"): "actuation-receipt tape (list of str)",
    ("RecordingHerdrRunner", "agent_start_names"): "actuation-receipt tape (list of str)",
    ("RecordingHerdrRunner", "created_workspaces"): (
        "actuation-receipt tape (dict of workspace id -> label)"
    ),
    ("RecordingHerdrRunner", "workspace_create_labels"): (
        "actuation-receipt tape (list of str labels)"
    ),
    ("RecordingHerdrRunner", "created_coordinators_workspaces"): (
        "property deriving a list of workspace ids from the receipt tape"
    ),
    ("RecordingHerdrRunner", "_lock"): (
        "threading.Lock used only as a context manager for the tape.  NOT first-party, so "
        "its __enter__ cannot be analysed; admitted on this recorded justification alone"
    ),
}

#: Builtins whose result is a plain container / scalar, never a dispatcher.
_NON_CALLABLE_BUILTINS = frozenset(
    {"dict", "list", "set", "tuple", "str", "int", "float", "bool", "frozenset", "sorted"}
)


def _is_non_callable_expr(node: ast.AST) -> bool:
    """Whether ``node`` provably evaluates to something that cannot be called.

    Deliberately small and fail-closed: an expression this does not recognise is NOT
    assumed safe.  Recognising "a parameter" or "another attribute" as non-callable is
    exactly the assumption review j#92165 F2 struck down.
    """
    if isinstance(node, ast.Constant):
        return True
    if isinstance(
        node,
        (
            ast.List,
            ast.Dict,
            ast.Set,
            ast.Tuple,
            ast.ListComp,
            ast.DictComp,
            ast.SetComp,
            ast.JoinedStr,
        ),
    ):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id in _NON_CALLABLE_BUILTINS
    return False


class _Walker:
    """Follow the gated runner through the first-party source (fail-closed)."""

    def __init__(self, index: dict) -> None:
        self.index = index
        self.tainted_params: set = set()
        self.tainted_attrs: set = set()
        #: ``(callee module, callee qualname) -> [(caller module, caller qualname, Call)]``
        #: — only edges along which a tainted value actually travelled.
        self.taint_edges: dict = {}
        #: ``(module, class, attribute) -> (module, class)`` for a tainted attribute
        #: assigned a first-party wrapper instance.
        self.attr_classes: dict = {}
        #: ``(module, function, parameter) -> {(module, class)}`` — the classes a tainted
        #: parameter is known to receive.  ``_invoke`` is handed the harness's
        #: ``RecordingHerdrRunner``, and without carrying that across the call the
        #: wrapper's own forwarding site has no argv anyone can resolve.
        self.param_classes: dict = {}
        #: Exception keys from :data:`_MODELLED_ATTRIBUTE_READS` this run actually
        #: consumed.  Without it a stale or mistyped entry sits in the table forever,
        #: pre-authorising a read nobody performs (review j#92266 F5).
        self.used_read_exceptions: set = set()
        self.dispatch_calls: list = []  # (module, qualname, Call node, func expr)
        self.unresolved_flows: list = []

    # -- symbol resolution --------------------------------------------------

    def _resolve_name(self, module: str, name: str) -> Optional[tuple]:
        """``name`` as seen in ``module`` -> ``(defining module, name)``, or ``None``."""
        current = self.index.get(module)
        if current is None:
            return None
        if name in current.functions or name in current.classes:
            return (module, name)
        target = current.imports.get(name)
        if target is None:
            return None
        origin, original = target
        origin_module = self.index.get(origin)
        if origin_module is None:
            return None
        if original in origin_module.functions or original in origin_module.classes:
            return (origin, original)
        # A name re-exported through a package ``__init__`` is followed one hop.
        forwarded = origin_module.imports.get(original)
        if forwarded is None:
            return None
        return (forwarded[0], forwarded[1])

    def _callee(self, module: str, qualname: str, call: ast.Call) -> Optional[tuple]:
        """The first-party callable a call targets, as ``(module, qualname)``."""
        return self._resolve_callable(module, qualname, call.func)

    def _resolve_callable(
        self, module: str, qualname: str, func: ast.AST
    ) -> Optional[tuple]:
        """The first-party callable an expression names, as ``(module, qualname)``.

        Split out from :meth:`_callee` because ``Process(target=f, ...)`` names its
        callee as a plain expression, not as the func of a call.
        """
        if isinstance(func, ast.Name):
            return self._resolve_name(module, func.id)
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id == "self":
                owner = _enclosing_class(qualname)
                if owner and f"{owner}.{func.attr}" in self.index[module].functions:
                    return (module, f"{owner}.{func.attr}")
            aliased = self.index[module].module_aliases.get(func.value.id)
            if aliased is not None and func.attr in self.index[aliased].functions:
                return (aliased, func.attr)
            resolved = self._resolve_name(module, func.value.id)
            if resolved is not None:
                origin, name = resolved
                candidate = f"{name}.{func.attr}"
                if candidate in self.index[origin].functions:
                    return (origin, candidate)
        return None

    # -- per-function analysis ----------------------------------------------

    def _rebound_scope_names(self, function: ast.AST) -> set:
        """Names this body rebinds in an OUTER scope via ``global`` / ``nonlocal``.

        The walk tracks locals.  Assigning to one of these names does not create a local
        at all — it writes module state, or an enclosing function's state, where another
        function can pick it up and dispatch.  Reading only the assignment target's node
        type made that look like an ordinary local binding, and the runner escaped
        through it in complete silence (review j#92266 F4).

        Collected across nested definitions too.  That over-collects — a name declared
        ``global`` only inside an inner function is excluded here as well — and that is
        the fail-closed direction: reporting a binding the walk might have handled is
        recoverable, missing one is what this module exists to prevent.
        """
        names: set = set()
        for node in ast.walk(function):
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                names.update(node.names)
        return names

    def _local_taint(self, module: str, qualname: str, function: ast.AST) -> tuple:
        """``(tainted names, kwargs maps)`` bound to a tainted value in one body.

        The second element exists because ``prepare_session`` forwards its whole
        signature as ``call = dict(..., runner=runner, ...)`` then
        ``_prepare_session_locked(**call)``.  Without modelling that hop the walk stops
        at the production entry point it exists to follow — and stopping there would
        have reported "no dispatch sites" for the entire session-start path.
        """
        owner = _enclosing_class(qualname)
        rebound = self._rebound_scope_names(function)
        local = {
            name
            for name in _parameter_names(function)
            if (module, qualname, name) in self.tainted_params
        }
        kwargs_maps: dict = {}
        # Instance attributes of the enclosing class reach the body through ``self``.
        changed = True
        while changed:
            changed = False
            for node in ast.walk(function):
                targets: list = []
                value: Optional[ast.AST] = None
                if isinstance(node, ast.Assign):
                    targets, value = node.targets, node.value
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    targets, value = [node.target], node.value
                elif isinstance(node, ast.withitem):
                    targets = [node.optional_vars] if node.optional_vars else []
                    value = node.context_expr
                if value is None:
                    continue
                carried = self._carried_keywords(
                    module, owner, local, value, qualname
                )
                if carried:
                    for target in targets:
                        if isinstance(target, ast.Name):
                            merged = kwargs_maps.setdefault(target.id, {})
                            for name, classes in carried.items():
                                known = merged.setdefault(name, set())
                                if not classes <= known:
                                    known |= classes
                                    changed = True
                                elif name not in merged:
                                    changed = True
                if not self._is_tainted(module, owner, local, value):
                    continue
                for target in targets:
                    if (
                        isinstance(target, ast.Name)
                        and target.id not in local
                        and target.id not in rebound
                    ):
                        local.add(target.id)
                        changed = True
        return local, kwargs_maps

    def _process_target(self, module: str, qualname: str, call: ast.Call) -> tuple:
        """``(callee, argument list)`` for ``Process(target=f, args=(...))``, else ``()``.

        The smoke starts its workers this way, so without modelling it the gated runner
        crosses the fork boundary through a plain tuple and the walk loses it exactly
        where the real herdr traffic begins.
        """
        keywords = {
            keyword.arg: keyword.value for keyword in call.keywords if keyword.arg
        }
        target = keywords.get("target")
        arguments = keywords.get("args")
        if target is None or not isinstance(arguments, (ast.Tuple, ast.List)):
            return ()
        resolved = self._resolve_callable(module, qualname, target)
        if resolved is None:
            return ()
        return (resolved, list(arguments.elts))

    def _carried_keywords(
        self, module: str, owner: str, local: set, expr: ast.AST, qualname: str = ""
    ) -> dict:
        """``{keyword: classes}`` under which ``dict(...)`` carries a tainted value.

        The classes travel with the name so a value forwarded through ``**kwargs`` still
        has a resolvable type at the far end; without that, protocol positions there can
        only fail closed (review j#92326).
        """
        if not (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id == "dict"
        ):
            return {}
        carried: dict = {}
        for keyword in expr.keywords:
            if not keyword.arg:
                continue
            if not self._is_tainted(module, owner, local, keyword.value):
                continue
            carried[keyword.arg] = self._value_classes(
                module, qualname, owner, local, keyword.value
            )
        return carried

    def _is_tainted(
        self, module: str, owner: str, local: set, expr: ast.AST
    ) -> bool:
        if isinstance(expr, ast.Name):
            return expr.id in local
        if isinstance(expr, ast.Attribute):
            if isinstance(expr.value, ast.Name) and expr.value.id == "self":
                return (module, owner, expr.attr) in self.tainted_attrs
            # ``<instance>.<attr>`` where the instance's class is known from a
            # constructor binding in the same module (the smoke driver's shape).
            for defining_module, class_name, attribute in self.tainted_attrs:
                if attribute == expr.attr and self._names_class(
                    module, expr.value, defining_module, class_name
                ):
                    return True
            return False
        if isinstance(expr, ast.BoolOp):
            return any(
                self._is_tainted(module, owner, local, operand)
                for operand in expr.values
            )
        if isinstance(expr, ast.IfExp):
            return self._is_tainted(
                module, owner, local, expr.body
            ) or self._is_tainted(module, owner, local, expr.orelse)
        if isinstance(expr, ast.Call):
            # A wrapper CONSTRUCTED around a tainted runner is itself tainted: calling it
            # dispatches through the wrapped runner (``RecordingHerdrRunner(runner)``).
            # Restricted to constructors on purpose — treating any first-party call with a
            # tainted argument as tainted made every value *read back through* the runner
            # (``rows = _list_rows(binary, runner, timeout)``) look like a runner itself,
            # and the walk then chased herdr JSON rows around the topology module.
            if not any(
                self._is_tainted(module, owner, local, argument)
                for argument in [
                    *expr.args,
                    *[keyword.value for keyword in expr.keywords],
                ]
            ):
                return False
            # ...and only a CALLABLE class.  ``RecordingHerdrRunner`` wraps the runner and
            # dispatches through it; ``SharedSpaceSmokeHarness`` merely *holds* one on an
            # attribute the walk tracks separately.  Treating a container as the runner
            # itself makes every one of its attributes look runner-carrying, which is a
            # different error from the one the wrapper rule exists to catch.
            constructed = self._constructed_class(module, "", expr)
            return constructed is not None and self._class_is_callable(constructed)
        return False

    def _base_classes(self, class_ref: tuple) -> tuple:
        """``(resolved bases, saw_unresolved)`` for ``class_ref``."""
        module, class_name = class_ref
        indexed = self.index.get(module)
        class_def = indexed.classes.get(class_name) if indexed else None
        if class_def is None:
            return ((), True)
        resolved: list = []
        unresolved = False
        for base in class_def.bases:
            if not isinstance(base, ast.Name):
                unresolved = True
                continue
            found = self._resolve_name(module, base.id)
            if found is None or found[1] not in self.index[found[0]].classes:
                # ``object`` and stdlib bases land here; so would a base the walk cannot
                # read.  It cannot tell those apart, so it says so.
                unresolved = True
                continue
            resolved.append(found)
        return (tuple(resolved), unresolved)

    def _class_is_callable(self, class_ref: tuple, _seen: tuple = ()) -> bool:
        """Whether instances of ``class_ref`` can be called (define a dispatch entry).

        Follows base classes (review j#92213 F3-3): a subclass that inherits ``__call__``
        is callable, and reading only its own body reported a real dispatcher as inert.
        An unresolvable base makes the answer **callable**, because that is the direction
        that keeps taint flowing rather than dropping it.
        """
        if class_ref in _seen:
            return False
        module, class_name = class_ref
        indexed = self.index.get(module)
        if indexed is None:
            return True
        if any(
            f"{class_name}.{spelling}" in indexed.functions
            for spelling in self._DISPATCH_ATTRS
        ):
            return True
        class_def = indexed.classes.get(class_name)
        if class_def is None:
            return True
        # ``run = __call__`` — a class-level alias of a dispatch entry.
        for node in class_def.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
                if node.value.id in self._DISPATCH_ATTRS:
                    return True
        bases, unresolved = self._base_classes(class_ref)
        if any(
            self._class_is_callable(base, _seen + (class_ref,)) for base in bases
        ):
            return True
        return unresolved and bool(class_def.bases)

    def _is_constructor(self, module: str, call: ast.Call) -> bool:
        func = call.func
        if not isinstance(func, ast.Name):
            return False
        resolved = self._resolve_name(module, func.id)
        if resolved is None:
            return False
        origin, name = resolved
        return name in self.index[origin].classes

    def _names_class(
        self, module: str, expr: ast.AST, defining_module: str, class_name: str
    ) -> bool:
        """Whether ``expr`` is a name bound to an instance of ``class_name``."""
        if not isinstance(expr, ast.Name):
            return False
        current = self.index.get(module)
        if current is None:
            return False
        for node in ast.walk(current.tree):
            targets: list = []
            value: Optional[ast.AST] = None
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                targets, value = [node.optional_vars], node.context_expr
            if value is None:
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == expr.id
                for target in targets
            ):
                continue
            if isinstance(value, ast.Call):
                resolved = self._callee(module, "", value)
                if resolved == (defining_module, class_name):
                    return True
        return False

    # -- fixpoint -----------------------------------------------------------

    def run(self) -> None:
        self.tainted_attrs.add((SEED_MODULE, SEED_CLASS, SEED_ATTRIBUTE))
        for _ in range(_MAX_DEPTH):
            before = self._state()
            self._sweep()
            after = self._state()
            if before == after:
                return
        raise DerivationError(
            "the taint walk did not reach a fixpoint; refusing to report a partial "
            "dispatch surface (Redmine #14658)"
        )

    def _state(self) -> tuple:
        return (
            len(self.tainted_params),
            len(self.tainted_attrs),
            len(self.attr_classes),
            sum(len(classes) for classes in self.param_classes.values()),
            sum(len(edges) for edges in self.taint_edges.values()),
        )

    def _sweep(self) -> None:
        self.dispatch_calls = []
        self.unresolved_flows = []
        self.used_read_exceptions = set()
        for module_name, module in self.index.items():
            for qualname, function in module.functions.items():
                local, kwargs_maps = self._local_taint(module_name, qualname, function)
                if (
                    not local
                    and not kwargs_maps
                    and not self._class_has_taint(module_name, qualname)
                    and not self._mentions_tainted_attribute(function)
                ):
                    continue
                self._analyse(module_name, qualname, function, local, kwargs_maps)

    def _mentions_tainted_attribute(self, function: ast.AST) -> bool:
        """Whether the body reads an attribute name the walk currently tracks.

        A function can hold a tainted EXPRESSION without holding a tainted local — the
        smoke driver's ``SharedSpaceSmokeHarness(runner=instance.runner, ...)`` is exactly
        that.  Gating analysis on locals alone therefore skipped the function that hands
        the runner to the harness; it was reached only because an over-broad wrapper rule
        happened to make ``cleanup_harness`` a tainted local as well.  Narrowing that rule
        exposed the gap, so the gate now asks the question directly.
        """
        tracked = {attribute for _, _, attribute in self.tainted_attrs}
        if not tracked:
            return False
        for node in ast.walk(function):
            if isinstance(node, ast.Attribute) and node.attr in tracked:
                return True
        return False

    def _class_has_taint(self, module: str, qualname: str) -> bool:
        owner = _enclosing_class(qualname)
        return bool(owner) and any(
            key[0] == module and key[1] == owner for key in self.tainted_attrs
        )

    def _analyse(
        self,
        module: str,
        qualname: str,
        function: ast.AST,
        local: set,
        kwargs_maps: dict,
    ) -> None:
        owner = _enclosing_class(qualname)
        self._check_escapes(module, qualname, owner, local, function)
        for node in ast.walk(function):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if not (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                        and owner
                    ):
                        continue
                    if self._is_tainted(module, owner, local, node.value):
                        self.tainted_attrs.add((module, owner, target.attr))
                    # Record the class for ANY tainted attribute, not only one that
                    # became tainted through this assignment.  The seed attribute
                    # (``DisposableHerdrInstance.runner``) is tainted directly, so keying
                    # this off the assigned value left the one attribute the whole walk
                    # starts from with no known class.
                    if (module, owner, target.attr) in self.tainted_attrs:
                        wrapper = self._constructed_class(module, qualname, node.value)
                        if wrapper is not None:
                            self.attr_classes[(module, owner, target.attr)] = wrapper
            if not isinstance(node, ast.Call):
                continue
            if self._carried_keywords(module, owner, local, node, qualname):
                continue
            if self._is_dispatch(module, owner, local, node):
                self.dispatch_calls.append((module, qualname, node))
                self._forward_into_wrapper(module, qualname, owner, local, node)
                continue
            self._propagate(module, qualname, owner, local, kwargs_maps, node)

    def _constructed_class(
        self, module: str, qualname: str, expr: ast.AST
    ) -> Optional[tuple]:
        """``(module, class)`` when ``expr`` is a direct first-party construction."""
        if not isinstance(expr, ast.Call):
            return None
        resolved = self._callee(module, qualname, expr)
        if resolved is None:
            return None
        origin, name = resolved
        return resolved if name in self.index[origin].classes else None

    def _value_classes(
        self, module: str, qualname: str, owner: str, local: set, expr: ast.AST
    ) -> set:
        """The first-party classes ``expr`` is known to hold."""
        if isinstance(expr, ast.Call):
            constructed = self._constructed_class(module, qualname, expr)
            return {constructed} if constructed is not None else set()
        if isinstance(expr, ast.Attribute):
            if isinstance(expr.value, ast.Name) and expr.value.id == "self":
                known = self.attr_classes.get((module, owner, expr.attr))
                return {known} if known is not None else set()
            # ``<instance>.<attr>`` — resolve the instance's class first, then the class
            # that attribute was assigned.  Without this the smoke driver's
            # ``instance.runner`` and ``harness.recorder`` have no known class, and the
            # attribute check below can only fail closed on values it should model.
            found: set = set()
            for holder_module, holder_class in self._value_classes(
                module, qualname, owner, local, expr.value
            ):
                known = self.attr_classes.get(
                    (holder_module, holder_class, expr.attr)
                )
                if known is not None:
                    found.add(known)
            return found
        if isinstance(expr, ast.Name):
            carried = self.param_classes.get((module, qualname, expr.id))
            if carried:
                return set(carried)
            function = self.index[module].functions.get(qualname)
            if function is None:
                return set()
            binding = _single_binding(function, expr.id)
            if binding is None or binding is expr:
                return set()
            return self._value_classes(module, qualname, owner, local, binding)
        return set()

    def _forward_into_wrapper(
        self, module: str, qualname: str, owner: str, local: set, call: ast.Call
    ) -> None:
        """Bind a wrapper's own ``__call__`` argv to the argv passed at this site.

        ``RecordingHerdrRunner`` forwards to the runner it wraps, so the forwarding call
        inside its ``__call__`` is a dispatch site whose argv is a bare parameter.  It is
        the SAME argv as the outer call, and saying so mechanically is what keeps that
        site from being reported as an unreadable one.
        """
        func = call.func
        target = func.value if isinstance(func, ast.Attribute) else func
        for wrapper_module, wrapper_class in self._value_classes(
            module, qualname, owner, local, target
        ):
            for spelling in ("__call__", "run"):
                candidate = f"{wrapper_class}.{spelling}"
                if candidate not in self.index[wrapper_module].functions:
                    continue
                edge = self.taint_edges.setdefault((wrapper_module, candidate), [])
                if not any(existing[2] is call for existing in edge):
                    edge.append((module, qualname, call))

    #: Parent node kinds a tainted value may legitimately appear under.  Anything else
    #: is reported: the walk models call arguments, name/attribute bindings, ``with``
    #: bindings, attribute reads off the runner, and boolean/ternary carriers — and
    #: nothing more.  A return value, a container element or a subscript would carry the
    #: runner somewhere the walk does not follow.
    _MODELLED_PARENTS = (
        ast.Call,
        ast.keyword,
        ast.Assign,
        ast.AnnAssign,
        ast.withitem,
        ast.BoolOp,
        ast.IfExp,
    )

    #: Where a syntactic position does not merely CARRY the value but runs a protocol on
    #: it — keyed by ``(parent node type, child field)`` (review j#92326 F6).
    #:
    #: ``_MODELLED_PARENTS`` answers "where may the value travel".  That is a different
    #: question from "what does this syntax execute on the value", and deciding both with
    #: one node-type allowlist is what let ``with self.runner:`` and ``runner or
    #: subprocess.run`` dispatch in silence.  The second question is answered here, and
    #: the two are combined rather than conflated.
    #:
    #: Audited exhaustively against the modelled parents: these three positions run a
    #: protocol, and no others do.  Every other implicit-effect position in Python
    #: (``Compare`` ``__eq__``, ``Subscript`` ``__getitem__``, ``For`` ``__iter__``,
    #: ``not`` / ``if`` / ``while`` / ``assert`` ``__bool__``, f-string ``__format__``)
    #: has no modelled parent at all, so it already reports.
    _PROTOCOL_POSITIONS: dict = {
        (ast.withitem, "context_expr"): ("__enter__", "__exit__"),
        (ast.BoolOp, "values"): ("__bool__",),
        (ast.IfExp, "test"): ("__bool__",),
    }

    #: Attribute spellings that ARE the dispatch entry point (see :meth:`_is_dispatch`).
    _DISPATCH_ATTRS = frozenset({"run", "__call__"})

    def _check_escapes(
        self, module: str, qualname: str, owner: str, local: set, function: ast.AST
    ) -> None:
        rebound = self._rebound_scope_names(function)
        parents: dict = {}
        for node in ast.walk(function):
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node
        for node in ast.walk(function):
            if not isinstance(node, (ast.Name, ast.Attribute)):
                continue
            if not self._is_tainted(module, owner, local, node):
                continue
            parent = parents.get(id(node))
            if parent is None:
                continue
            if self._is_process_args_tuple(module, qualname, parents, parent):
                continue
            if isinstance(parent, ast.Attribute):
                grandparent = parents.get(id(parent))
                called = (
                    isinstance(grandparent, ast.Call) and grandparent.func is parent
                )
                # No ``with`` exemption.  The previous round admitted ``with <attr>:``
                # because the value is bound to nothing — but ``with`` *executes*
                # ``__enter__`` / ``__exit__``, so "does not escape" was again standing in
                # for "does not dispatch" (j#92213).  A context manager on a tainted value
                # goes through the same read rule as everything else.
                if self._attribute_access_is_modelled(
                    module, qualname, owner, local, node, parent, called
                ):
                    continue
                self.unresolved_flows.append(
                    f"{module}:{qualname}:{getattr(node, 'lineno', 0)}: a runner-carrying "
                    f"value is reached through attribute {parent.attr!r} "
                    f"({'called' if called else 'read as a value'}), which this walk does "
                    f"not follow"
                )
                continue
            protocol = self._protocol_for_position(node, parent)
            if protocol is not None:
                if self._protocol_receiver_is_analysed(
                    module, qualname, owner, local, node, protocol
                ):
                    continue
                self.unresolved_flows.append(
                    f"{module}:{qualname}:{getattr(node, 'lineno', 0)}: a runner-carrying "
                    f"value sits where {' / '.join(protocol)} runs on it, and this walk "
                    f"cannot analyse that"
                )
                continue
            if self._is_modelled_parent(node, parent, rebound):
                continue
            self.unresolved_flows.append(
                f"{module}:{qualname}:{getattr(node, 'lineno', 0)}: a runner-carrying "
                f"value appears under {type(parent).__name__}, which this walk does not "
                f"follow"
            )

    def _lookup_member(self, class_ref: tuple, attr: str, _seen: tuple = ()) -> Optional[tuple]:
        """``(module, qualname)`` of ``attr`` on ``class_ref`` or a base, else ``None``."""
        if class_ref in _seen:
            return None
        module, class_name = class_ref
        indexed = self.index.get(module)
        if indexed is None:
            return None
        candidate = f"{class_name}.{attr}"
        if candidate in indexed.functions:
            return (module, candidate)
        bases, _ = self._base_classes(class_ref)
        for base in bases:
            found = self._lookup_member(base, attr, _seen + (class_ref,))
            if found is not None:
                return found
        return None

    def _method_on_receiver(
        self, module: str, qualname: str, owner: str, local: set, receiver, attr: str
    ) -> Optional[tuple]:
        """The method ``attr`` names on the receiver's class, as ``(module, qualname)``."""
        for class_ref in self._value_classes(module, qualname, owner, local, receiver):
            found = self._lookup_member(class_ref, attr)
            if found is not None:
                return found
        return None

    def _attribute_kind(self, class_ref: tuple, attr: str) -> str:
        """What ``attr`` is on ``class_ref``: method / property / alias / data / unknown.

        The distinction decides whether reading it hands someone a **callable**.  A data
        member or a property yields a value the walk does not need to follow; a method or
        a class-level callable alias (``run = __call__``, which is exactly how both
        runners spell it) yields something that can dispatch later.
        """
        module, class_name = class_ref
        indexed = self.index.get(module)
        if indexed is None:
            return "unknown"
        function = indexed.functions.get(f"{class_name}.{attr}")
        if function is not None:
            decorators = {
                d.id for d in function.decorator_list if isinstance(d, ast.Name)
            }
            return "property" if "property" in decorators else "method"
        class_def = indexed.classes.get(class_name)
        if class_def is None:
            return "unknown"
        for node in class_def.body:
            # A class-level ``attr = <other member>`` alias.
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == attr:
                        inner = f"{class_name}.{node.value.id}"
                        if inner in indexed.functions:
                            return "callable_alias"
        for node in ast.walk(class_def):
            # ``self.attr = ...`` anywhere in the class body: an instance data member.
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == attr
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    return "data"
        return "unknown"

    def _attribute_access_is_modelled(
        self,
        module: str,
        qualname: str,
        owner: str,
        local: set,
        node: ast.AST,
        attribute: ast.Attribute,
        called: bool,
    ) -> bool:
        """Whether a tainted value reached through ``attribute`` stays inside the model.

        Review j#92123 F1.  Two shapes were escaping silently:

        * ``runner.execute(argv)`` — an attribute CALL that is not the dispatch spelling.
          It was neither a dispatch (only ``run`` / ``__call__`` are) nor an escape (any
          ``Attribute`` parent was "modelled"), so it appeared nowhere at all.
        * ``forward = runner.run`` — the bound method taken as a VALUE.  The taint does
          not survive the attribute read, so the later ``forward(argv)`` is not a
          dispatch either.

        The rule is now decided from what the class declares.  Anything the walk cannot
        classify — including a tainted value whose class it does not know — is reported,
        because an attribute it cannot name is an attribute it cannot follow.
        """
        classes = self._value_classes(module, qualname, owner, local, node)
        if called:
            if attribute.attr in self._DISPATCH_ATTRS:
                return True  # the dispatch spelling; _is_dispatch already recorded it
            # A method on a class the walk analyses is modelled ONLY IF the receiver
            # taint was actually bound into it (``_propagate`` above).  Checking the
            # binding rather than the declaration is the point: F2(A) was precisely a
            # declaration that no propagation backed.
            if not classes:
                return False
            for class_ref in classes:
                found = self._lookup_member(class_ref, attribute.attr)
                if found is None:
                    return False
                found_module, found_qualname = found
                bound = self.index[found_module].functions[found_qualname]
                names = _parameter_names(bound)
                if not names or (found_module, found_qualname, names[0]) not in (
                    self.tainted_params
                ):
                    return False
            return True
        # Not called: reading the attribute yields a value.  Only a value that cannot BE
        # a dispatcher is modelled.
        #
        # Review j#92165 F2(B): "it is a data member or a property" was the second proxy
        # in a row standing in for that property, and it is not one.  Three of
        # ``EndpointBoundHerdrRunner``'s own data attributes hold callables, and one of
        # them — ``_inner`` — is the UNGATED inner runner: reading it out and calling it
        # bypasses the endpoint gate entirely, which is worse than a missing pair.  So the
        # question asked is now the property itself.
        if attribute.attr in self._DISPATCH_ATTRS:
            return False
        return bool(classes) and all(
            self._read_yields_no_dispatcher(ref, attribute.attr) for ref in classes
        )

    def _read_yields_no_dispatcher(self, class_ref: tuple, attr: str) -> bool:
        """Whether reading ``class_ref.attr`` is a read the walk models.

        Two admissible reasons, and no third:

        * the attribute is one the walk itself **tracks**, so it genuinely follows it;
        * the ``(class, attribute)`` pair is in :data:`_MODELLED_ATTRIBUTE_READS` with a
          recorded justification.

        Note what is deliberately NOT here: any attempt to decide from the shape of the
        assigned value.  A container holds callables, a property returns them, and a
        ``@property`` that looks like a list read can be anything — each of those was a
        separate silent omission (j#92213).
        """
        module, class_name = class_ref
        if (module, class_name, attr) in self.tainted_attrs:
            return True
        if (class_name, attr) in _MODELLED_ATTRIBUTE_READS:
            self.used_read_exceptions.add((class_name, attr))
            return True
        return False

    def _protocol_for_position(self, node: ast.AST, parent: ast.AST) -> Optional[tuple]:
        """The dunders this position runs on ``node``, or ``None`` if it only carries it."""
        for (parent_type, field), dunders in self._PROTOCOL_POSITIONS.items():
            if not isinstance(parent, parent_type):
                continue
            value = getattr(parent, field, None)
            if value is node or (
                isinstance(value, list) and any(item is node for item in value)
            ):
                return dunders
        return None

    def _protocol_receiver_is_analysed(
        self, module: str, qualname: str, owner: str, local: set, node: ast.AST,
        dunders: tuple,
    ) -> bool:
        """Bind the value into the dunders this position runs, and say whether that held.

        Same treatment as a declared method call: the position is modelled only when the
        receiver taint is actually bound into the body, so a dispatching ``__bool__``
        resolves as a real pair instead of being waved through.  A class the walk cannot
        resolve, or a dunder defined outside the first-party source, is reported — the
        walk cannot analyse what it cannot read.
        """
        classes = self._value_classes(module, qualname, owner, local, node)
        if not classes:
            return False
        for class_ref in classes:
            for dunder in dunders:
                found = self._lookup_member(class_ref, dunder)
                if found is None:
                    continue  # the class does not implement it: no effect to follow
                found_module, found_qualname = found
                function = self.index[found_module].functions[found_qualname]
                names = _parameter_names(function)
                if not names:
                    return False
                self.tainted_params.add((found_module, found_qualname, names[0]))
                self.param_classes.setdefault(
                    (found_module, found_qualname, names[0]), set()
                ).add(class_ref)
        return True

    def _is_modelled_parent(
        self, node: ast.AST, parent: ast.AST, rebound: set = frozenset()
    ) -> bool:
        """Whether ``parent`` is a context the walk actually follows for ``node``.

        Two shapes look modelled but are not, so they are excluded by position rather
        than by node type: an assignment whose *target* is a subscript (``d["r"] =
        runner``) stores the runner in a container the walk does not read, and a
        ``**runner`` keyword has no parameter name to bind.
        """
        if isinstance(parent, (ast.Assign, ast.AnnAssign)):
            targets = (
                parent.targets if isinstance(parent, ast.Assign) else [parent.target]
            )
            if parent.value is not node:
                return True  # the runner IS the target being rebound; taint follows it
            return all(
                (isinstance(target, ast.Name) and target.id not in rebound)
                or (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                )
                for target in targets
            )
        if isinstance(parent, ast.keyword):
            return parent.arg is not None
        return isinstance(parent, self._MODELLED_PARENTS)

    def _is_process_args_tuple(
        self, module: str, qualname: str, parents: dict, parent: ast.AST
    ) -> bool:
        """Whether ``parent`` is the ``args=`` tuple of a modelled process launch."""
        if not isinstance(parent, (ast.Tuple, ast.List)):
            return False
        keyword = parents.get(id(parent))
        if not (isinstance(keyword, ast.keyword) and keyword.arg == "args"):
            return False
        call = parents.get(id(keyword))
        return isinstance(call, ast.Call) and bool(
            self._process_target(module, qualname, call)
        )

    def _is_dispatch(
        self, module: str, owner: str, local: set, call: ast.Call
    ) -> bool:
        func = call.func
        if self._is_tainted(module, owner, local, func):
            return True
        # ``runner.run(argv, ...)`` — the Runner protocol's explicit spelling.
        if (
            isinstance(func, ast.Attribute)
            and func.attr in {"run", "__call__"}
            and self._is_tainted(module, owner, local, func.value)
        ):
            return True
        return False

    def _propagate(
        self,
        module: str,
        qualname: str,
        owner: str,
        local: set,
        kwargs_maps: dict,
        call: ast.Call,
    ) -> None:
        forwarded: dict = {}
        for keyword in call.keywords:
            if keyword.arg is None and isinstance(keyword.value, ast.Name):
                for name, classes in kwargs_maps.get(keyword.value.id, {}).items():
                    forwarded.setdefault(name, set()).update(classes)
        tainted_arguments = [
            argument
            for argument in [
                *call.args,
                *[keyword.value for keyword in call.keywords],
            ]
            if self._is_tainted(module, owner, local, argument)
        ]
        # Review j#92165 F2(A): a call whose RECEIVER is tainted was dropped here, because
        # only tainted arguments and forwarded keywords propagated.  The attribute check
        # meanwhile admitted such a call on the grounds that "the walk analyses that
        # method" — a property the walk did not actually have.  Bind the receiver to the
        # callee's ``self`` so the body really is analysed, and the claim becomes true.
        receiver = call.func.value if isinstance(call.func, ast.Attribute) else None
        if receiver is not None and self._is_tainted(module, owner, local, receiver):
            # Resolve through the RECEIVER'S CLASS, the same way
            # :meth:`_attribute_access_is_modelled` does.  ``_callee`` only understands a
            # plain-name receiver, so ``self.runner.execute(...)`` resolved for the
            # attribute check and not for propagation — the two decisions disagreed, and
            # that disagreement is F2(A).  One resolver now backs both.
            resolved_method = self._method_on_receiver(
                module, qualname, owner, local, receiver, call.func.attr
            )
            if resolved_method is not None:
                callee_module, callee_name = resolved_method
                function = self.index[callee_module].functions.get(callee_name)
                names = _parameter_names(function) if function is not None else []
                if names and names[0] in {"self", "cls"}:
                    self.tainted_params.add((callee_module, callee_name, names[0]))
                    classes = self._value_classes(
                        module, qualname, owner, local, receiver
                    )
                    if classes:
                        self.param_classes.setdefault(
                            (callee_module, callee_name, names[0]), set()
                        ).update(classes)
                    edge = self.taint_edges.setdefault(
                        (callee_module, callee_name), []
                    )
                    if not any(existing[2] is call for existing in edge):
                        edge.append((module, qualname, call))
        process_target = self._process_target(module, qualname, call)
        if process_target:
            (callee_module, callee_name), arguments = process_target
            function = self.index[callee_module].functions.get(callee_name)
            if function is None:
                self.unresolved_flows.append(
                    f"{module}:{qualname}:{call.lineno}: a process target body was not "
                    f"found"
                )
                return
            names = _parameter_names(function)
            marked = False
            for position, argument in enumerate(arguments):
                if position >= len(names):
                    break
                if not self._is_tainted(module, owner, local, argument):
                    continue
                self.tainted_params.add((callee_module, callee_name, names[position]))
                marked = True
                classes = self._value_classes(module, qualname, owner, local, argument)
                if classes:
                    self.param_classes.setdefault(
                        (callee_module, callee_name, names[position]), set()
                    ).update(classes)
            if marked:
                edge = self.taint_edges.setdefault((callee_module, callee_name), [])
                if not any(existing[2] is call for existing in edge):
                    edge.append((module, qualname, call))
                return
        if not tainted_arguments and not forwarded:
            return
        resolved = self._callee(module, qualname, call)
        if resolved is None:
            self.unresolved_flows.append(
                f"{module}:{qualname}:{call.lineno}: a runner-carrying value is passed "
                f"to a callee this walk cannot resolve"
            )
            return
        callee_module, callee_name = resolved
        target = self.index[callee_module]
        if callee_name in target.classes:
            callee_name = f"{callee_name}.__init__"
            if callee_name not in target.functions:
                # A tainted value handed to a class with no ``__init__`` of its own is a
                # shape this walk does not model; report rather than drop it.
                self.unresolved_flows.append(
                    f"{module}:{qualname}:{call.lineno}: a runner-carrying value is "
                    f"passed to a class without an analysable __init__"
                )
                return
        function = target.functions.get(callee_name)
        if function is None:
            self.unresolved_flows.append(
                f"{module}:{qualname}:{call.lineno}: callee body not found"
            )
            return
        bound = _bind_arguments(function, call, method="." in callee_name)
        marked = False
        for parameter, value in bound.items():
            if self._is_tainted(module, owner, local, value):
                self.tainted_params.add((callee_module, callee_name, parameter))
                marked = True
                classes = self._value_classes(module, qualname, owner, local, value)
                if classes:
                    self.param_classes.setdefault(
                        (callee_module, callee_name, parameter), set()
                    ).update(classes)
        for parameter in set(forwarded) & set(_parameter_names(function)):
            self.tainted_params.add((callee_module, callee_name, parameter))
            marked = True
            if forwarded[parameter]:
                self.param_classes.setdefault(
                    (callee_module, callee_name, parameter), set()
                ).update(forwarded[parameter])
        if not marked:
            self.unresolved_flows.append(
                f"{module}:{qualname}:{call.lineno}: a runner-carrying value could not "
                f"be bound to a parameter of {callee_module}:{callee_name}"
            )
            return
        edge = self.taint_edges.setdefault((callee_module, callee_name), [])
        if not any(existing[2] is call for existing in edge):
            edge.append((module, qualname, call))


# ------------------------------------------------------------------- argv resolution

#: Placeholder for an element whose value is not a compile-time string.
_UNKNOWN = object()

#: ``list`` methods that only read.  Everything not named here is treated as a possible
#: mutation, so an unfamiliar method makes the site unresolved rather than trusted.
_LIST_READS = frozenset({"index", "count", "copy"})


@dataclass(frozen=True)
class _Tail:
    """From this position on, the elements are ``parameter[0], parameter[1], ...``."""

    parameter: str


def _single_binding(function: ast.AST, name: str) -> Optional[ast.AST]:
    """The one value ``name`` is assigned in ``function``, or ``None`` if not exactly one."""
    bindings = [
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        )
    ]
    return bindings[0] if len(bindings) == 1 else None


def _safe_mutations(function: ast.AST, name: str, literal: list) -> str:
    """``""`` when no mutation of ``name`` can shift ``literal`` indices 1 and 2.

    Only the shapes this code base uses are admitted, and each is admitted for a
    *reason*, never because it looked harmless:

    * ``name.append(x)`` / ``name.extend(x)`` — append only, indices unchanged;
    * ``name[i:i] = x`` and ``name.insert(i, x)`` where ``i`` is provably ``>= 3``.
      ``build_agent_start_argv`` inserts the placement flags at
      ``name.index("--workspace") + 2``; ``index`` cannot return less than the first
      position whose element is not a *constant unequal to* the searched token, which
      bounds the insertion below the head.

    Anything else — a rebind, a ``del``, an insertion whose position cannot be bounded —
    returns the reason it could not be admitted, and the site is reported unresolved.
    """
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            target = node.func.value
            if not (isinstance(target, ast.Name) and target.id == name):
                continue
            if node.func.attr in {"append", "extend"}:
                continue
            if node.func.attr in _LIST_READS:
                continue
            if node.func.attr == "insert" and node.args:
                bound = _lower_bound(node.args[0], name, literal, function)
                if bound is not None and bound >= 3:
                    continue
                return f"{name}.insert() at a position that may shift the head"
            return f"unmodelled mutation {name}.{node.func.attr}()"
        if isinstance(node, ast.Delete):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and isinstance(
                    target.value, ast.Name
                ) and target.value.id == name:
                    return f"del on {name} may shift the head"
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Subscript):
                    continue
                if not (
                    isinstance(target.value, ast.Name) and target.value.id == name
                ):
                    continue
                if not isinstance(target.slice, ast.Slice):
                    return f"{name}[i] = ... may rewrite the head"
                lower = target.slice.lower
                if lower is None:
                    return f"{name}[:i] = ... may rewrite the head"
                bound = _lower_bound(lower, name, literal, function)
                if bound is None or bound < 3:
                    return f"{name}[i:i] = ... at an unbounded position"
    return ""


def _lower_bound(
    expr: ast.AST, name: str, literal: list, function: ast.AST
) -> Optional[int]:
    """A provable lower bound for ``expr`` as an index into ``literal``."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, int):
        return expr.value
    if isinstance(expr, ast.Name):
        binding = _single_binding(function, expr.id)
        if binding is None:
            return None
        return _lower_bound(binding, name, literal, function)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left = _lower_bound(expr.left, name, literal, function)
        right = _lower_bound(expr.right, name, literal, function)
        if left is None or right is None:
            return None
        return left + right
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "index"
        and isinstance(expr.func.value, ast.Name)
        and expr.func.value.id == name
        and len(expr.args) == 1
        and isinstance(expr.args[0], ast.Constant)
    ):
        needle = expr.args[0].value
        for position, element in enumerate(literal):
            # A constant that differs from the needle cannot be the match; the first
            # element that *could* match bounds ``index()`` from below.
            if isinstance(element, str) and element != needle:
                continue
            return position
        return len(literal)
    return None


class _ArgvResolver:
    """Turn a dispatch call's argv expression into a ``(group, subcommand)`` pair."""

    def __init__(self, walker: _Walker) -> None:
        self.walker = walker
        self.index = walker.index

    # -- entry --------------------------------------------------------------

    def sites(self, module: str, qualname: str, call: ast.Call) -> list:
        if not call.args:
            return [
                DispatchSite(
                    module,
                    qualname,
                    call.lineno,
                    None,
                    "the dispatch call passes no argv",
                )
            ]
        return self._sites_for(module, qualname, call.args[0], call.lineno, 0)

    def _sites_for(
        self, module: str, qualname: str, expr: ast.AST, lineno: int, depth: int
    ) -> list:
        if depth > _MAX_DEPTH:
            return [
                DispatchSite(module, qualname, lineno, None, "resolution depth exceeded")
            ]
        elements, failure = self._sequence(module, qualname, expr, depth)
        if failure:
            return [DispatchSite(module, qualname, lineno, None, failure)]
        pair, tail = self._pair_from(elements)
        if pair is not None:
            return [DispatchSite(module, qualname, lineno, pair, "")]
        if tail is None:
            return [
                DispatchSite(
                    module,
                    qualname,
                    lineno,
                    None,
                    "argv[1:3] is not a pair of compile-time strings",
                )
            ]
        parameter, drop = tail
        return self._resolve_through_callers(
            module, qualname, parameter, drop, lineno, depth
        )

    def _pair_from(self, elements: list) -> tuple:
        """``(pair, None)``, ``(None, (parameter, drop))``, or ``(None, None)``."""
        for position, element in enumerate(elements[:3]):
            if isinstance(element, _Tail):
                if position > 1:
                    return None, None
                return None, (element.parameter, 1 - position)
        if len(elements) < 3:
            return None, None
        group, subcommand = elements[1], elements[2]
        if isinstance(group, str) and isinstance(subcommand, str):
            return (group, subcommand), None
        return None, None

    # -- resolving a value into a flat element sequence ---------------------

    def _sequence(
        self, module: str, qualname: str, expr: ast.AST, depth: int
    ) -> tuple:
        """``(elements, "")`` or ``([], reason)``."""
        if depth > _MAX_DEPTH:
            return [], "resolution depth exceeded"
        function = self.index[module].functions.get(qualname)
        if isinstance(expr, ast.List):
            return self._flatten(module, qualname, expr.elts, depth)
        if isinstance(expr, ast.Name):
            if function is not None and expr.id in _parameter_names(function):
                return [_Tail(expr.id)], ""
            return self._from_local(module, qualname, expr.id, depth)
        if isinstance(expr, ast.Call):
            return self._from_call(module, qualname, expr, depth)
        return [], f"unmodelled argv expression {type(expr).__name__}"

    def _flatten(self, module: str, qualname: str, elts: list, depth: int) -> tuple:
        elements: list = []
        for element in elts:
            if len(elements) >= 3:
                break
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                elements.append(element.value)
                continue
            if isinstance(element, ast.Starred):
                inner, failure = self._starred(module, qualname, element.value, depth)
                if failure:
                    return [], failure
                elements.extend(inner)
                continue
            elements.append(_UNKNOWN)
        return elements, ""

    def _starred(
        self, module: str, qualname: str, expr: ast.AST, depth: int
    ) -> tuple:
        if isinstance(expr, ast.Name):
            constant = self.index[module].constants.get(expr.id)
            if constant is not None:
                return list(constant), ""
            function = self.index[module].functions.get(qualname)
            if function is not None and expr.id in _parameter_names(function):
                return [_Tail(expr.id)], ""
        return self._sequence(module, qualname, expr, depth + 1)

    def _from_local(
        self, module: str, qualname: str, name: str, depth: int
    ) -> tuple:
        function = self.index[module].functions.get(qualname)
        if function is None:
            return [], f"no body for {qualname}"
        bindings = [
            node.value
            for node in ast.walk(function)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets
            )
        ]
        if len(bindings) != 1:
            return [], (
                f"{name} is bound {len(bindings)} times; only a single binding is "
                f"resolvable"
            )
        elements, failure = self._sequence(module, qualname, bindings[0], depth + 1)
        if failure:
            return [], failure
        literal = [element for element in elements if not isinstance(element, _Tail)]
        mutation = _safe_mutations(function, name, literal)
        if mutation:
            return [], mutation
        return elements, ""

    def _from_call(
        self, module: str, qualname: str, call: ast.Call, depth: int
    ) -> tuple:
        resolved = self.walker._callee(module, qualname, call)
        if resolved is None:
            return [], "argv comes from a callee this walk cannot resolve"
        callee_module, callee_name = resolved
        function = self.index[callee_module].functions.get(callee_name)
        if function is None:
            return [], f"no body for {callee_module}:{callee_name}"
        returns = [
            node.value
            for node in ast.walk(function)
            if isinstance(node, ast.Return) and node.value is not None
        ]
        if not returns:
            return [], f"{callee_module}:{callee_name} returns nothing analysable"
        bound = _bind_arguments(function, call, method="." in callee_name)
        collected: list = []
        for value in returns:
            elements, failure = self._sequence(
                callee_module, callee_name, value, depth + 1
            )
            if failure:
                return [], failure
            # A builder that returns one of its own parameters is resolved against the
            # argument the caller supplied, so the pair is measured at the call site.
            resolved_elements: list = []
            for element in elements:
                if isinstance(element, _Tail):
                    argument = bound.get(element.parameter)
                    if argument is None:
                        return [], (
                            f"{callee_module}:{callee_name} forwards parameter "
                            f"{element.parameter!r}, unbound at this call"
                        )
                    inner, failure = self._sequence(module, qualname, argument, depth + 1)
                    if failure:
                        return [], failure
                    resolved_elements.extend(inner)
                    continue
                resolved_elements.append(element)
            collected.append(resolved_elements)
        heads = {tuple(candidate[:3]) for candidate in collected}
        if len(heads) != 1:
            return [], (
                f"{callee_module}:{callee_name} returns argvs with differing heads"
            )
        return collected[0], ""

    # -- resolving a parameter at every caller that carried the runner ------

    def _resolve_through_callers(
        self,
        module: str,
        qualname: str,
        parameter: str,
        drop: int,
        lineno: int,
        depth: int,
    ) -> list:
        edges = self.walker.taint_edges.get((module, qualname), [])
        if not edges:
            return [
                DispatchSite(
                    module,
                    qualname,
                    lineno,
                    None,
                    f"argv arrives as parameter {parameter!r} but no runner-carrying "
                    f"caller was found",
                )
            ]
        sites: list = []
        for caller_module, caller_qualname, call in edges:
            function = self.index[module].functions.get(qualname)
            bound = (
                _bind_arguments(function, call, method="." in qualname)
                if function is not None
                else {}
            )
            argument = bound.get(parameter)
            if argument is None:
                sites.append(
                    DispatchSite(
                        caller_module,
                        caller_qualname,
                        call.lineno,
                        None,
                        f"parameter {parameter!r} is unbound at this caller",
                    )
                )
                continue
            elements, failure = self._sequence(
                caller_module, caller_qualname, argument, depth + 1
            )
            if failure:
                sites.append(
                    DispatchSite(
                        caller_module, caller_qualname, call.lineno, None, failure
                    )
                )
                continue
            # ``drop`` says which element of the parameter becomes ``argv[1]``: with
            # ``argv = [binary, *tail]`` the pair is ``tail[0:2]``, so the resolved
            # sequence is shifted back under index 1 before the pair is read.
            padded = [_UNKNOWN] * (1 - drop) + list(elements)
            pair, tail = self._pair_from(padded)
            if pair is not None:
                sites.append(
                    DispatchSite(caller_module, caller_qualname, call.lineno, pair, "")
                )
                continue
            if tail is None:
                sites.append(
                    DispatchSite(
                        caller_module,
                        caller_qualname,
                        call.lineno,
                        None,
                        "argv[1:3] is not a pair of compile-time strings",
                    )
                )
                continue
            inner_parameter, inner_drop = tail
            sites.extend(
                self._resolve_through_callers(
                    caller_module,
                    caller_qualname,
                    inner_parameter,
                    inner_drop,
                    call.lineno,
                    depth + 1,
                )
            )
        return sites


# ------------------------------------------------------------------------ public API


def default_source_root() -> Path:
    """The repo's ``src`` directory, resolved from this file's location."""
    return Path(__file__).resolve().parents[2] / "src"


def derive_seed_flows(index: dict) -> tuple:
    """Every place the smoke driver hands the gated runner object to someone else.

    The taint walk seeds on :data:`SEED_ATTRIBUTE`; this re-measures, independently of
    the walk, where that object *leaves* the driver.  A new consumer added to
    ``run_disposable_shared_space_smoke`` therefore shows up as a new flow rather than
    being absorbed silently into a set someone already believed was complete.
    """
    module = index.get(DRIVER_MODULE)
    if module is None:
        raise DerivationError(f"driver module not indexed: {DRIVER_MODULE}")
    flows: list = []
    for qualname, function in module.functions.items():
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            callee = ""
            if isinstance(node.func, ast.Name):
                callee = node.func.id
            elif isinstance(node.func, ast.Attribute):
                callee = node.func.attr
            for parameter, value in [
                *[(f"#{position}", value) for position, value in enumerate(node.args)],
                *[(keyword.arg or "**", keyword.value) for keyword in node.keywords],
            ]:
                if (
                    isinstance(value, ast.Attribute)
                    and value.attr == SEED_ATTRIBUTE
                    and isinstance(value.value, ast.Name)
                ):
                    flows.append(
                        SeedFlow(
                            module=DRIVER_MODULE,
                            function=qualname,
                            lineno=node.lineno,
                            callee=callee,
                            parameter=parameter,
                        )
                    )
    return tuple(sorted(flows, key=lambda flow: (flow.lineno, flow.parameter)))


def derive_dispatch_surface(source_root: Optional[Path] = None) -> Derivation:
    """Measure every argv the disposable smoke's gated runner can dispatch.

    Read-only static analysis: no production module is imported, no subprocess is run,
    and no Herdr endpoint is addressed.  See the module docstring for the contract.
    """
    root = Path(source_root) if source_root is not None else default_source_root()
    index = _index_package(root)
    walker = _Walker(index)
    walker.run()
    resolver = _ArgvResolver(walker)
    sites: list = []
    for module, qualname, call in walker.dispatch_calls:
        sites.extend(resolver.sites(module, qualname, call))
    deduplicated = tuple(
        sorted(
            {
                (site.module, site.function, site.lineno, site.pair, site.unresolved_reason)
                for site in sites
            }
        )
    )
    return Derivation(
        sites=tuple(
            DispatchSite(module, function, lineno, pair, reason)
            for module, function, lineno, pair, reason in deduplicated
        ),
        seed_flows=derive_seed_flows(index),
        unresolved_flows=tuple(sorted(set(walker.unresolved_flows))),
        used_read_exceptions=frozenset(walker.used_read_exceptions),
    )


__all__ = (
    "Derivation",
    "DerivationError",
    "DispatchSite",
    "SeedFlow",
    "DRIVER_MODULE",
    "SEED_ATTRIBUTE",
    "SEED_CLASS",
    "SEED_MODULE",
    "default_source_root",
    "derive_dispatch_surface",
    "derive_seed_flows",
)
