#!/usr/bin/env sh
set -eu

# Install or update the local pipx runtime from a TestPyPI dev artifact and
# verify the installed CLI surface. This aligns the normal-PATH runtime
# (default pipx target: ~/.local/bin/mozyo-bridge) with the dev artifact that
# the automated `.github/workflows/testpypi.yml` main-CI job publishes, so
# #12709-style real smoke evidence does not depend on source-runtime,
# `PYTHONPATH=src`, or an ad-hoc current-checkout reinstall.
#
# It NEVER publishes anything; it only installs locally and reads CLI help.
#
# Usage:
#   scripts/install_testpypi_dev.sh <version>   # pin the exact dev version
#
# You MUST pass the exact dev version (e.g. 0.9.2.dev20260628090000123456789).
# Read it, and the commit SHA it maps to, from the source CI run's job summary
# in the "Publish to TestPyPI" workflow (see references/release.md).
#
# Why exact-only (no `latest`): this install uses TestPyPI as --index-url and
# PyPI as --extra-index-url so dependencies still resolve from PyPI. pip
# considers candidates for the TARGET package from BOTH indexes, and a dev
# release (0.9.2.devN) sorts BEFORE the PyPI final (0.9.2). An unpinned install
# could therefore resolve the PyPI production release instead of the intended
# TestPyPI dev artifact and silently taint smoke evidence. Pinning the exact
# dev version is safe because that version exists ONLY on TestPyPI (production
# PyPI never hosts dev releases), so the target can only resolve from TestPyPI.

usage() {
  cat <<USAGE
Usage: $0 <version>

  <version>  Exact PEP 440 dev version to install, e.g.
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
  echo "error: 'latest' is not supported; pass the exact dev version." >&2
  echo "       An unpinned install can resolve the PyPI production release" >&2
  echo "       instead of the intended TestPyPI dev artifact." >&2
  echo >&2
  usage >&2
  exit 64
fi

case "$version" in
  *.dev*) : ;;  # looks like a dev release; proceed
  *)
    echo "warning: '$version' has no '.dev' segment; this runbook targets" >&2
    echo "         TestPyPI dev artifacts. Continuing with the exact pin." >&2
    ;;
esac

spec="mozyo-bridge==$version"
echo "Installing TestPyPI dev artifact: $spec"

# Force the pip backend so TestPyPI serves mozyo-bridge while PyPI still serves
# its dependencies. The exact dev version exists ONLY on TestPyPI (PyPI never
# hosts dev releases), so the target resolves from TestPyPI even though PyPI is
# an extra-index for dependencies. --pre lets pip accept the pre-release;
# --force reinstalls / updates the existing pipx app in place on the normal PATH.
pipx install \
  --force \
  --backend pip \
  --index-url https://test.pypi.org/simple/ \
  --pip-args "--extra-index-url https://pypi.org/simple/ --pre" \
  "$spec"

echo
echo "=== CLI surface verification ==="
bin_path="$(command -v mozyo-bridge || true)"
echo "binary: ${bin_path:-<mozyo-bridge not on PATH>}"
if [ -z "$bin_path" ]; then
  echo "error: mozyo-bridge is not on PATH after install; check your pipx bin dir." >&2
  exit 1
fi

# Required surface: these must succeed (set -e aborts on failure).
mozyo-bridge --version
mozyo --version
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
