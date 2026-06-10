#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
poc_dir="$(cd -- "$script_dir/.." && pwd)"
repo_root="$(cd -- "$poc_dir/../.." && pwd)"

cd "$poc_dir"

if [ ! -d node_modules ]; then
  npm install --ignore-scripts
fi

npm run compile

code \
  --new-window \
  --disable-extension hollySizzle.taskpilot \
  --extensionDevelopmentPath="$poc_dir" \
  "$repo_root"
