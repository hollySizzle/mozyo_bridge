# Scaffold Rules Logic

## Purpose

`mozyo-bridge scaffold rules` installs project-local agent routers for a target ticket system. The routers point to centrally managed mozyo-bridge rule presets under the user's mozyo-bridge home.

The split axis is the ticket system, not the agent runtime. Claude Code and Codex should receive the same project rules through `AGENTS.md` and `CLAUDE.md` as a pair.

Supported presets:

- `asana`
- `redmine`
- `none`

## Common Responsibilities

Every preset must generate or update the same project-local router pair:

- `AGENTS.md`
- `CLAUDE.md`

The generated files are routers, not full rule books. They should point agents to the source of truth for the selected ticket system and to the centrally managed preset rules. They must not inline large process rules.

Central preset rules live under:

```text
${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/<asana|redmine|none>/
```

The CLI package is the distribution source for those preset files. `mozyo-bridge rules install` should copy the packaged presets into the mozyo-bridge home, and `mozyo-bridge scaffold rules <preset>` should create thin project routers that reference the installed central preset.

Common constraints:

- Do not embed private Notion URLs, credentials, tokens, or personal data in public templates.
- Do not overwrite existing `AGENTS.md` or `CLAUDE.md` by default.
- Do not generate tool-specific rules that make Codex and Claude follow different project processes.
- Treat pane messages as notifications, not authoritative task state.
- Keep project-specific policy in project-local docs or private systems, not in package templates or central public presets.
- Do not support a repo-local vendor mode in the initial implementation. A second distribution mode would double the update, diff, and diagnostic surface before there is proven need.

## Preset: redmine

The Redmine preset should be based on the legacy source material in `tmp/development_flow/`, especially:

- `tmp/development_flow/README.md`
- `tmp/development_flow/vibes/docs/rules/redmine_driven_dev.yaml`
- `tmp/development_flow/vibes/docs/rules/claude_codex_audit_system.yaml`
- `tmp/development_flow/vibes/docs/rules/terminal_agent_handoff.yaml`
- `tmp/development_flow/vibes/docs/tasks/implementation/claude_codex_redmine_handoff.md`

This material is a good Redmine process source, not merely an abstract pattern. The Redmine preset should preserve Redmine-native gates:

- Redmine issue is the execution unit and source of truth.
- Redmine journal id is the canonical handoff and review gate.
- Notification payloads should point to the same issue and journal as the durable work record.
- Review request and review result flows should require an existing journal before notifying another pane.
- Status, tracker, and journal conventions remain project-configurable, because Redmine instances differ.

The Redmine preset must remove or isolate source-project-specific assumptions:

- Fixed role split such as "Claude Code implements, Codex only audits".
- Source project docs catalog, `.claude-nagger`, active-doc resolver, route-check, or app-specific verification terms.
- Retired queue history, unless explicitly generating a migration note.
- `vibes/tools/mozyo_bridge` as a runtime path. This repository must keep `src/mozyo_bridge` as runtime code.

Central preset doc:

- `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine/agent-workflow.md`

That doc should describe Redmine issue and journal gates in a public, project-neutral form.

## Preset: asana

The Asana preset should not imitate Redmine journal semantics too closely. Asana has a different information model.

Asana-native responsibilities:

- Asana task is the execution unit.
- Asana project is the work area.
- Project notes or project description may carry project-level `llm:` metadata when the workspace uses that convention.
- Task description carries purpose, work paths, artifact paths, reference rules, completion criteria, and prohibitions.
- Task comments are the durable handoff and work log.
- Project status updates are for project-level progress, not ordinary task handoffs.

Asana has no exact Redmine journal equivalent. If the API exposes a durable story/comment id, use that as the handoff id. If not, use the task permalink plus the comment timestamp or latest comment context, and make the limitation explicit in the generated rules.

Central preset doc:

- `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/asana/agent-workflow.md`

The Asana preset should encourage the task description template already used in this repository:

```markdown
## 目的

## 作業対象パス

## 成果物パス

## 参照規約

## 完了条件

## 禁止事項
```

Asana-specific guardrails:

- Do not treat pane messages or chat messages as durable state.
- Do not require Project Custom Fields for the MVP path.
- Do not put private Notion URLs into package templates.
- Do not assume every Asana workspace exposes the same custom fields or comment ids.

## Preset: none

The `none` preset is a minimal router preset for projects without a ticket system.

Responsibilities:

- Generate `AGENTS.md` and `CLAUDE.md` as project-local routers.
- Point to the central `none` preset, repository docs, and explicit user instructions as the available source of truth.
- State that there is no durable external execution queue.
- Require agents to avoid pretending that pane messages, chat messages, or generated queues are authoritative state.

This preset is weaker than `asana` or `redmine` for auditability. It should be positioned as a lightweight bootstrap option, not as an equivalent governance model.

Central preset doc:

- `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/none/agent-workflow.md`

## Central Rules Management

Initial commands:

```bash
mozyo-bridge rules install
mozyo-bridge rules status
```

Responsibilities:

- `rules install` copies packaged preset rules into `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}`.
- `rules status` reports installed preset versions and missing preset files.
- `scaffold rules <preset>` refuses to complete if the referenced central preset is missing, unless a future explicit bootstrap flag installs it first.
- Agents must not pretend to have read central rules if the referenced file is unavailable.

The central rules store is the only initial distribution mode. Do not add `--vendor` or repo-local copies to the first implementation. If self-contained snapshots become necessary later, add a separate `rules export` or `snapshot` feature instead of mixing it into the normal scaffold path.

## File Safety Policy

Default behavior:

- If neither `AGENTS.md` nor `CLAUDE.md` exists, create both.
- If either file exists, refuse to write and report the paths that would be affected.
- Do not partially write only one file from the pair.
- Always write `.mozyo-bridge/scaffold.json` when routers are created or replaced.

Optional flags for implementation:

- `--backup`: before replacing existing files, copy each affected file to `<name>.bak.<timestamp>`.
- `--force`: replace existing files without backup only when explicitly requested.
- `--dry-run`: print the planned file operations and rendered target paths without writing.

`--backup` and `--force` are mutually exclusive. `--dry-run` may be combined with either flag to preview behavior.

## CLI Shape

Target command:

```bash
mozyo-bridge scaffold rules asana
mozyo-bridge scaffold rules redmine
mozyo-bridge scaffold rules none
```

Expected options:

```bash
mozyo-bridge scaffold rules <asana|redmine|none> \
  --target /path/to/project \
  --dry-run \
  --backup \
  --force
```

`--target` should default to the resolved project root, using the same root resolution rules as the rest of the CLI.

`scaffold.json` should record the selected preset, central preset version, mozyo-bridge version, generated router file hashes, and the rule path that the routers reference. It is a local installation record, not a copy of the central rules.

## Test Strategy

Implementation tests should cover:

- Parser accepts `scaffold rules asana`, `redmine`, and `none`.
- Parser rejects unsupported ticket systems.
- Rendering creates both `AGENTS.md` and `CLAUDE.md` for each preset.
- Rendering creates thin routers that reference `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/<preset>/agent-workflow.md`.
- `rules install` installs central preset docs for `asana`, `redmine`, and `none`.
- `scaffold rules <preset>` reports a clear error when the central preset is missing.
- Default behavior refuses to overwrite either existing router.
- Existing one-file state is handled atomically and does not leave a mismatched pair.
- `.mozyo-bridge/scaffold.json` is written with preset, version, paths, and file hashes.
- `--dry-run` writes nothing.
- `--backup` preserves previous files before replacement.
- `--force` replaces previous files only when explicitly provided.
- Rendered templates contain no private Notion URLs, credentials, or source-project paths.
- No `--vendor` path or repo-local preset copy is included in the initial CLI.
- Redmine templates mention issue and journal gates.
- Asana templates mention task, project, and comment based handoffs.
- `none` templates clearly state that no external execution queue exists.

Use filesystem temporary directories for write behavior tests. Override `MOZYO_BRIDGE_HOME` in tests so central rule installation never touches the real user home. Keep tests independent from live Asana, Redmine, Notion, or tmux state.
