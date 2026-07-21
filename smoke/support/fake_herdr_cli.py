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

import json
import os
import sys
from pathlib import Path

# The adapter runs as a subprocess of the installed CLI; put the repo's test-support package on
# the path so the CANONICAL fake is reused (never a second protocol model).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.support.herdr_fake import FakeHerdr  # noqa: E402


def _state_path() -> Path:
    raw = os.environ.get("MOZYO_FAKE_HERDR_STATE", "").strip()
    if not raw:
        sys.stderr.write("fake_herdr_cli: MOZYO_FAKE_HERDR_STATE is required\n")
        raise SystemExit(2)
    return Path(raw)


def main(argv: list[str]) -> int:
    state_path = _state_path()
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"fake_herdr_cli: unreadable state {state_path}: {exc}\n")
        return 2
    fake = FakeHerdr.from_state(state)
    # The turn-start wait rail (``wait agent-status``) is the canonical fake's POPEN seam, not its
    # run seam — model it here so the installed CLI's delivery confirmation observes an armed
    # transition (Redmine #14097 Design Consultation j#84712). Any other argv replays through run.
    if argv[:2] == ["wait", "agent-status"]:
        proc = fake.popen([sys.argv[0], *argv])
        out, err = proc.communicate()
        state_path.write_text(json.dumps(fake.to_state()), encoding="utf-8")
        if out:
            sys.stdout.write(out)
        if err:
            sys.stderr.write(err)
        return int(proc.returncode or 0)
    # ``fake.run`` takes the full argv (binary + command); replay exactly this invocation.
    result = fake.run([sys.argv[0], *argv])
    # Persist any mutation (pane close / agent start) so a later invocation sees it.
    try:
        state_path.write_text(json.dumps(fake.to_state()), encoding="utf-8")
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
