#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
poc_dir="$(cd -- "$script_dir/.." && pwd)"
repo_root="$(cd -- "$poc_dir/../.." && pwd)"
state_root="${MOZYO_AGENT_PANE_DEVHOST_STATE:-}"

cd "$poc_dir"

if [ ! -d node_modules ]; then
  npm install --ignore-scripts
fi

npm run compile

if [ -z "$state_root" ]; then
  state_root="$(mktemp -d "${TMPDIR:-/tmp}/mozyo-agent-pane-devhost.XXXXXX")"
else
  mkdir -p "$state_root"
fi

user_data_dir="$state_root/user-data"
extensions_dir="$state_root/extensions"

mkdir -p "$user_data_dir" "$extensions_dir"

env -u ELECTRON_RUN_AS_NODE open -n -a "Visual Studio Code" --args \
  --user-data-dir "$user_data_dir" \
  --extensions-dir "$extensions_dir" \
  --disable-extensions \
  --extensionDevelopmentPath="$poc_dir" \
  "$repo_root"

printf 'Opened isolated Extension Development Host state at %s\n' "$state_root"
