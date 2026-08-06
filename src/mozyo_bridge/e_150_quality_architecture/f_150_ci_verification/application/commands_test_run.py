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
import json as _json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.test_home_fence import (
    ambient_homes,
    effective_home,
    isolated_env,
    reexec_isolated,
    snapshot_home,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_home_isolation import (
    IsolatedRunOutcome,
    IsolationLayout,
    compare_snapshots,
)
from mozyo_bridge.shared.paths import process_home_fence, resolve_repo_root

#: Default arguments when the caller names no target: the authoritative full
#: discovery command, spelled exactly as CI and the placement policy spell it.
DEFAULT_UNITTEST_ARGS = ("discover", "-s", "tests")

#: Hidden flag a re-exec'ed child carries, so it runs in-process instead of
#: spawning a third generation. The fence env is the second, independent guard.
ISOLATED_FLAG = "--already-isolated"

_TEMP_PREFIX = "mozyo-tests-home-"


def _repo_root(args: argparse.Namespace) -> Path:
    return resolve_repo_root(getattr(args, "repo", None))


def guarded_isolated_run(
    *,
    repo_root: Path,
    child: Callable[[IsolationLayout, dict[str, str]], int],
    guarded_home: Path | None = None,
) -> IsolatedRunOutcome:
    """Run ``child`` inside a fresh fence and guard the operator home around it.

    ``child`` receives the layout and the child env and returns an exit code.

    The guarded target defaults to :func:`effective_home` — the one shared home
    *this* process would itself have resolved. From an unfenced parent (the normal
    case) that is the operator's home. From an already-fenced parent (a nested
    invocation) it is the outer fence's home, which is still the shared state the
    inner run could damage, so the guard stays meaningful either way rather than
    reaching past the fence to the operator's real home.
    """
    denied = ambient_homes()
    target = Path(guarded_home) if guarded_home is not None else effective_home()
    before = snapshot_home(target)
    with tempfile.TemporaryDirectory(prefix=_TEMP_PREFIX) as tmp:
        layout, env = isolated_env(Path(tmp), denied_homes=denied)
        returncode = child(layout, env)
        fence_root = str(layout.home)
    after = snapshot_home(target)
    return IsolatedRunOutcome(
        suite_success=returncode == 0,
        guard=compare_snapshots(before, after),
        returncode=returncode,
        fence_root=fence_root,
    )


def _unittest_child(
    repo_root: Path, unittest_args: tuple[str, ...]
) -> Callable[[IsolationLayout, dict[str, str]], int]:
    """A child that runs the authoritative ``python -m unittest`` command."""

    def run(layout: IsolationLayout, env: dict[str, str]) -> int:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", *unittest_args],
            cwd=str(repo_root),
            env=env,
        )
        return proc.returncode

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

    outcome = guarded_isolated_run(
        repo_root=repo_root, child=_unittest_child(repo_root, unittest_args)
    )
    _render(args, outcome, unittest_args)
    return 0 if outcome.success else 1


def render_outcome(
    outcome: IsolatedRunOutcome, *, label: str, fmt: str = "text"
) -> None:
    """Render one guarded run's verdict (shared with profile / parallel)."""
    if fmt == "json":
        payload = outcome.as_dict()
        payload["label"] = label
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
        return
    guard = outcome.guard
    print(f"=== isolated test run ({label}) ===")
    print(f"fence root: {outcome.fence_root}")
    print(f"guarded home: {guard.home}")
    print(
        "operator shared home: "
        + ("unchanged" if guard.unchanged else "CHANGED (fail-closed)")
    )
    print(f"suite: {'PASS' if outcome.suite_success else 'FAIL'}")
    print(f"result: {'PASS' if outcome.success else 'FAIL'}")
    for reason in outcome.all_reasons:
        print(f"  reason: {reason}")
    if not guard.unchanged:
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

    def child(layout: IsolationLayout, env: dict[str, str]) -> int:
        return reexec_isolated(
            argv,
            repo_root=repo_root,
            env=env,
            capture_stdout=captured if as_json else None,
        )

    outcome = guarded_isolated_run(repo_root=repo_root, child=child)
    if as_json:
        _emit_merged_json(captured.get("stdout", ""), outcome)
        return 0 if outcome.success else 1
    # The child already rendered its own summary; add only the guard verdict, so
    # `tests profile`'s runtime table and `tests parallel`'s shard table stay the
    # primary output and the guard reads as an additional gate on top.
    if outcome.guard.unchanged:
        print(f"operator shared home: unchanged ({outcome.guard.home})")
    else:
        print(f"operator shared home: CHANGED (fail-closed) ({outcome.guard.home})")
        for reason in outcome.all_reasons:
            print(f"  reason: {reason}")
    return 0 if outcome.success else 1


def _emit_merged_json(child_stdout: str, outcome: IsolatedRunOutcome) -> None:
    """Re-emit the child's JSON with the guard verdict merged in.

    A child document that does not parse is passed through verbatim rather than
    dropped -- losing the child's own output would be worse than emitting two
    documents -- and the guard is then reported on stderr so it cannot go
    unnoticed while the exit code still carries it.
    """
    guard = {
        "home_guard": outcome.guard.as_dict(),
        "fence_root": outcome.fence_root,
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
            payload["success"] = bool(payload["success"]) and outcome.guard.unchanged
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
