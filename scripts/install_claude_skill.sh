#!/usr/bin/env sh
set -eu

repo="${MOZYO_BRIDGE_SKILL_REPO:-hollySizzle/mozyo_bridge}"
ref="${MOZYO_BRIDGE_SKILL_REF:-main}"
shared_path="${MOZYO_BRIDGE_SHARED_SKILL_PATH:-skills/mozyo-bridge-agent}"
adapter_path="${MOZYO_BRIDGE_CLAUDE_ADAPTER_PATH:-.claude/skills/mozyo-bridge-agent}"
project_dir="${MOZYO_BRIDGE_CLAUDE_PROJECT_DIR:-$PWD}"

shared_dest="$project_dir/skills/mozyo-bridge-agent/"
adapter_dest="$project_dir/.claude/skills/mozyo-bridge-agent/"

tmp="${TMPDIR:-/tmp}/mozyo-bridge-claude-skill.$$"
cleanup() {
  rm -rf "$tmp"
}
trap cleanup EXIT INT TERM

mkdir -p "$tmp"

archive_url="https://codeload.github.com/$repo/tar.gz/$ref"
archive="$tmp/source.tar.gz"

if ! curl -fsSL "$archive_url" -o "$archive"; then
  echo "failed to download skill archive: $archive_url" >&2
  exit 1
fi

if ! tar -xzf "$archive" -C "$tmp"; then
  echo "failed to fetch skill from $repo at $ref" >&2
  exit 1
fi

archive_root=$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -n 1)
if [ -z "$archive_root" ]; then
  echo "fetched archive has no source directory: $repo at $ref" >&2
  exit 1
fi

shared_src="$archive_root/$shared_path/"
adapter_src="$archive_root/$adapter_path/"

if [ ! -f "$shared_src/SKILL.md" ]; then
  echo "fetched shared skill is missing SKILL.md: $repo at $ref:$shared_path" >&2
  exit 1
fi

if [ ! -f "$adapter_src/SKILL.md" ]; then
  echo "fetched Claude adapter is missing SKILL.md: $repo at $ref:$adapter_path" >&2
  exit 1
fi

mkdir -p "$(dirname -- "$shared_dest")" "$(dirname -- "$adapter_dest")"
rsync -a --delete "$shared_src" "$shared_dest"
rsync -a --delete "$adapter_src" "$adapter_dest"

echo "installed Claude Code project skill from github.com/$repo at $ref"
echo "shared skill destination: $shared_dest"
echo "Claude adapter destination: $adapter_dest"
echo "start Claude Code from $project_dir so project skills resolve"
