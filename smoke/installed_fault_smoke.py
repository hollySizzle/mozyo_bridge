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
(the canonical fake over the ``MOZYO_HERDR_BINARY`` boundary). No operator home, real Herdr,
tmux, SQLite, credential, network service, or managed lane is ever touched.
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


def verify_provenance(
    *, executable: str, module_file: str, version: str, venv_dir: str, checkout_root: str
) -> list[str]:
    """Return the provenance problems (empty == proven installed). PURE.

    The exercised CLI must resolve to the venv, and its ``mozyo_bridge`` module must load from the
    venv's ``site-packages`` — never the editable checkout tree or a ``pipx`` global. This is what
    distinguishes an installed-artifact run from a source-dispatch run.
    """
    problems: list[str] = []
    venv = str(Path(venv_dir).resolve())
    checkout = str(Path(checkout_root).resolve())
    exe = str(Path(executable).resolve()) if executable else ""
    mod = str(Path(module_file).resolve()) if module_file else ""
    if not exe.startswith(venv + os.sep):
        problems.append(f"executable {exe!r} is not inside the venv {venv!r}")
    if not mod:
        problems.append("mozyo_bridge module file could not be resolved")
    else:
        if not mod.startswith(venv + os.sep):
            problems.append(f"module {mod!r} is not inside the venv {venv!r}")
        if mod.startswith(checkout + os.sep):
            problems.append(f"module {mod!r} loaded from the checkout, not the installed artifact")
        if "site-packages" not in mod:
            problems.append(f"module {mod!r} is not under site-packages")
    if "pipx" in exe or "pipx" in mod:
        problems.append("resolved to a pipx global, not the isolated venv")
    if not version.strip():
        problems.append("mozyo-bridge --version produced no version")
    return problems


def build_summary(
    *, provenance_problems: list[str], wheel_name: str, wheel_sha256: str,
    entrypoints: dict[str, int], representative: dict[str, bool],
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
        representative = drive_representative(cli, tmp)
        return build_summary(
            provenance_problems=problems, wheel_name=wheel.name, wheel_sha256=sha256_file(wheel),
            entrypoints=entrypoints, representative=representative,
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
