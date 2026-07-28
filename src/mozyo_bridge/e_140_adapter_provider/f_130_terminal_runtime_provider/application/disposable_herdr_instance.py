"""Owned, capability-bound Herdr server lifecycle for disposable diagnostics (#14187).

The public shared-space smoke must never discover or reuse the operator's normal
Herdr server.  This module provides the narrow application port that was missing:
an exact child process, an explicit Unix-socket endpoint, isolated Herdr state, a
runner that binds every CLI request to that endpoint, and bounded shutdown.

Threat model (incident j#85754, owner design disposition j#85756)
----------------------------------------------------------------
A managed agent inherits the operator's ambient ``HERDR_SOCKET_PATH``.  An endpoint
is therefore **not a setting, it is a destruction capability**: the moment a
disposable binding goes missing, the *same* ``herdr server stop`` argv stops the
operator's server instead of ours.  The first implementation scored that risk with
post-hoc booleans (``all_calls_bound`` / ``operator_endpoint_connected``) computed
*after* dispatch; a mutation probe that dropped the binding line consequently sent a
real ``server stop`` to the operator endpoint before any boolean could be read.

Four rules follow, and this module implements all four:

1. **Ownership, not path equality.**  A matching socket path proves nothing —
   environments are inherited, overridden and mutated.  Cleanup authority is bound
   to an :class:`OwnedEndpointCapability`: minted only by :meth:`DisposableHerdrInstance.start`
   after *this* process launched the server, registered in a module-private mint
   registry, and carrying the owned child's pid.
2. **Gate before actuation, not verdict after.**  :class:`EndpointBoundHerdrRunner`
   evaluates the capability against the *effective* env of the call it is about to
   make and raises :class:`SmokeEndpointEscapeError` **before** touching the inner
   runner.  A refused call makes zero external requests; there is no window in which
   an unbound request is already in flight.
3. **Cleanup stays possible when the gate fires.**  Graceful ``server stop`` goes
   through the gate; if the gate refuses, shutdown falls back to signalling the exact
   owned process handle, which cannot address a foreign server at all.
4. **Guard-removal probes never hold live capability.**  Mutation/negative tests must
   run with an injected fake inner runner and a scrubbed/poison ambient endpoint (see
   ``tests/unit/.../test_disposable_herdr_instance.py``), never against the live
   :func:`run_disposable_shared_space_smoke` path.

Two authorities, not one (review j#85841 F2)
--------------------------------------------
A pid that still *equals* the one we minted for is not proof that the child is still
running; once it exits, the capability degrades into exactly the socket-path
addressing rule 1 rejects.  But liveness cannot simply be asked everywhere: the smoke's
forked workers inherit a *copy* of the owned handle without being the server's parent,
and :meth:`subprocess.Popen.poll` there reports a **live** child as dead (``waitpid``
raises ``ChildProcessError``, which CPython maps to "exited").  Bolting ``poll()`` onto
the shared gate would therefore fail-closed on every healthy worker call.

So the single authority is split in two:

* **client-call capability** — addressing the owned endpoint (``workspace list``,
  ``agent start``, …).  Held by the minting process and by its forked workers.  In the
  minting process — the only place the question is answerable — it is additionally
  fenced on the owned child still being alive.
* **cleanup authority** — :data:`MINTER_ONLY_SUBCOMMANDS` (``server stop``).  Granted
  **only** to the minting process, and only while the owned child is alive.  A forked
  worker is refused outright rather than asked a question it cannot answer.

Both are scoped by an allowlist, not a denylist (review j#91604 F1).  Only
:data:`CLIENT_CALL_SUBCOMMANDS` may be dispatched at all; every other Herdr control —
``session stop``/``session delete`` (whose help names ``default`` as a target),
``server reload-config``, ``update`` — is refused because it was never named.

That allowlist is **derived from the source, not enumerated by hand** (Redmine #14658).
Hand enumeration was measured to fail in both directions at once: the #14185 R3 live run
was refused because the production launcher preflight probe was missing from the set
(j#91992), while five pairs that no call site can emit were widening it.  The derivation
lives in ``tests/support/herdr_dispatch_derivation.py`` and is pinned bidirectionally by
``test_disposable_smoke_command_surface.py``.

The guarantee is scoped to ``(group, subcommand)`` **pairs** and claims nothing more
(review j#91638): matching happens on those two argv tokens, so a brand-new command
pair is denied by default, while an option added to an *already allowlisted* pair is
not something this check closes.  On Herdr v0.7.4 a leading global flag
(``--session <name>`` / ``--remote <target>``) shifts the pair out of the allowlist and
is refused with zero dispatch; that argv-grammar boundary is pinned by a drift test so a
parser change on Herdr's side surfaces rather than silently widening the surface.

Cross-process negative proof (review j#85841 F1)
------------------------------------------------
The counters live on the runner, so ``fork`` gives every worker its own copy and the
parent's ``operator_endpoint_requests == 0`` says nothing about the children — which is
where the real workspace/agent traffic happens.  :class:`EndpointGateCounters` is the
per-process snapshot a worker returns; :class:`EndpointGateEvidence` aggregates the
parent's snapshot with one per worker and is **fail-closed on absence**: a missing or
internally inconsistent worker snapshot is never counted as a zero.

It deliberately does not expose raw ``herdr server`` choreography to callers.  The
CLI composes this object and reports only closed booleans/counts; socket paths,
config paths, subprocess output, and environment values never enter evidence.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import weakref
from dataclasses import InitVar, dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Protocol, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.disposable_endpoint_gate_evidence import (  # noqa: E501
    EndpointGateCounters,
    EndpointGateEvidence,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.shared_space_smoke_observation import (  # noqa: E501
    SharedSpaceSmokeError,
)


HERDR_SOCKET_PATH_ENV = "HERDR_SOCKET_PATH"
HERDR_CLIENT_SOCKET_PATH_ENV = "HERDR_CLIENT_SOCKET_PATH"
HERDR_CONFIG_PATH_ENV = "HERDR_CONFIG_PATH"
_XDG_KEYS = (
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)
_DEFAULT_XDG_SUFFIX = {
    "XDG_CONFIG_HOME": ".config",
    "XDG_CACHE_HOME": ".cache",
    "XDG_DATA_HOME": ".local/share",
    "XDG_STATE_HOME": ".local/state",
}

# Closed refusal vocabulary.  Every refusal names WHY the call was not dispatched, so
# evidence and tests assert on a reason rather than a bare boolean.
REFUSAL_CAPABILITY_ABSENT = "capability_absent"
REFUSAL_CAPABILITY_NOT_MINTED = "capability_not_minted"
REFUSAL_ENDPOINT_UNBOUND = "endpoint_unbound"
REFUSAL_ENDPOINT_OUTSIDE_OWNED_ROOT = "endpoint_outside_owned_root"
REFUSAL_OPERATOR_ENDPOINT_TARGET = "operator_endpoint_target"
#: The owned server child is no longer running, so the capability would degrade into
#: bare socket-path addressing (review j#85841 F2).
REFUSAL_OWNED_CHILD_NOT_ALIVE = "owned_child_not_alive"
#: A destructive control request was issued by a process that did not launch the
#: server (typically a forked smoke worker), which never holds cleanup authority.
REFUSAL_CLEANUP_AUTHORITY_NOT_OWNER = "cleanup_authority_not_owner"
#: The request is not one of the client calls the smoke is allowed to make at all.
#: An *allowlist* miss, so a Herdr control the smoke never needed — including one added
#: after this code was written — is refused rather than silently permitted.
REFUSAL_COMMAND_NOT_ALLOWLISTED = "command_not_allowlisted"

#: Calls addressed to the **owned Herdr server**, as ``(group, subcommand)`` pairs.
#:
#: This is an **allowlist, and that is the point** (review j#91604 F1).  The previous
#: version denylisted a single control (``server stop``) and was therefore fail-open by
#: construction: Herdr 0.7.x also publishes ``session stop <name>`` / ``session delete
#: <name>`` — whose own help names ``default`` as a target — plus ``server
#: reload-config``, ``update``, ``channel set`` and ``config reset-keys``, none of which
#: the denylist caught.  A forked worker could dispatch every one of them.  Enumerating
#: what the smoke *needs* fails closed on the whole rest of the CLI, today and after the
#: next Herdr release, and a miss surfaces as a precise
#: :data:`REFUSAL_COMMAND_NOT_ALLOWLISTED` rather than as an unnoticed capability.
HERDR_SERVER_CLIENT_SUBCOMMANDS: frozenset = frozenset(
    {
        ("workspace", "list"),
        ("workspace", "create"),
        ("tab", "create"),
        ("agent", "start"),
        ("agent", "list"),
        ("agent", "read"),
        ("pane", "close"),
        ("pane", "layout"),
        ("pane", "resize"),
    }
)

#: Calls addressed to the **mozyo-bridge launcher**, not to Herdr at all (Redmine
#: #14658).  Both are actuation-free preflight probes the production session-start path
#: runs before its first herdr write: ``<launcher> herdr agent-attest --help`` asks
#: whether the launcher still carries the #13637 wrapper subcommand, and ``<launcher>
#: config check-parse --file <path>`` makes it parse a config with its own grammar.
#:
#: They are named apart from the server calls because they share only the *shape* of a
#: herdr command.  ``argv[0]`` is the launcher, so ``("herdr", "agent-attest")`` is a
#: mozyo-bridge command group and not a Herdr one — folding it in with the server calls
#: would read as though Herdr had grown a ``herdr`` group.  The gate itself matches on
#: the pair alone, exactly as before; the split is what the *record* says, not a second
#: matching rule.
LAUNCHER_PREFLIGHT_SUBCOMMANDS: frozenset = frozenset(
    {
        ("herdr", "agent-attest"),
        ("config", "check-parse"),
    }
)

#: The closed set of calls the shared-space smoke may dispatch at all.
#:
#: **Derived, not enumerated** (Redmine #14658).  The first version of this set was
#: assembled by reading the code, and the #14185 R3 live run measured what that costs
#: (evidence j#91992): the launcher preflight probe above was missing, so production
#: ``prepare_session`` was refused with :data:`REFUSAL_COMMAND_NOT_ALLOWLISTED` twice
#: before a single workspace existed — while five pairs that no call site can emit
#: (``agent get`` / ``agent pane`` / ``agent target`` / ``pane location`` /
#: ``wait agent-status``) sat in the set widening it.  Three of those five are not
#: commands at all — the same sequence occurs elsewhere in the tree as a JSON key / alias
#: tuple, which is what a text search for a tuple finds and a walk of the call graph does
#: not.  A hand pass that misses a live call and admits five dead ones is not a method, so
#: adding the missing literal would have fixed the run and left the method in place.
#:
#: The authority is now ``tests/support/herdr_dispatch_derivation.py``: it follows the
#: gated runner from this class through the first-party source and reports every call
#: site that can dispatch through it.  ``test_disposable_smoke_command_surface.py`` pins
#: this set against that derivation in **both** directions — a call site whose pair is
#: unlisted fails, and a listed pair no call site emits fails — so neither kind of drift
#: can reach a live run again.
#:
#: What that does NOT claim (unchanged from review j#91638): the guarantee is scoped to
#: ``(group, subcommand)`` pairs.  An option added to an already-listed pair is not
#: something this check closes, so an allowlisted pair that is an exec trampoline
#: (``agent start``, and now ``herdr agent-attest``) is bounded by the ownership and
#: endpoint fences above it, not by this set.
CLIENT_CALL_SUBCOMMANDS: frozenset = (
    HERDR_SERVER_CLIENT_SUBCOMMANDS | LAUNCHER_PREFLIGHT_SUBCOMMANDS
)

#: Closed vocabulary of reasons the owned path may be withheld.  Evidence carries this
#: token verbatim, so it is validated at the producer boundary rather than documented as
#: a convention: an unvalidated string let a caller's path or exception text reach the
#: CLI JSON and the durable record (review j#91741 F4).
WITHHOLD_WORKERS_UNVERIFIED = "workers_unverified"
WITHHOLD_WORKERS_NOT_CONTAINED = "workers_not_contained"
ROOT_WITHHOLD_REASONS: frozenset = frozenset(
    {WITHHOLD_WORKERS_UNVERIFIED, WITHHOLD_WORKERS_NOT_CONTAINED}
)

#: Closed vocabulary for what we actually observed about the owned tree.  ``unknown`` is
#: its own answer, distinct from ``absent`` — collapsing them is how an unreadable root
#: got reported as released (review j#91741 F2).
ROOT_OBSERVATION_ABSENT = "absent"
ROOT_OBSERVATION_PRESENT = "present"
ROOT_OBSERVATION_UNKNOWN = "unknown"

#: The only control beyond the client calls, and only for the process that launched the
#: child: the graceful stop of *its own* server.  Deliberately not reachable from a
#: forked worker, which is refused with :data:`REFUSAL_CLEANUP_AUTHORITY_NOT_OWNER`.
MINTER_ONLY_SUBCOMMANDS: frozenset = frozenset({("server", "stop")})

_ENDPOINT_CAPABILITY_TOKEN = object()
# Identity registry for minted capabilities.  ``isinstance`` alone is forgeable through
# a subclass or a copy; membership here is not.
_MINTED_CAPABILITIES: "weakref.WeakSet[OwnedEndpointCapability]" = weakref.WeakSet()


class SmokeEndpointEscapeError(SharedSpaceSmokeError):
    """A Herdr CLI request was refused because it was not provably endpoint-owned.

    Raised strictly *before* the request is dispatched, so a caller that sees this
    error knows the external request count for that call is zero.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        message = f"refused unbound herdr request ({reason})"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class OwnedServerProcess(Protocol):
    """The exact process handle the lifecycle is allowed to stop."""

    pid: int

    def poll(self) -> Optional[int]: ...
    def wait(self, timeout: Optional[float] = None) -> int: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


@dataclass(frozen=True)
class DisposableHerdrBinding:
    """Where a disposable endpoint *would* live.  Deliberately NOT an authority.

    These are plain paths, computed before anything is launched.  They are used to
    write the disposable config and to build the child env — never to decide whether a
    request may be dispatched.  That decision needs :class:`OwnedEndpointCapability`,
    because "the path matches" is not "we own the thing behind the path".
    """

    root: Path
    socket_path: Path
    client_socket_path: Path
    config_path: Path


@dataclass(frozen=True, eq=False)
class OwnedEndpointCapability:
    """Proof that this process launched, and therefore may address, one server.

    Minted ONLY by :meth:`DisposableHerdrInstance.start`, after :class:`subprocess.Popen`
    returned the handle of a server *we* started, and registered in the module-private
    mint registry.  Three constraints back the guarantee (nothing more is claimed):

    * it is **immutable** (a frozen dataclass — fields cannot be reassigned) and
      compares by **identity** (``eq=False``), so an equal-valued clone produced by
      ``copy``/pickle is not the minted object and fails the registry check;
    * construction requires the module-private mint token, so a hand-built capability
      is refused at ``__init__`` (mirrors ``IsolationCapability``, review j#83905 F1);
    * it carries ``owner_pid``, the pid of the child we launched, so the gate can
      re-check at actuation time that the process it is cleaning up is still the one
      the capability was minted for — rather than trusting a socket path that any
      environment may have redirected (design disposition j#85756 item 4);
    * it carries ``minter_pid``, the pid of the process that launched that child.  A
      forked smoke worker inherits the capability but not the parent relationship, so
      this is what separates client-call capability from cleanup authority and tells
      the gate which process is even *able* to answer the liveness question
      (review j#85841 F2).

    ``owner_pid`` alone is a stale record once the child exits; liveness is enforced by
    :class:`DisposableHerdrInstance`, not claimed by this value object.
    """

    root: Path
    socket_path: Path
    client_socket_path: Path
    config_path: Path
    owner_pid: int
    minter_pid: int = 0
    _mint_token: InitVar[object] = None

    def __post_init__(self, _mint_token: object) -> None:
        if _mint_token is not _ENDPOINT_CAPABILITY_TOKEN:
            raise SmokeEndpointEscapeError(
                REFUSAL_CAPABILITY_NOT_MINTED,
                "OwnedEndpointCapability must be minted by DisposableHerdrInstance.start()",
            )


def _mint_owned_endpoint(
    binding: DisposableHerdrBinding, owner_pid: int
) -> OwnedEndpointCapability:
    capability = OwnedEndpointCapability(
        root=binding.root,
        socket_path=binding.socket_path,
        client_socket_path=binding.client_socket_path,
        config_path=binding.config_path,
        owner_pid=int(owner_pid),
        # Captured here, not passed in: only the process running this mint is the one
        # that just launched the child, so cleanup authority cannot be handed around.
        minter_pid=os.getpid(),
        _mint_token=_ENDPOINT_CAPABILITY_TOKEN,
    )
    _MINTED_CAPABILITIES.add(capability)
    return capability


class EndpointBoundHerdrRunner:
    """Dispatch a Herdr CLI call only after proving it targets the owned endpoint.

    The wrapped runner keeps its normal ``subprocess.run`` signature.  A caller cannot
    accidentally drop the endpoint by passing another ``env`` mapping: the disposable
    binding is always applied last.  For ``agent start`` only, the operator's original
    XDG homes are explicitly restored on the child agent via Herdr's documented
    repeated ``--env`` flags.  Thus Herdr's own config/state is disposable while real
    Claude/Codex processes retain their normal auth/config.

    Pre-actuation gate (Redmine #14187, blocker j#85754 / disposition j#85756)
    -------------------------------------------------------------------------
    Every call computes the **effective** ``HERDR_SOCKET_PATH`` the child would
    receive, then requires ALL of:

    * a capability is available and is one this module minted;
    * the effective socket equals the capability's socket;
    * that socket lives inside the capability's owned root;
    * that socket is not an operator endpoint captured at construction time.

    Any miss raises :class:`SmokeEndpointEscapeError` **before** the inner runner is
    called, so ``dispatched_calls`` — and any request the operator's server could ever
    observe — stays at zero for that call.

    Two independent counters keep the negative proof load-bearing against two
    *different* regressions (the constant-``False`` vacuity of the earlier version
    could detect neither):

    * drop the binding → the gate fires → ``escape_refusals`` rises and
      ``dispatched_calls`` does not;
    * drop the gate → the operator-socket call actually dispatches →
      ``operator_endpoint_requests`` rises.

    ``tests/unit/.../test_disposable_herdr_instance.py`` runs both mutations against a
    fake inner runner with a scrubbed ambient env, which is the only sanctioned way to
    probe this guard (disposition j#85756 item 5).
    """

    def __init__(
        self,
        inner,
        *,
        capability_provider: Callable[[], Optional[OwnedEndpointCapability]],
        binding_env: Mapping[str, str],
        agent_env: Mapping[str, str],
        operator_socket_paths: Sequence[str] = (),
        lifecycle_authority: Optional[Callable[[Sequence[str]], str]] = None,
    ) -> None:
        self._inner = inner
        self._capability_provider = capability_provider
        self._binding_env = dict(binding_env)
        self._agent_env = dict(agent_env)
        self._operator_sockets = tuple(
            sorted({str(path) for path in operator_socket_paths if str(path or "")})
        )
        # Consulted per call, AFTER the capability conjunction and still before dispatch.
        # It answers the questions only the lifecycle can answer — is the owned child
        # still alive, and may *this* process issue a destructive control request.
        self._lifecycle_authority = lifecycle_authority or (lambda command: "")
        self.calls = 0
        self.dispatched_calls = 0
        self.bound_calls = 0
        self.operator_endpoint_requests = 0
        self.escape_refusals = 0
        self.last_refusal_reason = ""
        #: Every closed refusal token this runner produced.  Kept alongside the count
        #: so a receipt's count and its vocabulary can corroborate each other.
        self.refusal_reasons: set = set()

    def __call__(self, argv, *args, **kwargs):
        command = list(argv)
        if command[1:3] == ["agent", "start"]:
            command = self._restore_agent_environment(command)
        supplied = kwargs.get("env")
        merged = dict(os.environ if supplied is None else supplied)
        merged.update(self._binding_env)
        kwargs["env"] = merged
        self.calls += 1
        effective = merged.get(HERDR_SOCKET_PATH_ENV, "")
        refusal = self._refusal_reason(effective, command)
        if refusal:
            self.escape_refusals += 1
            self.last_refusal_reason = refusal
            self.refusal_reasons.add(refusal)
            # Nothing has been executed yet: external request count for this call is 0.
            raise SmokeEndpointEscapeError(refusal)
        # Reached only when the gate passed — or when a mutation removed the gate, which
        # is exactly what ``operator_endpoint_requests`` is here to catch.
        self.dispatched_calls += 1
        # Derived independently of the gate.  If it were incremented next to
        # ``dispatched_calls`` unconditionally, ``bound_calls == dispatched_calls`` could
        # never fail and ``all_calls_bound`` would be the constant-true vacuity that
        # #14247 warns about: a mutation that removes the gate must move these apart.
        if self._targets_owned_socket(effective):
            self.bound_calls += 1
        if effective in self._operator_sockets:
            self.operator_endpoint_requests += 1
        return self._inner(command, *args, **kwargs)

    run = __call__

    def _targets_owned_socket(self, effective: str) -> bool:
        """Whether ``effective`` is the socket of a capability this module minted."""
        capability = self._capability_provider()
        if capability is None or capability not in _MINTED_CAPABILITIES:
            return False
        return bool(effective) and effective == str(capability.socket_path)

    def _refusal_reason(self, effective: str, command: Sequence[str] = ()) -> str:
        capability = self._capability_provider()
        if capability is None:
            return REFUSAL_CAPABILITY_ABSENT
        if capability not in _MINTED_CAPABILITIES:
            return REFUSAL_CAPABILITY_NOT_MINTED
        owned_socket = str(capability.socket_path)
        if not effective or effective != owned_socket:
            return REFUSAL_ENDPOINT_UNBOUND
        if Path(owned_socket).parent != Path(capability.root):
            return REFUSAL_ENDPOINT_OUTSIDE_OWNED_ROOT
        if effective in self._operator_sockets:
            return REFUSAL_OPERATOR_ENDPOINT_TARGET
        return self._lifecycle_authority(list(command))

    @property
    def all_calls_bound(self) -> bool:
        """Every dispatched call carried the owned socket, and nothing was refused."""
        return (
            self.dispatched_calls > 0
            and self.bound_calls == self.dispatched_calls
            and self.escape_refusals == 0
        )

    @property
    def operator_endpoint_connected(self) -> bool:
        """At least one request actually reached an operator endpoint (must stay False)."""
        return self.operator_endpoint_requests > 0

    def _restore_agent_environment(self, argv: Sequence[str]) -> list[str]:
        command = list(argv)
        try:
            separator = command.index("--")
        except ValueError:
            # The production builder always emits ``--``.  Leave malformed input
            # unchanged so the real command fails closed at its normal boundary.
            return command
        flags: list[str] = []
        for key in _XDG_KEYS:
            value = self._agent_env.get(key, "")
            if value:
                flags.extend(["--env", f"{key}={value}"])
        return [*command[:separator], *flags, *command[separator:]]


class DisposableHerdrInstance:
    """Own one exact Herdr server process and its isolated endpoint/state tree."""

    def __init__(
        self,
        *,
        binary: str,
        root: Path,
        base_env: Mapping[str, str],
        runner=subprocess.run,
        popen_factory=subprocess.Popen,
        startup_timeout: float = 10.0,
        shutdown_timeout: float = 10.0,
        sleeper: Callable[[float], None] = time.sleep,
        ambient_env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.binary = binary
        self.root = Path(root).expanduser().resolve()
        self.base_env = dict(base_env)
        self._runner = runner
        self._popen_factory = popen_factory
        self.startup_timeout = float(startup_timeout)
        self.shutdown_timeout = float(shutdown_timeout)
        self._sleep = sleeper
        self.binding = DisposableHerdrBinding(
            root=self.root,
            socket_path=self.root / "herdr.sock",
            client_socket_path=self.root / "herdr-client.sock",
            config_path=self.root / "config.toml",
        )
        self._process: Optional[OwnedServerProcess] = None
        self._capability: Optional[OwnedEndpointCapability] = None
        self.started = False
        self.ready = False
        self.stopped = False
        self.graceful_stop_refused = False
        self.endpoint_residue = -1
        # Root-release policy lives on the lifecycle, not on a call argument, so that
        # EVERY teardown path obeys the same single decision — including the implicit
        # ``__exit__`` of ``with instance:``, which used to release the tree with the
        # default before a containment verdict could reach the explicit shutdown
        # (review j#91687 F1).  It starts permitted only because no worker exists yet.
        self._root_release_permitted = True
        self._root_withhold_reason = ""
        #: Observed AFTER teardown, never inferred from a flag: the two used to
        #: disagree in both directions (review j#91687 F4).
        self.owned_root_present = True
        self.owned_root_observation = ROOT_OBSERVATION_PRESENT
        # Operator endpoints the gate must never target.  BOTH sources are captured:
        # the caller's declared env, and the true process ambient — the incident's
        # unbound call inherited ``os.environ``, not ``base_env`` (j#85754).
        ambient = os.environ if ambient_env is None else ambient_env
        self._operator_sockets = tuple(
            sorted(
                {
                    str(value)
                    for value in (
                        self.base_env.get(HERDR_SOCKET_PATH_ENV, ""),
                        dict(ambient).get(HERDR_SOCKET_PATH_ENV, ""),
                    )
                    if str(value or "")
                }
            )
        )
        self.runner = EndpointBoundHerdrRunner(
            runner,
            capability_provider=self._current_capability,
            binding_env=self._binding_env(),
            agent_env=self._operator_agent_env(),
            operator_socket_paths=self._operator_sockets,
            lifecycle_authority=self._lifecycle_authority_refusal,
        )

    def _current_capability(self) -> Optional[OwnedEndpointCapability]:
        """The capability, only while it still describes the process we own.

        Before :meth:`start` there is none, so every request fails closed.  After the
        owned handle is released (or replaced), the pid check withdraws it — authority
        follows the owned child identity, not a path that outlived it.

        Liveness is deliberately NOT checked here: this provider is also consulted by
        forked smoke workers, where ``poll()`` cannot answer the question (see the
        module docstring).  The liveness and cleanup-authority fences live in
        :meth:`_lifecycle_authority_refusal`, which the gate consults on the same call,
        still before dispatch.
        """
        capability = self._capability
        process = self._process
        if capability is None or process is None:
            return None
        if getattr(process, "pid", None) != capability.owner_pid:
            return None
        return capability

    def _is_minting_process(self, capability: OwnedEndpointCapability) -> bool:
        """Whether this process is the one that launched the owned child."""
        return os.getpid() == capability.minter_pid

    def _lifecycle_authority_refusal(self, command: Sequence[str]) -> str:
        """Why this call must not be dispatched (``""`` when it may proceed).

        Evaluated before the inner runner, so a refusal still means zero external
        requests for that call.  Two rules, matching the two authorities:

        * a request outside :data:`CLIENT_CALL_SUBCOMMANDS` is refused for everyone
          unless it is the minter's own :data:`MINTER_ONLY_SUBCOMMANDS` graceful stop —
          an allowlist, so an unforeseen Herdr control is denied by default rather than
          having to be predicted (review j#91604 F1);
        * that graceful stop is refused
          unless this process minted the capability.  A forked worker holding an
          inherited copy is not the server's parent and never needs to stop it;
        * in the minting process — the only one that can answer — the owned child must
          still be running.  Otherwise the capability has decayed into the bare
          socket-path addressing the threat model rejects, and a stranger that took
          over the path would receive the request (review j#85841 F2).
        """
        capability = self._current_capability()
        if capability is None:
            return REFUSAL_CAPABILITY_ABSENT
        subcommand = tuple(list(command)[1:3])
        minting = self._is_minting_process(capability)
        if subcommand not in CLIENT_CALL_SUBCOMMANDS:
            # Not a client call.  Either it is the one control the minter may issue, or
            # it is outside the sanctioned surface entirely — a Herdr lifecycle control
            # the smoke never needed, which nobody here may send.
            if subcommand not in MINTER_ONLY_SUBCOMMANDS:
                return REFUSAL_COMMAND_NOT_ALLOWLISTED
            if not minting:
                return REFUSAL_CLEANUP_AUTHORITY_NOT_OWNER
        if not minting:
            return ""
        process = self._process
        if process is None or process.poll() is not None:
            return REFUSAL_OWNED_CHILD_NOT_ALIVE
        return ""

    @property
    def capability(self) -> Optional[OwnedEndpointCapability]:
        return self._current_capability()

    @property
    def process_alive(self) -> bool:
        """Whether the owned server child is still running (invariance assertions)."""
        process = self._process
        return process is not None and process.poll() is None

    def _binding_env(self) -> dict[str, str]:
        return {
            HERDR_SOCKET_PATH_ENV: str(self.binding.socket_path),
            HERDR_CLIENT_SOCKET_PATH_ENV: str(self.binding.client_socket_path),
            HERDR_CONFIG_PATH_ENV: str(self.binding.config_path),
            "XDG_CONFIG_HOME": str(self.root / "xdg-config"),
            "XDG_CACHE_HOME": str(self.root / "xdg-cache"),
            "XDG_DATA_HOME": str(self.root / "xdg-data"),
            "XDG_STATE_HOME": str(self.root / "xdg-state"),
        }

    def _operator_agent_env(self) -> dict[str, str]:
        home = Path(self.base_env.get("HOME", str(Path.home()))).expanduser()
        restored: dict[str, str] = {}
        for key in _XDG_KEYS:
            restored[key] = self.base_env.get(
                key, str(home / _DEFAULT_XDG_SUFFIX[key])
            )
        return restored

    def child_env(self) -> dict[str, str]:
        """Trusted launch env for harness/provider resolution, endpoint-bound."""
        env = dict(self.base_env)
        env.update(self._binding_env())
        env["MOZYO_HERDR_BINARY"] = self.binary
        return env

    def __enter__(self) -> "DisposableHerdrInstance":
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.shutdown()
        return False

    def start(self) -> None:
        if self._process is not None:
            raise SharedSpaceSmokeError("disposable Herdr instance already started")
        if self.root.exists() and any(self.root.iterdir()):
            raise SharedSpaceSmokeError(
                "disposable Herdr instance root is not empty; refuse to adopt or "
                "overwrite an existing endpoint/state tree"
            )
        self.root.mkdir(parents=True, exist_ok=True)
        self.binding.config_path.write_text(
            "onboarding = false\n\n[update]\n"
            "version_check = false\nmanifest_check = false\n",
            encoding="utf-8",
        )
        env = self.child_env()
        if str(self.binding.socket_path) in self._operator_sockets:
            raise SharedSpaceSmokeError(
                "disposable Herdr endpoint collides with an ambient operator endpoint"
            )
        try:
            self._process = self._popen_factory(
                [self.binary, "server"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise SharedSpaceSmokeError(
                f"could not start disposable Herdr server ({exc.__class__.__name__})"
            ) from exc
        self.started = True
        # Authority exists only from here: we hold the handle of a server we launched.
        self._capability = _mint_owned_endpoint(
            self.binding, getattr(self._process, "pid", -1)
        )
        deadline = time.monotonic() + max(0.1, self.startup_timeout)
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                break
            try:
                completed = self.runner(
                    [self.binary, "workspace", "list"],
                    capture_output=True,
                    text=True,
                    timeout=min(1.0, self.startup_timeout),
                )
            except SmokeEndpointEscapeError:
                # Never retried and never downgraded: an unbound readiness probe means
                # the binding itself is broken.  Tear the owned child down and surface it.
                self.shutdown()
                raise
            except (OSError, subprocess.TimeoutExpired):
                completed = None
            if completed is not None and completed.returncode == 0:
                self.ready = True
                return
            self._sleep(0.05)
        self.shutdown()
        raise SharedSpaceSmokeError(
            "disposable Herdr server did not become ready within the bounded startup window"
        )

    def withhold_root_release(self, reason: str) -> None:
        """Bind a containment verdict to the lifecycle: do not free the owned path.

        Called when an owned worker outlived even its ``kill``, or when the count could
        not be established at all.  A worker holds client-call capability bound to
        *this* socket path, so removing the tree would free that path for anything else
        to bind — the takeover the threat model exists to prevent.  The tree therefore
        stays, its presence is reported, and the run cannot claim success.

        Deliberately **not** a shutdown argument.  As an argument it only constrained
        the call site that passed it, and the context manager's own ``__exit__`` had
        already released the tree with the default (review j#91687 F1).
        """
        token = str(reason)
        if token not in ROOT_WITHHOLD_REASONS:
            raise SharedSpaceSmokeError(
                "root-release withhold reason must be one of "
                f"{sorted(ROOT_WITHHOLD_REASONS)}"
            )
        self._root_release_permitted = False
        self._root_withhold_reason = token

    def permit_root_release(self) -> None:
        """Record that containment was positively established, so the tree may go."""
        self._root_release_permitted = True
        self._root_withhold_reason = ""

    @property
    def root_release_withheld(self) -> bool:
        return not self._root_release_permitted

    @property
    def root_withhold_reason(self) -> str:
        return self._root_withhold_reason

    def _probe_root(self) -> str:
        """Tri-state presence of the owned tree, as a closed token.

        ``Path.exists()`` cannot be used here: it folds ``stat`` failures into ``False``
        without raising, so an unreadable root read as *absent* and the run reported the
        tree released while it was still on disk (review j#91741 F2).  ``os.lstat`` is
        asked directly, and only ``FileNotFoundError`` means absent — every other
        ``OSError`` is ``unknown``, which is never treated as gone.
        """
        try:
            os.lstat(self.root)
        except FileNotFoundError:
            return ROOT_OBSERVATION_ABSENT
        except OSError:
            return ROOT_OBSERVATION_UNKNOWN
        return ROOT_OBSERVATION_PRESENT

    def _observe_root(self) -> None:
        """Record what is actually on disk, rather than what we intended."""
        self.owned_root_observation = self._probe_root()
        # Only a positive ``absent`` clears it; ``unknown`` stays on the safe side.
        self.owned_root_present = (
            self.owned_root_observation != ROOT_OBSERVATION_ABSENT
        )

    def _release_root_if_permitted(self) -> None:
        """Remove exactly the owned tree when the lifecycle policy allows it.

        Runs on every shutdown path, including the one where the server never started
        (a failed ``Popen`` still left a written ``config.toml`` behind, which the old
        early return skipped — review j#91687 F4).
        """
        # Same tri-state authority before the action as after it: an ``unknown`` root is
        # still attempted, because "cannot tell" must not silently skip cleanup.
        if (
            self._root_release_permitted
            and self._probe_root() != ROOT_OBSERVATION_ABSENT
        ):
            try:
                shutil.rmtree(self.root)
            except FileNotFoundError:
                pass
            except OSError:
                # Left for the observation below to report rather than swallowed.
                pass
        self._observe_root()

    def shutdown(self) -> None:
        """Stop the owned child and, unless containment withholds it, remove its tree.

        The server is stopped whether or not the path is released: killing our own
        endpoint makes a survivor's calls fail to connect, which strengthens
        containment rather than weakening it.  What containment withholds is only the
        *release of the path*.
        """
        process = self._process
        if process is None:
            self._release_root_if_permitted()
            return
        if process.poll() is None:
            try:
                self.runner(
                    [self.binary, "server", "stop"],
                    capture_output=True,
                    text=True,
                    timeout=self.shutdown_timeout,
                )
            except SmokeEndpointEscapeError:
                # The graceful path is exactly the request that stopped the operator's
                # server in j#85754.  Refused here with zero external requests; the exact
                # owned handle below still guarantees cleanup, and it cannot address any
                # other server.
                self.graceful_stop_refused = True
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            process.wait(timeout=self.shutdown_timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=self.shutdown_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=self.shutdown_timeout)
        self.stopped = process.poll() is not None
        self.endpoint_residue = sum(
            int(path.exists())
            for path in (self.binding.socket_path, self.binding.client_socket_path)
        )
        # Only this exact, caller-provided instance root is removed.  The lifecycle
        # never scans for or kills another process and never removes a parent tree.
        if self.stopped:
            self._release_root_if_permitted()
            if not self.owned_root_present:
                self.endpoint_residue = 0
        else:
            self._observe_root()
        self._process = None
        self._capability = None

    def gate_evidence(self) -> EndpointGateEvidence:
        """This process's own endpoint-gate counters, as a single-process aggregate."""
        return EndpointGateEvidence.for_single_process(self.runner)

    def as_evidence(
        self, *, gate: Optional[EndpointGateEvidence] = None
    ) -> dict[str, object]:
        """Closed, path-free lifecycle/negative-proof facts.

        ``operator_endpoint_requests`` counts requests that were actually dispatched to
        an operator endpoint; ``endpoint_escape_refusals`` counts requests the gate
        stopped before dispatch.  A healthy run has both at zero.  A dropped binding
        raises the second; a dropped gate raises the first.  Neither can be satisfied by
        a constant, which the earlier hardcoded negative control could (review #14247).

        ``gate`` names the **scope** of that negative proof.  Omitted, it covers this
        process only — correct for a lifecycle that forked nothing, and misleading for
        one that did, because the counters do not cross a ``fork``.  A cross-process
        driver passes the aggregate it collected from its workers, which also carries
        whether every worker receipt was actually present and self-consistent
        (review j#85841 F1).
        """
        scope = self.gate_evidence() if gate is None else gate
        return {
            "server_started": self.started,
            "server_ready": self.ready,
            **scope.as_evidence(),
            "graceful_stop_refused": self.graceful_stop_refused,
            "server_stopped": self.stopped,
            "endpoint_residue": self.endpoint_residue,
            # Observed state, not the policy flag: the two disagreed in both directions
            # before (root gone but reported withheld; root present but reported
            # released) — review j#91687 F4.
            "owned_root_present": self.owned_root_present,
            "owned_root_observation": self.owned_root_observation,
            # Only a POSITIVE absent observation counts as released.
            "owned_root_released": (
                self.owned_root_observation == ROOT_OBSERVATION_ABSENT
            ),
            "root_withhold_reason": self._root_withhold_reason,
        }


__all__ = (
    "CLIENT_CALL_SUBCOMMANDS",
    "HERDR_SERVER_CLIENT_SUBCOMMANDS",
    "LAUNCHER_PREFLIGHT_SUBCOMMANDS",
    "ROOT_OBSERVATION_ABSENT",
    "ROOT_OBSERVATION_PRESENT",
    "ROOT_OBSERVATION_UNKNOWN",
    "ROOT_WITHHOLD_REASONS",
    "WITHHOLD_WORKERS_NOT_CONTAINED",
    "WITHHOLD_WORKERS_UNVERIFIED",
    "MINTER_ONLY_SUBCOMMANDS",
    "DisposableHerdrBinding",
    "DisposableHerdrInstance",
    "EndpointBoundHerdrRunner",
    "EndpointGateCounters",
    "EndpointGateEvidence",
    "OwnedEndpointCapability",
    "SmokeEndpointEscapeError",
    "HERDR_CLIENT_SOCKET_PATH_ENV",
    "HERDR_CONFIG_PATH_ENV",
    "HERDR_SOCKET_PATH_ENV",
    "REFUSAL_CAPABILITY_ABSENT",
    "REFUSAL_CAPABILITY_NOT_MINTED",
    "REFUSAL_CLEANUP_AUTHORITY_NOT_OWNER",
    "REFUSAL_COMMAND_NOT_ALLOWLISTED",
    "REFUSAL_ENDPOINT_OUTSIDE_OWNED_ROOT",
    "REFUSAL_ENDPOINT_UNBOUND",
    "REFUSAL_OPERATOR_ENDPOINT_TARGET",
    "REFUSAL_OWNED_CHILD_NOT_ALIVE",
)
