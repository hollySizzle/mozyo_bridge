"""Command handler for ``mozyo-bridge tests run`` (Redmine #14757).

The canonical isolated entry point for focused *and* full ``unittest`` runs. It
does three things around the run, and nothing to the run itself:

1. materialises a task-specific temp root and pins the home contract, the temp
   dirs, and the XDG roots into it, dropping the live cockpit-session env
   (:mod:`...application.test_home_fence`);
2. spawns ``python -m unittest <args>`` — the *literal* authoritative command
   from ``tests-placement-discovery-policy.md``, so the test set and the verdict
   are the serial ones by construction, not by re-implementation;
3. snapshots the operator's shared home before and after, and fails the run when
   anything in the guarded set moved — even if every test passed.

Step 3 is the point. #14477 j#94521 recorded a run whose tests were *green*
while the operator's shared store was forward-migrated v7 -> v8 underneath it,
and #14741's two regression tests inserted a temp workspace row into the
operator's live registry on every invocation. A green suite is therefore not
evidence of isolation; the before/after guard is what makes a mutation
impossible to report as a pass.

``guarded_isolated_run`` is shared with ``tests profile`` / ``tests parallel``,
which re-exec themselves through it rather than growing their own copy of the
fence.
"""

from __future__ import annotations

import argparse
import dataclasses
import errno as _errno
import json as _json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.test_home_audit_hook import (
    AuditHookInstallError,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.test_temp_root import (
    TESTS_HOME_PREFIX,
    TempRootUnavailable,
    diagnose_disk_pressure,
    effective_temp_base,
    resolve_tests_temp_base,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.test_home_os_fence import (
    OsFence,
    OsFenceUnavailable,
    inherited_fence,
    load_outer_context,
    verify_os_fence,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.test_home_fence import (
    ambient_homes,
    isolated_env,
    read_deny_ledger,
    reexec_isolated,
    snapshot_home,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_disk_pressure import (
    MarkerScanner,
    is_disk_pressure_errno,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_home_isolation import (
    IsolatedRunOutcome,
    IsolationLayout,
    path_label,
    compare_snapshots,
)
from mozyo_bridge.shared.paths import process_home_fence, resolve_repo_root

#: Default arguments when the caller names no target: the authoritative full
#: discovery command, spelled exactly as CI and the placement policy spell it.
DEFAULT_UNITTEST_ARGS = ("discover", "-s", "tests")

#: Hidden flag a re-exec'ed child carries, so it runs in-process instead of
#: spawning a third generation. The fence env is the second, independent guard.
ISOLATED_FLAG = "--already-isolated"

_TEMP_PREFIX = TESTS_HOME_PREFIX


def _repo_root(args: argparse.Namespace) -> Path:
    return resolve_repo_root(getattr(args, "repo", None))


def _make_task_root() -> tempfile.TemporaryDirectory:
    """The task temp root, under the operator's declared base when given.

    ``MOZYO_TESTS_TMPDIR`` (Redmine #15710) is the declarative escape from a
    quota-pressured ``/tmp``; an unusable declaration raises out of
    :func:`resolve_tests_temp_base` rather than silently landing back in the
    environment the declaration exists to avoid.
    """
    base = resolve_tests_temp_base()
    return tempfile.TemporaryDirectory(
        prefix=_TEMP_PREFIX, dir=None if base is None else str(base)
    )


def _pressure_refusal(exc: OSError) -> TempRootUnavailable:
    """Wrap a capacity refusal during setup into the typed environmental error.

    The observed incident (#15710): ``[Errno 122] Disk quota exceeded`` under
    a /tmp that ``df`` showed 3% full — per-user tmpfs quota, not a defect of
    the change under test. The message must make that distinction readable.
    """
    diagnosis = diagnose_disk_pressure(
        effective_temp_base(),
        markers=(_errno.errorcode.get(exc.errno or 0, f"errno {exc.errno}"),),
        stage="temp-root-setup",
    )
    return TempRootUnavailable(
        f"could not materialise the task temp root ({exc}); "
        + "; ".join(diagnosis.reasons),
        diagnosis,
    )


def guarded_isolated_run(
    *,
    repo_root: Path,
    child: Callable[[IsolationLayout, dict[str, str], Path, OsFence], int],
    guarded_homes: tuple[Path, ...] | None = None,
) -> IsolatedRunOutcome:
    """Run ``child`` behind the write-refusal fence, guarding every denied home.

    ``child`` receives the layout, the child env, and **the interpreter that
    carries the refusal hook** — it must launch with that interpreter, not with
    ``sys.executable``, or the hook does not ride along.

    Every root the fence denies is also snapshotted. R1 denied two homes and
    snapshotted one, so a child that lost the fence env could write the other and
    still be reported green (review j#100408 finding_2); the deny set and the guard
    set are now the same set by construction.

    An un-armed fence raises out of :func:`isolated_env` rather than degrading to a
    detect-only run: reporting isolation that is not there is the failure this round
    exists to remove.
    """
    denied = ambient_homes() if guarded_homes is None else tuple(guarded_homes)
    before = {home: snapshot_home(home) for home in denied}
    # A capacity refusal here (EDQUOT under a per-user /tmp quota, ENOSPC) is
    # an environment condition, not a fence failure and not a suite verdict:
    # surface it as the typed environmental refusal (#15710). Any other OSError
    # propagates unchanged.
    try:
        task_root = _make_task_root()
    except OSError as exc:
        if is_disk_pressure_errno(exc.errno):
            raise _pressure_refusal(exc) from exc
        raise
    with task_root as tmp:
        try:
            layout, env, interpreter, ledger, os_fence = isolated_env(
                Path(tmp), denied_homes=denied
            )
        except OSError as exc:
            if is_disk_pressure_errno(exc.errno):
                raise _pressure_refusal(exc) from exc
            raise
        returncode = child(layout, env, interpreter, os_fence)
        fence_root = str(layout.home)
        backend = os_fence.backend
        backend_verified = os_fence.verified
        attempts = read_deny_ledger(ledger)
    guards = tuple(
        # Pass the ordinal: with absolute paths withheld from default output,
        # two guarded homes would otherwise both render as `guarded-home[0]`.
        compare_snapshots(before[home], snapshot_home(home), ordinal)
        for ordinal, home in enumerate(denied)
    )
    return IsolatedRunOutcome(
        suite_success=returncode == 0,
        guards=guards,
        ledger=attempts,
        returncode=returncode,
        fence_root=fence_root,
        os_backend=backend,
        os_backend_verified=backend_verified,
    )


def _scanned_run(
    command: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    scanner: MarkerScanner,
) -> int:
    """Run ``command``, streaming its stderr through while scanning it.

    The quota errors of #15710 happen inside the *child* suite, where this
    process otherwise sees only a non-zero exit code. Stderr is pumped chunk
    by chunk to the parent's stderr — output stays visible in real time, the
    transcript is never buffered whole — and each chunk is fed to the marker
    scanner. Stdout is untouched.
    """
    proc = subprocess.Popen(command, cwd=cwd, env=env, stderr=subprocess.PIPE)
    stream = proc.stderr
    assert stream is not None  # stderr=PIPE above
    try:
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                break
            sys.stderr.buffer.write(chunk)
            sys.stderr.buffer.flush()
            scanner.feed(chunk)
    finally:
        stream.close()
    return proc.wait()


def _unittest_child(
    repo_root: Path,
    unittest_args: tuple[str, ...],
    scanner: MarkerScanner,
) -> Callable[[IsolationLayout, dict[str, str], Path, OsFence], int]:
    """A child that runs the authoritative ``python -m unittest`` command.

    Launched with the fence interpreter rather than ``sys.executable`` so the
    write-refusal hook is armed in the suite process and inherited by everything it
    spawns through its own ``sys.executable``.
    """

    def run(
        layout: IsolationLayout,
        env: dict[str, str],
        interpreter: Path,
        os_fence: OsFence,
    ) -> int:
        return _scanned_run(
            os_fence.wrap([str(interpreter), "-m", "unittest", *unittest_args]),
            cwd=str(repo_root),
            env=env,
            scanner=scanner,
        )

    return run


def _resolve_unittest_args(args: argparse.Namespace) -> tuple[str, ...]:
    explicit = tuple(getattr(args, "unittest_args", ()) or ())
    # argparse keeps the `--` separator when it precedes a REMAINDER list.
    if explicit and explicit[0] == "--":
        explicit = explicit[1:]
    return explicit or DEFAULT_UNITTEST_ARGS


def cmd_tests_run(args: argparse.Namespace) -> int:
    """Run ``unittest`` in an isolated process under the operator-home guard."""
    repo_root = _repo_root(args)
    unittest_args = _resolve_unittest_args(args)

    if getattr(args, "no_isolate", False):
        # Explicit operator/debug escape hatch: no fence, no guard. Announced on
        # stderr so a run recorded as verification cannot look isolated.
        print(
            "tests run: --no-isolate given; running WITHOUT the home fence and "
            "WITHOUT the operator-home guard (not a verification record).",
            file=sys.stderr,
        )
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", *unittest_args], cwd=str(repo_root)
        )
        return proc.returncode

    scanner = MarkerScanner()
    try:
        outcome = guarded_isolated_run(
            repo_root=repo_root,
            child=_unittest_child(repo_root, unittest_args, scanner),
        )
    except TempRootUnavailable as exc:
        # Environmental / configuration refusal (#15710): the task temp root is
        # unusable. The message already carries the typed diagnosis, so a red
        # exit here is readable as environment, never as a diff failure.
        print(f"tests run: {exc}", file=sys.stderr)
        return 1
    except (AuditHookInstallError, OsFenceUnavailable) as exc:
        # Fail closed and run nothing: a suite executed without the refusal layer
        # would produce a verdict that looks isolated and is not.
        print(f"tests run: refusing to run without the write fence -- {exc}",
              file=sys.stderr)
        return 1
    if scanner.suspected:
        # Annotate, never repair: the verdict is unchanged, but the summary says
        # the failures look environmental (quota / pressure) and measures the
        # temp base so the reader can triage without re-deriving #15710.
        outcome = dataclasses.replace(
            outcome,
            disk_pressure=diagnose_disk_pressure(
                effective_temp_base(), scanner.markers, stage="suite-stderr"
            ),
        )
    _render(args, outcome, unittest_args)
    return 0 if outcome.success else 1


def render_outcome(
    outcome: IsolatedRunOutcome,
    *,
    label: str,
    fmt: str = "text",
    reveal_paths: bool = False,
) -> None:
    """Render one guarded run's verdict (shared with profile / parallel)."""
    reveal = reveal_paths
    if fmt == "json":
        payload = outcome.as_dict(reveal_paths=reveal)
        payload["label"] = label
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"=== isolated test run ({label}) ===")
    print(
        "fence root: "
        + (
            outcome.fence_root
            if reveal
            else path_label(outcome.fence_root, "fence-root")
        )
    )
    print(
        f"OS write boundary: {outcome.os_backend}"
        + ("" if outcome.os_backend_verified else " (backend NOT measured in-repo)")
    )
    for guard in outcome.guards:
        print(
            f"guarded home: {guard.home if reveal else guard.label} -- "
            + ("unchanged" if guard.unchanged else "CHANGED (fail-closed)")
        )
    ledger = outcome.ledger
    print(
        "refused write attempts: "
        + (
            "none"
            if ledger.clean
            else ("LEDGER MISSING" if ledger.missing else str(len(ledger.attempts)))
        )
    )
    print(f"suite: {'PASS' if outcome.suite_success else 'FAIL'}")
    print(f"result: {'PASS' if outcome.success else 'FAIL'}")
    for reason in outcome.all_reasons:
        print(f"  reason: {reason}")
    if not outcome.homes_unchanged:
        print(
            "  the guard never repairs or deletes operator state; inspect the "
            "reported tier and decide the disposition yourself."
        )


def _render(
    args: argparse.Namespace,
    outcome: IsolatedRunOutcome,
    unittest_args: tuple[str, ...],
) -> None:
    render_outcome(
        outcome,
        label="python -m unittest " + " ".join(unittest_args),
        fmt=getattr(args, "format", "text"),
        reveal_paths=bool(getattr(args, "reveal_paths", False)),
    )


def isolate_self(args: argparse.Namespace, *, label: str) -> int | None:
    """Re-run this invocation in a fenced child, or ``None`` to run in-process.

    The shared entry hook for handlers that must keep running their own logic in
    the child (``tests profile`` / ``tests parallel``). Returns the exit code of
    the guarded child when it re-exec'ed, and ``None`` when the caller should
    just proceed — because it is already fenced, or because isolation was
    explicitly declined.
    """
    if getattr(args, "already_isolated", False) or process_home_fence() is not None:
        # Both of those are CLAIMS, not enforcement (j#100483 item 4): a hidden flag
        # anyone can pass, and an in-process marker that says a home resolver was
        # rebound. Proceeding in-process on a claim is how an unfenced run would
        # report itself isolated. Confirm the OS boundary is actually present --
        # `load_outer_context()` returns None only when there is genuinely none, and
        # raises for a corrupted one.
        try:
            context = load_outer_context()
        except OsFenceUnavailable as exc:
            print(f"tests {label}: {exc}", file=sys.stderr)
            return 2
        if context is not None:
            # A context is a DESCRIPTION of a boundary, not the boundary (F1). A
            # task venv carries its generated context module wherever it goes, so a
            # run started outside the OS wrapper -- with `--already-isolated` -- can
            # present a perfectly valid one while nothing is enforcing. Measure it:
            # build the prefix-less inherited fence the context describes and put it
            # through the same 4 probes any fresh fence must pass. The probes hit the
            # authority canary and its victims, never the operator's home.
            try:
                verify_os_fence(inherited_fence(context.outer_protected_roots),
                                Path(sys.executable))
            except OsFenceUnavailable as exc:
                print(
                    f"tests {label}: this process carries an OS boundary context but "
                    f"the boundary is not enforcing ({exc}). Refusing to run.",
                    file=sys.stderr,
                )
                return 2
            return None
        if context is None:
            # A non-zero exit, not an exception: this is a refusal the CLI reports,
            # and the other fence failures on this rail already surface as codes.
            print(
                f"tests {label}: this process is marked isolated (already_isolated / "
                f"process home fence) but carries no OS boundary context, so nothing "
                f"is enforcing. Refusing to run rather than reporting an unfenced run "
                f"as isolated.",
                file=sys.stderr,
            )
            return 2
        return None
    if getattr(args, "no_isolate", False):
        print(
            f"tests {label}: --no-isolate given; running WITHOUT the home fence "
            "and WITHOUT the operator-home guard (not a verification record).",
            file=sys.stderr,
        )
        return None
    invoked_argv = getattr(args, "invoked_argv", None)
    if not invoked_argv:
        # No recorded argv means this handler was called directly rather than
        # through `cli.main` (in-process callers construct their own namespace).
        # Replaying `sys.argv` there would re-exec whatever the host process was
        # started with, so run in-process instead. `cli.main` always records the
        # argv, and `IsolatedByDefaultThroughTheCliTest` pins that it does — the
        # CLI rail must never reach this branch.
        return None

    repo_root = _repo_root(args)
    argv = [*invoked_argv, ISOLATED_FLAG]
    # Under `--format json` the child's stdout is a single JSON document, so the
    # guard cannot simply be appended as prose -- that would make the document
    # unparseable for the consumer the flag exists for. Capture it instead and
    # merge the guard in as a key.
    as_json = getattr(args, "format", "text") == "json"
    captured: dict = {}

    def child(
        layout: IsolationLayout,
        env: dict[str, str],
        interpreter: Path,
        os_fence: OsFence,
    ) -> int:
        return reexec_isolated(
            argv,
            repo_root=repo_root,
            env=env,
            interpreter=interpreter,
            os_fence=os_fence,
            capture_stdout=captured if as_json else None,
        )

    try:
        outcome = guarded_isolated_run(repo_root=repo_root, child=child)
    except TempRootUnavailable as exc:
        # Environmental / configuration refusal (#15710); same shape as the
        # `tests run` handler so all three entry points read alike.
        print(f"tests {label}: {exc}", file=sys.stderr)
        return 1
    except (AuditHookInstallError, OsFenceUnavailable) as exc:
        print(f"tests {label}: refusing to run without the write fence -- {exc}",
              file=sys.stderr)
        return 1
    if as_json:
        _emit_merged_json(
            captured.get("stdout", ""),
            outcome,
            reveal_paths=bool(getattr(args, "reveal_paths", False)),
        )
        return 0 if outcome.success else 1
    # The child already rendered its own summary; add only the guard verdict, so
    # `tests profile`'s runtime table and `tests parallel`'s shard table stay the
    # primary output and the guard reads as an additional gate on top.
    if outcome.success:
        print(
            "operator shared homes: unchanged; refused write attempts: none "
            f"({len(outcome.guards)} guarded)"
        )
    else:
        print("operator shared home isolation: FAILED (fail-closed)")
        for reason in outcome.all_reasons:
            print(f"  reason: {reason}")
    return 0 if outcome.success else 1


def _emit_merged_json(
    child_stdout: str, outcome: IsolatedRunOutcome, *, reveal_paths: bool = False
) -> None:
    """Re-emit the child's JSON with the guard verdict merged in.

    A child document that does not parse is passed through verbatim rather than
    dropped -- losing the child's own output would be worse than emitting two
    documents -- and the guard is then reported on stderr so it cannot go
    unnoticed while the exit code still carries it.
    """
    guard = {
        "home_guards": [g.as_dict(reveal_paths=reveal_paths) for g in outcome.guards],
        "deny_ledger": outcome.ledger.as_dict(),
        "fence_root": (
            outcome.fence_root
            if reveal_paths
            else path_label(outcome.fence_root, "fence-root")
        ),
        "isolated_success": outcome.success,
    }
    try:
        payload = _json.loads(child_stdout)
    except ValueError:
        sys.stdout.write(child_stdout)
        print(
            "tests: could not merge the home-guard verdict into the child's JSON "
            f"output; guard={_json.dumps(guard, ensure_ascii=False)}",
            file=sys.stderr,
        )
        return
    if isinstance(payload, dict):
        payload.update(guard)
        # The document's own `success` must not claim a pass the guard refused.
        if "success" in payload:
            payload["success"] = bool(payload["success"]) and outcome.success
    else:
        payload = {"child": payload, **guard}
    print(_json.dumps(payload, ensure_ascii=False, indent=2))


__all__ = (
    "DEFAULT_UNITTEST_ARGS",
    "ISOLATED_FLAG",
    "cmd_tests_run",
    "guarded_isolated_run",
    "isolate_self",
    "render_outcome",
)
