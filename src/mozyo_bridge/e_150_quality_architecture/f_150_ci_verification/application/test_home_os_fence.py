"""OS-level write boundary around a test process tree (Redmine #14757 R3).

Two rounds of in-process guards were rejected, and the reason was the same both
times: they refuse the paths the implementer thought of, and the process tree can
reach the shared home by a path the implementer did not think of.

- **R1** detected mutations after they landed (review j#100407 R1-F1).
- **R2** refused them in-process with an audit hook installed through a ``.pth``
  in a task venv. Review j#100417 defeated it twice over, and both are structural
  rather than fixable in the hook:
  - ``os.open(path, ..., dir_fd=<fd of the denied home>)`` — CPython's ``open``
    audit event carries ``(path, mode, flags)`` and **no** ``dir_fd``, so the hook
    cannot know where a relative path resolves. Measured: rc 0, bytes changed,
    ledger empty.
  - ``<fence python> -S -c ...`` — ``-S`` skips ``site``, so the ``.pth`` is never
    processed. Same for ``sys._base_executable``, a console script, or any
    non-Python process. Measured: rc 0, row changed, ledger empty.

So enforcement moves to the layer that sees every syscall regardless of which
binary makes it. This is what acceptance 3 was asking for all along: *"macOS
``sandbox-exec`` だけに依存せず Linux CI 相当境界を持つ"* presupposes an OS-level
boundary and forbids depending on **one** platform's version of it — it does not
forbid using one. R1 read it as "do not use an OS sandbox", which was wrong.

Measured on macOS (temp fixture, not the operator's home): with
``(deny file-write* (subpath <home>))`` both bypasses above raise
``PermissionError`` and the file's bytes are unchanged.

**Fail-closed, and honest about which half is proven.** The macOS backend is
measured here. The Linux backend is written to the same contract but **cannot be
measured in this environment**, so it is marked unverified until CI exercises it —
claiming an unmeasured boundary is exactly the failure this round exists to stop.
When no backend is available the caller refuses to run rather than degrading to a
weaker guard.
"""

from __future__ import annotations

import errno
import json
import os
import platform
import sqlite3
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

#: Backend ids. ``VERIFIED_BACKENDS`` names the ones actually measured in this
#: repository. NOTE: `OsFence.verified` is now a *runtime* result, never a static
#: per-platform claim -- see `verify_os_fence` (j#100449 item 6).
BACKEND_SANDBOX_EXEC = "sandbox_exec"
BACKEND_BWRAP = "bwrap"
BACKEND_NONE = "none"
VERIFIED_BACKENDS = frozenset({BACKEND_SANDBOX_EXEC})


class OsFenceUnavailable(RuntimeError):
    """No OS-level write boundary is available on this host.

    Raised so the caller refuses to run. The alternative — falling back to the
    in-process guard — is what review j#100417 rejected, twice.
    """


@dataclass(frozen=True)
class OsFence:
    """A resolved OS boundary: how to wrap a command, and how proven it is.

    ``inherited`` means the process is *already* inside a boundary that covers the
    requested roots, so ``argv_prefix`` is empty and the payload runs under the
    boundary it inherited. Nesting is not merely wasteful — measured, a nested
    ``sandbox-exec`` given ``env={}`` fails with ``sandbox_apply: Operation not
    permitted`` (exit 71) and applies **no** boundary, while looking like a refusal
    to any test that only checks a non-zero exit (j#100436).
    """

    backend: str
    argv_prefix: tuple[str, ...]
    denied: tuple[Path, ...]
    verified: bool
    #: Fixture authority, carried BY the fence and **required** (j#100463). Optional
    #: fields still allowed a fence with no authority to exist, so the inner/outer
    #: mismatch stayed expressible; making them mandatory removes the invalid state
    #: from the type rather than guarding against it at each call site.
    canary: Path
    control_root: Path
    #: The task root this fence's fixtures live under. Completes the authority on
    #: the fence (j#100473): backend, denied set, fixtures, and the root that
    #: classifies a requested home as task-local now travel together.
    task_root: Path
    inherited: bool = False

    def wrap(self, argv: list[str]) -> list[str]:
        """The command to actually run, with the boundary applied."""
        return [*self.argv_prefix, *argv]

    def as_dict(self) -> dict:
        return {
            "backend": self.backend,
            "verified_backend": self.verified,
            "inherited": self.inherited,
            "denied_root_count": len(self.denied),
        }


def _sandbox_profile(denied: tuple[Path, ...]) -> str:
    """A macOS Seatbelt profile denying writes under each denied root.

    ``allow default`` keeps everything else working — the goal is a write boundary
    around specific paths, not a general sandbox, and tightening it further would
    break unrelated tests without improving the property under review.

    Paths are emitted with ``pwd -P``-style resolution by the caller and quoted as
    Seatbelt string literals; a path containing a double quote would end the
    literal, so such a root is rejected rather than silently mis-fenced.
    """
    lines = ["(version 1)", "(allow default)"]
    for root in denied:
        text = str(root)
        if '"' in text or "\\" in text:
            raise OsFenceUnavailable(
                "cannot build a sandbox profile for a denied root containing a "
                "quote or backslash; refusing rather than fencing the wrong path"
            )
        lines.append(f'(deny file-write* (subpath "{text}"))')
    return "\n".join(lines) + "\n"


BACKEND_INHERITED = "inherited"

#: Refusal errno **per backend** (j#100447 Phase A item 4). Not one shared set: a
#: Seatbelt deny reports EPERM and a `bwrap --ro-bind` reports EROFS, and accepting
#: either from either backend would let an unrelated failure look like enforcement.
#: A bare EACCES is deliberately NOT accepted in Phase A -- it is the errno an
#: ordinary permission problem produces, so on its own it is not evidence that the
#: boundary is what refused.
BACKEND_REFUSAL_ERRNOS = {
    BACKEND_SANDBOX_EXEC: frozenset({errno.EPERM}),
    BACKEND_BWRAP: frozenset({errno.EROFS}),
}


def _canary_write_is_refused(canary: Path, expected_errnos: frozenset[int]) -> bool:
    """Probe a **task-local** canary to learn whether a boundary is enforcing.

    This is the boundary-detection primitive, and it is deliberately a *write*
    rather than a flag: env vars do not survive ``env={}`` and a marker file only
    proves someone wrote it, whereas an actually-refused write proves the OS is
    enforcing right now.

    The canary lives in the task temp tree, never in the operator's home, so a
    false context cannot make this touch shared state (j#100440 item 3). A write
    that *succeeds* is cleaned up immediately.
    ``expected_errnos`` comes from the *origin* backend, so a refusal only counts
    when it is the refusal that backend actually produces.
    """
    probe = Path(canary) / ".mozyo-canary-probe"
    try:
        probe.parent.mkdir(parents=True, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("probe")
    except OSError as exc:
        # ONLY these errnos are an OS refusal (j#100442 item 1). Classifying by
        # exception type was wrong: `bwrap --ro-bind` refuses with EROFS, which is
        # an OSError and NOT a PermissionError, so the earlier code would have
        # reported "not enforcing" for every inherited Linux boundary and nested
        # regardless. Anything else is not evidence of a boundary and is
        # fail-closed -- the caller applies its own rather than assuming cover.
        return exc.errno in expected_errnos
    else:
        # Only ever cleaned up inside the task temp tree.
        probe.unlink(missing_ok=True)
        return False


@dataclass(frozen=True)
class OuterContext:
    """The outer boundary's typed, versioned contract (j#100447 Phase A item 1)."""

    version: int
    origin_backend: str
    outer_protected_roots: tuple[Path, ...]
    task_root: Path
    canary_root: Path
    allowed_control_root: Path


CONTEXT_VERSION = 1
#: The context module's own name. Only THIS module being absent means "no boundary".
_CONTEXT_MODULE = "_mozyo_test_fence"
_CONTEXT_FIELDS = (
    ("MOZYO_FENCE_CONTEXT_VERSION", int),
    ("MOZYO_FENCE_ORIGIN_BACKEND", str),
    ("MOZYO_FENCE_OUTER_PROTECTED_ROOTS", tuple),
    ("MOZYO_FENCE_TASK_ROOT", str),
    ("MOZYO_FENCE_CANARY_ROOT", str),
    ("MOZYO_FENCE_ALLOWED_CONTROL_ROOT", str),
)


def load_outer_context() -> OuterContext | None:
    """The outer boundary contract, or ``None`` only when there is genuinely none.

    ``ModuleNotFoundError`` is the **only** "no outer context" answer (j#100445
    item 1). If the module exists but is broken -- SyntaxError, unreadable, a
    missing or mistyped field, an unknown backend, or a path outside the task root
    -- that is a corrupted boundary, not an absent one, and building a second
    wrapper on top of it would be exactly the silent degrade this round exists to
    remove. Those raise :class:`OsFenceUnavailable` so the run ends with zero tests.
    """
    try:
        import _mozyo_test_fence as context  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        # ONLY our own module being absent means "no outer boundary" (j#100483
        # item 3). A ModuleNotFoundError raised from *inside* a context module that
        # does exist is a broken boundary, and swallowing it here would silently
        # downgrade an enforcing run to an unfenced one.
        if exc.name == _CONTEXT_MODULE:
            return None
        raise OsFenceUnavailable(
            f"the outer boundary context module failed to import a dependency "
            f"({exc.name!r}); refusing to run rather than reading a broken boundary "
            f"as an absent one"
        ) from exc
    except Exception as exc:  # SyntaxError, PermissionError, anything else
        raise OsFenceUnavailable(
            f"the outer boundary context module exists but could not be loaded "
            f"({type(exc).__name__}: {exc}); refusing to run rather than building a "
            f"second boundary over a corrupted one"
        ) from exc

    values = {}
    for field_name, expected_type in _CONTEXT_FIELDS:
        if not hasattr(context, field_name):
            raise OsFenceUnavailable(
                f"the outer boundary context is missing {field_name}; refusing to run"
            )
        value = getattr(context, field_name)
        # `isinstance(True, int)` is True, so a bool version would sail through the
        # type check and then compare unequal to CONTEXT_VERSION with a confusing
        # message -- or, at 1, compare EQUAL. Reject it as a type error.
        if expected_type is int and isinstance(value, bool):
            raise OsFenceUnavailable(
                f"the outer boundary context field {field_name} is a bool, "
                f"expected int"
            )
        # EXACT type, not a subclass (j#100489 F3). The producer emits plain
        # built-ins; a str/tuple subclass reaching here means something other than
        # the canonical producer wrote this context, and a subclass is free to
        # override __eq__/__fspath__ so later checks stop meaning what they read.
        if isinstance(value, expected_type) and type(value) is not expected_type:
            raise OsFenceUnavailable(
                f"the outer boundary context field {field_name} is a "
                f"{type(value).__name__}, expected exactly {expected_type.__name__}"
            )
        if not isinstance(value, expected_type):
            raise OsFenceUnavailable(
                f"the outer boundary context field {field_name} has type "
                f"{type(value).__name__}, expected {expected_type.__name__}"
            )
        values[field_name] = value

    version = values["MOZYO_FENCE_CONTEXT_VERSION"]
    if version != CONTEXT_VERSION:
        raise OsFenceUnavailable(
            f"the outer boundary context is version {version}, this runtime "
            f"understands {CONTEXT_VERSION}; refusing to run"
        )
    backend = values["MOZYO_FENCE_ORIGIN_BACKEND"]
    if backend not in BACKEND_REFUSAL_ERRNOS:
        raise OsFenceUnavailable(
            f"the outer boundary context names an unknown backend {backend!r}; "
            f"refusing to run"
        )
    # A relative path would be `.resolve()`d against whatever cwd this generation
    # happens to have, quietly inventing a root that no boundary protects. The
    # context is a contract between processes with different working directories,
    # so only absolute paths can mean anything in it.
    _raw_paths = [
        (name, values[name])
        for name in ("MOZYO_FENCE_TASK_ROOT", "MOZYO_FENCE_CANARY_ROOT",
                     "MOZYO_FENCE_ALLOWED_CONTROL_ROOT")
    ]
    _raw_paths += [
        ("MOZYO_FENCE_OUTER_PROTECTED_ROOTS", entry)
        for entry in values["MOZYO_FENCE_OUTER_PROTECTED_ROOTS"]
    ]
    for _name, _raw in _raw_paths:
        if type(_raw) is not str or not _raw or not Path(_raw).is_absolute():
            raise OsFenceUnavailable(
                f"the outer boundary context field {_name} holds a non-absolute "
                f"path {_raw!r}; refusing to run"
            )
        # Byte-canonical, not merely absolute (F3): `..` segments and symlinks are
        # normalised away by `.resolve()`, so a path that LOOKS like the protected
        # root can be admitted and then compared unequal to it everywhere else. The
        # producer writes canonical paths, so anything else is not from it.
        if str(Path(_raw).resolve()) != _raw:
            raise OsFenceUnavailable(
                f"the outer boundary context field {_name} holds a non-canonical "
                f"path {_raw!r} (canonical: {str(Path(_raw).resolve())!r}); refusing"
            )
    try:
        task_root = Path(values["MOZYO_FENCE_TASK_ROOT"]).resolve()
        canary_root = Path(values["MOZYO_FENCE_CANARY_ROOT"]).resolve()
        allowed_root = Path(values["MOZYO_FENCE_ALLOWED_CONTROL_ROOT"]).resolve()
        protected = tuple(
            Path(entry).resolve()
            for entry in values["MOZYO_FENCE_OUTER_PROTECTED_ROOTS"]
        )
    except (TypeError, OSError) as exc:
        raise OsFenceUnavailable(
            f"the outer boundary context holds an unusable path ({exc})"
        ) from exc
    # The canary and control fixtures must live inside the task root; a context
    # pointing them elsewhere is not one this runtime may trust.
    for label, path in (("canary", canary_root), ("control", allowed_root)):
        if path != task_root and task_root not in path.parents:
            raise OsFenceUnavailable(
                f"the outer boundary context puts its {label} root outside the task "
                f"root; refusing to run"
            )
    return OuterContext(
        version=version,
        origin_backend=backend,
        outer_protected_roots=protected,
        task_root=task_root,
        canary_root=canary_root,
        allowed_control_root=allowed_root,
    )


#: Typed outcomes of classifying one requested home against an outer context.
CLASSIFY_PROTECTED = "outer_protected_root"
CLASSIFY_TASK_LOCAL = "task_local"
CLASSIFY_REFUSED = "outside_task_root"


def classify_requested_home(home: Path, context: OuterContext) -> str:
    """Classify one requested denied home against the outer context (pure).

    Three outcomes, and the third is a refusal rather than a widening
    (j#100447 Phase A item 5):

    - already an outer protected root -> keep it;
    - resolves inside the outer **task root** -> ``task_local``: it is scratch space
      for this run, not operator shared state, so it must NOT be promoted into the
      shared deny set;
    - anything else -> ``outside_task_root``: the outer boundary does not cover it
      and a second wrapper cannot be stacked, so the caller refuses the run.
    """
    resolved = Path(home).resolve()
    if resolved in context.outer_protected_roots:
        return CLASSIFY_PROTECTED
    if resolved == context.task_root or context.task_root in resolved.parents:
        return CLASSIFY_TASK_LOCAL
    return CLASSIFY_REFUSED


def inherited_fence(denied_homes: tuple[Path, ...]) -> OsFence | None:
    """The boundary this process already runs under, when it covers ``denied_homes``.

    Preserves the **origin backend** rather than collapsing to a synthetic
    ``inherited`` id (j#100445 item 4): the backend determines which errno counts as
    a refusal, so losing it would make the canary probe meaningless. ``verified`` is
    left False here -- this run's own check has not happened yet, and a static flag
    must never stand in for it (j#100447 Phase A item 3).
    """
    context = load_outer_context()
    if context is None:
        return None
    for home in denied_homes:
        verdict = classify_requested_home(home, context)
        if verdict == CLASSIFY_REFUSED:
            raise OsFenceUnavailable(
                f"a requested denied home lies outside the outer task root, so the "
                f"inherited boundary does not cover it and a second boundary cannot "
                f"be stacked on macOS (measured: nested sandbox-exec exits 71). "
                f"Refusing to run."
            )
    return OsFence(
        backend=context.origin_backend,
        argv_prefix=(),
        denied=context.outer_protected_roots,
        verified=False,
        inherited=True,
        # The OUTER fixtures are the authority; an inherited fence must never be
        # verified against fixtures of its own (j#100455 / j#100461).
        canary=context.canary_root,
        control_root=context.allowed_control_root,
        task_root=context.task_root,
    )


def resolve_os_fence(
    denied_homes: tuple[Path, ...], *, work_dir: Path, canary: Path | None = None
) -> OsFence:
    """Pick and materialise the OS write boundary for this host.

    Reuses an inherited boundary when one already covers the requested roots (see
    :func:`inherited_fence`); otherwise builds a fresh one. ``work_dir`` holds any
    generated profile, and ``canary`` is the task-local directory the boundary also
    denies so a nested run can detect it. Raises :class:`OsFenceUnavailable` when
    this host offers no boundary, so the caller can refuse the run.
    """
    roots = tuple(Path(home).resolve() for home in denied_homes)
    if not roots:
        raise OsFenceUnavailable("no denied roots were given; nothing to fence")

    reused = inherited_fence(roots)
    if reused is not None:
        return reused
    if canary is None:
        raise OsFenceUnavailable(
            "a fence cannot be built without a canary: its fixture authority is "
            "required, not optional (j#100463)"
        )
    canary_path = Path(canary).resolve()
    control_path = canary_path.parent / "control"

    # The canary is denied alongside the real roots, so a nested run can prove the
    # boundary is live without going near the operator's home.
    fenced = roots if canary is None else (*roots, Path(canary).resolve())

    system = platform.system()
    if system == "Darwin" and shutil.which("sandbox-exec"):
        profile = Path(work_dir) / "deny-operator-home.sb"
        profile.write_text(_sandbox_profile(fenced), encoding="utf-8")
        return OsFence(
            backend=BACKEND_SANDBOX_EXEC,
            argv_prefix=("sandbox-exec", "-f", str(profile)),
            denied=roots,
            canary=canary_path,
            control_root=control_path,
            task_root=Path(work_dir).resolve(),
            # NEVER verified at resolve time (j#100449 item 6). Only this run's own
            # 4-probe check may grant it; a static per-platform flag standing in for
            # a measurement is the failure this round removed.
            verified=False,
        )

    if system == "Linux" and shutil.which("bwrap"):
        # bubblewrap: keep the filesystem as-is, then re-bind each denied root
        # read-only on top of itself. `--dev-bind /` preserves everything the suite
        # needs (including /dev and the repo) while the later --ro-bind wins for
        # the denied subtree.
        #
        # A denied root need not EXIST on the host (j#100489 F6): a clean CI runner
        # has no `~/.mozyo_bridge` until something creates it. `--ro-bind` fails when
        # its *source* is missing, so binding the root onto itself would abort bwrap
        # before a single test ran -- and the obvious repair, mkdir-ing the operator
        # home, would have the test rail create operator state to protect it. Bind a
        # task-local empty directory as the source instead: same read-only, empty
        # destination inside the sandbox, and nothing made on the host (bwrap creates
        # the destination in the mount namespace only).
        prefix: list[str] = ["bwrap", "--dev-bind", "/", "/"]
        empty_source: Path | None = None
        for root in fenced:
            source = root
            if not root.exists():
                if empty_source is None:
                    empty_source = Path(work_dir).resolve() / "absent-denied-source"
                    empty_source.mkdir(parents=True, exist_ok=True)
                source = empty_source
            prefix += ["--ro-bind", str(source), str(root)]
        return OsFence(
            backend=BACKEND_BWRAP,
            argv_prefix=tuple(prefix),
            denied=roots,
            canary=canary_path,
            control_root=control_path,
            task_root=Path(work_dir).resolve(),
            # Same rule as macOS: only this run's 4-probe check grants verified.
            verified=False,
        )

    raise OsFenceUnavailable(
        f"no OS-level write boundary is available on this host "
        f"(system={system!r}; looked for sandbox-exec on macOS and bwrap on "
        f"Linux). Refusing to run: an in-process guard is not a substitute "
        f"(Redmine #14757 review j#100417)."
    )


BYTES_VICTIM_NAME = "victim.bytes"
SQLITE_VICTIM_NAME = "victim.sqlite"
CONTROL_WRITE_NAME = "control.txt"
#: Concurrent verifications share an inherited control root, and a fixed filename
#: made them race: one run's cleanup deleted another's file, surfacing as a spurious
#: "control write did not land" (j#100461). Unique per **call**, not per process --
#: measured, a per-pid name still collided across threads in one process, which is
#: how two verifications inside a single runner can overlap.
def _control_name() -> str:
    return f"control-{uuid.uuid4().hex}.txt"
    # NOTE: generate ONCE per verification and thread it through. Calling this at
    # both the write site and the check site yields two different names, so the
    # check looks for a file the probe never wrote.
_BYTES_VICTIM_CONTENT = b"mozyo-fence-victim-v1\n"
_SQLITE_VICTIM_VALUE = "t0"
_TOKEN_PREFIX = "MOZYO_FENCE_PROBE "

PROBE_DIR_FD = "dir_fd"
PROBE_NO_SITE = "no_site_sqlite"
PROBE_BASE_EXECUTABLE = "base_executable_sqlite"
PROBE_CONTROL = "allowed_control"
_SQLITE_READONLY_MESSAGE = "attempt to write a readonly database"

#: What each probe is allowed to report. ``"errno"`` means an ``errno=<n>`` reason
#: whose ``n`` must be one the backend actually refuses with.
_PROBE_REASONS = {
    PROBE_DIR_FD: frozenset({"errno"}),
    PROBE_NO_SITE: frozenset({"errno", "sqlite"}),
    PROBE_BASE_EXECUTABLE: frozenset({"errno", "sqlite"}),
    PROBE_CONTROL: frozenset({"wrote"}),
}


def create_boundary_fixtures(canary: Path, control_root: Path) -> None:
    """Pre-create the victims the 3-probe proof writes against (j#100449 item 1).

    Created **before** the boundary is applied, because afterwards the canary is
    denied — and they must exist so a probe's failure means "refused", not "no such
    file". Contents are fixed so the parent can read back and prove non-mutation.
    """
    canary = Path(canary)
    canary.mkdir(parents=True, exist_ok=True)
    Path(control_root).mkdir(parents=True, exist_ok=True)
    (canary / BYTES_VICTIM_NAME).write_bytes(_BYTES_VICTIM_CONTENT)
    store = canary / SQLITE_VICTIM_NAME
    if store.exists():
        store.unlink()
    conn = sqlite3.connect(store)
    try:
        conn.execute("CREATE TABLE victim (id TEXT PRIMARY KEY, seen TEXT)")
        conn.execute("INSERT INTO victim VALUES ('row-1', ?)", (_SQLITE_VICTIM_VALUE,))
        conn.commit()
    finally:
        conn.close()


def _probe_source(
    probe: str,
    canary: Path,
    control_root: Path,
    errnos: tuple[int, ...],
    control_name: str,
) -> str:
    """Child source that catches ONLY the expected refusal and emits one token.

    Any other exception, or success where refusal was required, exits non-zero
    without a token — so a parent that requires ``rc == 0`` plus the exact token
    cannot be satisfied by an unrelated failure (which is how the R2 tests passed
    vacuously, j#100436).
    """
    bytes_victim = Path(canary) / BYTES_VICTIM_NAME
    store = Path(canary) / SQLITE_VICTIM_NAME
    emit = (
        f"def emit(reason):\n"
        f"    print({_TOKEN_PREFIX!r} + json.dumps("
        f"{{'probe': {probe!r}, 'reason': reason}}))\n"
        f"    sys.exit(0)\n"
    )
    head = "import json, os, sqlite3, sys\n" + emit
    if probe == PROBE_DIR_FD:
        return head + (
            f"d = os.open({str(Path(canary))!r}, os.O_RDONLY)\n"
            "try:\n"
            f"    fd = os.open({bytes_victim.name!r}, os.O_WRONLY | os.O_TRUNC, dir_fd=d)\n"
            "except OSError as exc:\n"
            f"    sys.exit(0) if exc.errno in {errnos!r} and emit('errno=%d' % exc.errno) else sys.exit(5)\n"
            "else:\n"
            "    os.write(fd, b'MUTATED')\n"
            "    sys.exit(4)\n"
        )
    if probe in (PROBE_NO_SITE, PROBE_BASE_EXECUTABLE):
        return head + (
            "try:\n"
            f"    c = sqlite3.connect({str(store)!r})\n"
            "    c.execute(\"UPDATE victim SET seen='t1'\")\n"
            "    c.commit()\n"
            "except sqlite3.OperationalError as exc:\n"
            f"    emit('sqlite') if str(exc).strip().lower() == {_SQLITE_READONLY_MESSAGE!r} else sys.exit(6)\n"
            "except OSError as exc:\n"
            f"    emit('errno=%d' % exc.errno) if exc.errno in {errnos!r} else sys.exit(5)\n"
            "else:\n"
            "    sys.exit(4)\n"
        )
    return head + (
        f"open({str(Path(control_root) / control_name)!r}, 'w').write('ok')\n"
        "emit('wrote')\n"
    )


def run_boundary_probe(
    fence: OsFence,
    argv_head: list[str],
    probe: str,
    canary: Path,
    control_root: Path,
    control_name: str = CONTROL_WRITE_NAME,
) -> dict:
    """Run one probe through ``fence`` and return its parsed token (j#100451 item 2).

    The single shared probe/parser for both the runtime self-check and the committed
    regressions, so a test cannot drift into a weaker check of its own (the broad
    stderr-signature matching this replaces is exactly how the R2 tests passed
    vacuously). Raises :class:`OsFenceUnavailable` with a specific reason on every
    failure mode: boundary-not-applied, wrong rc, missing/duplicate token, wrong
    probe name, unparseable token.
    """
    # Closed vocabulary, checked before a process is spawned (j#100489 F4). An
    # unknown probe or backend used to surface as a KeyError from inside the
    # measurement -- an untyped crash rather than the typed refusal callers
    # distinguish from a bug.
    if probe not in _PROBE_REASONS:
        raise OsFenceUnavailable(
            f"unknown probe {probe!r}; known probes are {sorted(_PROBE_REASONS)}"
        )
    if fence.backend not in BACKEND_REFUSAL_ERRNOS:
        raise OsFenceUnavailable(
            f"unknown backend {fence.backend!r}; known backends are "
            f"{sorted(BACKEND_REFUSAL_ERRNOS)}"
        )
    errnos = tuple(sorted(BACKEND_REFUSAL_ERRNOS[fence.backend]))
    code = _probe_source(probe, canary, control_root, errnos, control_name)
    completed = subprocess.run(
        fence.wrap([*argv_head, "-c", code]), capture_output=True, text=True
    )
    stderr = completed.stderr or ""
    if "sandbox_apply" in stderr:
        raise OsFenceUnavailable(
            f"probe {probe}: the boundary failed to APPLY (sandbox_apply); nothing "
            f"is enforced. Refusing to run."
        )
    tokens = [
        line[len(_TOKEN_PREFIX):]
        for line in (completed.stdout or "").splitlines()
        if line.startswith(_TOKEN_PREFIX)
    ]
    if completed.returncode != 0 or len(tokens) != 1:
        raise OsFenceUnavailable(
            f"probe {probe}: expected rc 0 with exactly one token, got "
            f"rc={completed.returncode} tokens={len(tokens)}; "
            f"stderr={stderr.strip()[:200]}"
        )
    def _no_duplicate_keys(pairs):
        seen: dict = {}
        for key, value in pairs:
            if key in seen:
                raise ValueError(f"duplicate key {key!r} in probe token")
            seen[key] = value
        return seen

    try:
        # `json.loads` keeps the LAST of duplicate keys, so a token could carry a
        # rejected reason followed by an accepted one and parse as accepted.
        payload = json.loads(tokens[0], object_pairs_hook=_no_duplicate_keys)
    except ValueError as exc:
        raise OsFenceUnavailable(f"probe {probe}: unparseable token ({exc})") from exc
    # Shape first: a JSON list or string would reach `.get` and raise AttributeError
    # rather than a typed refusal, and an extra key means the token was produced by
    # something other than this probe source.
    if not isinstance(payload, dict) or set(payload) != {"probe", "reason"}:
        raise OsFenceUnavailable(
            f"probe {probe}: token payload is not exactly "
            f"{{'probe', 'reason'}} ({payload!r}); refusing"
        )
    if payload["probe"] != probe:
        raise OsFenceUnavailable(
            f"probe {probe}: token names {payload['probe']!r}; refusing"
        )
    # The reason is the probe's evidence, so it is checked per probe rather than
    # accepted as free text (j#100483 item 3). A refusal probe must report the
    # backend's own errno or SQLite's read-only message; the control probe must
    # report a completed write. Free text here let a stub answer "x" and still be
    # taken for a proven boundary.
    reason = payload["reason"]
    if not isinstance(reason, str) or not reason:
        raise OsFenceUnavailable(
            f"probe {probe}: reason is not a non-empty string ({reason!r})"
        )
    allowed = _PROBE_REASONS[probe]
    if reason.startswith("errno="):
        if "errno" not in allowed:
            raise OsFenceUnavailable(
                f"probe {probe}: reported an errno refusal, which this probe does "
                f"not produce ({reason!r})"
            )
        digits = reason[len("errno="):]
        # `int()` accepts "+1", " 1" and "01"; the canonical producer emits "%d".
        # Accepting variants means accepting tokens it did not write.
        if not digits.isdigit() or (len(digits) > 1 and digits[0] == "0"):
            raise OsFenceUnavailable(
                f"probe {probe}: malformed errno reason {reason!r}"
            )
        seen = int(digits)
        if seen not in errnos:
            raise OsFenceUnavailable(
                f"probe {probe}: refused with errno {seen}, but backend "
                f"{fence.backend} refuses with {list(errnos)}; refusing"
            )
    elif reason not in allowed:
        raise OsFenceUnavailable(
            f"probe {probe}: reason {reason!r} is not one this probe can report "
            f"({sorted(allowed)}); refusing"
        )
    return payload


def verify_os_fence(fence: OsFence, python: Path) -> OsFence:
    """Prove this boundary refuses all three bypasses, and return a verified fence.

    The **only** place a fence becomes ``verified`` (j#100449 item 6): a static
    per-platform flag must never stand in for this run's measurement. Four
    conditions are conjoined, all through **one** boundary — never a nested second
    one, which on macOS exits 71 without applying anything:

    1. ``dir_fd`` relative overwrite refused with the origin backend's exact errno;
    2. ``python -S`` in-place SQLite update refused as read-only;
    3. ``sys._base_executable`` in-place SQLite update refused likewise;
    4. the allowed control write **succeeds** — so a boundary that blocks
       everything cannot pass as a correct one.

    Then both victims are read back and required to be unchanged. The operator's
    home is never probed (j#100445 item 3).
    """
    if fence.backend not in BACKEND_REFUSAL_ERRNOS:
        raise OsFenceUnavailable(f"no refusal errno for backend {fence.backend!r}")
    # Authority comes from the fence and ONLY the fence (j#100473). The override
    # arguments are gone: they let a fence be verified against another's fixtures,
    # which is the inner/outer mismatch this whole round removed.
    canary = Path(fence.canary)
    control_root = Path(fence.control_root)
    bytes_victim = canary / BYTES_VICTIM_NAME
    store = canary / SQLITE_VICTIM_NAME
    for fixture in (bytes_victim, store):
        if not fixture.is_file():
            raise OsFenceUnavailable(
                f"boundary fixture {fixture.name} is missing, so a probe failure "
                f"could not be distinguished from a missing file; refusing to run"
            )
    before_bytes = bytes_victim.read_bytes()

    # One name for this whole verification: the probe writes it and the check reads
    # the same one, while a concurrent verification uses a different one.
    control_name = _control_name()
    base = getattr(sys, "_base_executable", None) or str(python)
    for probe, argv_head in (
        (PROBE_DIR_FD, [str(python)]),
        (PROBE_NO_SITE, [str(python), "-S"]),
        (PROBE_BASE_EXECUTABLE, [str(base)]),
        (PROBE_CONTROL, [str(python)]),
    ):
        run_boundary_probe(
            fence, argv_head, probe, canary, control_root, control_name
        )

    if bytes_victim.read_bytes() != before_bytes:
        raise OsFenceUnavailable("the bytes victim was mutated despite refusal")
    try:
        conn = sqlite3.connect(f"file:{store.as_posix()}?mode=ro", uri=True)
        try:
            seen = conn.execute("SELECT seen FROM victim").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise OsFenceUnavailable(f"could not read back the SQLite victim ({exc})") from exc
    if seen != _SQLITE_VICTIM_VALUE:
        raise OsFenceUnavailable("the SQLite victim row was mutated despite refusal")

    control_file = control_root / control_name
    if not control_file.is_file() or control_file.read_text(encoding="utf-8") != "ok":
        raise OsFenceUnavailable(
            "the allowed control write did not land, so this boundary blocks more "
            "than the denied roots and proves nothing about targeted refusal"
        )
    control_file.unlink(missing_ok=True)
    return replace(fence, verified=True)


__all__ = (
    "BYTES_VICTIM_NAME",
    "CONTROL_WRITE_NAME",
    "PROBE_BASE_EXECUTABLE",
    "PROBE_CONTROL",
    "PROBE_DIR_FD",
    "PROBE_NO_SITE",
    "SQLITE_VICTIM_NAME",
    "create_boundary_fixtures",
    "run_boundary_probe",
    "BACKEND_BWRAP",
    "BACKEND_INHERITED",
    "BACKEND_REFUSAL_ERRNOS",
    "CLASSIFY_PROTECTED",
    "CLASSIFY_REFUSED",
    "CLASSIFY_TASK_LOCAL",
    "CONTEXT_VERSION",
    "OuterContext",
    "classify_requested_home",
    "inherited_fence",
    "load_outer_context",
    "BACKEND_NONE",
    "BACKEND_SANDBOX_EXEC",
    "VERIFIED_BACKENDS",
    "OsFence",
    "OsFenceUnavailable",
    "resolve_os_fence",
    "verify_os_fence",
)
