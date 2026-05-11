#!/usr/bin/env python3
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
    tmux("new-session", "-d", "-s", session, "-c", str(REPO_ROOT), "bash")
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
        tmux("set-option", "-p", "-t", sender, "@agent_name", "codex")
        tmux("set-option", "-p", "-t", receiver, "@agent_name", "claude")

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

        if not wait_for(receiver, "RECEIVED:[mozyo-bridge from:codex pane:"):
            print(capture(receiver), file=sys.stderr)
            print("fail: receiver did not get submitted message", file=sys.stderr)
            return 1
        if not wait_for(receiver, "Redmine #9020 journal #46005 is ready for claude"):
            print(capture(receiver), file=sys.stderr)
            print("fail: receiver message did not include Redmine journal gate", file=sys.stderr)
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
        tmux("set-option", "-p", "-t", message_receiver, "@agent_name", "claude")
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

        print("ok: real tmux notify smoke passed")
        return 0
    finally:
        tmux("kill-session", "-t", session, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
