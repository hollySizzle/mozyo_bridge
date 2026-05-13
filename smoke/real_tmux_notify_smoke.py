#!/usr/bin/env python3
"""Real-tmux smoke for the strict `--mode standard` delivery rail.

The relaxed `--mode queue-enter` rail (defined in
``vibes/docs/logics/tmux-send-safety-contract.md`` ``## Relaxed Queue-Enter
Rail``) is intentionally NOT exercised by this smoke. Its distinguishing
semantic — pressing Enter despite a landing marker that was never observed in
the captured pane text — only manifests against a real Claude/Codex TUI that
queues prompts during a running turn. A plain ``sh`` receiver has no prompt
queue, and queue-enter rejects ``--force``, so spoofing one with a non-agent
process is not a valid stand-in. In-process coverage of the new rail
(observed / unobserved marker, ``--force`` rejection, ``--target``
window-mismatch guard, agent-gate stricter than strict, durable-record
wording, ``last_input`` projection, ``next_action_owner``) lives in
``tests/test_mozyo_bridge.py::RelaxedQueueEnterRailTest``.

When an operator change touches the queue-enter rail, the manual verification
recipe is:

1. From the repo root, open the standard pair: ``mozyo`` (creates / attaches a
   tmux session with a ``claude`` window and a ``codex`` window running the
   real Claude / Codex CLI).
2. In a sender pane, run:

       mozyo-bridge handoff send --to codex --source asana \\
           --task-id <task> --comment-id <comment> --kind reply \\
           --mode queue-enter

   Expected (marker observed in the receiver's scrollback before timeout):
   ``Outcome`` line reads ``sent (queue-enter, marker observed)`` and the JSON
   outcome shows ``status=sent reason=ok mode=queue-enter``.
3. To exercise the unobserved-marker path, force the receiver to be mid-turn
   (e.g., send a long-running prompt so the next handoff marker scrolls past
   or wraps below the capture window) and rerun the same command. Expected:
   ``Outcome`` line reads ``sent (queue-enter, marker unobserved)``, JSON
   shows ``status=sent reason=queue_enter``, the durable record carries the
   ``Operator note`` line pointing at ``--mode standard`` as the fallback,
   and the receiver's TUI still picks the prompt up off its queue once its
   current turn ends.
4. Strict rail regression check (must keep working): rerun step 3 without
   ``--mode queue-enter``. Expected: the command dies with
   ``handoff marker was not observed...``, no Enter is pressed, the JSON
   outcome shows ``status=blocked reason=marker_timeout mode=standard``.

If any of the above diverges from the contract section ``## Relaxed
Queue-Enter Rail``, stop and record the gap in the owning Asana task before
landing the change.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_COMMAND = shlex.split(os.environ.get("MOZYO_BRIDGE_COMMAND", f"{sys.executable} -m mozyo_bridge"))


def run(*args: str, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=REPO_ROOT,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run("tmux", *args, check=check)


def capture(target: str, lines: int = 80) -> str:
    return tmux("capture-pane", "-t", target, "-p", "-J", "-S", f"-{lines}").stdout


def wait_for(target: str, needle: str, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in capture(target):
            return True
        time.sleep(0.2)
    return False


def main() -> int:
    if shutil.which("tmux") is None:
        print("skip: tmux is not installed", file=sys.stderr)
        return 77

    session = f"mozyo-bridge-smoke-{os.getpid()}"
    # The smoke session has one window; the sender pane lives in that window.
    # `cmd_message` stamps the sender-side header with the pane's window name
    # (window-only model, Asana task 1214759644692283), so name the window
    # `codex` to match the `from:codex` assertion below. All splits inherit
    # the window name, so receivers spawned later are also in the `codex`
    # window — that is fine because the smoke targets them via explicit pane
    # ids and `--force`, not by agent-label resolution.
    tmux("new-session", "-d", "-s", session, "-n", "codex", "-c", str(REPO_ROOT), "bash")
    try:
        sender = tmux("list-panes", "-t", session, "-F", "#{pane_id}").stdout.splitlines()[0]
        receiver = tmux(
            "split-window",
            "-t",
            session,
            "-h",
            "-c",
            str(REPO_ROOT),
            "-P",
            "-F",
            "#{pane_id}",
            "sh",
            "-lc",
            "IFS= read -r line; printf 'RECEIVED:%s\\n' \"$line\"; sleep 30",
        ).stdout.strip()

        env = os.environ.copy()
        env["TMUX_PANE"] = sender
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        result = run(
            *BRIDGE_COMMAND,
            "notify-claude",
            "--issue",
            "9020",
            "--journal",
            "46005",
            "--type",
            "review_result",
            "--target",
            receiver,
            "--force",
            "--read-lines",
            "80",
            "--landing-timeout",
            "5",
            "--submit-delay",
            "0.2",
            check=False,
            env=env,
        )
        if result.returncode != 0:
            print(result.stdout, file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            print(f"fail: notify command exited with {result.returncode}", file=sys.stderr)
            return 1
        if "notified claude: journal=46005" not in result.stdout:
            print(result.stdout, file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            print("fail: notify command did not report success", file=sys.stderr)
            return 1

        # The standard notify-* wrappers now route through the new handoff
        # primitive (audit-approved in commit 5012aac), so the receiver sees
        # the `[mozyo:handoff:...]` marker shape and the new durable-anchor
        # body. The legacy `[mozyo-bridge from:...]` marker is still used by
        # the bare `mozyo-bridge message` subcommand exercised below.
        if not wait_for(receiver, "RECEIVED:[mozyo:handoff:source=redmine:issue=9020:journal=46005:"):
            print(capture(receiver), file=sys.stderr)
            print("fail: receiver did not get submitted handoff marker", file=sys.stderr)
            return 1
        if not wait_for(receiver, "review result ready for claude"):
            print(capture(receiver), file=sys.stderr)
            print("fail: receiver did not see new handoff body intent", file=sys.stderr)
            return 1
        if not wait_for(receiver, "Redmine #9020 journal #46005 is the durable anchor"):
            print(capture(receiver), file=sys.stderr)
            print("fail: receiver message did not include Redmine durable-anchor body", file=sys.stderr)
            return 1

        message_receiver = tmux(
            "split-window",
            "-t",
            session,
            "-h",
            "-c",
            str(REPO_ROOT),
            "-P",
            "-F",
            "#{pane_id}",
            "sh",
            "-lc",
            "IFS= read -r line; printf 'RECEIVED:%s\\n' \"$line\"; sleep 30",
        ).stdout.strip()
        run(*BRIDGE_COMMAND, "read", message_receiver, env=env)
        message_result = run(
            *BRIDGE_COMMAND,
            "message",
            message_receiver,
            "handoff body without notify-* flow",
            "--landing-timeout",
            "5",
            "--submit-delay",
            "0.2",
            "--read-lines",
            "80",
            check=False,
            env=env,
        )
        if message_result.returncode != 0:
            print(message_result.stdout, file=sys.stderr)
            print(message_result.stderr, file=sys.stderr)
            print(f"fail: message command exited with {message_result.returncode}", file=sys.stderr)
            return 1
        if not wait_for(message_receiver, "RECEIVED:[mozyo-bridge from:"):
            print(capture(message_receiver), file=sys.stderr)
            print("fail: bare message command did not submit Enter on the receiver", file=sys.stderr)
            return 1
        if not wait_for(message_receiver, "handoff body without notify-* flow"):
            print(capture(message_receiver), file=sys.stderr)
            print("fail: bare message body did not reach the receiver", file=sys.stderr)
            return 1

        marker = "[mozyo-bridge from:codex pane:%1 at:agents:0.0]"
        first_half, second_half = marker.split(" ", 1)
        wrap_receiver = tmux(
            "split-window",
            "-t",
            session,
            "-h",
            "-c",
            str(REPO_ROOT),
            "-P",
            "-F",
            "#{pane_id}",
            "sh",
            "-c",
            f"printf '%b' '> {first_half}\\n  {second_half} wrap probe body\\n'; sleep 60",
        ).stdout.strip()
        if not wait_for(wrap_receiver, second_half.split(" ", 1)[0], timeout=5.0):
            print(capture(wrap_receiver), file=sys.stderr)
            print("fail: wrap probe receiver did not render the split marker", file=sys.stderr)
            return 1

        sys.path.insert(0, str(REPO_ROOT / "src"))
        from mozyo_bridge.application.commands import wait_for_text  # type: ignore

        if not wait_for_text(wrap_receiver, marker, 200, 5.0):
            print(capture(wrap_receiver), file=sys.stderr)
            print("fail: wait_for_text did not detect the wrap-split marker", file=sys.stderr)
            return 1

        if wait_for_text(wrap_receiver, "[mozyo-bridge from:absent pane:%9 at:none]", 200, 0.5):
            print(capture(wrap_receiver), file=sys.stderr)
            print("fail: wait_for_text falsely matched a marker that was not in the pane", file=sys.stderr)
            return 1

        print("ok: real tmux notify smoke passed")
        return 0
    finally:
        tmux("kill-session", "-t", session, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
