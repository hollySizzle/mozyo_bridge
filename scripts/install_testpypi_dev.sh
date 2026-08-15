#!/usr/bin/env sh
set -eu

# Install or update the local pipx runtime from a TestPyPI artifact and
# verify the installed CLI surface. This aligns the normal-PATH runtime
# (default pipx target: ~/.local/bin/mozyo-bridge) with the exact-candidate
# artifact the manual `.github/workflows/testpypi.yml` dispatch publishes
# (the automatic main-CI dev path was removed by Redmine #15487), so real
# smoke evidence does not depend on source-runtime,
# `PYTHONPATH=src`, or an ad-hoc current-checkout reinstall.
#
# It NEVER publishes anything; it only installs locally and reads CLI help.
#
# Usage:
#   scripts/install_testpypi_dev.sh <version>   # pin the exact version
#
# You MUST pass the exact version (e.g. 1.0.0rc5). Read it, and the commit
# SHA it maps to, from the "Publish to TestPyPI" run's job summary
# (see references/release.md).
#
# Why exact-only (no `latest`): this install uses TestPyPI as --index-url and
# PyPI as --extra-index-url so dependencies still resolve from PyPI. pip
# considers candidates for the TARGET package from BOTH indexes with NO
# priority between them (pip `install` "Finding Packages"), so an unpinned
# install could resolve the PyPI production release and silently taint smoke
# evidence. The exact pin alone does NOT prove the TestPyPI source either: if
# the SAME version ever existed on PyPI, pip may serve either artifact and the
# post-install `--version` check cannot tell them apart. Source provenance is
# therefore proven mechanically below (Redmine #15487 R2 finding_1): right
# before the pipx mutation the script requires the pinned version to be ABSENT
# from production PyPI, failing closed on any unprovable lookup.

usage() {
  cat <<USAGE
Usage: $0 <version>

  <version>  Exact PEP 440 version to install, e.g. 1.0.0rc5 or
             0.9.2.dev20260628090000123456789

Pass the exact version from the 'Publish to TestPyPI' run summary so smoke
evidence ties to one commit SHA. 'latest' is intentionally unsupported.
USAGE
}

if [ "$#" -ne 1 ]; then
  usage >&2
  exit 64
fi

case "$1" in
  -h|--help)
    usage
    exit 0
    ;;
esac

version="$1"

if ! command -v pipx >/dev/null 2>&1; then
  echo "error: pipx not found in PATH; install pipx first (e.g. 'python -m pip install --user pipx')." >&2
  exit 1
fi

if [ "$version" = "latest" ]; then
  echo "error: 'latest' is not supported; pass the exact version." >&2
  echo "       An unpinned install can resolve the PyPI production release" >&2
  echo "       instead of the intended TestPyPI artifact." >&2
  echo >&2
  usage >&2
  exit 64
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "error: curl not found in PATH; it is required to verify TestPyPI" >&2
  echo "       Simple Index propagation before changing the pipx environment." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 not found in PATH; it is required to parse the" >&2
  echo "       TestPyPI Simple JSON response without substring matches." >&2
  exit 1
fi

# TestPyPI's project JSON can expose a newly uploaded release before the
# Simple Index used by pip has propagated through Warehouse/CDN caches. A
# direct pipx attempt during that window reports "No matching distribution"
# even with pip's local cache disabled. Poll the canonical resolver URL before
# asking pipx to mutate the existing environment. Do not add a cache-busting
# query: it creates a different CDN cache key that can expose the new release
# before the query-free URL pip actually resolves. The bound covers the Simple
# response's normal 10-minute max-age; a timeout is temporary failure and
# leaves the previous pipx environment untouched.
simple_url="https://test.pypi.org/simple/mozyo-bridge/"
simple_max_attempts=41
simple_attempt=1
simple_interval_seconds=15

echo "Waiting for TestPyPI Simple Index: $version"
while :; do
  if curl \
    --fail \
    --silent \
    --show-error \
    --location \
    --header "Cache-Control: no-cache" \
    --header "Accept: application/vnd.pypi.simple.v1+json" \
    "${simple_url}" \
    | MOZYO_TESTPYPI_EXPECTED_VERSION="$version" python3 -c '
import json
import os
import sys

try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, UnicodeError):
    raise SystemExit(1)

files = payload.get("files")
if not isinstance(files, list):
    raise SystemExit(1)

version = os.environ["MOZYO_TESTPYPI_EXPECTED_VERSION"]
wheel_prefix = f"mozyo_bridge-{version}-"
sdist_names = {
    f"mozyo_bridge-{version}.tar.gz",
    f"mozyo_bridge-{version}.zip",
}
for item in files:
    filename = item.get("filename") if isinstance(item, dict) else None
    if isinstance(filename, str) and (
        filename.startswith(wheel_prefix) or filename in sdist_names
    ):
        raise SystemExit(0)
raise SystemExit(1)
'; then
    echo "OK: TestPyPI Simple Index lists $version"
    break
  fi

  if [ "$simple_attempt" -ge "$simple_max_attempts" ]; then
    echo "error: TestPyPI JSON/publication may exist, but the Simple Index" >&2
    echo "       still does not list '$version' after 10 minutes." >&2
    echo "       The existing pipx environment was not changed; retry later" >&2
    echo "       only after the Simple Index lists the exact version." >&2
    exit 75
  fi

  echo "PENDING: Simple Index propagation (${simple_attempt}/${simple_max_attempts}); retrying in ${simple_interval_seconds}s"
  sleep "$simple_interval_seconds"
  simple_attempt=$((simple_attempt + 1))
done

# --- PyPI absence gate (source provenance, Redmine #15487 R2 finding_1) ---
# pip gives --index-url no priority over --extra-index-url, so the pinned
# version must be proven ABSENT from production PyPI immediately before the
# mutation — otherwise pip may serve the PyPI artifact under the same version
# and the smoke evidence cannot prove its TestPyPI origin. A 404 proves the
# project (and thus the version) is absent; a 200 listing without the
# version's files proves the version is absent; anything else — the version
# present, an unreadable payload, an unexpected status, or a failed lookup —
# refuses fail-closed with the pipx environment untouched.
pypi_url="https://pypi.org/simple/mozyo-bridge/"
pypi_body="${TMPDIR:-/tmp}/mozyo-pypi-absence-$$.json"
pypi_code=""
if ! pypi_code="$(curl \
  --silent \
  --show-error \
  --location \
  --header "Cache-Control: no-cache" \
  --header "Accept: application/vnd.pypi.simple.v1+json" \
  --output "$pypi_body" \
  --write-out '%{http_code}' \
  "$pypi_url")"; then
  rm -f "$pypi_body"
  echo "error: PyPI lookup failed; cannot prove '$version' is absent from" >&2
  echo "       production PyPI (fail-closed). Environment unchanged." >&2
  exit 75
fi
case "$pypi_code" in
  404)
    echo "OK: PyPI does not host mozyo-bridge; '$version' cannot resolve from PyPI"
    ;;
  200)
    if MOZYO_PYPI_EXPECTED_VERSION="$version" python3 -c '
import json
import os
import sys

try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, UnicodeError):
    raise SystemExit(2)

files = payload.get("files")
if not isinstance(files, list):
    raise SystemExit(2)

version = os.environ["MOZYO_PYPI_EXPECTED_VERSION"]
wheel_prefix = f"mozyo_bridge-{version}-"
sdist_names = {
    f"mozyo_bridge-{version}.tar.gz",
    f"mozyo_bridge-{version}.zip",
}
for item in files:
    filename = item.get("filename") if isinstance(item, dict) else None
    if isinstance(filename, str) and (
        filename.startswith(wheel_prefix) or filename in sdist_names
    ):
        raise SystemExit(1)
raise SystemExit(0)
' < "$pypi_body"; then
      echo "OK: PyPI Simple Index does not list $version"
    else
      pypi_check_status=$?
      rm -f "$pypi_body"
      if [ "$pypi_check_status" -eq 1 ]; then
        echo "error: version '$version' EXISTS on production PyPI; the pinned" >&2
        echo "       install cannot prove a TestPyPI source (pip treats both" >&2
        echo "       indexes as acceptable for the same version). Refusing;" >&2
        echo "       environment unchanged. Publish a distinct candidate" >&2
        echo "       version instead." >&2
      else
        echo "error: PyPI Simple response is unreadable; cannot prove" >&2
        echo "       '$version' is absent (fail-closed). Environment unchanged." >&2
      fi
      exit 75
    fi
    ;;
  *)
    rm -f "$pypi_body"
    echo "error: unexpected PyPI Simple status '$pypi_code'; cannot prove" >&2
    echo "       '$version' is absent (fail-closed). Environment unchanged." >&2
    exit 75
    ;;
esac
rm -f "$pypi_body"

spec="mozyo-bridge==$version"
echo "Installing TestPyPI artifact: $spec"

# Force the pip backend so TestPyPI serves mozyo-bridge while PyPI still serves
# its dependencies. The exact candidate version is published only to TestPyPI,
# so the pinned target resolves from TestPyPI even though PyPI is
# an extra-index for dependencies. --pre lets pip accept the pre-release;
# --no-cache-dir prevents a just-published exact version from being hidden by a
# stale cached Simple Index response. --force reinstalls / updates the existing
# pipx app in place on the normal PATH.
pipx install \
  --force \
  --backend pip \
  --index-url https://test.pypi.org/simple/ \
  --pip-args "--extra-index-url https://pypi.org/simple/ --pre --no-cache-dir" \
  "$spec"

echo
echo "=== CLI surface verification ==="
bin_path="$(command -v mozyo-bridge || true)"
echo "binary: ${bin_path:-<mozyo-bridge not on PATH>}"
if [ -z "$bin_path" ]; then
  echo "error: mozyo-bridge is not on PATH after install; check your pipx bin dir." >&2
  exit 1
fi

# Required surface: the two console entry points must both report the EXACT
# version we pinned. `--version` prints `<prog> <version>` (argparse
# `%(prog)s {__version__}`), so the version token is the last whitespace field.
# Both entry points share the runtime `__version__`, and the TestPyPI build
# mirrors that literal to the pinned version (Redmine #13586); if either CLI
# disagrees, the installed artifact is not the pinned build and smoke evidence
# would be inaccurate, so fail non-zero rather than merely displaying it.
assert_cli_version() {
  cli_name="$1"
  raw="$("$cli_name" --version)"   # e.g. "mozyo-bridge 1.0.0rc5"
  got="${raw##* }"                 # last whitespace field == version token
  if [ "$got" != "$version" ]; then
    echo "error: '$cli_name --version' reported '$got' but the requested" >&2
    echo "       TestPyPI version is '$version'. The installed artifact" >&2
    echo "       does not match the pinned version; refusing so smoke" >&2
    echo "       evidence cannot silently tie to the wrong build." >&2
    exit 1
  fi
  echo "OK: $cli_name --version == $version"
}

assert_cli_version mozyo-bridge
assert_cli_version mozyo
mozyo-bridge project-gateway consult --help >/dev/null
echo "OK: mozyo-bridge project-gateway consult --help"

# Future surface (#12755): tolerate absence so this runbook works against
# artifacts built before `workflow step` ships.
if mozyo-bridge workflow step --help >/dev/null 2>&1; then
  echo "OK: mozyo-bridge workflow step --help"
else
  echo "PENDING: mozyo-bridge workflow step --help not in this artifact (#12755 not yet shipped)"
fi

echo
echo "=== smoke evidence ==="
echo "artifact_version: $(mozyo-bridge --version)"
echo "binary: $bin_path"
echo "Record the artifact version above AND the commit SHA it maps to (from the"
echo "'Publish to TestPyPI' run summary) in the Redmine smoke-evidence journal."
