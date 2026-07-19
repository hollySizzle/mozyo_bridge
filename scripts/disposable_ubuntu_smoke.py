#!/usr/bin/env python3
"""Disposable-Ubuntu black-box smoke for a built mozyo-bridge wheel.

This helper runs the user-facing CLI harness of a *candidate* wheel inside a
throwaway, pinned Ubuntu container, as a NON-root user, in a fresh HOME, with
NO source checkout mounted and NO editable/local-source install. It is the
container-level counterpart of the in-workflow venv fresh-install smoke: where
that smoke proves the artifact imports and exposes the console scripts on the
build runner, this smoke proves the SAME wheel bytes install and drive the real
onboarding surface (rules / scaffold / docs / doctor) on a clean foreign OS the
maintainer does not control (Redmine #14100, parent #14098).

Difference from the existing venv fresh-install smoke
(``.github/workflows/*.yml`` "Fresh-install smoke"):
  - OS boundary: a pinned Ubuntu LTS image, not the GitHub build runner.
  - Identity: a non-root user with a fresh HOME, not the runner's user/home.
  - Source absence: only an artifact-only directory is mounted (read-only);
    the repo checkout is never visible inside the container.
  - Surface breadth: not just ``--version`` / ``--help`` but the actual user
    harness — ``rules install/status``, ``scaffold apply/status`` to a fresh
    target, ``docs validate/resolve``, and the read-only ``doctor runtime``
    fingerprint.

Blocking authority is the image DIGEST. In the default ``blocking`` mode the
``--image`` argument MUST be digest-pinned (``ubuntu@sha256:...``); a floating
tag is refused fail-closed so a release gate can never silently smoke a moving
image. The optional ``canary`` mode exists ONLY for a separate, advisory
floating-LTS job and never runs as a blocking release gate (see
``vibes/docs/logics/tiered-ci-gate-policy.md`` and ``release-distribution.md``).

The script is intentionally dependency-free (standard library only) so it runs
on a fresh CI runner before the package is installed. It shells out to the
Docker CLI; the container-internal program is fed on stdin so nothing from the
repo tree is bind-mounted. No secrets, tokens, or host paths are written into
the image, the container environment, or the emitted summary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

# ``ubuntu@sha256:<64-hex>``: the blocking gate accepts only a digest-pinned
# reference. ``ubuntu:24.04`` and friends are floating tags and are refused in
# blocking mode.
_DIGEST_PATTERN = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")

# Marker lines emitted by the container-internal program. The orchestrator
# reads them back from stdout so a passing container is proven by the observed
# surface list + fact block, not merely by the process exit code.
_STEP_MARKER = "MOZYO_SMOKE_STEP:"
_FACTS_MARKER = "MOZYO_SMOKE_FACTS="

# Ordered black-box surfaces the container must exercise. Keep in lockstep with
# ``_container_program`` below and with the doc contract.
EXPECTED_STEPS = (
    "version_mozyo_bridge",
    "version_mozyo",
    "install_provenance",
    "rules_install",
    "rules_status",
    "scaffold_apply",
    "scaffold_status",
    "docs_validate",
    "docs_resolve",
    "doctor_runtime",
)


class SmokeError(RuntimeError):
    """Raised on any fail-closed condition (bad input, provenance mismatch)."""


def resolve_wheel(artifact_dir: Path, expected_version: str) -> Path:
    """Return the single wheel in ``artifact_dir`` for ``expected_version``.

    The artifact directory is the *only* install source, so it must contain
    exactly one wheel whose version tag equals ``expected_version``. Zero or
    multiple matches are fail-closed: a release gate must never guess which
    byte-set it is certifying.
    """
    if not artifact_dir.is_dir():
        raise SmokeError(f"artifact dir is not a directory: {artifact_dir}")
    # ``mozyo_bridge-<version>-<pytag>-<abi>-<plat>.whl``; match the exact
    # version segment so a neighbouring build cannot be smoked by accident.
    prefix = f"mozyo_bridge-{expected_version}-"
    matches = sorted(
        p for p in artifact_dir.glob("mozyo_bridge-*.whl") if p.name.startswith(prefix)
    )
    if not matches:
        available = ", ".join(p.name for p in sorted(artifact_dir.glob("*.whl"))) or "none"
        raise SmokeError(
            f"no wheel for version {expected_version!r} in {artifact_dir} "
            f"(available: {available})"
        )
    if len(matches) > 1:
        names = ", ".join(p.name for p in matches)
        raise SmokeError(
            f"multiple wheels for version {expected_version!r} in {artifact_dir}: {names}"
        )
    return matches[0]


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 of ``path`` (the exact artifact bytes smoked)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_image(image: str, mode: str) -> None:
    """Fail closed unless ``image`` is admissible for ``mode``.

    ``blocking`` requires a digest-pinned reference so the gate authority is the
    immutable digest, never a floating tag. ``canary`` accepts a floating tag
    because it is advisory-only and is never wired as a blocking gate.
    """
    if not image or image.isspace():
        raise SmokeError("image reference is empty")
    if mode == "blocking" and not _DIGEST_PATTERN.match(image):
        raise SmokeError(
            f"blocking mode requires a digest-pinned image (name@sha256:<64-hex>), got {image!r}; "
            "a floating tag cannot be the blocking authority"
        )


def build_docker_command(
    docker_bin: str,
    image: str,
    artifact_dir: Path,
    wheel_name: str,
    expected_version: str,
    preset: str,
) -> list[str]:
    """Return the ``docker run`` argv (pure; no side effects).

    Only ``artifact_dir`` is mounted, read-only, at ``/artifacts``. The repo
    tree is never mounted. ``-i`` lets the container program arrive on stdin so
    no script file is bind-mounted either. The container starts as root ONLY to
    provision the OS (apt); the harness itself drops to a non-root user.
    """
    return [
        docker_bin,
        "run",
        "--rm",
        "-i",
        "--network",
        "bridge",
        "-v",
        f"{artifact_dir.resolve()}:/artifacts:ro",
        "-e",
        f"EXPECTED_VERSION={expected_version}",
        "-e",
        f"PRESET={preset}",
        "-e",
        f"WHEEL={wheel_name}",
        image,
        "bash",
        "-euo",
        "pipefail",
        "-s",
    ]


def _container_program() -> str:
    """Return the bash program executed inside the container (fed on stdin).

    Root phase: provision python + venv support and create a non-root user.
    Non-root phase (`smoke`, fresh HOME): install the artifact-only wheel, prove
    install provenance, and drive the user harness. Every surface prints a
    ``MOZYO_SMOKE_STEP:<name>:ok`` line; a final ``MOZYO_SMOKE_FACTS=<json>``
    line carries the machine-readable facts. ``set -e`` makes any surface
    failure abort the container with a non-zero exit.
    """
    # The inner (non-root) program uses a single-quoted heredoc so the root
    # shell does not expand its ``$HOME`` / ``$(...)``. Only the three trusted
    # values (EXPECTED_VERSION / PRESET / WHEEL, already validated host-side)
    # are injected via ``printf %q`` header lines.
    return r"""
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq python3 python3-venv >/dev/null
# Auto-assign the UID (no fixed 1000): recent ubuntu images ship a default
# uid-1000 `ubuntu` user, so a fixed UID collides. We only require non-root,
# which the harness asserts from the observed `id -u` anyway.
id smoke >/dev/null 2>&1 || useradd -m -s /bin/bash smoke

{
  printf 'EXPECTED_VERSION=%q\n' "$EXPECTED_VERSION"
  printf 'PRESET=%q\n' "$PRESET"
  printf 'WHEEL=%q\n' "$WHEEL"
  cat <<'INNER'
set -euo pipefail
export HOME=/home/smoke
export MOZYO_BRIDGE_HOME="$HOME/mozyo_home"
cd "$HOME"

step() { printf 'MOZYO_SMOKE_STEP:%s:ok\n' "$1"; }

WHEEL_PATH="/artifacts/$WHEEL"
[ -f "$WHEEL_PATH" ] || { echo "FAIL: wheel not found at $WHEEL_PATH" >&2; exit 3; }

python3 -m venv "$HOME/venv"
# shellcheck disable=SC1091
. "$HOME/venv/bin/activate"
python -m pip install --quiet --upgrade pip >/dev/null
# Artifact-only: install the EXACT wheel file. Dependencies resolve from PyPI,
# but mozyo-bridge itself can only come from the mounted artifact bytes.
python -m pip install --quiet "$WHEEL_PATH" >/dev/null

RUNTIME_UID="$(id -u)"
RUNTIME_USER="$(id -un)"

OBS_VERSION="$(mozyo-bridge --version | awk '{print $NF}')"
step version_mozyo_bridge
OBS_VERSION_MOZYO="$(mozyo --version | awk '{print $NF}')"
step version_mozyo

PKG_PATH="$(python -c 'import mozyo_bridge, os; print(os.path.dirname(os.path.realpath(mozyo_bridge.__file__)))')"
# Provenance: the package must live under this user's venv (installed from the
# artifact), never under a mounted source tree, and the reported version must
# equal the expected candidate version on BOTH console scripts.
case "$PKG_PATH" in
  "$HOME/venv/"*) : ;;
  *) echo "FAIL: package path not under venv: $PKG_PATH" >&2; exit 3 ;;
esac
[ "$OBS_VERSION" = "$EXPECTED_VERSION" ] || { echo "FAIL: mozyo-bridge --version $OBS_VERSION != $EXPECTED_VERSION" >&2; exit 3; }
[ "$OBS_VERSION_MOZYO" = "$EXPECTED_VERSION" ] || { echo "FAIL: mozyo --version $OBS_VERSION_MOZYO != $EXPECTED_VERSION" >&2; exit 3; }
[ "$RUNTIME_UID" != "0" ] || { echo "FAIL: harness ran as root" >&2; exit 3; }
step install_provenance

TARGET="$HOME/fresh_project"
mkdir -p "$TARGET"

mozyo-bridge rules install --repo-local "$TARGET" >/dev/null
step rules_install
mozyo-bridge rules status --repo-local "$TARGET" >/dev/null
step rules_status
mozyo-bridge scaffold apply "$PRESET" --target "$TARGET" --repo-local --force >/dev/null
step scaffold_apply
mozyo-bridge scaffold status --target "$TARGET" >/dev/null
step scaffold_status

# Minimal fresh-project fixture: promote the scaffold's own catalog example to
# an active catalog so docs validate/resolve have a catalog to read. This is
# derived entirely from the installed package's scaffold output; nothing is
# mounted from the maintainer repo.
cp "$TARGET/.mozyo-bridge/docs/catalog.yaml.example" "$TARGET/.mozyo-bridge/docs/catalog.yaml"
mozyo-bridge docs validate --repo "$TARGET" >/dev/null
step docs_validate
mozyo-bridge docs resolve --repo "$TARGET" AGENTS.md >/dev/null
step docs_resolve

# Read-only runtime fingerprint (no network, no install). Confirms the active
# surface is an installed venv, not a source checkout, from inside the box.
DOCTOR_JSON="$(mozyo-bridge doctor runtime --json)"
python3 - "$DOCTOR_JSON" <<'PYCHECK'
import json, sys
data = json.loads(sys.argv[1])
active = data.get("active", {})
surface = active.get("surface")
if surface == "source":
    print(f"FAIL: doctor runtime reports source surface: {surface}", file=sys.stderr)
    raise SystemExit(3)
if active.get("version") is None:
    print("FAIL: doctor runtime reports no version", file=sys.stderr)
    raise SystemExit(3)
PYCHECK
step doctor_runtime

python3 - "$RUNTIME_USER" "$RUNTIME_UID" "$HOME" "$OBS_VERSION" "$OBS_VERSION_MOZYO" "$PKG_PATH" "$PRESET" <<'PYFACTS'
import json, sys
(user, uid, home, ver, ver_mozyo, pkg, preset) = sys.argv[1:8]
facts = {
    "harness_user": user,
    "harness_uid": int(uid),
    "harness_home": home,
    "observed_version_mozyo_bridge": ver,
    "observed_version_mozyo": ver_mozyo,
    "package_path": pkg,
    "preset": preset,
    "source_mount_present": False,
}
print("MOZYO_SMOKE_FACTS=" + json.dumps(facts, sort_keys=True))
PYFACTS
INNER
} > /tmp/inner.sh
chmod +x /tmp/inner.sh
chown smoke /tmp/inner.sh
runuser -u smoke -- bash /tmp/inner.sh
"""


def parse_container_output(text: str) -> dict:
    """Parse the container stdout into ``{"steps": [...], "facts": {...}}``.

    ``facts`` is ``None`` when the fact line is absent (an incomplete run).
    Unknown / duplicate lines are ignored; only the marked lines are read.
    """
    steps: list[str] = []
    facts: dict | None = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(_STEP_MARKER):
            body = line[len(_STEP_MARKER):]
            name, _, status = body.partition(":")
            if status == "ok" and name:
                steps.append(name)
        elif line.startswith(_FACTS_MARKER):
            payload = line[len(_FACTS_MARKER):]
            try:
                facts = json.loads(payload)
            except (ValueError, TypeError):
                facts = None
    return {"steps": steps, "facts": facts}


def build_summary(
    *,
    mode: str,
    image: str,
    wheel_name: str,
    wheel_sha256: str,
    expected_version: str,
    preset: str,
    parsed: dict,
    resolved_digest: str | None,
    duration_seconds: float,
    container_exit: int,
) -> dict:
    """Assemble the machine-readable, secret-safe smoke summary."""
    facts = parsed.get("facts")
    steps = parsed.get("steps", [])
    missing = [name for name in EXPECTED_STEPS if name not in steps]
    provenance_ok = bool(
        facts
        and facts.get("observed_version_mozyo_bridge") == expected_version
        and facts.get("observed_version_mozyo") == expected_version
        and isinstance(facts.get("harness_uid"), int)
        and facts.get("harness_uid") != 0
        and facts.get("source_mount_present") is False
    )
    ok = container_exit == 0 and not missing and provenance_ok
    return {
        "ok": ok,
        "mode": mode,
        "image_ref": image,
        "image_digest": resolved_digest,
        "artifact": {
            "wheel": wheel_name,
            "sha256": wheel_sha256,
            "expected_version": expected_version,
            "source": "artifact-only mount (/artifacts:ro)",
        },
        "runtime": {
            "os": "ubuntu-container",
            "non_root": bool(facts and facts.get("harness_uid") not in (None, 0)),
            "harness_uid": facts.get("harness_uid") if facts else None,
            "harness_user": facts.get("harness_user") if facts else None,
            "fresh_home": facts.get("harness_home") if facts else None,
            "source_checkout_mounted": facts.get("source_mount_present") if facts else None,
            "package_path": facts.get("package_path") if facts else None,
        },
        "preset": preset,
        "surfaces": {
            "expected": list(EXPECTED_STEPS),
            "observed": steps,
            "missing": missing,
        },
        "provenance_ok": provenance_ok,
        "container_exit": container_exit,
        "duration_seconds": round(duration_seconds, 3),
    }


def _resolve_image_digest(docker_bin: str, image: str) -> str | None:
    """Best-effort: return the image's repo digest after the run (or None)."""
    try:
        out = subprocess.run(
            [docker_bin, "image", "inspect", "--format", "{{index .RepoDigests 0}}", image],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    value = out.stdout.strip()
    return value or None


def run_smoke(args: argparse.Namespace) -> dict:
    """Validate inputs, run the container, and return the summary dict."""
    artifact_dir = Path(args.artifact_dir)
    validate_image(args.image, args.mode)
    wheel = resolve_wheel(artifact_dir, args.expected_version)
    wheel_sha = sha256_file(wheel)

    command = build_docker_command(
        docker_bin=args.docker,
        image=args.image,
        artifact_dir=artifact_dir,
        wheel_name=wheel.name,
        expected_version=args.expected_version,
        preset=args.preset,
    )
    program = _container_program()

    # ``perf_counter`` is monotonic and side-effect-free (no wall-clock / RNG).
    from time import perf_counter

    start = perf_counter()
    completed = subprocess.run(
        command,
        input=program,
        capture_output=True,
        text=True,
        check=False,
    )
    duration = perf_counter() - start

    # Surface container output to the operator log (stderr) verbatim; it never
    # contains secrets (no credentials are injected into the container).
    if completed.stdout:
        sys.stderr.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)

    parsed = parse_container_output(completed.stdout)
    resolved_digest = _resolve_image_digest(args.docker, args.image)
    return build_summary(
        mode=args.mode,
        image=args.image,
        wheel_name=wheel.name,
        wheel_sha256=wheel_sha,
        expected_version=args.expected_version,
        preset=args.preset,
        parsed=parsed,
        resolved_digest=resolved_digest,
        duration_seconds=duration,
        container_exit=completed.returncode,
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the mozyo-bridge user harness for a built wheel inside a pinned, "
            "disposable, non-root Ubuntu container (Redmine #14100)."
        )
    )
    parser.add_argument(
        "--artifact-dir",
        required=True,
        help="Directory containing ONLY built distributions; mounted read-only at /artifacts.",
    )
    parser.add_argument(
        "--expected-version",
        required=True,
        help="Exact package version the wheel must carry and both console scripts must report.",
    )
    parser.add_argument(
        "--image",
        required=True,
        help=(
            "Ubuntu image reference. blocking mode requires a digest pin "
            "(ubuntu@sha256:<64-hex>); canary mode may use a floating tag."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("blocking", "canary"),
        default="blocking",
        help="blocking = digest-pinned release gate (default); canary = advisory floating LTS.",
    )
    parser.add_argument(
        "--preset",
        default="redmine-governed",
        help="Scaffold preset to apply to the fresh in-container target (default: redmine-governed).",
    )
    parser.add_argument(
        "--docker",
        default="docker",
        help="Docker CLI executable (default: docker).",
    )
    parser.add_argument(
        "--json-summary",
        help="Write the machine-readable summary JSON to this path (in addition to stdout).",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        summary = run_smoke(args)
    except SmokeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        # Docker CLI missing: fail closed. A release gate must not pass when the
        # container substrate is unavailable (Redmine #14100: never claim
        # success from a mock-only run).
        print(f"error: docker unavailable: {exc}", file=sys.stderr)
        return 2

    text = json.dumps(summary, indent=2, sort_keys=True)
    print(text)
    if args.json_summary:
        Path(args.json_summary).write_text(text + "\n", encoding="utf-8")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
