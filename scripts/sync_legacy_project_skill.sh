#!/usr/bin/env sh
set -eu

# Sync (or drift-check) the legacy project Claude skill PARTIAL mirror at
# `.claude/skills/mozyo-bridge-agent/references/` from the canonical body at
# `skills/mozyo-bridge-agent/references/` (Redmine #14580).
#
# Why this exists as a separate script from `scripts/sync_plugin_skill.sh`:
# the plugin mirror is a FULL byte mirror (`rsync -a --delete` over the whole
# canonical tree), while this one is deliberately PARTIAL and has an intentional
# divergence:
#
#   - only the reference files listed in MIRRORED_REFERENCES are shipped;
#     canonical carries additional references (`redmine-issue-authoring.md`,
#     `subagent-delegation.md`) and `agents/` metadata that are intentionally
#     NOT mirrored;
#   - `SKILL.md` in the mirror is an intentional Claude Code adapter stub, not a
#     copy of the canonical `SKILL.md`. This script NEVER writes or compares it.
#
# Running `rsync -a --delete` here would destroy both properties, which is why
# the legacy mirror had no sync path at all and drifted: commit `7ca3380f`
# updated canonical + plugin mirror and silently skipped this one, because only
# the plugin mirror had a script and a `release check drift` gate.
#
# ---------------------------------------------------------------------------
# THE MIRROR CONTRACT
# ---------------------------------------------------------------------------
#
# Reviews j#90322 / j#90342 / j#90360 each found the same fail-open — the sync
# reporting success in the presence of a state it could not resolve — reached
# through a different axis: the mode (`--check` audited what the sync did not),
# the entry type (a dangling symlink is `-e`-false; a hardlink is a regular
# file), and the path topology (`-d` follows symlinks). j#90378 then found two
# more: the audit's filename domain (`*.md` misses `unpinned.txt`, hidden
# entries and stale temps) and the canonical SOURCE side (`-f "$src/$name"`
# follows symlinks too, so an aliased source was never rejected).
#
# Adding one more narrow audit per axis is what kept missing the next one, so
# the rules below are ONE enumerated contract, applied in one order, by both
# modes. A new rule goes in this list; it does not get bolted on elsewhere.
#
#   A. source topology   every component of the canonical references path is an
#                        existing, non-symlink directory.
#   B. source entries    every pinned name under it is a non-symlink REGULAR
#                        file. (`-f` alone follows symlinks — j#90378 R4-F2.)
#   C. dest topology     every EXISTING component of the mirror references path
#                        is a non-symlink directory. Missing components are fine
#                        in sync mode (it creates them) and are "mirror missing"
#                        in check mode — but an existing NON-directory component
#                        is a topology violation, not a missing mirror, because
#                        rerunning the sync cannot fix it (j#90378 R4-F3).
#   D. dest entry set    every DIRECT ENTRY of the mirror references directory —
#                        hidden or not, any extension, any type — is either a
#                        pinned name or a violation. This script's own stale
#                        temp is a named sub-case with its own disposition.
#   E. dest entry types  every pinned entry present is a non-symlink REGULAR
#                        file (no directory / FIFO / socket / device / symlink).
#   F. content parity    every pinned entry is byte-identical to its source.
#
# `--check` reports A-F and writes nothing. Sync enforces A-E BEFORE writing
# anything, clears its own stale temp, then replaces each pinned entry by
# rename. Refusing after a partial copy would leave a tree that neither the exit
# code nor the banner describes.
#
# Recovery guidance is per violation class. "Rerun the sync" is printed only for
# the classes a rerun actually clears; for the others it would send the operator
# to a command that exits 1 on the same tree (j#90322 F1).
#
# Modes:
#   default      replace each mirrored reference from canonical.
#   --check      dry-run; exit 0 when the mirror satisfies A-F, 1 otherwise.
#                Writes nothing. Designed for CI / `mozyo-bridge release check
#                drift` gating without modifying the worktree.

# The tracked partial-mirror reference set. `LegacyProjectSkillMirrorTest` pins
# the same set and cross-checks this list against it, so the two cannot drift
# apart. Adding or dropping a mirrored reference means editing BOTH this list
# and the test's MIRRORED_REFERENCES in the same commit.
MIRRORED_REFERENCES="project-map.md release.md safety.md workflow.md"

#: Prefix for this script's rename-staging file. It lives inside the mirror
#: reference directory, so rule D sees it: residue from a crashed run is a
#: reported violation rather than something hidden from the audit by its name
#: (j#90378 R4-F1 condition 2). Sync clears its own residue — deleting a file
#: this script created is not the reviewed decision that deleting an unpinned
#: reference is.
TEMP_PREFIX=".sync-legacy-project-skill.tmp."

usage() {
  cat <<USAGE
Usage: $0 [--check]

Without --check, replaces the mirrored reference set in
.claude/skills/mozyo-bridge-agent/references/ from canonical
(skills/mozyo-bridge-agent/references/).

With --check, verifies only and exits 1 if the mirror contract is violated.

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

src_relative="skills/mozyo-bridge-agent/references"
dest_relative=".claude/skills/mozyo-bridge-agent/references"
src="$repo_root/$src_relative"
dest="$repo_root/$dest_relative"

# One flag per violation class, so each disposition is printed only when its own
# class is present.
source_bad=0        # A + B
dest_topology_bad=0 # C (existing component that is not a real directory)
dest_missing=0      # C (nothing there yet)
unpinned_found=0    # D
stale_temp_found=0  # D, this script's own residue
entry_type_bad=0    # E
content_drift=0     # F

# --- rule helpers ----------------------------------------------------------

# Walk the components of a repo-relative path, testing each with `-L` (which
# does NOT follow) before `-d` (which does). `-d "$path"` on the full path is
# not a substitute: it followed a symlinked `references/` and reported the
# external directory as a healthy mirror (j#90342 R3-F1), and it returns false
# for an ENOTDIR further up, which the caller would otherwise misread as
# "missing" (j#90378 R4-F3).
#
# Sets `walk_symlink` / `walk_not_dir` / `walk_missing` for the caller.
walk_path_components() {
  walk_symlink=""
  walk_not_dir=""
  walk_missing=""
  _probe="$repo_root"
  _rest="$1"
  while [ -n "$_rest" ]; do
    _part="${_rest%%/*}"
    case "$_rest" in
      */*) _rest="${_rest#*/}" ;;
      *) _rest="" ;;
    esac
    _probe="$_probe/$_part"
    if [ -L "$_probe" ]; then
      walk_symlink="$_probe"
      return 0
    fi
    if [ ! -e "$_probe" ]; then
      walk_missing="$_probe"
      return 0
    fi
    if [ ! -d "$_probe" ]; then
      walk_not_dir="$_probe"
      return 0
    fi
  done
}

# Every direct entry of a directory: hidden entries included, `.`/`..` excluded,
# no-match globs excluded, and entry TYPE irrelevant (a dangling symlink is
# `-e`-false, so `-L` is tested too). Rule D depends on this being the full
# domain — restricting it to `*.md` is what let `unpinned.txt`,
# `.unpinned.md` and a stale temp through (j#90378 R4-F1).
list_direct_entries() {
  for _path in "$1"/* "$1"/.*; do
    { [ -e "$_path" ] || [ -L "$_path" ]; } || continue
    _base="${_path##*/}"
    case "$_base" in
      .|..) continue ;;
    esac
    printf '%s\n' "$_base"
  done
}

is_pinned() {
  for _name in $MIRRORED_REFERENCES; do
    if [ "$1" = "$_name" ]; then
      return 0
    fi
  done
  return 1
}

is_our_temp() {
  case "$1" in
    "$TEMP_PREFIX"*) return 0 ;;
  esac
  return 1
}

# --- rules -----------------------------------------------------------------

# A + B: the canonical source must be a real directory of real regular files.
# `[ -d "$src" ]` and `[ -f "$src/$name" ]` both FOLLOW symlinks, so an aliased
# source passed every earlier check and the sync copied external bytes into the
# mirror (j#90378 R4-F2). A canonical alias is never a valid content source: the
# fix is to restore the real path, not to mirror whatever it points at.
audit_source() {
  walk_path_components "$src_relative"
  if [ -n "$walk_symlink" ]; then
    echo "canonical source path component is a symlink: $walk_symlink" >&2
    source_bad=1
    return 0
  fi
  if [ -n "$walk_missing" ]; then
    echo "canonical source path component is missing: $walk_missing" >&2
    source_bad=1
    return 0
  fi
  if [ -n "$walk_not_dir" ]; then
    echo "canonical source path component is not a directory: $walk_not_dir" >&2
    source_bad=1
    return 0
  fi
  for name in $MIRRORED_REFERENCES; do
    path="$src/$name"
    if [ -L "$path" ]; then
      echo "canonical reference is a symlink: $src_relative/$name" >&2
      source_bad=1
    elif [ ! -e "$path" ]; then
      echo "canonical reference missing: $src_relative/$name" >&2
      source_bad=1
    elif [ ! -f "$path" ]; then
      echo "canonical reference is not a regular file: $src_relative/$name" >&2
      source_bad=1
    fi
  done
}

# C: the mirror path. An existing non-directory component is a topology
# violation; a missing one is only "mirror missing".
audit_dest_topology() {
  walk_path_components "$dest_relative"
  if [ -n "$walk_symlink" ]; then
    echo "legacy project skill mirror path component is a symlink: $walk_symlink" >&2
    dest_topology_bad=1
    return 0
  fi
  if [ -n "$walk_not_dir" ]; then
    echo "legacy project skill mirror path component is not a directory: $walk_not_dir" >&2
    dest_topology_bad=1
    return 0
  fi
  if [ -n "$walk_missing" ]; then
    dest_missing=1
  fi
}

# D: the exact entry set.
audit_dest_entry_set() {
  [ -d "$dest" ] || return 0
  for base in $(list_direct_entries "$dest"); do
    if is_pinned "$base"; then
      continue
    fi
    if is_our_temp "$base"; then
      echo "legacy project skill mirror holds a stale sync temp file: $dest_relative/$base" >&2
      stale_temp_found=1
      continue
    fi
    echo "legacy project skill mirror has an unpinned entry: $dest_relative/$base" >&2
    unpinned_found=1
  done
}

# E: the type of each pinned entry that is present.
audit_dest_entry_types() {
  [ -d "$dest" ] || return 0
  for name in $MIRRORED_REFERENCES; do
    path="$dest/$name"
    if [ -L "$path" ]; then
      echo "legacy project skill mirror reference is a symlink: $dest_relative/$name" >&2
      entry_type_bad=1
    elif [ -e "$path" ] && [ ! -f "$path" ]; then
      # `-f` is a stat, not an open, so this rejects a FIFO without blocking on
      # the open that `cp` would have performed (j#90342 R3-F1).
      echo "legacy project skill mirror reference is not a regular file: $dest_relative/$name" >&2
      entry_type_bad=1
    fi
  done
}

# F: content parity — the drift #14580 actually hit.
audit_content_parity() {
  [ -d "$dest" ] || return 0
  for name in $MIRRORED_REFERENCES; do
    path="$dest/$name"
    if [ -L "$path" ] || { [ -e "$path" ] && [ ! -f "$path" ]; }; then
      continue # rule E already reported this entry; cmp would follow the link.
    fi
    if [ ! -e "$path" ]; then
      echo "legacy project skill mirror missing file: $dest_relative/$name" >&2
      content_drift=1
      continue
    fi
    # `set -e` would abort on cmp's non-zero exit, so branch on it explicitly.
    if cmp -s "$src/$name" "$path"; then
      continue
    fi
    echo "legacy project skill mirror drift detected: $dest_relative/$name differs from canonical" >&2
    content_drift=1
  done
}

# --- dispositions ----------------------------------------------------------

resync_recovery() {
  echo "Rerun 'scripts/sync_legacy_project_skill.sh' (no --check, from the repo root) to resync the mirror." >&2
}

source_disposition() {
  echo "The canonical body at $src_relative must be real directories and regular files." >&2
  echo "Restore the tracked canonical path (git restore / checkout); do NOT point it at an" >&2
  echo "external location. This script refuses rather than mirror bytes from an aliased" >&2
  echo "source, which would silently publish content the repo does not track." >&2
}

dest_topology_disposition() {
  echo "The mirror path $dest_relative must be real directories all the way down." >&2
  echo "Replace the offending component with a real directory (or restore it from git)," >&2
  echo "then rerun this script. Rerunning it now cannot fix the path it has to write into," >&2
  echo "and syncing through an alias would write outside the mirror." >&2
}

unpinned_disposition() {
  echo "Unpinned entries need a reviewed disposition: either delete them, or add them to" >&2
  echo "MIRRORED_REFERENCES in this script AND in" >&2
  echo "tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py." >&2
  echo "Rerunning this script does NOT clear them — it refuses while they are present." >&2
}

stale_temp_disposition() {
  echo "The stale temp file is residue from an interrupted sync. Rerunning this script" >&2
  echo "(no --check) clears it before writing; it is reported rather than ignored so a" >&2
  echo "crashed run never leaves the mirror looking clean." >&2
}

entry_type_disposition() {
  echo "Mirror references must be regular files — not symlinks, directories, FIFOs," >&2
  echo "sockets or devices. Replace the offending entry with a regular file (or delete" >&2
  echo "it), then rerun this script. Syncing over a symlink or a hardlink would write" >&2
  echo "through it into the link target, so this script refuses instead." >&2
}

# --- check mode ------------------------------------------------------------

if [ "$check_only" -eq 1 ]; then
  audit_source
  audit_dest_topology
  audit_dest_entry_set
  audit_dest_entry_types
  audit_content_parity

  violations=0
  [ "$source_bad" -eq 0 ] || violations=1
  [ "$dest_topology_bad" -eq 0 ] || violations=1
  [ "$unpinned_found" -eq 0 ] || violations=1
  [ "$stale_temp_found" -eq 0 ] || violations=1
  [ "$entry_type_bad" -eq 0 ] || violations=1
  [ "$content_drift" -eq 0 ] || violations=1

  if [ "$dest_missing" -ne 0 ] && [ "$dest_topology_bad" -eq 0 ]; then
    echo "legacy project skill mirror missing: $dest" >&2
    violations=1
  fi

  if [ "$violations" -ne 0 ]; then
    echo "" >&2
    # Only the classes a rerun actually clears get the rerun line.
    if [ "$content_drift" -ne 0 ] || { [ "$dest_missing" -ne 0 ] && [ "$dest_topology_bad" -eq 0 ] && [ "$source_bad" -eq 0 ]; }; then
      resync_recovery
    fi
    [ "$source_bad" -eq 0 ] || source_disposition
    [ "$dest_topology_bad" -eq 0 ] || dest_topology_disposition
    [ "$unpinned_found" -eq 0 ] || unpinned_disposition
    [ "$stale_temp_found" -eq 0 ] || stale_temp_disposition
    [ "$entry_type_bad" -eq 0 ] || entry_type_disposition
    exit 1
  fi

  echo "legacy project skill mirror is up to date"
  echo "  source: $src"
  echo "  destination: $dest"
  exit 0
fi

# --- sync mode -------------------------------------------------------------

# Enforce A-E before writing anything. Every one of these is a state the sync
# cannot resolve, and reporting success in their presence is the defect all
# three reviews found.
audit_source
audit_dest_topology
audit_dest_entry_set
audit_dest_entry_types

if [ "$source_bad" -ne 0 ] || [ "$dest_topology_bad" -ne 0 ] \
  || [ "$unpinned_found" -ne 0 ] || [ "$entry_type_bad" -ne 0 ]; then
  echo "refusing to sync the legacy project skill mirror; nothing was written." >&2
  [ "$source_bad" -eq 0 ] || source_disposition
  [ "$dest_topology_bad" -eq 0 ] || dest_topology_disposition
  [ "$unpinned_found" -eq 0 ] || unpinned_disposition
  [ "$entry_type_bad" -eq 0 ] || entry_type_disposition
  exit 1
fi

mkdir -p "$dest"

# Clear this script's own residue from an interrupted run. Scoped to the exact
# prefix, inside our own directory — not the reviewed decision that deleting an
# unpinned reference is.
if [ "$stale_temp_found" -ne 0 ]; then
  for base in $(list_direct_entries "$dest"); do
    if is_our_temp "$base"; then
      echo "clearing stale sync temp file: $dest_relative/$base"
      rm -f "$dest/$base"
    fi
  done
fi

# Replace by rename, never by writing into the existing entry.
#
# `cp "$src/$name" "$dest/$name"` opens and truncates whatever the destination
# name resolves to. Reaching an unrelated file's inode through a HARDLINK under
# a pinned name rewrote that file (j#90342 R3-F1) — and a hardlink is a regular
# file, so rule E cannot see it. Copying into a fresh temp and renaming it into
# place replaces the DIRECTORY ENTRY: the old inode keeps its content and its
# other names, and the swap is atomic, so an interrupted sync never leaves a
# half-written reference. The temp lives in the destination directory so the
# rename is a same-filesystem operation.
tmp="$dest/$TEMP_PREFIX$$"
trap 'rm -f "$tmp"' EXIT HUP INT TERM
for name in $MIRRORED_REFERENCES; do
  rm -f "$tmp"
  cp "$src/$name" "$tmp"
  # `cp` into a new file takes the umask; pin the mode so a restrictive umask
  # cannot leave the mirror less readable than the canonical body it copies.
  # 644 matches the tracked mode of both canonical and mirror references.
  chmod 644 "$tmp"
  mv -f "$tmp" "$dest/$name"
done
trap - EXIT HUP INT TERM

echo "synced legacy project skill mirror"
echo "  source: $src"
echo "  destination: $dest"
echo "  references: $MIRRORED_REFERENCES"
echo "  SKILL.md adapter stub left untouched (intentional divergence)"
