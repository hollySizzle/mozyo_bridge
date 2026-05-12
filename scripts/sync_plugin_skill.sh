#!/usr/bin/env sh
set -eu

# Regenerate the plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/ mirror
# from the canonical skills/mozyo-bridge-agent/ source. Run this whenever the
# canonical skill body changes; the drift test in tests/test_mozyo_bridge.py
# will otherwise fail.

script_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"

src="$repo_root/skills/mozyo-bridge-agent/"
dest="$repo_root/plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/"

if [ ! -f "$src/SKILL.md" ]; then
  echo "canonical skill missing: $src/SKILL.md" >&2
  exit 1
fi

mkdir -p "$dest"
rsync -a --delete "$src" "$dest"

echo "synced plugin skill mirror"
echo "  source: $src"
echo "  destination: $dest"
