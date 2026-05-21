# mozyo-bridge Bootstrap Guide

LLM-first bootstrap for installing or updating `mozyo-bridge` through project
initialization. Optimized for Claude / Codex agent execution: each stage names
exact commands, expected success signals, and the failure branch.

This is the canonical entrypoint for "install/update + project bootstrap". Read
this BEFORE the install sections of `README.md`, `skill-distribution.md`, or
`scaffold-rules.md`. Those docs remain the authoritative reference for their
specific surfaces; this doc orders the stages and links into them.

The doc is also human-readable, but it does not duplicate prose available
elsewhere. When a stage points at another doc, prefer following the link
rather than restating its contents here.

## Scope

In scope:

- CLI install (primary: PyPI via `pipx`).
- existing CLI / rules / scaffold update after a release ships.
- user-global rules install (`mozyo-bridge rules install`).
- agent skill install:
  - Claude Code primary path: plugin marketplace.
  - Codex primary path: `$skill-installer` against the canonical GitHub skill path.
  - Codex skill install is a user/operator action in the Codex environment.
  - curl/script install is prohibited in bootstrap.
- project router scaffold (`mozyo-bridge scaffold apply <preset>`).
- governed scaffold catalog setup (`catalog.yaml.example` -> `catalog.yaml`) and
  `mozyo-bridge docs ...` verification for projects that opt into
  `redmine-rails-governed`.
- bootstrap verification (`mozyo-bridge doctor`, `--target`, `--json`).
- per-preset isolated target smoke under `./tmp/mb-smoke-*` (non-destructive).
- failure recovery for the symptoms an LLM is most likely to observe.

Out of scope (covered by sibling docs):

- release flow (`vibes/docs/logics/release-flow.md`).
- destructive post-release acceptance (`vibes/docs/logics/turnkey-e2e-acceptance.md`).
- runtime usage and notification commands (root `README.md` `Notification Commands`).
- agent work-rules and ticket-system workflow (`vibes/docs/rules/agent-workflow.md`).
- release helper invocations (`mozyo-bridge release …` is out of bootstrap; see release-flow.md).

## Path selection — fresh install vs existing update

Use this decision point before running the staged flow:

- If the host does not have `mozyo-bridge`, start at Stage 0 and continue
  through Stage 6.
- If `mozyo-bridge` is already installed and a project already has
  `.mozyo-bridge/scaffold.json`, use the Existing Install Update section below.
  Do not repeat the full bootstrap unless the host or project is being rebuilt
  from scratch.
- If the CLI exists but the project was never scaffolded, upgrade the CLI first,
  then continue at Stage 2 for central mode or Stage 4 for repo-local mode.

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

Do not hard-code a release expectation in bootstrap. Any current `X.Y.Z` from
PyPI is acceptable here; `mozyo-bridge doctor` and the command-surface smoke in
later stages are the substantive checks. When testing an unreleased checkout,
use the GitHub-main / editable install path documented in README instead of
pretending that unreleased commands are already on PyPI.

Fallback (no `pipx`):

```bash
python3 -m pip install --user mozyo-bridge
python3 -m mozyo_bridge --version
```

This is a fallback because it installs into the user-site Python environment without isolation. Prefer `pipx`.

There is no `curl ... | sh` install path for the CLI. Bootstrap also forbids curl/script install for agent skills; use the primary paths in Stage 3.

## Existing Install Update

Use this path after a `mozyo-bridge` release ships and the user already has a
scaffolded project. This updates three different surfaces: the installed CLI,
the preset store agents read at runtime, and each target repo's scaffold
manifest/artifacts.

Central preset store:

```bash
pipx upgrade mozyo-bridge
mozyo-bridge --version
mozyo-bridge rules install
mozyo-bridge rules status

cd /path/to/scaffolded-project
mozyo-bridge scaffold status --target .
mozyo-bridge scaffold apply <preset> --target . --backup
mozyo-bridge scaffold status --target .
mozyo-bridge doctor --target .
```

Repo-local preset store:

```bash
pipx upgrade mozyo-bridge
mozyo-bridge --version

cd /path/to/scaffolded-project
mozyo-bridge rules install --repo-local .
mozyo-bridge scaffold status --target .
mozyo-bridge scaffold apply <preset> --target . --repo-local --backup
mozyo-bridge scaffold status --target .
mozyo-bridge doctor --target .
```

Important update rules:

- `scaffold status` is the decision point. If it is clean, the repo already
  matches the currently installed preset. If it reports drift, inspect the diff
  and accept the new shipped guardrails with `scaffold apply <preset> --backup`.
- Use repo-local mode when the target environment may not preserve
  `~/.mozyo_bridge` (Dev Container, Codespace, or other ephemeral-home
  workspaces). Do not pass `--home` to a repo-local manifest.
- `catalog.yaml` is target-owned data and is not overwritten by scaffold
  re-apply. The shipped `catalog.yaml.example` may be refreshed by scaffold.
- `redmine-rails-governed` no longer vendors docs tooling as
  `.mozyo-bridge/tools/*.py`; use `mozyo-bridge docs ...` from the installed
  package.
- From v0.5.0 onward, `agent-workflow.md` is the governed execution contract.
  Older scaffold-managed artifacts such as
  `.mozyo-bridge/rules/development_flow.md` and
  `.mozyo-bridge/tools/*.py` may be removed by outgoing reconcile when
  `--backup` or `--force` is used. Prefer `--backup` so local state is preserved
  as `.bak.<timestamp>`.
- `doctor` may report the Claude Nagger skeleton separately from whether the
  operator has copied `.claude-nagger/config.yaml.example` to
  `.claude-nagger/config.yaml`. A skeleton-only state is an integration prompt,
  not scaffold drift.

If `pipx upgrade mozyo-bridge` fails because the local pipx backend cannot
install the package, retry with pipx's pip backend using a forced install of the
desired release:

```bash
pipx install --force --backend pip mozyo-bridge==<X.Y.Z>
```

Use the exact version only when pinning to a known release. For routine updates,
prefer unpinned `pipx upgrade mozyo-bridge`.

## Stage 2 — Install user-global rules

```bash
mozyo-bridge rules install
mozyo-bridge rules status
```

Expected `rules status` output:

- header line `PRESET STATUS INSTALLED PACKAGED PATH`.
- one row per packaged preset (`asana`, `redmine`, `redmine-rails`,
  `redmine-rails-governed`, `none`), each with `STATUS=ok`.
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

### 3b. Codex — primary: `$skill-installer` (user-run)

The bootstrap agent must instruct the user/operator to run `$skill-installer` inside the Codex environment. Do not treat this as an agent-executed step. Continue only after the installed files can be verified.

Command the user must run:

```
$skill-installer https://github.com/hollySizzle/mozyo_bridge/tree/main/skills/mozyo-bridge-agent
```

Expected install destination:

- `${CODEX_HOME:-$HOME/.codex}/skills/mozyo-bridge-agent/`.
- inside, `SKILL.md`, `references/`, and `agents/openai.yaml` must exist.

If the install is incomplete:

- check `${CODEX_HOME:-$HOME/.codex}` for a partial directory; remove it and retry.
- override the source ref via the `MOZYO_BRIDGE_SKILL_REF` env var (default = `main`).

### 3c. Prohibited in bootstrap — curl/script install paths

Do NOT use the curl/script install paths during bootstrap. They are legacy/manual paths and are not part of the canonical bootstrap flow.

Same-name skill precedence trap (Claude Code only): personal skill (`~/.claude/skills/`) overrides project skill (`<project>/.claude/skills/`). The plugin marketplace path avoids this trap entirely.

After ANY skill install path: restart Claude Code / Codex so the new skill index is loaded. Same-session skill index is cached and a fresh skill will not appear without a restart.

For deeper packaging / precedence details, read `vibes/docs/logics/skill-distribution.md`.

## Stage 4 — Scaffold a project

Choose a preset based on the project's ticket system:

- `asana` — Asana-driven projects (most current mozyo-bridge work).
- `redmine` — Redmine-driven projects.
- `redmine-rails` — Redmine-driven Rails projects that want thin routers and
  project-local governance filled in by the target repo.
- `redmine-rails-governed` — Redmine-driven Rails projects that want the full
  repo-local governance package, docs catalog skeleton, Claude Nagger skeleton,
  and tmux UI artifact up front.
- `none` — projects with no ticket system gate.

```bash
cd /path/to/your-project
mozyo-bridge scaffold apply <preset>
```

Expected files written:

- `AGENTS.md`
- `CLAUDE.md`
- `.mozyo-bridge/scaffold.json`

When the target environment may not persist `~/.mozyo_bridge` (Dev Container,
Codespace, or other ephemeral-home workspaces), use repo-local mode instead of
the central home store:

```bash
mozyo-bridge rules install --repo-local /path/to/your-project
mozyo-bridge scaffold apply <preset> --target /path/to/your-project --repo-local
```

Repo-local mode writes the preset store under
`<repo>/.mozyo-bridge/rules/presets/` and generated routers point at that
repo-relative path. `scaffold status` auto-detects the mode from the manifest;
do not pass `--home` to a repo-local manifest.

For `redmine-rails-governed`, initialize the docs catalog after scaffold:

```bash
cp .mozyo-bridge/docs/catalog.yaml.example .mozyo-bridge/docs/catalog.yaml
mozyo-bridge docs validate --repo .
mozyo-bridge docs validate --check-file-coverage --repo .
```

Then update `.mozyo-bridge/docs/catalog.yaml` with project-specific
`documents`, `related_document_refs`, `file_conventions`, and optional
`coverage_roots`. The configured `catalog.yaml` is target-owned data and is not
overwritten by scaffold re-apply. The tooling is not copied into the target repo
as `.mozyo-bridge/tools/*.py`; it is provided by the installed
`mozyo-bridge docs ...` CLI.

If `AGENTS.md` or `CLAUDE.md` already exists:

- the helper refuses to overwrite by default.
- preview with `--dry-run`.
- replace and keep backups with `--backup`.
- replace without backups with `--force`.

To scaffold a different directory than cwd:

```bash
mozyo-bridge scaffold apply <preset> --target /path/to/project
```

Do NOT run `mozyo-bridge scaffold apply <preset>` without `--target` from inside the `mozyo_bridge` source repo itself. The helper would target the source repo's own `AGENTS.md` / `CLAUDE.md`. When smoke-testing from a source checkout, use the per-preset isolated targets under `./tmp/mb-smoke-<preset>/` (Stage 6).

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

Expected state when the Claude primary path (plugin marketplace) is used:

- `mozyo-bridge doctor` scans the plugin cache as well as legacy
  `~/.claude/skills/` and project-local skill directories.
- A plugin-only install is healthy when the `claude_skill` section reports the
  plugin-managed skill as present. `claude plugin list` remains the direct
  Claude-side confirmation command from Stage 3a.

For machine-readable gating:

```bash
mozyo-bridge doctor --json
mozyo-bridge doctor --target /path/to/project --json
```

Output shape:

```json
{"ok": <bool>, "sections": {"cli": {...}, "rules": {...}, "codex_skill": {...}, "claude_skill": {...}, "scaffold": {...}, "tmux": {...}}}
```

CI gate examples: `jq -e '.sections.scaffold.status == "ok"'`, `jq -e '.ok'`. Exit code is non-zero when `ok` is false.

If any section is `missing` or `drifted`, read its `next_action` field. The CLI prints the next command to run; follow it.

## Stage 6 — Isolated target smoke

Run multiple presets, not just the one used by the current project. Preset
boundary defects often only show up when another preset is exercised. At
minimum, cover Asana, Redmine, Redmine Rails, and the governed Redmine Rails
preset.

```bash
mkdir -p ./tmp/mb-smoke-asana
mozyo-bridge scaffold apply asana --target ./tmp/mb-smoke-asana
mozyo-bridge scaffold status --target ./tmp/mb-smoke-asana
mozyo-bridge doctor --target ./tmp/mb-smoke-asana

mkdir -p ./tmp/mb-smoke-redmine
mozyo-bridge scaffold apply redmine --target ./tmp/mb-smoke-redmine
mozyo-bridge scaffold status --target ./tmp/mb-smoke-redmine
mozyo-bridge doctor --target ./tmp/mb-smoke-redmine

mkdir -p ./tmp/mb-smoke-redmine-rails
mozyo-bridge scaffold apply redmine-rails --target ./tmp/mb-smoke-redmine-rails
mozyo-bridge scaffold status --target ./tmp/mb-smoke-redmine-rails
mozyo-bridge doctor --target ./tmp/mb-smoke-redmine-rails

mkdir -p ./tmp/mb-smoke-redmine-rails-governed
mozyo-bridge scaffold apply redmine-rails-governed --target ./tmp/mb-smoke-redmine-rails-governed
cp ./tmp/mb-smoke-redmine-rails-governed/.mozyo-bridge/docs/catalog.yaml.example \
   ./tmp/mb-smoke-redmine-rails-governed/.mozyo-bridge/docs/catalog.yaml
mozyo-bridge docs validate --repo ./tmp/mb-smoke-redmine-rails-governed
mozyo-bridge docs validate --check-file-coverage --repo ./tmp/mb-smoke-redmine-rails-governed
mozyo-bridge docs generate-file-conventions --repo ./tmp/mb-smoke-redmine-rails-governed
mozyo-bridge docs generate-file-conventions --check --repo ./tmp/mb-smoke-redmine-rails-governed
mozyo-bridge scaffold status --target ./tmp/mb-smoke-redmine-rails-governed
mozyo-bridge doctor --target ./tmp/mb-smoke-redmine-rails-governed
```

Expected per target:

- `scaffold status` exits `0` with the line `result: clean`.
- `doctor --target` reports the `scaffold` section as `ok`.
- governed smoke also proves that `mozyo-bridge docs ...` reads
  `<repo>/.mozyo-bridge/docs/catalog.yaml` without any target-repo
  `.mozyo-bridge/tools/*.py` vendor copy.

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
- `doctor` reports `claude_skill: missing` after plugin marketplace install:
  - the plugin is not installed, not visible to the current Claude install, or
    the CLI/doctor version is older than the plugin-cache scan. Verify with
    `claude plugin list`, then upgrade/reinstall `mozyo-bridge` if the plugin
    is listed but doctor still cannot see it.
- `mozyo-bridge docs validate` fails with `.mozyo-bridge/docs/catalog.yaml` missing:
  - governed scaffold only ships `catalog.yaml.example`; copy it to
    `.mozyo-bridge/docs/catalog.yaml` and then fill project-specific docs and
    file conventions.
- `doctor` reports `claude_skill: ok` AND a project skill exists in `<project>/.claude/skills/mozyo-bridge-agent/`:
  - personal (`~/.claude/skills/`) overrides project. The project copy is shadowed.
  - keep ONE path. The plugin marketplace path avoids the conflict.
- `mozyo-bridge scaffold apply <preset>` refused to overwrite existing routers:
  - protection is intentional. Inspect with `--dry-run`, then replace via `--backup` (keep originals) or `--force` (no backup).
- `doctor` reports `tmux: missing`:
  - `tmux` is not on `PATH`. Install via the OS package manager.
- every section reports `ok` but agents still do not see the rules / skill:
  - restart Claude Code / Codex. Same-session skill index is cached.
- pane / notification commands fail with "no agent windows":
  - run bare `mozyo` from the repo root: `cd /path/to/repo && mozyo`. This creates `claude` and `codex` windows in a repo-scoped tmux session.
  - for an existing pane (VS Code tmux terminal, hand-managed tmux pane), run `mozyo-bridge init <agent>` from inside that pane to rename its window.

## Where this doc sits relative to the others

- **This doc** owns the fresh-install stage order and the existing-install update path through a working scaffold + verified doctor + per-preset isolated smoke.
- `README.md` `Quick Start` / `Beta Tester Install` / `Agent Skill Install` / `Agent Rules Scaffold` are the operator reference for the individual commands. Read README when you need a flag detail; read this bootstrap doc when you need the stage order.
- `vibes/docs/logics/skill-distribution.md` is the source of truth for skill packaging, precedence, marketplace metadata, and drift.
- `vibes/docs/logics/scaffold-rules.md` is the source of truth for scaffold preset semantics and manifest invariants.
- `vibes/docs/logics/turnkey-e2e-acceptance.md` is the destructive post-release acceptance; not a bootstrap step.
- `vibes/docs/logics/release-flow.md` is the release operator guide. Bootstrap does not invoke release helpers.
- `vibes/docs/rules/agent-workflow.md` is the per-ticket-system work rules; bootstrap stops at "the project can route Claude / Codex against its ticket system" and hands off to that doc.

## Pinning bootstrap to a specific release

Routine bootstrap does not need to pin to a release tag — running `pipx install mozyo-bridge` plus the primary skill paths against `main` is the documented experience.

For post-release acceptance, the install ref MUST match the package version. See `vibes/docs/logics/turnkey-e2e-acceptance.md` `Install the published package` for the pinned form (`MOZYO_BRIDGE_SKILL_REF=v<X.Y.Z>` combined with the matching `pipx install`).
