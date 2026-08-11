#!/usr/bin/env python3
"""A standalone ``MOZYO_HERDR_BINARY`` adapter for the installed fault smoke (Redmine #14097).

The installed fault smoke drives the REAL installed ``mozyo-bridge`` in an isolated venv
subprocess. Under the herdr backend that CLI shells out to ``MOZYO_HERDR_BINARY`` — a boundary
the in-process :class:`~tests.support.herdr_fake.FakeHerdr` cannot cross. This thin adapter is
the smoke's only new executable: it OWNS nothing about the Herdr protocol (coordinator decision
j#83808 Q3). It rehydrates the CANONICAL fake from a state file, replays exactly one command
through it, persists any mutation back, and prints the fake's own JSON — so the command
vocabulary / JSON shape stay single-sourced in ``herdr_fake.py``.

Safety: it reads only a caller-provided, secret-free temp state file (``MOZYO_FAKE_HERDR_STATE``),
never the operator home / a real Herdr / tmux / SQLite. An unknown command fails closed (the
fake raises); it never falls back to a real backend.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import time
from pathlib import Path

# The adapter runs as a subprocess of the installed CLI; put the repo's test-support package on
# the path so the CANONICAL fake is reused (never a second protocol model).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.support.herdr_fake import (  # noqa: E402
    WAIT_ABSENT_MESSAGE,
    WAIT_TIMEOUT_MESSAGE,
    FakeHerdr,
)


def _state_path() -> Path:
    raw = os.environ.get("MOZYO_FAKE_HERDR_STATE", "").strip()
    if not raw:
        sys.stderr.write("fake_herdr_cli: MOZYO_FAKE_HERDR_STATE is required\n")
        raise SystemExit(2)
    return Path(raw)


def _flag_value(argv: list[str], flag: str) -> str:
    try:
        index = argv.index(flag)
    except ValueError:
        return ""
    return argv[index + 1] if index + 1 < len(argv) else ""


def _wait_registration_path(state_path: Path, target: str) -> Path:
    """A target-scoped adapter handshake; its name contains no pane identity."""
    digest = hashlib.sha256(target.encode("utf-8")).hexdigest()[:24]
    return state_path.with_name(f".{state_path.name}.wait-{digest}")


def _pid_is_live(raw: str) -> bool:
    try:
        pid = int(raw.strip())
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _has_live_registration(path: Path) -> bool:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError:
        return False
    if _pid_is_live(raw):
        return True
    path.unlink(missing_ok=True)
    return False


def _wait_for_live_registration(path: Path, *, timeout: float = 1.0) -> bool:
    """Let the wait subprocess publish its handshake before an Enter can fire."""
    deadline = time.monotonic() + timeout
    while True:
        if _has_live_registration(path):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)


def _load_fake(state_path: Path) -> FakeHerdr:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"unreadable state {state_path}: {exc}") from exc
    return FakeHerdr.from_state(state)


def _persist_fake(state_path: Path, fake: FakeHerdr) -> None:
    state_path.write_text(json.dumps(fake.to_state()), encoding="utf-8")


def _emit_process(proc) -> int:
    out, err = proc.communicate()
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    return int(proc.returncode or 0)


def _run_wait(argv: list[str], state_path: Path) -> int:
    """Keep the installed wait process live until a later Enter fires its transition.

    The in-process canonical fake delays its state change until ``communicate()``. Across an
    executable boundary, immediately communicating would make the process exit *before* the
    product's Enter effect fence. This adapter therefore publishes a PID handshake, leaves the
    process alive, and observes the successful Enter invocation consume the same canonical armed
    transition from the shared state. No body send, absent wait, or timeout can fire it.
    """
    target = argv[2] if len(argv) > 2 else ""
    wanted = _flag_value(argv, "--status")
    try:
        timeout_ms = max(1, int(_flag_value(argv, "--timeout") or "1000"))
    except ValueError:
        timeout_ms = 1000
    registration = _wait_registration_path(state_path, target)
    proc = None
    try:
        with _state_lock(state_path):
            try:
                fake = _load_fake(state_path)
            except RuntimeError as exc:
                sys.stderr.write(f"fake_herdr_cli: {exc}\n")
                return 2
            proc = fake.popen([sys.argv[0], *argv])
            if proc.returncode != 0:
                return _emit_process(proc)
            try:
                registration.write_text(str(os.getpid()), encoding="utf-8")
            except OSError as exc:
                sys.stderr.write(
                    f"fake_herdr_cli: could not register causal wait: {exc.__class__.__name__}\n"
                )
                return 2

        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            with _state_lock(state_path):
                try:
                    current = _load_fake(state_path)
                except RuntimeError as exc:
                    sys.stderr.write(f"fake_herdr_cli: {exc}\n")
                    return 2
                agent = current._resolve_agent(target)
                if agent is None:
                    sys.stderr.write(WAIT_ABSENT_MESSAGE)
                    return 1
                transition_pending = any(
                    armed_target == target and to_status == wanted
                    for armed_target, to_status in current._armed_transitions
                )
                if agent.status == wanted and not transition_pending:
                    assert proc is not None
                    return _emit_process(proc)
            time.sleep(0.005)
        sys.stderr.write(WAIT_TIMEOUT_MESSAGE)
        return 1
    finally:
        registration.unlink(missing_ok=True)


def _is_enter_send(argv: list[str]) -> bool:
    return (
        argv[:2] == ["pane", "send-keys"]
        and len(argv) > 3
        and argv[3].strip().lower() == "enter"
    )


def _fire_enter_transition(fake: FakeHerdr, target: str) -> None:
    """Apply one target-matching transition through the canonical fake wait process."""
    wanted = next(
        (
            to_status
            for armed_target, to_status in fake._armed_transitions
            if armed_target == target
        ),
        "",
    )
    if not wanted:
        return
    proc = fake.popen(
        [
            sys.argv[0],
            "wait",
            "agent-status",
            target,
            "--status",
            wanted,
            "--timeout",
            "1",
        ]
    )
    proc.communicate()


@contextlib.contextmanager
def _state_lock(state_path: Path):
    """Serialize the read->mutate->write cycle across concurrent adapter invocations.

    The standard-rail turn-start choreography ARMS its ``wait agent-status`` (a non-blocking
    background invocation of this adapter) and THEN injects (``pane send-text`` / ``send-keys``,
    further invocations) — so two adapter processes hold the SAME state file at once. Without a
    lock their read-modify-write cycles interleave and one clobbers the other (a stale snapshot
    restores a just-consumed armed transition, so the confirmed turn-start intermittently reads
    ``uncertain``): the exact non-determinism a deterministic harness must not have (Redmine
    #14097). An exclusive advisory lock on a sibling lock file makes each cycle atomic. Degrades to
    a no-op where ``fcntl`` is unavailable (non-POSIX) rather than failing the smoke.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - POSIX-only; the smoke runs on Linux/macOS CI
        yield
        return
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    with open(lock_path, "w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main(argv: list[str]) -> int:
    state_path = _state_path()
    if argv[:2] == ["wait", "agent-status"]:
        return _run_wait(argv, state_path)

    enter_send = _is_enter_send(argv)
    registration = None
    wait_registered = False
    if enter_send:
        # Popen returning is not a child-readiness handshake. Give the wait subprocess a short,
        # bounded chance to register before consuming its transition. The ordinary fake send
        # itself still succeeds without a waiter, but an unarmed Enter must never manufacture a
        # causal turn-start confirmation.
        registration = _wait_registration_path(state_path, argv[2])
        wait_registered = _wait_for_live_registration(registration)
    # One atomic read->mutate->write cycle: a concurrently-armed ``wait`` and an injecting ``send``
    # can no longer lose each other's writes (the intermittent-uncertain race above).
    with _state_lock(state_path):
        try:
            fake = _load_fake(state_path)
        except RuntimeError as exc:
            sys.stderr.write(f"fake_herdr_cli: {exc}\n")
            return 2
        # ``fake.run`` takes the full argv (binary + command); replay exactly this invocation.
        result = fake.run([sys.argv[0], *argv])
        if (
            enter_send
            and result.returncode == 0
            and wait_registered
            and registration is not None
            and _has_live_registration(registration)
        ):
            _fire_enter_transition(fake, argv[2])
        # Persist any mutation (pane close / agent start) so a later invocation sees it.
        try:
            _persist_fake(state_path, fake)
        except OSError as exc:  # pragma: no cover - a temp write failure is a smoke infra fault
            sys.stderr.write(f"fake_herdr_cli: could not persist state: {exc}\n")
            return 2
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return int(result.returncode or 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
