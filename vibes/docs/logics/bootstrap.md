# mozyo-bridge Bootstrap Guide

LLM-first bootstrap for installing or updating `mozyo-bridge` through project
initialization. Optimized for Claude / Codex agent execution: each stage names
exact commands, expected success signals, and the failure branch.

This is the detailed stage-order / FAQ / troubleshooting reference for
"install/update + project bootstrap". The entrypoint is `README.md` `Quick
Start`: run `mozyo-bridge doctor --target .` then
`mozyo-bridge instruction doctor --target . --profile redmine-codex` first, and
follow the link here when a step fails or you need the full stage sequence. This
doc is no longer the first thing to read. `README.md`,
`skill-distribution.md`, and `scaffold-rules.md` remain the authoritative
reference for their specific surfaces; this doc orders the stages and links into
them.

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
  `redmine-governed` or `redmine-rails-governed`.
- workspace-local Redmine default project startup (`.mozyo-bridge/workspace-defaults.yaml`
  -> `.mozyo-bridge/redmine-defaults.md`) and optional LLM/MCP runtime config
  placement guidance.
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

## Startup Decision Flow

Run this decision flow before choosing a scaffold preset. The goal is to decide
the minimum governance layer that will be true for the project on day 2, not to
install the strongest preset everywhere.

1. Identify the durable work system.
   - Redmine issue / journal gates -> choose a Redmine preset.
   - Asana task / comment gates -> choose `asana`.
   - No durable ticket system -> choose `none` and keep the project lightweight.
   - Mixed or unclear systems -> stop and record the intended source of truth
     before scaffolding. Do not guess from pane text or chat history.
2. Identify the framework surface.
   - Rails repository with Rails-specific review, DB, route, or test
     conventions -> use a `redmine-rails*` preset when the durable system is
     Redmine.
   - Non-Rails repository -> use `redmine*`, `asana`, or `none` as appropriate.
3. Decide whether full governance is justified.
   - Use full governance when agents will repeatedly edit or audit the repo,
     work must be replayable from Redmine journals, role boundaries or direct
     edit gates matter, or path-to-doc resolution must be machine-checkable.
   - Stay lightweight when the project only needs router files, has no active
     docs catalog owner, is a one-off/sandbox project, or would not maintain
     `catalog.yaml` and generated checks after bootstrap.
4. Choose central vs repo-local rules storage.
   - Central mode is the default for normal local machines with a persistent
     `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}`.
   - Repo-local mode is for Dev Containers, Codespaces, or ephemeral-home
     workspaces where agents must read the preset from the repository itself.
5. Plan workspace-local Redmine default project handling when Redmine is the
   durable system.
   - If `.mozyo-bridge/redmine-defaults.md` exists, read it before creating
     Redmine issues without an explicit `project_id`.
   - If default project config is missing, do not guess or create a Redmine
     project. Ask the operator for the project name, identifier, URL, and
     optional parent label.
   - Keep business placement decisions in the target workspace / owner context,
     not in distributed `mozyo_bridge` docs.
6. Plan verification before writing files.
   - Every scaffolded repo needs `scaffold status` and `doctor --target`.
   - Governed repos additionally need `catalog.yaml` initialization,
     docs validation, file coverage, file-conventions generation, and generated
     check.
   - Workflow / guardrail changes need a later workflow verification task; the
     bootstrap smoke proves installation shape, not agent behavior in a real
     work issue.

Preset selection summary:

| Situation | Preset |
| --- | --- |
| Asana is the durable work queue | `asana` |
| Redmine, non-Rails, lightweight routers only | `redmine` |
| Redmine, non-Rails, full governance package | `redmine-governed` |
| Redmine, Rails, lightweight routers + project-owned local policy | `redmine-rails` |
| Redmine, Rails, full governance package | `redmine-rails-governed` |
| No durable ticket system | `none` |

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
- one row per packaged preset (`asana`, `redmine`, `redmine-governed`,
  `redmine-rails`, `redmine-rails-governed`, `none`), each with `STATUS=ok`.
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

Choose the preset using the Startup Decision Flow above, then scaffold exactly
that preset. Do not apply a governed preset just because it is available, and do
not apply a lightweight preset when the project has already committed to
Redmine journal gates plus catalog-driven audit.

Full governance means the scaffold provides more than routers:

- central governed workflow rules that require durable gates;
- repo-local rule authoring and docs catalog governance artifacts;
- `catalog.yaml.example` as the starting point for project-owned active docs;
- optional runtime guardrail artifacts such as Claude Nagger skeleton and tmux
  UI snippet;
- a verification obligation to keep catalog and generated file conventions in
  sync.

Lightweight scaffold means routers plus `.mozyo-bridge/scaffold.json` only. The
target project may still add Project-Local Additions, but the scaffold does not
claim that docs catalog governance, file coverage, or role gates are active.

```bash
cd /path/to/your-project
mozyo-bridge scaffold apply <preset>
```

Expected files written:

- `AGENTS.md`
- `CLAUDE.md`
- `.mozyo-bridge/scaffold.json`

Project-Local Additions:

- The marker block in generated `AGENTS.md` / `CLAUDE.md` is where target-owned
  policy belongs.
- Put repository-specific role boundaries, local docs namespaces, forbidden
  paths, required verification, and durable-source notes there.
- Keep the marker block concise. Do not paste the full preset workflow into it;
  routers stay thin and point at the selected preset.
- Re-run `scaffold diff` / `scaffold apply --backup` for preset updates; the
  marker content is preserved.

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

For `redmine-governed` and `redmine-rails-governed`, initialize the docs catalog after scaffold:

```bash
cp .mozyo-bridge/docs/catalog.yaml.example .mozyo-bridge/docs/catalog.yaml
mozyo-bridge docs validate --repo .
mozyo-bridge docs validate --check-file-coverage --repo .
mozyo-bridge docs generate-file-conventions --repo .
mozyo-bridge docs generate-file-conventions --check --repo .
```

Then update `.mozyo-bridge/docs/catalog.yaml` with project-specific
`documents`, `related_document_refs`, `file_conventions`, and optional
`coverage_roots`. The configured `catalog.yaml` is target-owned data and is not
overwritten by scaffold re-apply. The tooling is not copied into the target repo
as `.mozyo-bridge/tools/*.py`; it is provided by the installed
`mozyo-bridge docs ...` CLI.

Catalog adoption is not complete until:

- every active `documents[].canonical_path` resolves to a real file;
- key implementation and docs roots are covered by `file_conventions`;
- generated file conventions are created from the catalog and pass `--check`;
- agents can resolve changed paths with `mozyo-bridge docs resolve <path>`.

If the project cannot maintain these checks, use the lightweight preset instead
and record that full governance was intentionally not adopted.

### Redmine default project startup

For Redmine-backed projects, configure the workspace-local default project
after scaffold and before final `doctor` verification. This does not decide
which business work belongs in which Redmine project; that remains an
operator / workspace decision. Bootstrap only records and verifies the selected
default so LLM agents and MCP calls do not silently create issues in the wrong
project.

If the workspace already has the generated snippet, read it first:

```bash
test -f .mozyo-bridge/redmine-defaults.md && sed -n '1,160p' .mozyo-bridge/redmine-defaults.md
```

If it is missing, ask the operator for:

- Redmine project identifier.
- Redmine project display name.
- Redmine project URL.
- Optional parent project label.

Then create or update `.mozyo-bridge/workspace-defaults.yaml` with those
workspace-specific values and render the generated snippet:

```bash
mozyo-bridge workspace-defaults
mozyo-bridge workspace-defaults --check
```

Do not store Redmine API keys, OAuth tokens, cookies, passwords, or other
credentials in `.mozyo-bridge/workspace-defaults.yaml`, generated snippets,
`.codex/config.toml`, `.mcp.json`, ticket journals, or chat output. Authentication
belongs in user-level tool config or secret stores.

For a Codex workspace that will use the Redmine MCP, check whether the
repo-root `<repo>/.codex/config.toml` already declares the verified default
project. Do not put this workspace default in a home-directory config; it is a
repo-local fact. If the file is missing or has no Redmine default, ask the
operator before creating or updating it. Use only the project identifier and
non-secret MCP URL:

```toml
[redmine]
default_project = "<project-identifier>"
default_project_name = "<project display name>"
default_project_url = "https://redmine.example.invalid/projects/<project-identifier>"

[mcp_servers.redmine_epic_grid]
url = "https://redmine.example.invalid/mcp/rpc"
http_headers = { X-Default-Project = "<project-identifier>" }
```

This file is a startup checkpoint and example, not a generated output kind. Do
not point `workspace-defaults.yaml` at `.codex/config.toml` unless a future
typed `codex_toml` renderer exists and validates TOML, suffixes, and secret
rejection. If `.codex/config.toml` is created or updated, restart or reload
Codex before verification.

When a target Claude / MCP runtime uses `.mcp.json`, place it at the repo root
as `<repo>/.mcp.json`, not in the home directory. Do not treat it as
authoritative runtime config unless the target runtime has been verified to read
that repo-root file. Until then, `<repo>/.mcp.json` belongs in examples or
project-local experiments only, and must not contain credentials.

After restarting or reloading the relevant LLM/MCP runtime, verify the default
project with a Redmine MCP call that omits `project_id`. A successful result
must resolve to the same project identifier recorded in
`.mozyo-bridge/redmine-defaults.md`. For comparison, repeat the call with the
explicit `project_id` and confirm both results name the same project. If either
check fails, stop and ask the operator to correct the workspace-local config.

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
minimum, cover Asana, Redmine, Redmine Governed, Redmine Rails, and the
governed Redmine Rails preset.

```bash
mkdir -p ./tmp/mb-smoke-asana
mozyo-bridge scaffold apply asana --target ./tmp/mb-smoke-asana
mozyo-bridge scaffold status --target ./tmp/mb-smoke-asana
mozyo-bridge doctor --target ./tmp/mb-smoke-asana

mkdir -p ./tmp/mb-smoke-redmine
mozyo-bridge scaffold apply redmine --target ./tmp/mb-smoke-redmine
mozyo-bridge scaffold status --target ./tmp/mb-smoke-redmine
mozyo-bridge doctor --target ./tmp/mb-smoke-redmine

mkdir -p ./tmp/mb-smoke-redmine-governed
mozyo-bridge scaffold apply redmine-governed --target ./tmp/mb-smoke-redmine-governed
cp ./tmp/mb-smoke-redmine-governed/.mozyo-bridge/docs/catalog.yaml.example \
   ./tmp/mb-smoke-redmine-governed/.mozyo-bridge/docs/catalog.yaml
mozyo-bridge docs validate --repo ./tmp/mb-smoke-redmine-governed
mozyo-bridge docs validate --check-file-coverage --repo ./tmp/mb-smoke-redmine-governed
mozyo-bridge docs generate-file-conventions --repo ./tmp/mb-smoke-redmine-governed
mozyo-bridge docs generate-file-conventions --check --repo ./tmp/mb-smoke-redmine-governed
mozyo-bridge scaffold status --target ./tmp/mb-smoke-redmine-governed
mozyo-bridge doctor --target ./tmp/mb-smoke-redmine-governed

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
- VS Code `tmux-integrated` collapses a non-ASCII workspace basename to a low-information session name (for example `2026PBL_ローカル` → `2026PBL_____`), and same-named `____` sessions across workspaces break `agents list` / `--target-repo` repo-identity recovery (Redmine #10796):
  - bare `mozyo` already derives a collision-safe ASCII session name (prefers `redmine.default_project.identifier` from `.mozyo-bridge/workspace-defaults.yaml`, else `mozyo-<basename-slug>-<hash>`). Inspect it with `mozyo-bridge session name --repo <repo>`. An explicit `mozyo --session NAME` still overrides.
  - VS Code starts its own session (not via `mozyo`), so pin the same name **per workspace**: run `mozyo-bridge session vscode-settings --repo . --write` to set `"tmux-integrated.sessionName"` in `<repo>/.vscode/settings.json` (workspace-local only; user-global settings are never touched; JSONC files are refused, not clobbered). For a custom task menu, replace `basename "$PWD" | sed ...` with `mozyo-bridge session name --repo .`.
  - do **not** set a user-global fixed `tmux-integrated.sessionName`; it collapses every workspace onto one session and risks cross-repo misdelivery.
  - migration: if a legacy basename-named session lingers, bare `mozyo` prints a notice; attach it with `mozyo --session <old>` or remove it once empty with `tmux kill-session -t <old>`.

### `instruction doctor` FAQ (repo-local LLM runtime config)

`mozyo-bridge instruction doctor --target . --profile redmine-codex` is the
machine check for a Redmine/Codex workspace's repo-root runtime config. It is
read-only: it never creates, edits, or autofixes config, and it makes no network
call. The README Quick Start runs it second (after `doctor`); the failures it
reports and their fixes:

- **`<repo>/.codex/config.toml is missing`**:
  - cause: the workspace has no repo-root Redmine default project config. The
    docs require this file at `<repo>/.codex/config.toml`, not in a home config.
  - fix: an agent must **ask the operator before creating it** — the default
    project is a workspace-specific fact, not something to guess. Once the
    operator confirms the project identity, create the file with `[redmine]`
    `default_project` / `default_project_name` / `default_project_url` and the
    `[mcp_servers.redmine_epic_grid]` `url` + `http_headers.X-Default-Project`.
    Restart / reload the runtime, then verify with a Redmine MCP call that omits
    `project_id`.
- **`X-Default-Project` mismatch**:
  - cause: `http_headers.X-Default-Project` does not equal
    `[redmine].default_project`. The MCP header and the declared default
    disagree, so MCP calls resolve a different project than the docs claim.
  - fix: an operator decides which value is correct and reconciles the two.
    Do not silently pick one — a wrong default project routes issues/searches to
    the wrong place.
- **`.mcp.json` is `info` / non-authoritative**:
  - reason: no runtime has been verified to read the repo-root `<repo>/.mcp.json`,
    so the command reports its presence/absence as `info` and never fails on it
    alone (deferral). `instruction doctor` still parses it and scans it for
    credential shapes when present, but does not treat it as authoritative
    runtime config. Treating an unread file as fact is the risk this avoids.
- **home config must not hold the default project**:
  - reason: a default project placed in a home-directory config leaks across
    every workspace on the machine — opening a different repo would inherit the
    wrong default. Repo-root placement (`<repo>/.codex/config.toml`,
    `<repo>/.mcp.json`) isolates the default as a workspace-local fact.
- **what an agent may auto-fix vs must confirm with an operator**:
  - auto-fix (no confirmation): mechanical, reversible, fact-preserving edits
    once the project identity is already established — e.g. correcting an
    `X-Default-Project` header to match an operator-confirmed
    `[redmine].default_project`, or removing an obviously misplaced credential.
  - operator confirmation required: choosing or changing the default project
    identity itself, creating `<repo>/.codex/config.toml` from scratch, or
    anything that decides *which* project a workspace targets. `instruction
    doctor` itself never writes; these are actions an agent takes only after the
    check reports a failure and the operator has confirmed intent.

## Where this doc sits relative to the others

- **`README.md` `Quick Start`** is the entrypoint: install, then `doctor` +
  `instruction doctor` first, with the `instruction doctor` failure summary.
  Start there.
- **This doc** is the detailed reference reached from the README: it owns the
  fresh-install stage order, the existing-install update path through a working
  scaffold + verified doctor + per-preset isolated smoke, and the failure /
  `instruction doctor` FAQ. Read it when a Quick Start step fails or you need the
  full stage sequence — not as the first doc.
- `vibes/docs/logics/skill-distribution.md` is the source of truth for skill packaging, precedence, marketplace metadata, and drift.
- `vibes/docs/logics/scaffold-rules.md` is the source of truth for scaffold preset semantics and manifest invariants.
- `vibes/docs/logics/turnkey-e2e-acceptance.md` is the destructive post-release acceptance; not a bootstrap step.
- `vibes/docs/logics/release-flow.md` is the release operator guide. Bootstrap does not invoke release helpers.
- `vibes/docs/rules/agent-workflow.md` is the per-ticket-system work rules; bootstrap stops at "the project can route Claude / Codex against its ticket system" and hands off to that doc.

## Pinning bootstrap to a specific release

Routine bootstrap does not need to pin to a release tag — running `pipx install mozyo-bridge` plus the primary skill paths against `main` is the documented experience.

For post-release acceptance, the install ref MUST match the package version. See `vibes/docs/logics/turnkey-e2e-acceptance.md` `Install the published package` for the pinned form (`MOZYO_BRIDGE_SKILL_REF=v<X.Y.Z>` combined with the matching `pipx install`).
