#!/usr/bin/env python3
"""Installed-artifact fault-path smoke (Redmine #14097 installed layer).

The hermetic ``tests/scenarios`` layer proves the fault *truth tables* over the source under
review through the public command dispatch. This smoke proves the missing half the review
required (j#83738 F1): that the SAME public fault-path entrypoints run from a **built + installed
artifact**, not the checkout — a wheel built from the exact review head, installed into an
isolated temp venv, driven as a real ``mozyo-bridge`` subprocess whose provenance is proven to be
the venv (never the editable checkout or a ``pipx`` global).

Two-layer split (coordinator decision j#83766 / ratification j#83808): this file lives under
``smoke/`` because it OWNS real network (the wheel build fetches the build backend) and real
install — resources the offline ``tests/scenarios`` contract forbids. Its PURE decision surface
(provenance verdict, shape argv, summary) is unit-tested hermetically in
``tests/scenarios/test_installed_fault_smoke.py`` with a stubbed subprocess; the real
build+venv+subprocess run is the CI gate wired into ``.github/workflows/test.yml`` after the
existing ``Build wheel and sdist`` / ``Fresh-install smoke`` steps, reusing the same exact wheel.

Isolation: every driven command runs under an isolated ``MOZYO_BRIDGE_HOME`` + a scratch
herdr-backend repo + a secret-free temp state file served by ``smoke/support/fake_herdr_cli.py``
(the canonical fake over the ``MOZYO_HERDR_BINARY`` boundary). Destructive-recovery approval is
fresh-read from a secret-free, loopback-only fake Redmine using the marker emitted by the installed
CLI's own preflight. No operator home, real Herdr, tmux, SQLite, real credential, external network
service, or managed lane is ever touched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: Each fault shape's installed public entrypoint (proves the built artifact dispatches it).
SHAPE_ENTRYPOINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("recover_stale", ("sublane", "recover-stale", "--help")),
    ("session_rollback", ("herdr", "session-rollback", "--help")),
    ("sublane_list", ("sublane", "list", "--help")),
    ("callback_lease", ("workflow", "callback-lease", "--help")),
    ("retire_migrate", ("sublane", "retire", "--help")),
)

#: The fault-shape CRITICAL paths the installed layer must drive as a real subprocess and assert
#: (not merely dispatch ``--help``). The summary fails closed if any is missing (review j#84441
#: F1): F2 recover-stale close/resume, F3 session-rollback replay, F4 callback exactly-once are
#: the accepted-finding critical paths, alongside the callback-lease + stale-projection paths.
REQUIRED_REPRESENTATIVE: tuple[str, ...] = (
    "callback_lease", "sublane_list", "recover_stale", "recover_stale_negative",
    "session_rollback", "callback_exactly_once",
)


class SmokeError(RuntimeError):
    """A fatal smoke precondition / assertion failure (fail-closed, never a silent skip)."""


# --------------------------------------------------------------------------- pure surface


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(Path(path).read_bytes())
    return digest.hexdigest()


# Closed vocabulary of provenance rejection reasons (Redmine #14247). Each code names exactly one
# classification branch. The human-readable message ECHOES the offending path, so asserting on a
# substring of the message is vacuous: a `pipx` global path is also outside the venv, so the
# executable-outside-venv message alone contains "pipx" and satisfies `any("pipx" in p)` even when
# the pipx branch is deleted. Negative controls therefore assert CODES, never message substrings.
PROVENANCE_EXECUTABLE_OUTSIDE_VENV = "executable_outside_venv"
PROVENANCE_MODULE_UNRESOLVED = "module_unresolved"
PROVENANCE_MODULE_OUTSIDE_VENV = "module_outside_venv"
PROVENANCE_MODULE_FROM_CHECKOUT = "module_from_checkout"
PROVENANCE_MODULE_NOT_SITE_PACKAGES = "module_not_under_site_packages"
PROVENANCE_PIPX_GLOBAL = "pipx_global"
PROVENANCE_VERSION_EMPTY = "version_empty"

PROVENANCE_REASON_CODES: tuple[str, ...] = (
    PROVENANCE_EXECUTABLE_OUTSIDE_VENV,
    PROVENANCE_MODULE_UNRESOLVED,
    PROVENANCE_MODULE_OUTSIDE_VENV,
    PROVENANCE_MODULE_FROM_CHECKOUT,
    PROVENANCE_MODULE_NOT_SITE_PACKAGES,
    PROVENANCE_PIPX_GLOBAL,
    PROVENANCE_VERSION_EMPTY,
)


def provenance_findings(
    *, executable: str, module_file: str, version: str, venv_dir: str, checkout_root: str
) -> list[tuple[str, str]]:
    """Return ``(reason_code, message)`` per failed provenance check. PURE. Single source of truth.

    ``verify_provenance`` (messages) and ``provenance_reason_codes`` (typed codes) BOTH derive from
    this one list, so a code can never drift away from the branch that produces its message.
    """
    findings: list[tuple[str, str]] = []
    venv = str(Path(venv_dir).resolve())
    checkout = str(Path(checkout_root).resolve())
    exe = str(Path(executable).resolve()) if executable else ""
    mod = str(Path(module_file).resolve()) if module_file else ""
    if not exe.startswith(venv + os.sep):
        findings.append((
            PROVENANCE_EXECUTABLE_OUTSIDE_VENV,
            f"executable {exe!r} is not inside the venv {venv!r}",
        ))
    if not mod:
        findings.append((
            PROVENANCE_MODULE_UNRESOLVED, "mozyo_bridge module file could not be resolved"
        ))
    else:
        if not mod.startswith(venv + os.sep):
            findings.append((
                PROVENANCE_MODULE_OUTSIDE_VENV, f"module {mod!r} is not inside the venv {venv!r}"
            ))
        if mod.startswith(checkout + os.sep):
            findings.append((
                PROVENANCE_MODULE_FROM_CHECKOUT,
                f"module {mod!r} loaded from the checkout, not the installed artifact",
            ))
        if "site-packages" not in mod:
            findings.append((
                PROVENANCE_MODULE_NOT_SITE_PACKAGES, f"module {mod!r} is not under site-packages"
            ))
    if "pipx" in exe or "pipx" in mod:
        findings.append((
            PROVENANCE_PIPX_GLOBAL, "resolved to a pipx global, not the isolated venv"
        ))
    if not version.strip():
        findings.append((PROVENANCE_VERSION_EMPTY, "mozyo-bridge --version produced no version"))
    return findings


def provenance_reason_codes(**facts: str) -> list[str]:
    """Return only the typed reason codes. Negative controls assert against THIS, not messages."""
    return [code for code, _ in provenance_findings(**facts)]


def verify_provenance(
    *, executable: str, module_file: str, version: str, venv_dir: str, checkout_root: str
) -> list[str]:
    """Return the provenance problems (empty == proven installed). PURE.

    The exercised CLI must resolve to the venv, and its ``mozyo_bridge`` module must load from the
    venv's ``site-packages`` — never the editable checkout tree or a ``pipx`` global. This is what
    distinguishes an installed-artifact run from a source-dispatch run.

    Output is unchanged by Redmine #14247: the summary keeps publishing these same messages.
    """
    return [message for _, message in provenance_findings(
        executable=executable, module_file=module_file, version=version,
        venv_dir=venv_dir, checkout_root=checkout_root,
    )]


def recover_stale_accepts(outcome: "dict | None") -> bool:
    """The SINGLE F2 acceptance predicate: a completed post-close-resume terminal, one confirmed
    redispatch, no additional close (Redmine #14097 review j#85090 / j#85253). PURE.

    Shared VERBATIM by the installed positive drive, the installed negative CONTROL (which asserts
    THIS predicate returns False on an injected-uncertain outcome), and the hermetic scenario. One
    predicate — not two copies — is the point: weakening any conjunct (say, dropping the
    ``confirmed`` check, the very post_close_resume-only regression j#85090 flagged) makes the
    negative control's ``not recover_stale_accepts(uncertain)`` flip green->red instead of being
    silently tolerated by a laxer second copy. An absent / malformed outcome is not accepted.

    ``outcome`` keys (built identically by both layers):
    ``pass1`` / ``pass2`` (the two recover-stale payloads), ``fresh_locator`` / ``old_locator``,
    ``agents_unchanged`` (bool: the inventory row set is identical across pass 2 — the additional-
    close-0 observable), ``redispatch_attempt_count`` (ALL exact-marker/target delivery_outcome
    rows) and ``redispatch_ok_count`` (the ``reason=ok`` subset).
    """
    if not isinstance(outcome, dict):
        return False
    p1 = outcome.get("pass1") or {}
    p2 = outcome.get("pass2") or {}
    return bool(
        p1.get("closed_old_worker") and p1.get("status") == "stopped"
        and p1.get("recovery_status") == "in_progress"
        and outcome.get("fresh_locator")
        and outcome.get("fresh_locator") != outcome.get("old_locator")
        and p2.get("status") == "completed" and p2.get("recovery_status") == "recovered"
        and p2.get("redispatch_status") == "confirmed" and p2.get("fresh_slot_attested")
        and p2.get("post_close_resume")
        # DURABLE close-committed reflection (phase past close_owed), true on a completed resume;
        # "no additional close" is the inventory observable below, not this flag (review j#85253
        # 判定済み: closed_old_worker == true is correct, not a per-pass close count).
        and p2.get("closed_old_worker")
        and outcome.get("agents_unchanged") is True  # additional close 0 (a close deletes a row)
        and outcome.get("redispatch_attempt_count") == 1  # exactly one dispatch attempt...
        and outcome.get("redispatch_ok_count") == 1       # ...and it confirmed (reason=ok)
    )


def session_rollback_accepts(outcome: "dict | None") -> bool:
    """Accept only the installed zero-close terminal current Herdr can safely provide.

    Herdr protocol 19 has no server-side conditional close for one observed terminal generation.
    The representative is therefore green only when preflight, execute, and replay all preserve
    the exact participant and debt; the execute must return nonzero with the typed
    ``conditional_close_unavailable`` reason. This is deliberately stricter than treating any
    blocked rollback as a successful smoke.
    """
    if not isinstance(outcome, dict):
        return False
    preflight = outcome.get("preflight")
    execute = outcome.get("execute")
    replay = outcome.get("replay")
    if not all(isinstance(item, dict) for item in (preflight, execute, replay)):
        return False

    def participant(payload: dict) -> "dict | None":
        rows = payload.get("participants")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            return None
        return rows[0]

    preflight_participant = participant(preflight)
    execute_participant = participant(execute)
    replay_participant = participant(replay)
    if not all(
        isinstance(item, dict)
        for item in (
            preflight_participant,
            execute_participant,
            replay_participant,
        )
    ):
        return False
    participants = (
        preflight_participant,
        execute_participant,
        replay_participant,
    )
    assert all(item is not None for item in participants)
    def nonblank_text(value: object) -> bool:
        return type(value) is str and bool(value.strip())

    action_id = preflight.get("action_id")
    identity = (
        preflight_participant.get("role"),
        preflight_participant.get("assigned_name"),
        preflight_participant.get("locator"),
    )
    return bool(
        type(outcome.get("preflight_exit")) is int
        and outcome.get("preflight_exit") == 0
        and type(outcome.get("execute_exit")) is int
        and outcome.get("execute_exit") == 1
        and type(outcome.get("replay_exit")) is int
        and outcome.get("replay_exit") == 0
        and nonblank_text(action_id)
        and nonblank_text(execute.get("action_id"))
        and nonblank_text(replay.get("action_id"))
        and execute.get("action_id") == action_id
        and replay.get("action_id") == action_id
        and preflight.get("state") == "blocked"
        and preflight.get("reason") == "preflight_only"
        and preflight.get("executed") is False
        and execute.get("state") == "blocked"
        and execute.get("reason") == "conditional_close_unavailable"
        and execute.get("executed") is False
        and replay.get("state") == "blocked"
        and replay.get("reason") == "preflight_only"
        and replay.get("executed") is False
        and identity[0] == "claude"
        and all(nonblank_text(value) for value in identity[1:])
        and all(
            (
                row.get("role"),
                row.get("assigned_name"),
                row.get("locator"),
            )
            == identity
            for row in participants
        )
        and all(
            row.get("verdict") == "conditional_close_unavailable"
            and row.get("closed") is False
            for row in participants
        )
        and outcome.get("agents_unchanged") is True
        and type(outcome.get("live_agent_count")) is int
        and outcome.get("live_agent_count") == 1
    )


def build_summary(
    *, provenance_problems: list[str], wheel_name: str, wheel_sha256: str,
    entrypoints: dict[str, int], representative: dict[str, bool],
    representative_diagnostics: "dict | None" = None,
) -> dict:
    """The final smoke verdict (secret-free, JSON-safe). PURE.

    Fail-closed on a MISSING required critical path (review j#84441 F1): the summary must not read
    ``ok`` while a shape's installed critical path was never driven — an absent key is a failure,
    not a pass.
    """
    missing = [k for k in REQUIRED_REPRESENTATIVE if k not in representative]
    entrypoints_ok = bool(entrypoints) and all(code == 0 for code in entrypoints.values())
    representative_ok = not missing and all(representative.values())
    ok = not provenance_problems and entrypoints_ok and representative_ok
    return {
        "ok": ok,
        "provenance_ok": not provenance_problems,
        "provenance_problems": list(provenance_problems),
        "artifact": {"wheel": wheel_name, "sha256": wheel_sha256},
        "entrypoints": dict(entrypoints),
        "entrypoints_ok": entrypoints_ok,
        "representative": dict(representative),
        "representative_ok": representative_ok,
        "representative_missing": missing,
        # Redmine #14248: CONTENT-FREE per-path failure detail (counts / exit codes / closed
        # vocabulary tokens only — no body, path, raw ANSI or credential). Emitted ONLY for paths
        # that failed, so a green run's output is unchanged and a red CI log is diagnosable.
        "representative_diagnostics": {
            name: detail
            for name, detail in (representative_diagnostics or {}).items()
            if representative.get(name) is not True
        },
    }


# --------------------------------------------------------------------------- shell-out surface


def build_wheel(src_root: Path, out_dir: Path, *, runner=subprocess.run) -> Path:
    """Build the exact-head wheel via pip's isolated build (network for the build backend)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    result = runner(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(out_dir), str(src_root)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SmokeError(f"wheel build failed (build deps unavailable?): {result.stderr[-800:]}")
    wheels = sorted(out_dir.glob("mozyo_bridge-*.whl"))
    if len(wheels) != 1:
        raise SmokeError(f"expected exactly one built wheel, found {[w.name for w in wheels]}")
    return wheels[0]


def make_venv_install(wheel: Path, venv_dir: Path, *, runner=subprocess.run) -> Path:
    """Create an isolated venv and install the wheel (network for runtime deps). Returns the CLI."""
    runner([sys.executable, "-m", "venv", str(venv_dir)], check=True,
           capture_output=True, text=True)
    venv_python = venv_dir / "bin" / "python"
    runner([str(venv_python), "-m", "pip", "install", "--quiet", str(wheel)],
           check=True, capture_output=True, text=True)
    cli = venv_dir / "bin" / "mozyo-bridge"
    if not cli.exists():
        raise SmokeError(f"installed wheel exposes no mozyo-bridge entrypoint at {cli}")
    return cli


def installed_facts(venv_python: Path, *, runner=subprocess.run) -> dict:
    """Read the installed artifact's provenance facts from the venv (executable / module / version)."""
    version = runner([str(venv_python.parent / "mozyo-bridge"), "--version"],
                     capture_output=True, text=True).stdout.strip()
    module_file = runner(
        [str(venv_python), "-c", "import mozyo_bridge,sys;sys.stdout.write(mozyo_bridge.__file__)"],
        capture_output=True, text=True,
    ).stdout.strip()
    return {
        "executable": str(venv_python.parent / "mozyo-bridge"),
        "module_file": module_file, "version": version,
    }


def run_smoke(args: argparse.Namespace) -> dict:
    """Build -> install -> prove provenance -> drive each shape entrypoint + representative paths."""
    from installed_fault_smoke_driver import (  # local import: shell-heavy driver
        drive_entrypoints, drive_representative,
    )

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        wheel = (
            Path(args.wheel) if getattr(args, "wheel", None)
            else build_wheel(_REPO_ROOT, tmp / "wheelhouse")
        )
        cli = make_venv_install(wheel, tmp / "venv")
        facts = installed_facts(tmp / "venv" / "bin" / "python")
        problems = verify_provenance(
            executable=facts["executable"], module_file=facts["module_file"],
            version=facts["version"], venv_dir=str(tmp / "venv"), checkout_root=str(_REPO_ROOT),
        )
        entrypoints = drive_entrypoints(cli, tmp)
        representative_diagnostics: dict = {}
        representative = drive_representative(cli, tmp, representative_diagnostics)
        return build_summary(
            provenance_problems=problems, wheel_name=wheel.name, wheel_sha256=sha256_file(wheel),
            entrypoints=entrypoints, representative=representative,
            representative_diagnostics=representative_diagnostics,
        )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="installed_fault_smoke")
    parser.add_argument("--wheel", help="a pre-built wheel to install (default: build from head)")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        summary = run_smoke(args)
    except SmokeError as exc:
        sys.stderr.write(f"installed fault smoke: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
