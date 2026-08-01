"""Host AF_UNIX endpoint-path budget, measured before any actuation (Redmine #14657).

Incident (#14185 R3 live smoke j#91992)
---------------------------------------
The first live ``herdr smoke-shared-space --execute`` run derived its disposable
endpoint under an isolated home deep inside a temp tree.  The resulting socket path was
216 bytes, over the host's ``sockaddr_un.sun_path`` capacity, so the *server child's*
own ``bind()`` failed with ``OSError: AF_UNIX path too long``.  That child runs with
``stderr=DEVNULL``, so the error was never read: the readiness loop simply never saw a
ready server and the run ended in the generic ``did not become ready within the bounded
startup window``.  Re-running the identical smoke under a short owned tmp path (socket
61 bytes) started, converged and cleaned up.

Fail-closed behaviour, cleanup and operator-endpoint non-contact all held.  The defect
is narrower and entirely diagnostic: **an unbindable endpoint path was reported as a
readiness timeout**, and an operator reading that report cannot tell the two apart.

Why this is measured rather than tabulated
------------------------------------------
``sun_path`` is 104 bytes on macOS and 108 on Linux, and CPython, Rust and libc each
draw the "one byte for the NUL" line slightly differently.  A per-platform constant
table in this repo would therefore be a *guess about someone else's contract*, and the
first host that disagreed would fail exactly the way the incident did.  So the budget
is obtained from the runtime that performs the bind:

* :func:`probe_af_unix_path_budget` binary-searches the longest total path that a real
  ``AF_UNIX`` ``bind()`` accepts inside a scratch directory the probe itself owns, and
  unlinks every node it creates.  The answer is a host fact, so
  :func:`host_af_unix_path_budget` memoises it per process.
* the over-budget verdict is read from the runtime's own error contract, not from a
  message: CPython rejects an oversized ``sun_path`` in its pre-check with a bare
  ``OSError`` carrying **no errno**, and a kernel-side rejection arrives as
  ``ENAMETOOLONG`` (see :func:`_is_over_budget`).  Any *other* ``OSError`` means the
  probe could not answer the question at all, which is reported as
  :data:`ENDPOINT_PATH_BUDGET_UNMEASURED` rather than folded into a number.

The budget is a property of the total path length, not of the directory, which is what
makes a scratch-directory measurement usable for a path that does not exist yet — and
which is why nothing here ever binds inside, reads from, or even names the operator's
home or endpoint.  The probe consults no environment variable.

Two facts, both fail-closed
---------------------------
:class:`EndpointPathBudget` pairs the measured budget with the byte length of the
longest path a disposable instance would bind, and names the blocker as a closed token.
"Cannot measure" is its own answer, distinct from "does not fit": collapsing them is how
an unanswerable probe would silently become a permissive one.  Evidence carries counts,
bools and closed tokens only — never a path — so it can be summarised into a durable
Redmine journal.
"""

from __future__ import annotations

import errno
import os
import shutil
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.shared_space_smoke_observation import (  # noqa: E501
    SharedSpaceSmokeError,
)


#: The single declaration of the endpoint file names a disposable instance binds, and of
#: the instance sub-tree name the smoke driver derives from an isolated home.  Shared
#: with ``DisposableHerdrBinding`` so the preflight measures the paths that are actually
#: bound instead of re-deriving them and drifting.
SERVER_SOCKET_NAME = "herdr.sock"
CLIENT_SOCKET_NAME = "herdr-client.sock"
INSTANCE_ROOT_NAME = "herdr-instance"

#: Closed vocabulary for the endpoint-path verdict.  ``""`` is "within budget".
ENDPOINT_PATH_OK = ""
#: The derived endpoint path is longer than the measured host budget.
ENDPOINT_PATH_TOO_LONG = "endpoint_path_too_long"
#: The host budget could not be measured, so no path can be promised bindable.  A
#: distinct answer from :data:`ENDPOINT_PATH_TOO_LONG` on purpose: "we could not tell"
#: and "it does not fit" are different facts, and only the first is about the probe.
ENDPOINT_PATH_BUDGET_UNMEASURED = "endpoint_path_budget_unmeasured"
ENDPOINT_PATH_BLOCKERS: frozenset = frozenset(
    {ENDPOINT_PATH_TOO_LONG, ENDPOINT_PATH_BUDGET_UNMEASURED}
)

#: Sentinel for "the probe could not establish a budget".  Never a fallback length.
BUDGET_UNMEASURED = -1

#: Upper bound on the probe's candidate name length.  ``sun_path`` capacity is far below
#: the POSIX ``NAME_MAX`` floor, so a longer component would only add a *different*
#: failure (an over-long path component) to the answer we are measuring.
MAX_PROBE_NAME_BYTES = 255

#: How an operator resolves a blocked endpoint path.  Value-free by construction: it
#: names the input to shorten, never the derived path.
ENDPOINT_PATH_RESOLUTION = (
    "the disposable endpoint path is derived from --isolated-home, and this host binds "
    "an AF_UNIX endpoint only within a fixed total-path byte budget; pass a shorter "
    "--isolated-home (a short base directory, not a deep temp tree) and re-run"
)


class SmokeEndpointPathBudgetError(SharedSpaceSmokeError):
    """The derived endpoint path is not bindable on this host.

    Raised strictly before anything is created or launched, so a caller that sees this
    error knows the actuation count is zero.  The message carries byte counts and the
    closed blocker token — never a path — and names the resolution.
    """

    def __init__(self, budget: "EndpointPathBudget") -> None:
        self.blocker = budget.blocker
        self.budget = budget
        super().__init__(endpoint_path_refusal(budget))


def endpoint_path_refusal(budget: "EndpointPathBudget") -> str:
    """The one wording for a blocked endpoint path, shared by the error and the CLI."""
    return (
        "disposable Herdr endpoint path is not bindable on this host "
        f"({budget.blocker}: endpoint_path_bytes={budget.path_bytes}, "
        f"endpoint_path_budget_bytes={budget.budget_bytes}); "
        f"{ENDPOINT_PATH_RESOLUTION}"
    )


@dataclass(frozen=True)
class EndpointPathBudget:
    """The measured host budget beside the longest endpoint path we would bind."""

    #: Longest total path the host accepted from a real ``bind()``, or
    #: :data:`BUDGET_UNMEASURED`.
    budget_bytes: int
    #: Byte length of the longest derived endpoint path (``os.fsencode``, not ``len``
    #: of the ``str``: the kernel counts bytes and a non-ASCII home is not one byte per
    #: character).
    path_bytes: int
    #: Closed token from the vocabulary above.
    blocker: str

    @property
    def within_budget(self) -> bool:
        """Whether the endpoint path is *provably* bindable on this host."""
        return self.blocker == ENDPOINT_PATH_OK

    def raise_if_blocked(self) -> None:
        """Refuse before actuation, or return having promised nothing else."""
        if not self.within_budget:
            raise SmokeEndpointPathBudgetError(self)

    def as_evidence(self) -> dict[str, object]:
        """Counts / bool / closed token only — no path may reach a durable record."""
        return {
            "endpoint_path_bytes": self.path_bytes,
            "endpoint_path_budget_bytes": self.budget_bytes,
            "endpoint_path_within_budget": self.within_budget,
            "endpoint_path_blocker": self.blocker,
        }


def path_bytes(path) -> int:
    """The byte length the kernel sees, which is what ``sun_path`` bounds."""
    return len(os.fsencode(os.fspath(path)))


def disposable_instance_root(isolated_home) -> Path:
    """Where a disposable instance's endpoint/state tree lives under an isolated home.

    The single derivation, so the preflight and the driver cannot disagree about which
    path is measured.  Resolved for the same reason
    :class:`DisposableHerdrInstance` resolves it: a symlinked or relative base changes
    the byte length that ``bind()`` will actually see.
    """
    return Path(isolated_home).expanduser().resolve() / INSTANCE_ROOT_NAME


def derived_endpoint_paths(root) -> tuple[Path, ...]:
    """Every AF_UNIX endpoint path the disposable binding hands the Herdr runtime.

    Both are returned and the evaluation takes the **longest**.  That is deliberately
    the fail-closed reading: this module does not claim to know which of the two the
    runtime actually binds, only that both are given to it as endpoint paths, and the
    client socket name is the longer one — so measuring the server socket alone would
    admit a root whose client endpoint is already over budget.
    """
    base = Path(root)
    return (base / SERVER_SOCKET_NAME, base / CLIENT_SOCKET_NAME)


def _is_over_budget(exc: OSError) -> bool:
    """Whether ``exc`` says the address itself exceeded ``sun_path``.

    Read from the runtime's error contract rather than from its message text: CPython's
    own pre-check raises a bare ``OSError`` with **no errno** ("AF_UNIX path too long"),
    while a kernel-side rejection arrives as ``ENAMETOOLONG``.  Every other ``OSError``
    is a different fault (permissions, a missing directory, a sandboxed socket family)
    and must not be reported as a length verdict.
    """
    return exc.errno is None or exc.errno == errno.ENAMETOOLONG


def _bind_and_unlink(path: Path) -> None:
    """Bind a real AF_UNIX socket at ``path``, then remove the node again.

    The only syscall that can answer the question, aimed exclusively at a directory the
    caller owns.  Nothing is read from the environment, so no operator endpoint is
    reachable from here even in principle.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(os.fspath(path))
    finally:
        # Closed on both paths; a failed ``bind`` created no node, so the unlink below
        # is deliberately outside this block rather than removing someone else's socket.
        sock.close()
    try:
        os.unlink(path)
    except OSError:
        # The probe never reuses a name, so a node it could not remove cannot make a
        # later candidate answer ``EADDRINUSE``.
        pass


def _binds(path: Path, binder: Callable[[Path], None]) -> bool:
    """``True`` if the host bound ``path``; ``False`` if it was over budget.

    Any other ``OSError`` is re-raised, because a probe that cannot bind for an
    unrelated reason has not measured a budget and must not pretend to.
    """
    try:
        binder(path)
    except OSError as exc:
        if _is_over_budget(exc):
            return False
        raise
    return True


def _measure_in(directory: Path, binder: Callable[[Path], None]) -> int:
    """Longest total path length that binds inside ``directory``, or the sentinel.

    Binary search over the candidate *name* length: acceptance is monotone in the total
    path length, so the largest name that binds gives the host's total-path budget
    directly.  ``directory`` is only ever a scratch tree the caller owns.
    """
    base = len(os.fsencode(os.fspath(directory))) + 1  # + the path separator
    low, high, longest = 1, MAX_PROBE_NAME_BYTES, 0
    try:
        while low <= high:
            middle = (low + high) // 2
            if _binds(directory / ("p" * middle), binder):
                longest = middle
                low = middle + 1
            else:
                high = middle - 1
    except OSError:
        # Not a length answer: the probe itself is unavailable here.
        return BUDGET_UNMEASURED
    if longest == 0:
        # Not even a one-byte name fits, so ``directory`` is itself over budget and the
        # host figure stays unknown rather than being reported as "zero bytes".
        return BUDGET_UNMEASURED
    return base + longest


def probe_af_unix_path_budget(
    *,
    scratch_dir=None,
    binder: Optional[Callable[[Path], None]] = None,
) -> int:
    """Measure the host's AF_UNIX total-path budget, in bytes.

    Returns :data:`BUDGET_UNMEASURED` when the probe could not establish it.  The
    scratch directory is created and removed here unless the caller supplies one, and
    every socket the probe binds is unlinked again, so a completed probe leaves nothing
    on disk.
    """
    bind = _bind_and_unlink if binder is None else binder
    if scratch_dir is not None:
        return _measure_in(Path(scratch_dir), bind)
    scratch = Path(tempfile.mkdtemp(prefix="mzb-"))
    try:
        return _measure_in(scratch, bind)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


_HOST_BUDGET: Optional[int] = None


def host_af_unix_path_budget() -> int:
    """The measured budget for this host, probed once per process.

    Memoised because it is a host property, not a per-call one — and because the
    lifecycle evaluates it for every disposable instance it constructs.
    """
    global _HOST_BUDGET
    if _HOST_BUDGET is None:
        _HOST_BUDGET = probe_af_unix_path_budget()
    return _HOST_BUDGET


def evaluate_endpoint_paths(
    paths: Sequence, *, budget_bytes: Optional[int] = None
) -> EndpointPathBudget:
    """Compare the derived endpoint paths against the host budget.

    ``budget_bytes`` defaults to the measured host budget; passing it is how a test
    pins a boundary deterministically instead of depending on the host's own capacity.
    An empty ``paths`` is refused rather than answered: "nothing to bind" would make
    the verdict vacuously ``within_budget`` for a caller that simply lost its
    derivation.
    """
    candidates = list(paths)
    if not candidates:
        raise SharedSpaceSmokeError(
            "endpoint-path budget needs at least one derived endpoint path to measure"
        )
    measured = host_af_unix_path_budget() if budget_bytes is None else int(budget_bytes)
    longest = max(path_bytes(candidate) for candidate in candidates)
    if measured < 0:
        blocker = ENDPOINT_PATH_BUDGET_UNMEASURED
    elif longest > measured:
        blocker = ENDPOINT_PATH_TOO_LONG
    else:
        blocker = ENDPOINT_PATH_OK
    return EndpointPathBudget(
        budget_bytes=measured, path_bytes=longest, blocker=blocker
    )


def endpoint_path_budget_for_isolated_home(
    isolated_home, *, budget_bytes: Optional[int] = None
) -> EndpointPathBudget:
    """The endpoint-path verdict for the instance an isolated home would produce."""
    return evaluate_endpoint_paths(
        derived_endpoint_paths(disposable_instance_root(isolated_home)),
        budget_bytes=budget_bytes,
    )


__all__ = (
    "BUDGET_UNMEASURED",
    "CLIENT_SOCKET_NAME",
    "ENDPOINT_PATH_BLOCKERS",
    "ENDPOINT_PATH_BUDGET_UNMEASURED",
    "ENDPOINT_PATH_OK",
    "ENDPOINT_PATH_RESOLUTION",
    "ENDPOINT_PATH_TOO_LONG",
    "INSTANCE_ROOT_NAME",
    "MAX_PROBE_NAME_BYTES",
    "SERVER_SOCKET_NAME",
    "EndpointPathBudget",
    "SmokeEndpointPathBudgetError",
    "derived_endpoint_paths",
    "disposable_instance_root",
    "endpoint_path_budget_for_isolated_home",
    "endpoint_path_refusal",
    "evaluate_endpoint_paths",
    "host_af_unix_path_budget",
    "path_bytes",
    "probe_af_unix_path_budget",
)
