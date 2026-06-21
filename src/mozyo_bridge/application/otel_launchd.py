"""launchd residency for the OTel receiver daemon (Redmine #11690).

The owner decision (#11639 journal #56088 constraint 1) makes launchd
residency mandatory: the receiver is push-based and best-effort, so a
non-resident receiver silently loses telemetry. This module is the
minimal management face — install / uninstall / status / restart — and
deliberately not a general daemon manager: start and stop fall out of
``RunAtLoad`` + ``KeepAlive`` and ``bootout``, and anything fancier
belongs to launchd itself.

Safety boundary:

- The plist carries **no EnvironmentVariables block at all**, so no
  credential (``MOZYO_REDMINE_API_KEY`` included) can ever land in it.
  A launchd-started receiver inherits no shell environment, so the
  Redmine API key/URL reach it through the home-scoped credential file
  (``${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/redmine-credentials.yaml``,
  resolved by ``redmine_credentials``; Redmine #12306) — never the plist.
  Without that file (or shell env, for an interactive ``otel serve``) the
  cockpit's Redmine layer degrades to ``unconfigured``; the other two
  layers are unaffected.
- The receiver keeps its own loopback-only bind gate; the plist's
  ProgramArguments add no ``--host`` so the 127.0.0.1 default applies.
- All ``launchctl`` invocations are structured argv — no shell strings.
- The plist file is wholly owned by this label under
  ``~/Library/LaunchAgents``; install rewrites it idempotently and
  uninstall removes exactly it. Nothing else on the host is touched
  except the log directory under ``~/Library/Logs/mozyo-bridge``.
- Nothing here writes to Redmine; absence of the agent degrades exactly
  like a stopped receiver (events lost by design, ``unknown`` states,
  tmux fallback) — pinned behavior from phases 1–3.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

from mozyo_bridge.shared.errors import die

LAUNCHD_LABEL = "biz.asile.mozyo-bridge.otel"
PLIST_RELATIVE = Path("Library/LaunchAgents") / f"{LAUNCHD_LABEL}.plist"
LOG_RELATIVE = Path("Library/Logs/mozyo-bridge/otel-receiver.log")


def plist_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / PLIST_RELATIVE


def log_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / LOG_RELATIVE


def resolve_serve_command(port: int | None = None) -> list[str]:
    """The exact argv launchd runs: the installed CLI, no shell.

    Resolved at install time via PATH so the plist survives shell-env
    differences; a missing executable is a user-actionable error (pipx
    install first).
    """
    executable = shutil.which("mozyo-bridge")
    if not executable:
        die(
            "mozyo-bridge is not on PATH; install it (e.g. `pipx install "
            "mozyo-bridge`) before wiring launchd."
        )
    command = [executable, "otel", "serve"]
    if port is not None:
        command.extend(["--port", str(port)])
    return command


def render_plist(command: list[str], *, home: Path | None = None) -> bytes:
    """Render the LaunchAgent plist.

    Intentionally minimal: no EnvironmentVariables key exists in the
    output at all, so no secret can be serialized into it by any code
    path. ``KeepAlive`` restarts the receiver on crash; ``RunAtLoad``
    starts it at login.
    """
    payload = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": command,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_path(home)),
        "StandardErrorPath": str(log_path(home)),
        "ProcessType": "Background",
    }
    return plistlib.dumps(payload)


def _run_launchctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


def install(*, port: int | None = None, home: Path | None = None) -> dict:
    """Write the plist and (re)bootstrap the agent. Idempotent."""
    command = resolve_serve_command(port)
    target = plist_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    log_path(home).parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(render_plist(command, home=home))
    # A previously loaded agent must be booted out before bootstrap or
    # launchd rejects the duplicate label; not-loaded is fine to ignore.
    _run_launchctl(["bootout", f"{_gui_domain()}/{LAUNCHD_LABEL}"])
    result = _run_launchctl(["bootstrap", _gui_domain(), str(target)])
    if result.returncode != 0:
        die(
            f"launchctl bootstrap failed: "
            f"{result.stderr.strip() or result.stdout.strip() or 'unknown'}. "
            f"The plist was written to {target}; fix the cause and re-run "
            "`mozyo-bridge otel launchd install`."
        )
    return {"action": "install", "plist": str(target), "command": command}


def uninstall(*, home: Path | None = None) -> dict:
    """Boot the agent out and remove exactly our plist."""
    _run_launchctl(["bootout", f"{_gui_domain()}/{LAUNCHD_LABEL}"])
    target = plist_path(home)
    existed = target.exists()
    if existed:
        target.unlink()
    return {"action": "uninstall", "plist": str(target), "removed": existed}


def restart() -> dict:
    """Kickstart (kill + relaunch) the loaded agent — the upgrade step."""
    result = _run_launchctl(
        ["kickstart", "-k", f"{_gui_domain()}/{LAUNCHD_LABEL}"]
    )
    if result.returncode != 0:
        die(
            f"launchctl kickstart failed: "
            f"{result.stderr.strip() or result.stdout.strip() or 'unknown'}. "
            "Is the agent installed? Run `mozyo-bridge otel launchd "
            "install` first."
        )
    return {"action": "restart", "label": LAUNCHD_LABEL}


def status(*, home: Path | None = None) -> dict:
    """Launchd-side state only — additive to `otel status` (receiver
    health / store), which stays the health source; this answers "is the
    agent wired and running under launchd"."""
    target = plist_path(home)
    result = _run_launchctl(["print", f"{_gui_domain()}/{LAUNCHD_LABEL}"])
    loaded = result.returncode == 0
    pid: str | None = None
    if loaded:
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("pid = "):
                pid = stripped.split("=", 1)[1].strip()
                break
    return {
        "label": LAUNCHD_LABEL,
        "plist": str(target),
        "plist_exists": target.exists(),
        "loaded": loaded,
        "pid": pid,
        "log": str(log_path(home)),
    }
