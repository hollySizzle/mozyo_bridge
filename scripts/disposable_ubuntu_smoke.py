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
    "mount_isolation",
)

# Distribution-file suffixes allowed in an artifact-only directory. Anything
# else (a source tree, a .py file, a .git checkout) means /artifacts could leak
# source into the container, so it is refused host-side.
_DIST_SUFFIXES = (".whl", ".tar.gz")


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


def verify_artifact_dir_is_artifact_only(artifact_dir: Path) -> list[str]:
    """Fail closed unless ``artifact_dir`` holds ONLY distribution files.

    The directory is bind-mounted read-only at ``/artifacts`` and is the sole
    install source, so it must not carry a source checkout. A subdirectory
    (``src/``, ``.git/``), a ``pyproject.toml``, or any non-distribution file
    would let the container see source rather than only the built artifact;
    every such entry is refused. Returns the sorted distribution filenames.
    This is the host-side half of the source-absence assertion (the container
    observes ``/proc/self/mountinfo`` as the other half) — neither relies on a
    self-reported constant.
    """
    if not artifact_dir.is_dir():
        raise SmokeError(f"artifact dir is not a directory: {artifact_dir}")
    dists: list[str] = []
    offenders: list[str] = []
    for entry in sorted(artifact_dir.iterdir()):
        if entry.is_dir():
            offenders.append(entry.name + "/")
        elif entry.name.endswith(_DIST_SUFFIXES):
            dists.append(entry.name)
        else:
            offenders.append(entry.name)
    if offenders:
        raise SmokeError(
            f"artifact dir {artifact_dir} is not artifact-only; non-distribution "
            f"entries present (source could leak into /artifacts): {', '.join(offenders)}"
        )
    if not dists:
        raise SmokeError(f"artifact dir {artifact_dir} contains no distribution files")
    return dists


# Every docker flag that can introduce a mount / filesystem into the container.
# ``-v`` / ``--volume`` / ``--mount`` are normalized and compared to the one
# expected bind; ``--tmpfs`` / ``--volumes-from`` are never emitted by this tool,
# so their mere presence is a regression and fails closed. All of docker's
# equivalent spellings are covered so an equivalent syntax cannot smuggle a
# source tree past the check:
#   - separate:  ``-v X`` / ``--volume X`` / ``--mount X``
#   - inline `=`: ``-v=X`` / ``--volume=X`` / ``--mount=X``
#   - compact short form: ``-vX`` (a single glued token — docker accepts a short
#     option's value glued to the flag, e.g. ``-v/src:/dst:ro``)
# (Redmine #14100 reviews j#82888 and j#82912).
_MOUNT_VALUE_FLAGS = ("-v", "--volume", "--mount")
_OTHER_MOUNT_FLAGS = ("--tmpfs", "--volumes-from")


def _normalize_mount_spec(value: str) -> str:
    """Return a canonical ``src:dst[:ro]`` string for one mount value.

    Accepts both the ``-v`` short form (``src:dst[:opts]``) and the ``--mount``
    key=value form (``type=bind,source=..,target=..,readonly``). A ``--mount``
    whose type is not ``bind`` normalizes to a ``type:<t>`` sentinel so it can
    never equal the expected bind spec (and is therefore rejected).
    """
    if "=" in value and ("," in value or value.split("=", 1)[0] in {
        "type", "source", "src", "destination", "dst", "target", "readonly", "ro"
    }):
        # --mount key=value[,key=value...] form.
        fields: dict[str, str] = {}
        flags: set[str] = set()
        for part in value.split(","):
            if "=" in part:
                key, _, val = part.partition("=")
                fields[key.strip()] = val.strip()
            else:
                flags.add(part.strip())
        mount_type = fields.get("type", "volume")
        if mount_type != "bind":
            return f"type:{mount_type}"
        src = fields.get("source", fields.get("src", ""))
        dst = fields.get("target", fields.get("destination", fields.get("dst", "")))
        read_only = (
            "readonly" in flags
            or "ro" in flags
            or fields.get("readonly", "").lower() in {"", "true", "1"}
            and "readonly" in fields
        )
        return f"{src}:{dst}:ro" if read_only else f"{src}:{dst}"
    # -v / --volume short form: already src:dst[:opts].
    return value


def verify_command_mounts_artifact_only(command: list[str], artifact_dir: Path) -> None:
    """Fail closed unless the docker argv mounts EXACTLY the artifact dir ro.

    Collects EVERY mount-introducing flag from the assembled argv — ``-v`` /
    ``--volume`` / ``--mount`` in the separate, ``=`` and compact ``-vX``
    spellings, plus ``--tmpfs`` / ``--volumes-from`` — normalizes them, and
    requires the single normalized mount ``<artifact_dir>:/artifacts:ro`` and
    nothing else. This makes "no source mount" a verified property of the actual
    command in every equivalent syntax, so a regression that bind-mounted a
    source tree via ``--mount`` (even under a ``/dev`` / ``/proc`` / ``/sys``
    target) or via the compact ``-v/src:/dst`` glued form trips here before the
    container starts (Redmine #14100 reviews j#82888, j#82912).
    """
    mounts: list[str] = []
    offenders: list[str] = []
    index = 0
    while index < len(command):
        token = command[index]
        if token in _MOUNT_VALUE_FLAGS:
            if index + 1 < len(command):
                mounts.append(_normalize_mount_spec(command[index + 1]))
            else:
                offenders.append(token)  # dangling mount flag, no value
            index += 2
            continue
        matched_value = False
        for flag in _MOUNT_VALUE_FLAGS:
            if token.startswith(flag + "="):
                mounts.append(_normalize_mount_spec(token[len(flag) + 1:]))
                matched_value = True
                break
        if matched_value:
            index += 1
            continue
        # Short-flag cluster containing ``v`` (the volume flag). Docker accepts a
        # short option's value glued to the flag (``-v/src:/dst``) and short
        # flags clustered together (``-itv/src:/dst`` == ``-i -t -v /src:/dst``).
        # ``v`` is the ONLY value-taking short flag whose letter is ``v``, so any
        # single-dash cluster containing ``v`` introduces a volume mount whose
        # value is either glued after the ``v`` or, if ``v`` is the last char,
        # the next token. ``-v`` (exact) and ``-v=X`` are handled above; this
        # catches every other glued / clustered spelling (Redmine #14100 review
        # j#82912).
        if (
            token.startswith("-")
            and not token.startswith("--")
            and len(token) > 1
            and "v" in token[1:]
        ):
            value = token[token.index("v", 1) + 1:]
            if value.startswith("="):
                value = value[1:]
            if value:
                mounts.append(_normalize_mount_spec(value))
                index += 1
            elif index + 1 < len(command):  # `v` is the last char: value is next token
                mounts.append(_normalize_mount_spec(command[index + 1]))
                index += 2
            else:
                offenders.append(token)  # dangling clustered volume flag
                index += 1
            continue
        if token in _OTHER_MOUNT_FLAGS or any(
            token.startswith(flag + "=") for flag in _OTHER_MOUNT_FLAGS
        ):
            offenders.append(token)
        index += 1
    if offenders:
        raise SmokeError(
            f"docker command carries unexpected mount flag(s) {offenders}; "
            "only a single read-only artifact bind mount is allowed"
        )
    expected = f"{artifact_dir.resolve()}:/artifacts:ro"
    if mounts != [expected]:
        raise SmokeError(
            f"docker mounts must be exactly ['{expected}'] (artifact-only, read-only); "
            f"got {mounts}"
        )


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

# Mount isolation: OBSERVE (not assume) that no host source checkout is bind-
# mounted into the container. Read /proc/self/mountinfo and flag any mount whose
# FILESYSTEM TYPE is a real (non-pseudo) filesystem — i.e. host content — unless
# it is one of the tiny allowed set (/ overlay root, the expected /artifacts
# bind, and the /etc/{resolv.conf,hostname,hosts} bind files docker injects). A
# real filesystem mounted anywhere — including UNDER /dev, /proc or /sys — is
# extra host content and fails CLOSED. Keying on fstype (not a path prefix)
# closes the bypass where a bind mount targeted at e.g. /dev/src evaded a naive
# /dev prefix allow (Redmine #14100 review j#82888). Pseudo filesystems (proc,
# sysfs, cgroup, tmpfs, devpts, ...) are the container's own plumbing and are
# allowed; a source checkout's backing fs (ext4/xfs/overlay/9p/virtiofs/...) is
# never in that set, so it is always caught.
EXTRA_MOUNTS="$(python3 <<'PYMOUNT'
PSEUDO_FSTYPES = {
    "proc", "sysfs", "cgroup", "cgroup2", "tmpfs", "devpts", "mqueue", "bpf",
    "tracefs", "debugfs", "securityfs", "pstore", "hugetlbfs", "devtmpfs",
    "ramfs", "fusectl", "configfs", "binfmt_misc", "autofs", "nsfs", "efivarfs",
}
ALLOWED_EXACT = {"/", "/artifacts", "/etc/resolv.conf", "/etc/hostname", "/etc/hosts"}
extra = []
with open("/proc/self/mountinfo", encoding="utf-8") as fh:
    for line in fh:
        parts = line.split()
        try:
            sep = parts.index("-")
        except ValueError:
            continue
        if len(parts) < 5 or sep + 1 >= len(parts):
            continue
        mount_point = parts[4]
        fstype = parts[sep + 1]
        if mount_point in ALLOWED_EXACT:
            continue
        if fstype in PSEUDO_FSTYPES:
            continue
        # A non-pseudo filesystem at an unexpected mount point == host content.
        extra.append(f"{mount_point}({fstype})")
print(",".join(sorted(set(extra))))
PYMOUNT
)"
if [ -n "$EXTRA_MOUNTS" ]; then
  echo "FAIL: unexpected host mount(s) present (possible source checkout): $EXTRA_MOUNTS" >&2
  exit 3
fi
step mount_isolation

python3 - "$RUNTIME_USER" "$RUNTIME_UID" "$HOME" "$OBS_VERSION" "$OBS_VERSION_MOZYO" "$PKG_PATH" "$PRESET" "$EXTRA_MOUNTS" <<'PYFACTS'
import json, sys
(user, uid, home, ver, ver_mozyo, pkg, preset, extra_mounts_csv) = sys.argv[1:9]
extra_mounts = [m for m in extra_mounts_csv.split(",") if m]
facts = {
    "harness_user": user,
    "harness_uid": int(uid),
    "harness_home": home,
    "observed_version_mozyo_bridge": ver,
    "observed_version_mozyo": ver_mozyo,
    "package_path": pkg,
    "preset": preset,
    # OBSERVED from /proc/self/mountinfo above (empty extra mounts => absent).
    "source_mount_present": bool(extra_mounts),
    "extra_mounts": extra_mounts,
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
    mounts_host_verified: bool = False,
) -> dict:
    """Assemble the machine-readable, secret-safe smoke summary."""
    facts = parsed.get("facts")
    steps = parsed.get("steps", [])
    missing = [name for name in EXPECTED_STEPS if name not in steps]
    # Source-absence is verified at BOTH layers: host-side (argv + artifact-dir
    # boundary, `mounts_host_verified`) and container-side (the observed
    # `mount_isolation` surface + the observed `source_mount_present` fact). The
    # verdict requires both, so neither a self-reported constant nor a single
    # regressing layer can pass it.
    source_mount_absent_verified = bool(
        mounts_host_verified
        and "mount_isolation" in steps
        and facts
        and facts.get("source_mount_present") is False
    )
    provenance_ok = bool(
        facts
        and facts.get("observed_version_mozyo_bridge") == expected_version
        and facts.get("observed_version_mozyo") == expected_version
        and isinstance(facts.get("harness_uid"), int)
        and facts.get("harness_uid") != 0
        and source_mount_absent_verified
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
            "source_mount_absent_verified": source_mount_absent_verified,
            "mounts_host_verified": bool(mounts_host_verified),
            "extra_mounts": (facts.get("extra_mounts") if facts else None),
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
    # Host-side source-absence assertion #1: the mounted directory must hold
    # only distribution files (no source tree can leak through /artifacts).
    verify_artifact_dir_is_artifact_only(artifact_dir)
    wheel_sha = sha256_file(wheel)

    command = build_docker_command(
        docker_bin=args.docker,
        image=args.image,
        artifact_dir=artifact_dir,
        wheel_name=wheel.name,
        expected_version=args.expected_version,
        preset=args.preset,
    )
    # Host-side source-absence assertion #2: the actual argv must mount exactly
    # the artifact dir read-only and nothing else.
    verify_command_mounts_artifact_only(command, artifact_dir)
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
        mounts_host_verified=True,
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
