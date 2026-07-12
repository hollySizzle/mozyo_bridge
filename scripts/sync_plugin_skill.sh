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
  # Dry-run: compare canonical and mirror by file content and file set
  # only. Reading both trees is read-only.
  #
  # We deliberately do NOT use `rsync --itemize-changes` here. `rsync -a`
  # preserves mtime, so its itemized dry-run reports checkout-induced
  # timestamp-only differences (`>f..t......` / `.d..t......`) as drift
  # even when every byte is identical. That made this CI gate depend on
  # the runner's clock and fail non-deterministically (Redmine #13580).
  # `diff -r` compares file contents and directory membership and ignores
  # mtime entirely, so a byte-identical mirror passes regardless of
  # checkout timestamps, while content changes, missing files, and extra
  # files are still reported with their paths.
  if [ ! -d "$dest" ]; then
    echo "plugin skill mirror missing: $dest" >&2
    echo "Rerun 'scripts/sync_plugin_skill.sh' (no --check, from the repo root) to regenerate the mirror." >&2
    exit 1
  fi
  # `set -e` would abort on diff's non-zero exit, so capture it in an
  # AND-OR list. diff exit codes: 0 = identical, 1 = differences found,
  # >1 = trouble (e.g. unreadable path).
  output=$(diff -r "$src" "$dest") && diff_status=0 || diff_status=$?
  if [ "$diff_status" -gt 1 ]; then
    echo "plugin skill mirror check failed while comparing:" >&2
    echo "  source: $src" >&2
    echo "  destination: $dest" >&2
    echo "$output" >&2
    exit "$diff_status"
  fi
  if [ "$diff_status" -ne 0 ]; then
    echo "plugin skill mirror drift detected; content or file set differs:" >&2
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
