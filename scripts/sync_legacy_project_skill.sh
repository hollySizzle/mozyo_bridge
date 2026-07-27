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

# File-set audit: an extra mirrored reference means a canonical file was copied
# in without extending the pinned set. It is NOT auto-removed — deleting a
# tracked file is a reviewed decision, not a sync side effect.
#
# Both modes run this, and both refuse. Review j#90322 F1 caught the earlier
# split where only `--check` audited the set: the sync then exited 0 with a
# success banner while the very next `--check` exited 1, and the documented
# "rerun the sync" recovery could never converge. A mode that cannot resolve a
# drift class must not report success in its presence.
unpinned_found=0
audit_unpinned_references() {
  [ -d "$dest" ] || return 0
  for path in "$dest"/*.md; do
    # Distinguish the glob no-match case (an unmatched glob stays literal in
    # sh) from a real directory entry. `-e` alone is NOT that test: it follows
    # symlinks, so a *dangling* symlink is `-e`-false and would be skipped as
    # though the glob had not matched — the fail-open review j#90342 R2-F1
    # measured. `-L` is true for a symlink whether or not its target exists,
    # so the pair covers every entry the glob can actually produce.
    [ -e "$path" ] || [ -L "$path" ] || continue
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
      unpinned_found=1
    fi
  done
}

# Symlinks are banned outright in the mirror's reference set — for PINNED names
# too, which the unpinned audit above cannot see by construction.
#
# Review j#90342 R2-F1 named the dangling-symlink fail-open; measuring it showed
# the same entry-type blindness does worse on a pinned name. A pinned reference
# that is a symlink passes content parity (`cmp` follows it), and then the sync's
# `cp` writes THROUGH it: pointing `references/safety.md` at an unrelated file
# made the sync overwrite that file with the canonical body, exit 0, and report
# success. The mirror is a byte copy, so a symlink is never a correct entry here;
# banning the type is what actually closes the hazard.
symlink_found=0
audit_symlink_references() {
  [ -d "$dest" ] || return 0
  for path in "$dest"/*.md; do
    [ -e "$path" ] || [ -L "$path" ] || continue
    if [ -L "$path" ]; then
      echo "legacy project skill mirror reference is a symlink: references/$(basename "$path")" >&2
      symlink_found=1
    fi
  done
}

unpinned_disposition() {
  echo "Unpinned reference(s) need a reviewed disposition: either delete them, or add" >&2
  echo "them to MIRRORED_REFERENCES in this script AND in" >&2
  echo "tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py." >&2
  echo "Rerunning this script does NOT clear them — it refuses while they are present." >&2
}

symlink_disposition() {
  echo "The mirror is a byte copy; a symlinked reference is never valid here. Replace it" >&2
  echo "with a regular file (or delete it), then rerun this script. Syncing over a symlink" >&2
  echo "would write through it into the link target, so this script refuses instead." >&2
}

if [ "$check_only" -eq 1 ]; then
  if [ ! -d "$dest" ]; then
    echo "legacy project skill mirror missing: $dest" >&2
    echo "$recovery" >&2
    exit 1
  fi

  content_drift=0

  # Content parity over the pinned set (the drift #14580 actually hit).
  for name in $MIRRORED_REFERENCES; do
    if [ ! -f "$dest/$name" ]; then
      echo "legacy project skill mirror missing file: references/$name" >&2
      content_drift=1
      continue
    fi
    # `set -e` would abort on cmp's non-zero exit, so branch on it explicitly.
    if cmp -s "$src/$name" "$dest/$name"; then
      continue
    fi
    echo "legacy project skill mirror drift detected: references/$name differs from canonical" >&2
    content_drift=1
  done

  audit_unpinned_references
  audit_symlink_references

  # Report the recovery that actually resolves each class present. Printing the
  # blanket "rerun the sync" line for an unpinned-reference drift would send the
  # operator to a command that refuses.
  if [ "$content_drift" -ne 0 ] || [ "$unpinned_found" -ne 0 ] || [ "$symlink_found" -ne 0 ]; then
    echo "" >&2
    if [ "$content_drift" -ne 0 ]; then
      echo "$recovery" >&2
    fi
    if [ "$unpinned_found" -ne 0 ]; then
      unpinned_disposition
    fi
    if [ "$symlink_found" -ne 0 ]; then
      symlink_disposition
    fi
    exit 1
  fi

  echo "legacy project skill mirror is up to date"
  echo "  source: $src"
  echo "  destination: $dest"
  exit 0
fi

# Sync mode. Audit BEFORE writing anything: refusing after a partial copy would
# leave the tree in a state neither the exit code nor the banner describes. The
# symlink audit in particular MUST precede the copy loop — that is the write it
# exists to prevent.
audit_unpinned_references
audit_symlink_references
if [ "$unpinned_found" -ne 0 ] || [ "$symlink_found" -ne 0 ]; then
  echo "refusing to sync the legacy project skill mirror; nothing was written." >&2
  if [ "$unpinned_found" -ne 0 ]; then
    unpinned_disposition
  fi
  if [ "$symlink_found" -ne 0 ]; then
    symlink_disposition
  fi
  exit 1
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
