#!/usr/bin/env sh
set -eu

repo="${MOZYO_BRIDGE_SKILL_REPO:-hollySizzle/mozyo_bridge}"
ref="${MOZYO_BRIDGE_SKILL_REF:-main}"
path="${MOZYO_BRIDGE_SKILL_PATH:-skills/mozyo-bridge-agent}"
dest="${CODEX_HOME:-$HOME/.codex}/skills/mozyo-bridge-agent/"
archive_url="${MOZYO_BRIDGE_SKILL_ARCHIVE_URL:-https://codeload.github.com/$repo/tar.gz/$ref}"

tmp="${TMPDIR:-/tmp}/mozyo-bridge-skill.$$"
cleanup() {
  rm -rf "$tmp"
}
trap cleanup EXIT INT TERM

mkdir -p "$tmp"

archive="$tmp/source.tar.gz"

if ! curl -fsSL "$archive_url" -o "$archive"; then
  echo "failed to download skill archive: $archive_url" >&2
  exit 1
fi

if ! tar -xzf "$archive" -C "$tmp"; then
  echo "failed to fetch skill from $archive_url:$path" >&2
  exit 1
fi

archive_root=$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -n 1)
if [ -z "$archive_root" ]; then
  echo "fetched archive has no source directory: $archive_url" >&2
  exit 1
fi

src="$archive_root/$path/"
if [ ! -f "$src/SKILL.md" ]; then
  echo "fetched skill is missing SKILL.md: $archive_url:$path" >&2
  exit 1
fi

mkdir -p "$(dirname -- "$dest")"
rsync -a --delete "$src" "$dest"

echo "installed Codex skill from $archive_url:$path"
echo "destination: $dest"
echo "restart Codex to pick up new skills"
