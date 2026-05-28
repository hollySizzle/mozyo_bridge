#!/usr/bin/env sh
set -eu

# Regenerate the plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/ mirror
# from the canonical skills/mozyo-bridge-agent/ source. Run this whenever the
# canonical skill body changes; the drift test in tests/test_mozyo_bridge.py
# will otherwise fail.
#
# Modes:
#   default      regenerate the plugin mirror (rsync -a --delete).
#   --check      dry-run; exit 0 if mirror matches canonical, 1 on drift.
#                Writes nothing. Designed for CI gating without modifying
#                the worktree.

usage() {
  cat <<USAGE
Usage: $0 [--check]

Without --check, regenerates the plugin skill mirror from canonical.
With --check, performs a dry-run sync and exits 1 if drift exists.
USAGE
}

check_only=0
for arg in "$@"; do
  case "$arg" in
    --check)
      check_only=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $arg" >&2
      usage >&2
      exit 64
      ;;
  esac
done

script_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"

src="$repo_root/skills/mozyo-bridge-agent/"
dest="$repo_root/plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/"

if [ ! -f "$src/SKILL.md" ]; then
  echo "canonical skill missing: $src/SKILL.md" >&2
  exit 1
fi

if [ "$check_only" -eq 1 ]; then
  # Dry-run: rsync emits one itemized-changes line per file it would
  # transfer or delete. Any output means drift. Reading the mirror
  # directory is read-only.
  if [ ! -d "$dest" ]; then
    echo "plugin skill mirror missing: $dest" >&2
    echo "Rerun 'scripts/sync_plugin_skill.sh' (no --check, from the repo root) to regenerate the mirror." >&2
    exit 1
  fi
  output=$(rsync -an --delete --itemize-changes "$src" "$dest")
  if [ -n "$output" ]; then
    echo "plugin skill mirror drift detected; would change:" >&2
    echo "$output" >&2
    echo "" >&2
    echo "Rerun 'scripts/sync_plugin_skill.sh' (no --check, from the repo root) to regenerate the mirror." >&2
    exit 1
  fi
  echo "plugin skill mirror is up to date"
  echo "  source: $src"
  echo "  destination: $dest"
  exit 0
fi

mkdir -p "$dest"
rsync -a --delete "$src" "$dest"

echo "synced plugin skill mirror"
echo "  source: $src"
echo "  destination: $dest"
