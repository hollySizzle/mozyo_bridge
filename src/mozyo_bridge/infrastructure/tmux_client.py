from __future__ import annotations

import subprocess
from pathlib import Path

from mozyo_bridge.shared.errors import die
from mozyo_bridge.shared.paths import DEFAULT_TMUX_CONF, LABEL_OPTION


def run_tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def require_tmux() -> None:
    if subprocess.run(["sh", "-c", "command -v tmux >/dev/null 2>&1"]).returncode != 0:
        die("tmux is not installed or not in PATH")


def source_tmux_conf(path: str | None = None, *, optional: bool = False) -> bool:
    """Source `path` (or the default tmux conf) into tmux.

    When `optional` is True and the resolved file does not exist, this is a
    no-op that returns False so the auto-startup paths can proceed without
    a config file. When `optional` is False, a missing file is fatal as before.
    Returns True when tmux source-file was invoked successfully.
    """
    conf = Path(path or DEFAULT_TMUX_CONF).expanduser()
    if not conf.exists():
        if optional:
            return False
        die(f"tmux config not found: {conf}")
    result = run_tmux("source-file", str(conf), check=False)
    if result.returncode != 0:
        die(f"tmux source-file failed: {result.stderr.strip() or result.stdout.strip()}")
    return True


def pane_lines() -> list[dict[str, str]]:
    fmt = (
        "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t"
        "#{pane_current_command}\t#{@agent_name}\t#{pane_current_path}\t"
        "#{window_name}\t#{pane_active}"
    )
    result = run_tmux("list-panes", "-a", "-F", fmt, check=False)
    if result.returncode != 0:
        die(f"tmux list-panes failed: {result.stderr.strip() or 'no tmux server'}")
    panes: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = (line.split("\t", 6) + [""] * 7)[:7]
        pane_id, location, command, label, cwd, window_name, pane_active = parts
        panes.append(
            {
                "id": pane_id,
                "location": location,
                "command": command,
                "label": label,
                "cwd": cwd,
                "window_name": window_name,
                "pane_active": pane_active,
            }
        )
    return panes


def capture_pane(target: str, lines: int) -> str:
    result = run_tmux("capture-pane", "-t", target, "-p", "-J", "-S", f"-{lines}")
    return result.stdout


def validate_target(target: str) -> None:
    result = run_tmux("display-message", "-t", target, "-p", "#{pane_id}", check=False)
    if result.returncode != 0:
        die(f"invalid tmux target: {target}")


def pane_label(pane_id: str) -> str:
    result = run_tmux("display-message", "-t", pane_id, "-p", f"#{{{LABEL_OPTION}}}", check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def pane_location(pane_id: str) -> str:
    result = run_tmux("display-message", "-t", pane_id, "-p", "#{session_name}:#{window_index}.#{pane_index}")
    return result.stdout.strip()


def session_exists(session: str) -> bool:
    result = run_tmux("has-session", "-t", session, check=False)
    return result.returncode == 0


def set_pane_label(pane_id: str, label: str) -> None:
    run_tmux("set-option", "-p", "-t", pane_id, LABEL_OPTION, label)
