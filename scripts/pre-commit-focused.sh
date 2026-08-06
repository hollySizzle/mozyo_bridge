#!/bin/sh
# Opt-in pre-commit focused verification (Redmine #13079).
#
# Runs the cheap, staged-scoped checks an implementer wants BEFORE every
# commit, in seconds:
#
#   1. `git diff --cached --check`             — whitespace / conflict markers;
#   2. `mozyo-bridge docs audit-impact --staged --check-generated`
#                                              — staged docs/catalog impact
#                                                (skipped when the repo has no
#                                                docs catalog);
#   3. `mozyo-bridge tests resolve --staged`   — the #12752/#13078 impact
#      resolver picks the focused tests for the staged paths and runs them.
#
# Boundaries (see vibes/docs/logics/pre-commit-focused-verification.md):
#
# - OPT-IN ONLY. Nothing installs this automatically; adoption is a per-repo
#   operator action (see the adoption doc for install / uninstall).
# - THE FULL SUITE NEVER RUNS HERE. When the resolver fail-closes to a `full`
#   recommendation, this hook prints the command and SKIPS it: a ~35s hook
#   invites `--no-verify` habits, which is worse than no hook. The full suite
#   stays a pre-push / CI duty (Redmine #12753), and this hook is NOT the
#   governed Required Verification (that rule surface is Redmine #13080).
# - Bypass: `git commit --no-verify` skips the hook entirely (standard git).
#
# Env overrides:
#   MOZYO_BRIDGE_CMD  — command used for the mozyo-bridge CLI (default:
#                       `mozyo-bridge` on PATH, else `python3 -m mozyo_bridge`
#                       with PYTHONPATH pointing at this repo's src/).
#   MOZYO_PYTHON      — python interpreter for the focused unittest run
#                       (default: python3).

set -eu

say() { printf '%s\n' "pre-commit-focused: $*"; }
fail() { say "FAIL: $*" >&2; exit 1; }

PYTHON="${MOZYO_PYTHON:-python3}"

# Resolve the mozyo-bridge CLI: installed entry point first, else the package
# source next to this script (the script lives in <repo>/scripts/).
if [ -n "${MOZYO_BRIDGE_CMD:-}" ]; then
    MOZYO="$MOZYO_BRIDGE_CMD"
elif command -v mozyo-bridge >/dev/null 2>&1; then
    MOZYO="mozyo-bridge"
else
    SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    if [ -d "$SCRIPT_DIR/../src/mozyo_bridge" ]; then
        PYTHONPATH="$SCRIPT_DIR/../src${PYTHONPATH:+:$PYTHONPATH}"
        export PYTHONPATH
        MOZYO="$PYTHON -m mozyo_bridge"
    else
        fail "mozyo-bridge CLI not found (install it or set MOZYO_BRIDGE_CMD)"
    fi
fi

# --- 1. whitespace / conflict-marker check over the staged diff. -------------
say "git diff --cached --check"
git diff --cached --check || fail "staged diff has whitespace/conflict problems"

# --- 2. staged docs/catalog impact (governed repos only). --------------------
if [ -f ".mozyo-bridge/docs/catalog.yaml" ]; then
    say "docs audit-impact --staged --check-generated"
    $MOZYO docs audit-impact --staged --check-generated --repo . \
        || fail "staged docs impact check failed"
else
    say "no docs catalog; skipping docs audit-impact"
fi

# --- 3. focused tests for the staged paths (#12752/#13078 resolver). ---------
say "tests resolve --staged"
TARGETS=$($MOZYO tests resolve --staged --format targets) \
    || fail "tests resolve failed"

if [ -z "$TARGETS" ]; then
    say "resolver selected no targets; nothing to run"
    exit 0
fi

case "$TARGETS" in
    discover*)
        # Fail-closed full recommendation: the hook never runs the full suite
        # (boundary above). Surface the duty loudly and let the commit pass —
        # the full run belongs to pre-push / CI, not to a per-commit hook.
        say "resolver recommends the FULL suite for these staged paths."
        say "NOT running it in the hook; run it before push:"
        say "  $MOZYO tests run"
        exit 0
        ;;
esac

say "running focused tests:"
printf '%s\n' "$TARGETS" | sed 's/^/  - /'

# Focused tests run through `tests run` (Redmine #14757), not bare `python -m
# unittest`: a focused run reaches the operator's shared mozyo-bridge home just as
# a full one does (#14757 j#94599 recorded a focused run migrating the shared
# store v8 -> v9). `tests run` pins a task-specific temp root for the run and
# fails if the operator's home changed, and it passes these targets through to
# the identical `python -m unittest` invocation.
#
# The resolved CLI may predate `tests run` (this hook prefers an installed
# `mozyo-bridge` on PATH over the repo's own source, per #13079). An earlier
# version of this hook fell back to a bare `python -m unittest` with a warning;
# review #14757 j#100408 finding_3 rejected that, because a warning does not
# prevent a shared-home mutation and j#94599 requires EVERY test process a
# documented helper starts to be isolated. So: try the repo's own committed CLI
# next (it necessarily has the subcommand), and if that is unavailable too, FAIL —
# never run un-isolated.
if ! $MOZYO tests run --help >/dev/null 2>&1; then
    say "resolved mozyo-bridge CLI has no \`tests run\`; trying this repo's source CLI"
    SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    if [ -d "$SCRIPT_DIR/../src/mozyo_bridge" ]; then
        REPO_SRC=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)/src
        MOZYO="env PYTHONPATH=$REPO_SRC${PYTHONPATH:+:$PYTHONPATH} $PYTHON -m mozyo_bridge"
    fi
fi

if ! $MOZYO tests run --help >/dev/null 2>&1; then
    fail "no mozyo-bridge CLI with \`tests run\` is available, so the focused tests
  cannot be isolated from your shared mozyo-bridge home (Redmine #14757). This hook
  will NOT run them un-isolated. Upgrade mozyo-bridge, or point MOZYO_BRIDGE_CMD at
  a CLI that has \`tests run\`. To commit without this check: git commit --no-verify."
fi

# shellcheck disable=SC2086  # targets are newline-separated unittest args
printf '%s\n' "$TARGETS" | xargs $MOZYO tests run --repo . -- \
    || fail "focused tests failed"

say "OK"
