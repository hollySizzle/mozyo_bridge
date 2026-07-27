#!/usr/bin/env sh
set -eu

# Thin compatibility wrapper for the legacy project Claude skill PARTIAL mirror
# sync (Redmine #14580).
#
# This script deliberately carries NO mirror logic: no pinned reference set, no
# audit, no copy, no cleanup. It resolves the repo root plus `src/` and execs
# the Python authority in
# `mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution`.
#
# Why: six review rounds found the same fail-open reached through a different
# axis each time, and the last round's three defects were all shell-specific —
# `$(...)` word splitting and pathname expansion losing filename boundaries,
# a name prefix standing in for temp ownership, and composite recovery advice.
# The design consultation (j#90400) answered by j#90402 chose a Python
# authority: `os.scandir` cannot re-split a name, an exclusive `mkstemp` fd is
# real ownership, and the rules are unit-testable individually.
#
# The wrapper stays so that the operator-facing command, the docs, and the
# `mozyo-bridge release check drift` subprocess contract are unchanged.
#
# Modes (unchanged):
#   default      replace each mirrored reference from canonical.
#   --check      dry-run; exit 0 when the mirror contract holds, 1 otherwise.
#
# Env override:
#   MOZYO_PYTHON  interpreter to use (default: python3).

script_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"

PYTHON="${MOZYO_PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "python interpreter not found: $PYTHON" >&2
  echo "Set MOZYO_PYTHON to a Python 3 interpreter and retry." >&2
  exit 1
fi

# Prefer the target tree's own package so a staged release copy checks itself
# rather than whatever happens to be importable (same posture as the canonical
# sub-check in `release check drift`).
if [ -f "$repo_root/src/mozyo_bridge/__init__.py" ]; then
  PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"
  export PYTHONPATH
fi

module="mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution.application.cli_legacy_mirror_sync"

if ! "$PYTHON" -c "import $module" >/dev/null 2>&1; then
  echo "cannot import the legacy mirror sync module: $module" >&2
  echo "Run from a mozyo_bridge checkout (or install the package) and retry." >&2
  exit 1
fi

exec "$PYTHON" -m "$module" --repo "$repo_root" "$@"
