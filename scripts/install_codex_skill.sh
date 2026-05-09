#!/usr/bin/env sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
src="$repo_root/skills/mozyo-bridge-agent/"
dest="${CODEX_HOME:-$HOME/.codex}/skills/mozyo-bridge-agent/"

if [ ! -f "$src/SKILL.md" ]; then
  echo "missing skill source: $src/SKILL.md" >&2
  exit 1
fi

mkdir -p "$(dirname -- "$dest")"
rsync -a --delete "$src" "$dest"

echo "installed Codex skill: $dest"
echo "restart Codex to pick up new skills"
