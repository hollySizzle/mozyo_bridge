#!/usr/bin/env sh
set -eu

# Sync (or drift-check) the legacy project Claude skill PARTIAL mirror at
# `.claude/skills/mozyo-bridge-agent/references/` from the canonical body at
# `skills/mozyo-bridge-agent/references/` (Redmine #14580).
#
# Why this exists as a separate script from `scripts/sync_plugin_skill.sh`:
# the plugin mirror is a FULL byte mirror (`rsync -a --delete` over the whole
# canonical tree), while this one is deliberately PARTIAL and has an
# intentional divergence:
#
#   - only the reference files listed in MIRRORED_REFERENCES are shipped;
#     canonical carries additional references (`redmine-issue-authoring.md`,
#     `subagent-delegation.md`) and `agents/` metadata that are intentionally
#     NOT mirrored;
#   - `SKILL.md` in the mirror is an intentional Claude Code adapter stub, not
#     a copy of the canonical `SKILL.md`. This script NEVER writes or compares
#     it, so a sync can never clobber the adapter.
#
# Running `rsync -a --delete` here would destroy both properties, which is why
# the legacy mirror had no sync path at all and drifted: Redmine #14580
# confirmed commit `7ca3380f` ("Pin coordinator work-unit resolution") updated
# canonical + plugin mirror and silently skipped this one, because only the
# plugin mirror had a script and a `release check drift` gate.
#
# Modes:
#   default      copy each mirrored reference from canonical into the mirror.
#   --check      dry-run; exit 0 when the mirror matches canonical, 1 on drift.
#                Writes nothing. Designed for CI / `mozyo-bridge release check
#                drift` gating without modifying the worktree.

# The tracked partial-mirror reference set. `LegacyProjectSkillMirrorTest`
# pins the same set and cross-checks this list against it, so the two cannot
# drift apart silently. Adding or dropping a mirrored reference means editing
# BOTH this list and the test's MIRRORED_REFERENCES in the same commit.
MIRRORED_REFERENCES="project-map.md release.md safety.md workflow.md"

usage() {
  cat <<USAGE
Usage: $0 [--check]

Without --check, copies the mirrored reference set from canonical
(skills/mozyo-bridge-agent/references/) into the legacy project mirror
(.claude/skills/mozyo-bridge-agent/references/).

With --check, compares only and exits 1 if drift exists.

The mirror's SKILL.md adapter stub is never written or compared.
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

src="$repo_root/skills/mozyo-bridge-agent/references"
dest="$repo_root/.claude/skills/mozyo-bridge-agent/references"

recovery="Rerun 'scripts/sync_legacy_project_skill.sh' (no --check, from the repo root) to resync the mirror."

if [ ! -d "$src" ]; then
  echo "canonical references dir missing: $src" >&2
  exit 1
fi

# A missing canonical source file is a hard error in BOTH modes: the pinned set
# is the contract, and silently skipping a name would let --check pass while
# the mirror is stale.
for name in $MIRRORED_REFERENCES; do
  if [ ! -f "$src/$name" ]; then
    echo "canonical reference missing: $src/$name" >&2
    echo "The pinned mirrored set names a file canonical does not have; fix the set in" >&2
    echo "scripts/sync_legacy_project_skill.sh and the matching test, or restore the file." >&2
    exit 1
  fi
done

if [ "$check_only" -eq 1 ]; then
  if [ ! -d "$dest" ]; then
    echo "legacy project skill mirror missing: $dest" >&2
    echo "$recovery" >&2
    exit 1
  fi

  drift=0

  # 1. content parity over the pinned set (the drift #14580 actually hit).
  for name in $MIRRORED_REFERENCES; do
    if [ ! -f "$dest/$name" ]; then
      echo "legacy project skill mirror missing file: references/$name" >&2
      drift=1
      continue
    fi
    # `set -e` would abort on cmp's non-zero exit, so branch on it explicitly.
    if cmp -s "$src/$name" "$dest/$name"; then
      continue
    fi
    echo "legacy project skill mirror drift detected: references/$name differs from canonical" >&2
    drift=1
  done

  # 2. file-set parity: an extra mirrored reference means a canonical file was
  # copied in without extending the pinned set. Not auto-removed — deleting a
  # tracked file is a reviewed decision, not a sync side effect.
  for path in "$dest"/*.md; do
    [ -e "$path" ] || continue
    found=0
    base="$(basename "$path")"
    for name in $MIRRORED_REFERENCES; do
      if [ "$base" = "$name" ]; then
        found=1
        break
      fi
    done
    if [ "$found" -eq 0 ]; then
      echo "legacy project skill mirror has an unpinned reference: references/$base" >&2
      echo "Either delete it, or add it to MIRRORED_REFERENCES in this script AND in" >&2
      echo "tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py." >&2
      drift=1
    fi
  done

  if [ "$drift" -ne 0 ]; then
    echo "" >&2
    echo "$recovery" >&2
    exit 1
  fi

  echo "legacy project skill mirror is up to date"
  echo "  source: $src"
  echo "  destination: $dest"
  exit 0
fi

mkdir -p "$dest"
for name in $MIRRORED_REFERENCES; do
  cp "$src/$name" "$dest/$name"
done

echo "synced legacy project skill mirror"
echo "  source: $src"
echo "  destination: $dest"
echo "  references: $MIRRORED_REFERENCES"
echo "  SKILL.md adapter stub left untouched (intentional divergence)"
