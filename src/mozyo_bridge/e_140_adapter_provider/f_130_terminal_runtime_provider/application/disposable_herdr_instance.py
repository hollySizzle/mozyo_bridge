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
      environment may have redirected (design disposition j#85756 item 4).
    """

    root: Path
    socket_path: Path
    client_socket_path: Path
    config_path: Path
    owner_pid: int
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
    ) -> None:
        self._inner = inner
        self._capability_provider = capability_provider
        self._binding_env = dict(binding_env)
        self._agent_env = dict(agent_env)
        self._operator_sockets = tuple(
            sorted({str(path) for path in operator_socket_paths if str(path or "")})
        )
        self.calls = 0
        self.dispatched_calls = 0
        self.bound_calls = 0
        self.operator_endpoint_requests = 0
        self.escape_refusals = 0
        self.last_refusal_reason = ""

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
        refusal = self._refusal_reason(effective)
        if refusal:
            self.escape_refusals += 1
            self.last_refusal_reason = refusal
            # Nothing has been executed yet: external request count for this call is 0.
            raise SmokeEndpointEscapeError(refusal)
        # Reached only when the gate passed — or when a mutation removed the gate, which
        # is exactly what ``operator_endpoint_requests`` is here to catch.
        self.dispatched_calls += 1
        self.bound_calls += 1
        if effective in self._operator_sockets:
            self.operator_endpoint_requests += 1
        return self._inner(command, *args, **kwargs)

    run = __call__

    def _refusal_reason(self, effective: str) -> str:
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
        return ""

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
        )

    def _current_capability(self) -> Optional[OwnedEndpointCapability]:
        """The capability, only while it still describes the process we own.

        Before :meth:`start` there is none, so every request fails closed.  After the
        owned handle is released (or replaced), the pid check withdraws it — authority
        follows the owned child identity, not a path that outlived it.
        """
        capability = self._capability
        process = self._process
        if capability is None or process is None:
            return None
        if getattr(process, "pid", None) != capability.owner_pid:
            return None
        return capability

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

    def shutdown(self) -> None:
        process = self._process
        if process is None:
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
        if self.stopped and self.root.exists():
            shutil.rmtree(self.root)
            self.endpoint_residue = 0
        self._process = None
        self._capability = None

    def as_evidence(self) -> dict[str, object]:
        """Closed, path-free lifecycle/negative-proof facts.

        ``operator_endpoint_requests`` counts requests that were actually dispatched to
        an operator endpoint; ``endpoint_escape_refusals`` counts requests the gate
        stopped before dispatch.  A healthy run has both at zero.  A dropped binding
        raises the second; a dropped gate raises the first.  Neither can be satisfied by
        a constant, which the earlier hardcoded negative control could (review #14247).
        """
        return {
            "server_started": self.started,
            "server_ready": self.ready,
            "endpoint_bound": self.runner.all_calls_bound,
            "operator_server_connected": self.runner.operator_endpoint_connected,
            "operator_endpoint_requests": self.runner.operator_endpoint_requests,
            "endpoint_escape_refusals": self.runner.escape_refusals,
            "graceful_stop_refused": self.graceful_stop_refused,
            "server_stopped": self.stopped,
            "endpoint_residue": self.endpoint_residue,
        }


__all__ = (
    "DisposableHerdrBinding",
    "DisposableHerdrInstance",
    "EndpointBoundHerdrRunner",
    "OwnedEndpointCapability",
    "SmokeEndpointEscapeError",
    "HERDR_CLIENT_SOCKET_PATH_ENV",
    "HERDR_CONFIG_PATH_ENV",
    "HERDR_SOCKET_PATH_ENV",
    "REFUSAL_CAPABILITY_ABSENT",
    "REFUSAL_CAPABILITY_NOT_MINTED",
    "REFUSAL_ENDPOINT_OUTSIDE_OWNED_ROOT",
    "REFUSAL_ENDPOINT_UNBOUND",
    "REFUSAL_OPERATOR_ENDPOINT_TARGET",
)
