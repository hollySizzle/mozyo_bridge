# mozyo-bridge Bootstrap Guide

LLM-first bootstrap for setting up `mozyo-bridge` from a clean machine through
project initialization. Optimized for Claude / Codex agent execution: each
stage names exact commands, expected success signals, and the failure branch.

This is the canonical entrypoint for "install + project bootstrap". Read this
BEFORE the install sections of `README.md`, `skill-distribution.md`, or
`scaffold-rules.md`. Those docs remain the authoritative reference for their
specific surfaces; this doc orders the stages and links into them.

The doc is also human-readable, but it does not duplicate prose available
elsewhere. When a stage points at another doc, prefer following the link
rather than restating its contents here.

## Scope

In scope:

- CLI install (primary: PyPI via `pipx`).
- user-global rules install (`mozyo-bridge rules install`).
- agent skill install:
  - Claude Code primary path: plugin marketplace.
  - Codex primary path: `$skill-installer` against the canonical GitHub skill path.
  - curl/script install as fallback only.
- project router scaffold (`mozyo-bridge scaffold rules <preset>`).
- bootstrap verification (`mozyo-bridge doctor`, `--target`, `--json`).
- per-preset isolated target smoke under `./tmp/mb-smoke-*` (non-destructive).
- failure recovery for the symptoms an LLM is most likely to observe.

Out of scope (covered by sibling docs):

- release flow (`vibes/docs/logics/release-flow.md`).
- destructive post-release acceptance (`vibes/docs/logics/turnkey-e2e-acceptance.md`).
- runtime usage and notification commands (root `README.md` `Notification Commands`).
- agent work-rules and ticket-system workflow (`vibes/docs/rules/agent-workflow.md`).
- release helper invocations (`mozyo-bridge release …` is out of bootstrap; see release-flow.md).

## Stage 0 — Prerequisites

```bash
command -v tmux
command -v python3
python3 --version
command -v pipx
```

Expected:

- `tmux` resolves to an installed path.
- `python3 --version` reports `3.10` or newer (`mozyo-bridge` requires Python >= 3.10).
- `pipx` resolves to an installed path.

If a signal is missing:

- `tmux` missing → install via the OS package manager (for example `brew install tmux`, `apt install tmux`). `mozyo-bridge --help` works without tmux; every pane / notification command requires it.
- `python3` < 3.10 → install a newer Python before continuing.
- `pipx` missing → see Stage 1 fallback.

For agent skill install, also check:

```bash
command -v claude
command -v codex
```

Either or both may be present. Claude plugin install needs `claude`; Codex `$skill-installer` needs `codex`.

## Stage 1 — Install the CLI

Primary path:

```bash
pipx install mozyo-bridge
```

Expected success signal:

```bash
mozyo-bridge --version
# prints: mozyo-bridge <X.Y.Z>
```

The current GA version line is `0.2.0`. Any current `X.Y.Z` from PyPI is acceptable here; the doctor in Stage 5 is the substantive check.

Fallback (no `pipx`):

```bash
python3 -m pip install --user mozyo-bridge
python3 -m mozyo_bridge --version
```

This is a fallback because it installs into the user-site Python environment without isolation. Prefer `pipx`.

There is no `curl ... | sh` install path for the CLI. The only curl-based install paths in this project are the agent skill scripts in Stage 3, and those are fallback only.

## Stage 2 — Install user-global rules

```bash
mozyo-bridge rules install
mozyo-bridge rules status
```

Expected `rules status` output:

- header line `PRESET STATUS INSTALLED PACKAGED PATH`.
- one row per preset (`asana`, `redmine`, `none`), each with `STATUS=ok`.
- exit code `0`.

If `STATUS != ok` or exit is non-zero:

- the central preset store under `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/` is missing or has drifted.
- re-run `mozyo-bridge rules install`.
- if `MOZYO_BRIDGE_HOME` is set, install routes to that path instead of `~/.mozyo_bridge`. Confirm the env value matches the path you expect.

## Stage 3 — Install agent skills

### 3a. Claude Code — primary: plugin marketplace

```bash
claude plugin marketplace add hollySizzle/mozyo_bridge
claude plugin install mozyo-bridge-agent@mozyo-bridge --scope user
```

Expected success signals:

```bash
claude plugin marketplace list   # must list `mozyo-bridge`
claude plugin list               # must list `mozyo-bridge-agent@mozyo-bridge`
```

If either listing is missing:

- the marketplace `add` may have failed silently (network / auth). Re-run `add`, then `install`.

Plugin skills are namespaced `mozyo-bridge-agent:mozyo-bridge-agent`, so they do not collide with same-name personal (`~/.claude/skills/`) or project (`<project>/.claude/skills/`) skills. This is the canonical reason to prefer the marketplace path over the curl fallback.

### 3b. Codex — primary: `$skill-installer`

In Codex, run `$skill-installer` and point it at the canonical GitHub skill path:

```
https://github.com/hollySizzle/mozyo_bridge/tree/main/skills/mozyo-bridge-agent
```

Expected install destination:

- `${CODEX_HOME:-$HOME/.codex}/skills/mozyo-bridge-agent/`.
- inside, `SKILL.md`, `references/`, and `agents/openai.yaml` must exist.

If the install is incomplete:

- check `${CODEX_HOME:-$HOME/.codex}` for a partial directory; remove it and retry.
- override the source ref via the `MOZYO_BRIDGE_SKILL_REF` env var (default = `main`).

### 3c. Fallback only — curl-based install scripts

Use the curl scripts only when the primary paths above are not available (offline mirrors, internal forks, fresh-tester acceptance smoke):

```bash
# Codex skill (fallback)
curl -fsSL https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/main/scripts/install_codex_skill.sh | sh

# Claude Code skill (fallback, user-global)
curl -fsSL https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/main/scripts/install_claude_skill.sh \
  -o /tmp/install_mozyo_bridge_claude_skill.sh
MOZYO_BRIDGE_CLAUDE_SCOPE=global sh /tmp/install_mozyo_bridge_claude_skill.sh
```

Do NOT pipe Claude install as `MOZYO_BRIDGE_CLAUDE_SCOPE=global curl … | sh`. The env var goes to `curl`, not to the `sh` that runs the script, and the install silently falls back to `scope=project`.

Same-name skill precedence trap (Claude Code only): personal skill (`~/.claude/skills/`) overrides project skill (`<project>/.claude/skills/`). The plugin marketplace path avoids this trap entirely.

After ANY skill install path: restart Claude Code / Codex so the new skill index is loaded. Same-session skill index is cached and a fresh skill will not appear without a restart.

For deeper packaging / precedence details, read `vibes/docs/logics/skill-distribution.md`.

## Stage 4 — Scaffold a project

Choose a preset based on the project's ticket system:

- `asana` — Asana-driven projects (most current mozyo-bridge work).
- `redmine` — Redmine-driven projects.
- `none` — projects with no ticket system gate.

```bash
cd /path/to/your-project
mozyo-bridge scaffold rules <preset>
```

Expected files written:

- `AGENTS.md`
- `CLAUDE.md`
- `.mozyo-bridge/scaffold.json`

If `AGENTS.md` or `CLAUDE.md` already exists:

- the helper refuses to overwrite by default.
- preview with `--dry-run`.
- replace and keep backups with `--backup`.
- replace without backups with `--force`.

To scaffold a different directory than cwd:

```bash
mozyo-bridge scaffold rules <preset> --target /path/to/project
```

Do NOT run `mozyo-bridge scaffold rules <preset>` without `--target` from inside the `mozyo_bridge` source repo itself. The helper would target the source repo's own `AGENTS.md` / `CLAUDE.md`. When smoke-testing from a source checkout, use the per-preset isolated targets under `./tmp/mb-smoke-<preset>/` (Stage 6).

For preset semantics and manifest invariants, read `vibes/docs/logics/scaffold-rules.md`.

## Stage 5 — Verify with `doctor`

```bash
mozyo-bridge doctor
```

For a specific scaffolded target:

```bash
mozyo-bridge doctor --target /path/to/project
```

Expected:

- exit code `0`.
- 6 sections checked: `cli`, `rules`, `codex_skill`, `claude_skill`, `scaffold`, `tmux`.
- all `ok` when both primary skill paths and a scaffolded target are in place.

Expected exception when the Claude primary path (plugin marketplace) is used WITHOUT the curl fallback:

- `claude_skill: missing` is the expected state, not a failure.
- `mozyo-bridge doctor` currently scans only `~/.claude/skills/` and `<project>/.claude/skills/`; it does not scan `~/.claude/plugins/cache/` yet. Verify the Claude plugin install via `claude plugin list` (Stage 3a) instead.

For machine-readable gating:

```bash
mozyo-bridge doctor --json
mozyo-bridge doctor --target /path/to/project --json
```

Output shape:

```json
{"ok": <bool>, "sections": {"cli": {...}, "rules": {...}, "codex_skill": {...}, "claude_skill": {...}, "scaffold": {...}, "tmux": {...}}}
```

CI gate examples: `jq -e '.sections.scaffold.status == "ok"'`, `jq -e '.ok'`. Exit code is non-zero when `ok` is false. Do NOT gate on `jq '.sections.claude_skill.status == "ok"'` when only the plugin marketplace path is in use (see above).

If any section is `missing` or `drifted`, read its `next_action` field. The CLI prints the next command to run; follow it.

## Stage 6 — Isolated target smoke

Run BOTH presets, not just the one used by the current project. Preset boundary defects often only show up when the other preset is exercised.

```bash
mkdir -p ./tmp/mb-smoke-asana
mozyo-bridge scaffold rules asana --target ./tmp/mb-smoke-asana
mozyo-bridge scaffold status --target ./tmp/mb-smoke-asana
mozyo-bridge doctor --target ./tmp/mb-smoke-asana

mkdir -p ./tmp/mb-smoke-redmine
mozyo-bridge scaffold rules redmine --target ./tmp/mb-smoke-redmine
mozyo-bridge scaffold status --target ./tmp/mb-smoke-redmine
mozyo-bridge doctor --target ./tmp/mb-smoke-redmine
```

Expected per target:

- `scaffold status` exits `0` with the line `result: clean`.
- `doctor --target` reports the `scaffold` section as `ok`.

If either preset fails:

- check that `mozyo-bridge rules install` (Stage 2) succeeded.
- check that `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/<preset>/agent-workflow.md` exists.
- re-run `mozyo-bridge rules install` to refresh the central store.

This smoke is non-destructive and isolated under `./tmp/`. It is NOT the same as the destructive post-release acceptance test in `vibes/docs/logics/turnkey-e2e-acceptance.md` (which deletes the source repo's own routers and verifies recovery). Do not invoke turnkey acceptance as a routine bootstrap step.

## Stage 7 — Failure recovery and common pitfalls

The symptoms below are the ones an LLM is most likely to observe while executing this guide. For each, the entry names the likely cause and the next command.

- `doctor` reports `cli: ok` but `rules: missing`:
  - Stage 2 was skipped or `MOZYO_BRIDGE_HOME` points elsewhere.
  - run `mozyo-bridge rules install`; if `MOZYO_BRIDGE_HOME` is set, pass `--home <path>` to match.
- `doctor` reports `claude_skill: missing` after only the plugin marketplace install:
  - expected state. Verify with `claude plugin list` instead.
- `doctor` reports `claude_skill: ok` AND a project skill exists in `<project>/.claude/skills/mozyo-bridge-agent/`:
  - personal (`~/.claude/skills/`) overrides project. The project copy is shadowed.
  - keep ONE path. The plugin marketplace path avoids the conflict.
- `mozyo-bridge scaffold rules <preset>` refused to overwrite existing routers:
  - protection is intentional. Inspect with `--dry-run`, then replace via `--backup` (keep originals) or `--force` (no backup).
- `doctor` reports `tmux: missing`:
  - `tmux` is not on `PATH`. Install via the OS package manager.
- every section reports `ok` but agents still do not see the rules / skill:
  - restart Claude Code / Codex. Same-session skill index is cached.
- pane / notification commands fail with "no agent windows":
  - run bare `mozyo` from the repo root: `cd /path/to/repo && mozyo`. This creates `claude` and `codex` windows in a repo-scoped tmux session.
  - for an existing pane (VS Code tmux terminal, hand-managed tmux pane), run `mozyo-bridge init <agent>` from inside that pane to rename its window.

## Where this doc sits relative to the others

- **This doc** owns the bootstrap stage order from a fresh machine through a working scaffold + verified doctor + per-preset isolated smoke.
- `README.md` `Quick Start` / `Beta Tester Install` / `Agent Skill Install` / `Agent Rules Scaffold` are the operator reference for the individual commands. Read README when you need a flag detail; read this bootstrap doc when you need the stage order.
- `vibes/docs/logics/skill-distribution.md` is the source of truth for skill packaging, precedence, marketplace metadata, and drift.
- `vibes/docs/logics/scaffold-rules.md` is the source of truth for scaffold preset semantics and manifest invariants.
- `vibes/docs/logics/turnkey-e2e-acceptance.md` is the destructive post-release acceptance; not a bootstrap step.
- `vibes/docs/logics/release-flow.md` is the release operator guide. Bootstrap does not invoke release helpers.
- `vibes/docs/rules/agent-workflow.md` is the per-ticket-system work rules; bootstrap stops at "the project can route Claude / Codex against its ticket system" and hands off to that doc.

## Pinning bootstrap to a specific release

Routine bootstrap does not need to pin to a release tag — running `pipx install mozyo-bridge` plus the primary skill paths against `main` is the documented experience.

For post-release acceptance, the install ref MUST match the package version. See `vibes/docs/logics/turnkey-e2e-acceptance.md` `Install the published package` for the pinned form (`MOZYO_BRIDGE_SKILL_REF=v<X.Y.Z>` combined with the matching `pipx install`).
