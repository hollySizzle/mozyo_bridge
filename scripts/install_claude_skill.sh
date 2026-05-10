#!/usr/bin/env sh
set -eu

repo="${MOZYO_BRIDGE_SKILL_REPO:-hollySizzle/mozyo_bridge}"
ref="${MOZYO_BRIDGE_SKILL_REF:-main}"
shared_path="${MOZYO_BRIDGE_SHARED_SKILL_PATH:-skills/mozyo-bridge-agent}"
adapter_path="${MOZYO_BRIDGE_CLAUDE_ADAPTER_PATH:-.claude/skills/mozyo-bridge-agent}"
project_dir="${MOZYO_BRIDGE_CLAUDE_PROJECT_DIR:-$PWD}"
claude_home="${MOZYO_BRIDGE_CLAUDE_HOME:-$HOME/.claude}"
scope="${MOZYO_BRIDGE_CLAUDE_SCOPE:-project}"
archive_url="${MOZYO_BRIDGE_SKILL_ARCHIVE_URL:-https://codeload.github.com/$repo/tar.gz/$ref}"

case "$scope" in
  project|global) ;;
  *)
    echo "MOZYO_BRIDGE_CLAUDE_SCOPE must be one of: project, global (got '$scope')" >&2
    echo "To install at both scopes, run the script twice with each scope." >&2
    exit 2
    ;;
esac

shared_dest_project="$project_dir/skills/mozyo-bridge-agent/"
adapter_dest_project="$project_dir/.claude/skills/mozyo-bridge-agent/"
shared_dest_global="$claude_home/skills/mozyo-bridge-agent/"

tmp="${TMPDIR:-/tmp}/mozyo-bridge-claude-skill.$$"
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
  echo "failed to fetch skill from $archive_url" >&2
  exit 1
fi

archive_root=$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -n 1)
if [ -z "$archive_root" ]; then
  echo "fetched archive has no source directory: $archive_url" >&2
  exit 1
fi

shared_src="$archive_root/$shared_path/"
adapter_src="$archive_root/$adapter_path/"

if [ ! -f "$shared_src/SKILL.md" ]; then
  echo "fetched shared skill is missing SKILL.md: $archive_url:$shared_path" >&2
  exit 1
fi

if [ "$scope" = "project" ] && [ ! -f "$adapter_src/SKILL.md" ]; then
  echo "fetched Claude adapter is missing SKILL.md: $archive_url:$adapter_path" >&2
  exit 1
fi

case "$scope" in
  project)
    mkdir -p "$(dirname -- "$shared_dest_project")" "$(dirname -- "$adapter_dest_project")"
    rsync -a --delete "$shared_src" "$shared_dest_project"
    rsync -a --delete "$adapter_src" "$adapter_dest_project"
    ;;
  global)
    mkdir -p "$(dirname -- "$shared_dest_global")"
    rsync -a --delete "$shared_src" "$shared_dest_global"
    ;;
esac

echo "installed Claude Code skill from $archive_url"
echo "scope: $scope"
case "$scope" in
  project)
    echo "shared skill destination (project): $shared_dest_project"
    echo "Claude adapter destination (project): $adapter_dest_project"
    echo "start Claude Code from $project_dir so project skills resolve"
    echo "note: when a same-named skill is also installed at ~/.claude/skills/, Claude Code loads the personal/global one (personal overrides project for same-name skills)"
    ;;
  global)
    echo "shared skill destination (global): $shared_dest_global"
    echo "applies to every Claude Code session for this user; personal/global skills override project skills with the same name"
    ;;
esac
