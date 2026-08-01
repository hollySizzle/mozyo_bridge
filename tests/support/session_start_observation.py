"""Instrumented observation of what production session-start actually dispatches.

Redmine #14658, ruling j#93308 / authorization j#93312.

Twelve review rounds established that a static walk cannot bound this question.  Each
round closed one Python spelling and the next round opened another — decorated dunders,
``metaclass=``, annotated bindings, unpacking targets, namespace-writing right-hand
sides, ``type.__setattr__``, aliases, helpers, ``type(self)`` inside a constructor,
``globals()[...]``.  The ruling's conclusion is the one this module implements: the
authority becomes **execution**, and the static derivation is demoted to a drift
diagnostic that may not silence an unknown candidate.

What is instrumented is the real boundary, not a paraphrase of it: every scenario drives
production code through the actual gated runner, and the tape is the argv the gate was
asked to dispatch.  A scenario that cannot reach its branch is a failure of the scenario,
not a property of the system, so each one asserts its own precondition.

Three fail-closed rules follow, and they are what make the tape evidence rather than
decoration:

* an observed argv whose ``(group, subcommand)`` is not admitted fails;
* an admitted pair no scenario observes fails — coverage is proven, not assumed;
* an observed argv the gate never saw (a bypass) fails, because a call that reaches the
  server without passing the gate is exactly the escape this subsystem exists to refuse.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT / "src", ROOT / "tests", ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


@dataclass
class ScenarioResult:
    """One designated branch, executed."""

    name: str
    #: Every argv the gated runner was asked to dispatch, in order.
    argvs: list = field(default_factory=list)
    #: Calls that reached the backend without passing the gate.  Always empty in a
    #: healthy run; non-empty is a bypass and fails the oracle.
    bypassed: list = field(default_factory=list)

    @property
    def pairs(self) -> set:
        return {tuple(a[:2]) for a in self.argvs if len(a) >= 2}


class GateTape:
    """Records what the gate dispatched, and what reached the backend without it.

    Both directions matter.  Recording only what the gate passed would make a bypass —
    a call that never consulted the gate — indistinguishable from no call at all, which
    is the same "a lost flow looks like no flow" failure the static walk kept making.
    """

    def __init__(self, backend: Callable) -> None:
        self._backend = backend
        self.through_gate: list = []
        self.at_backend: list = []

    def gated(self, argv, *args, **kwargs):
        self.through_gate.append(list(argv))
        return self._backend(argv, *args, **kwargs)

    def raw(self, argv, *args, **kwargs):
        self.at_backend.append(list(argv))
        return self._backend(argv, *args, **kwargs)

    @property
    def bypassed(self) -> list:
        """Calls seen at the backend that the gate never recorded."""
        seen = [tuple(a) for a in self.through_gate]
        out = []
        for call in self.at_backend:
            key = tuple(call)
            if key in seen:
                seen.remove(key)
            else:
                out.append(call)
        return out


def executable_stub(path: Path, body: str = "exit 0") -> Path:
    """A real executable file — resolvable by ``shutil.which`` and by an exec probe."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def pair_of(argv) -> tuple:
    """The ``(group, subcommand)`` the GATE reads out of a **gate-boundary** argv.

    This is the gate's own rule, ``command[1:3]``, and it is deliberately the only
    normalisation in this module: a pair means what the gate means by it, or the tape is
    not evidence about the gate.

    It takes gate-boundary argvs only — the ones production passes as
    ``[binary, group, subcommand, ...]``.  An earlier version claimed to accept the
    backend fake's already-stripped records too, by falling back on length.  Measured,
    that claim is impossible to keep: ``['pane', 'close', 'w1:p1']`` and
    ``['/x/herdr', 'pane', 'close']`` have the same length and different meanings, and the
    fallback silently read the first as ``('close', 'w1:p1')``.  Guessing a shape from a
    length is the same class of error as guessing a flow from a syntax — so the caller
    records full argvs (see :class:`GateTape`, which captures them before any stripping)
    and this function does not guess.
    """
    if len(argv) < 3:
        raise ValueError(
            f"not a gate-boundary argv (expected [binary, group, subcommand, ...]): {argv!r}"
        )
    return tuple(argv[1:3])


def gated_instance(*, binary: str, root: Path, home: Path, path: str, backend,
                   popen_factory=None):
    """A real :class:`DisposableHerdrInstance` whose gate can be instrumented.

    ``instance.runner`` IS the production ``EndpointBoundHerdrRunner``.  Driving
    production code through it — rather than handing the fake straight to the harness —
    is what makes the tape evidence about the gated boundary the ruling named, instead of
    evidence about a fake that no gate ever guarded.
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.disposable_herdr_instance import (  # noqa: E501
        DisposableHerdrInstance,
    )

    return DisposableHerdrInstance(
        binary=binary,
        root=root,
        base_env={"HOME": str(home), "PATH": path},
        runner=backend,
        popen_factory=popen_factory or (lambda *a, **k: _StubProcess()),
        startup_timeout=1.0,
        shutdown_timeout=1.0,
    )


class _StubProcess:
    """A server process stand-in: alive until asked to stop, then not."""

    def __init__(self, *a, **k) -> None:
        self.pid = 4242
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        self._alive = False
        return 0

    def terminate(self) -> None:
        self._alive = False

    kill = terminate


def launch_env(bindir: Path, *, launcher: bool = False) -> dict:
    """The trusted launch env, optionally carrying a resolvable attest launcher.

    ``launcher=False`` reproduces the pre-#14658 harness exactly: no launcher resolves,
    so session-start takes the unwrapped fallback.  That is the configuration under which
    the #14185 R3 refusal was invisible, and it is kept as a scenario rather than deleted
    so the difference between the two branches stays measurable.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    for name in ("herdr", "claude", "codex"):
        executable_stub(bindir / name)
    env = {
        "MOZYO_HERDR_BINARY": str(bindir / "herdr"),
        "PATH": str(bindir),
    }
    if launcher:
        env["MOZYO_BRIDGE_LAUNCHER"] = str(executable_stub(bindir / "fake-mozyo-bridge"))
    return env
