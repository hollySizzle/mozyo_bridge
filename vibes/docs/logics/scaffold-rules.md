# Scaffold Rules Logic

## Purpose

`mozyo-bridge scaffold apply` installs project-local agent routers for a target ticket system. The routers point to centrally managed mozyo-bridge rule presets under the user's mozyo-bridge home.

The split axis is the ticket system, not the agent runtime. Claude Code and Codex should receive the same project rules through `AGENTS.md` and `CLAUDE.md` as a pair.

Supported presets:

- `asana`
- `redmine`
- `redmine-governed` (extends `redmine`)
- `redmine-rails` (extends `redmine`)
- `redmine-rails-governed` (extends `redmine-rails`)
- `none`

Preset selection is explicit. `mozyo-bridge scaffold apply <preset>` applies only the chosen preset's workflow, so cross-preset policy matrices should not be duplicated in shared workflow docs. Keep each preset self-contained, and keep project-specific mandatory policies in the target project's local docs or private systems.

## Startup Preset Selection

Select the preset from durable work state first, then framework, then governance
depth. Do not infer the preset from which agent is currently open.

Decision order:

1. Durable work system:
   - Asana task/comment is the source of truth -> `asana`.
   - Redmine issue/journal is the source of truth -> a Redmine preset.
   - No durable ticket system -> `none`.
2. Framework specialization:
   - Rails + Redmine -> `redmine-rails` or `redmine-rails-governed`.
   - Non-Rails + Redmine -> `redmine` or `redmine-governed`.
3. Governance depth:
   - Full governance -> governed preset.
   - Thin routers only -> lightweight preset.

Use a governed preset only when the project is prepared to operate the full
package:

- Redmine journals are the durable replay record for start, handoff, review,
  verification, commit hash, and close gates.
- Agents must resolve active docs from changed paths before implementation or
  audit.
- `catalog.yaml` will be owned by the target project, reviewed in diffs, and
  kept in sync with generated file conventions.
- Project-Local Additions carry repo-specific role boundaries, path rules,
  verification commands, and local docs namespaces.
- `scaffold status`, `docs validate`, file coverage, generator, and generated
  check are part of the normal verification path.

Stay on a lightweight preset when:

- the project only needs Claude/Codex routers that point at a ticket workflow;
- a docs catalog would be created but not maintained;
- the repo is a throwaway sandbox, short-lived demo, or hand-edited experiment;
- there is no durable Redmine journal lifecycle to replay;
- role split and direct-edit gates would be ceremonial rather than enforced.

The `none` preset is not a weak governed preset. It is the honest choice for
projects that have no external execution queue. It must not pretend pane
messages or chat history are durable state.

Project-Local Additions and catalog have different jobs:

- Project-Local Additions are concise target-owned policy in the generated
  routers. They preserve local routing facts across scaffold re-sync.
- `.mozyo-bridge/docs/catalog.yaml` is the active-doc map. It answers which
  rule/spec/logic docs must be read for a changed path.
- `.mozyo-bridge/docs/file_conventions.generated.yaml` is generated output, not
  the source of truth. Change the catalog and regenerate it.
- Workflow verification is a later behavioral check using a real work issue; it
  is not satisfied by scaffold status alone.

## Common Responsibilities

Every preset must generate or update the same project-local router pair:

- `AGENTS.md`
- `CLAUDE.md`

The generated files are routers, not full rule books. They should point agents to the source of truth for the selected ticket system and to the centrally managed preset rules. They must not inline large process rules.

Central preset rules live under:

```text
${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/<preset>/
```

The CLI package is the distribution source for those preset files. `src/mozyo_bridge/scaffold/presets/presets.yaml` is the preset registry. `mozyo-bridge rules install` should copy the packaged presets into the mozyo-bridge home, and `mozyo-bridge scaffold apply <preset>` should create thin project routers that reference the installed central preset.

Common constraints:

- Do not embed private Notion URLs, credentials, tokens, or personal data in public templates.
- Do not overwrite existing `AGENTS.md` or `CLAUDE.md` by default.
- Do not generate tool-specific rules that make Codex and Claude follow different project processes.
- Treat pane messages as notifications, not authoritative task state.
- Keep project-specific policy in project-local docs or private systems, not in package templates or central public presets.
- Do not put repo-local mandatory Claude/Codex audit policies into the shared skill or into unrelated presets.

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

## Preset: redmine-governed

The `redmine-governed` preset is the opt-in full guardrail governance package for non-Rails Redmine projects. It extends `redmine` directly.

Responsibilities:

- Inherit the Redmine issue / journal gate lifecycle from `redmine`.
- Keep gate schema, role split, Codex direct edit gate, and completion
  contract in the preset `agent-workflow.md` itself. Do not distribute a
  second development-flow rule file that competes with the entry workflow.
- Ship the same governance artifact categories as the Rails governed preset:
  - `.mozyo-bridge/rules/llm_rule_authoring.md`
  - `.mozyo-bridge/rules/docs_catalog_governance.yaml`
  - `.mozyo-bridge/docs/catalog.yaml.example`
  - `.mozyo-bridge/tmux/agent-ui.conf`
  - `.claude-nagger/{config,command_conventions,mcp_conventions}.yaml.example`
  - `.claude-nagger/.gitignore`
- Keep the catalog skeleton framework-neutral. It may use placeholders such as `src/**`, `tests/**`, `lib/**`, and `config/**`, but must not include Rails app, migration, Presenter, or Rails test assumptions.
- Provide docs catalog tooling from the `mozyo-bridge` package CLI, not as target-repo Python source.
- Track every shipped artifact in `.mozyo-bridge/scaffold.json` so `scaffold status` detects drift.
- Avoid touching `.mozyo-bridge/docs/catalog.yaml` itself; only the `.example` file is shipped.

Central preset doc:

- `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine-governed/agent-workflow.md`

Repo-local artifacts source:

- Files live under `src/mozyo_bridge/scaffold/presets/redmine-governed/files/` in the package source tree.

## Preset: redmine-rails-governed

The `redmine-rails-governed` preset is the opt-in full guardrail governance package for Rails + Redmine projects. It extends `redmine-rails`, which in turn extends `redmine`.

Where `redmine-rails` intentionally defers strong project-local rules (gate schema, docs catalog governance, active-doc resolver tooling) to the target repo's project-local layer, `redmine-rails-governed` treats the strong language and the supporting tooling as central. It is meant for projects that have decided they want the full governance package up-front, not a lightweight bootstrap.

Responsibilities:

- Inherit the layered router behavior from `redmine-rails`.
- Keep gate schema, role split, Codex direct edit gate, and completion
  contract in the preset `agent-workflow.md` itself. Do not distribute a
  second development-flow rule file that competes with the entry workflow.
- Layer additional governance language in the central `agent-workflow.md`: gate schema field requirements, Codex direct edit gate, docs catalog governance, LLM rule authoring contract, journal templates.
- Ship a fixed set of repo-local artifacts when `scaffold apply` runs:
  - `.mozyo-bridge/rules/llm_rule_authoring.md`
  - `.mozyo-bridge/rules/docs_catalog_governance.yaml`
  - `.mozyo-bridge/docs/catalog.yaml.example`
  - `.mozyo-bridge/tmux/agent-ui.conf`
  - `.claude-nagger/{config,command_conventions,mcp_conventions}.yaml.example`
  - `.claude-nagger/.gitignore`
- Provide docs catalog tooling from the `mozyo-bridge` package CLI, not as target-repo Python source. Operators run `mozyo-bridge docs validate / resolve / generate-file-conventions / audit-impact` against the target repo's `.mozyo-bridge/docs/catalog.yaml`.
- Track every shipped artifact in `.mozyo-bridge/scaffold.json` so `scaffold status` detects drift. The status command labels the manifest's file list as `tracked files:` (renamed from the earlier `router files:` to reflect that governed presets also ship repo-local artifacts beyond the router pair).
- Refuse silent overwrite of any shipped artifact; `--backup` or `--force` is required, same as the router pair.
- Avoid touching `.mozyo-bridge/docs/catalog.yaml` itself (only the `.example` file is shipped); the configured catalog stays under the operator's control.
- Catalog supports an optional `coverage_roots` list. `mozyo-bridge docs validate --check-file-coverage` walks those repo-relative paths when no `--coverage-root` flag is given; `--coverage-root` always wins when present. Missing roots stay informational (`notice:` line, no exit 1); unmatched files inside an existing root remain exit 1.

Non-goals:

- Do not embed project-specific business domain identifiers (NIPT, FeatureList, customer-visible product codes) into the preset or the shipped artifacts. The catalog skeleton is generic.
- Do not require the operator to keep a specific generated-file consumer (for example, a nagger configuration in some other directory). The generator's default output is `.mozyo-bridge/docs/file_conventions.generated.yaml`; alternative paths are caller-driven via `--output`.
- Do not collapse the central `redmine-rails` preset into the governed preset. The lightweight `redmine-rails` preset stays a valid choice for projects that prefer to fill the governance layer themselves.

Central preset doc:

- `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine-rails-governed/agent-workflow.md`

Repo-local artifacts source:

- Files live under `src/mozyo_bridge/scaffold/presets/redmine-rails-governed/files/` in the package source tree. The scaffold walks this subdirectory verbatim into the target repo. Any new artifact must be packaged here (and added to `pyproject.toml` package-data) rather than hard-coded into the scaffold module.

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
- `scaffold apply <preset>` refuses to complete if the referenced central preset is missing, unless a future explicit bootstrap flag installs it first.
- Agents must not pretend to have read central rules if the referenced file is unavailable.

The central rules store under `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}` remains the default distribution mode. The repo-local store described below is an opt-in second mode for Dev Container / ephemeral-home workspaces and shares the same install / status / apply / diff / status surface; do not introduce a third distribution mode (`--vendor`, ad-hoc symlinks, etc.) before retiring one of the existing two.

## Repo-Local Rules Mode (Dev Container / ephemeral home)

Dev Container, Codespace, and other ephemeral-home workspaces do not persist `~/.mozyo_bridge`. The repo-local mode lets the target repo carry its own preset rules store under `<repo>/.mozyo-bridge/rules/presets/<preset>/`, so agents can read guardrails without a user home that survives container rebuilds.

CLI surface (mirrors the central surface):

```bash
mozyo-bridge rules install --repo-local /path/to/repo
mozyo-bridge rules status  --repo-local /path/to/repo
mozyo-bridge scaffold apply <preset> --target /path/to/repo --repo-local
mozyo-bridge scaffold diff  <preset> --target /path/to/repo --repo-local
mozyo-bridge scaffold status --target /path/to/repo            # auto-detects mode
```

Responsibilities and constraints:

- `--home` and `--repo-local` are mutually exclusive on every command that accepts both. Passing both is an operator error and exits non-zero before any filesystem work.
- `rules install --repo-local <repo>` writes presets into `<repo>/.mozyo-bridge/rules/presets/<preset>/`. It does not touch `~/.mozyo_bridge`.
- `rules status --repo-local <repo>` inspects only the target repo's store; a missing host install is irrelevant in this mode.
- `scaffold apply <preset> --target <repo> --repo-local` reads from `<repo>/.mozyo-bridge/rules/presets/...`, embeds `mode: "repo-local"` in `.mozyo-bridge/scaffold.json`, and sets `rule_path` to the **repo-relative** form `.mozyo-bridge/rules/presets/<preset>/agent-workflow.md`. No `${MOZYO_BRIDGE_HOME:...}` expansion is involved at agent read time.
- `scaffold diff --repo-local` behaves identically to `scaffold diff` but renders against the repo-local store, so a diff between two modes against the same target will show different `rule_path` lines (expected).
- `scaffold status` reads the manifest's `mode` field and routes its central-preset comparison to whichever store the manifest declares. The user does not pass a mode flag to `status`. Passing `--home` against a `mode: "repo-local"` manifest is rejected as `manifest: invalid` to surface the operator-mode mismatch instead of silently comparing against the wrong store.
- Default behavior (no `--repo-local` anywhere) is unchanged: central mode, `mode: "central"` in the manifest, and the same `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}` portable `rule_path` as before. Switching a repo between modes requires re-running both `rules install --repo-local ...` (or `--home ...`) and `scaffold apply ... [--repo-local]` so the manifest mode matches the store.
- The repo-local `rule_path` must remain a repo-relative string. Host absolute paths must never leak into committed `AGENTS.md` / `CLAUDE.md` / `.mozyo-bridge/scaffold.json`; the existing host-path leak guard is extended to cover the repo-local artifacts.
- Project-Local Additions marker preservation works in both modes; the marker pair sits in the router templates and is mode-agnostic.

Operationally the two modes are a clean choice, not a layered cake: a repo is either central or repo-local at any given time, and `scaffold status` enforces that by reading the manifest's `mode` field. If the operator wants to switch modes, they re-run `rules install` and `scaffold apply` under the new mode; there is no in-place migration command.

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
mozyo-bridge scaffold apply asana
mozyo-bridge scaffold apply redmine
mozyo-bridge scaffold apply redmine-rails
mozyo-bridge scaffold apply none
```

Expected options:

```bash
mozyo-bridge scaffold apply <preset> \
  --target /path/to/project \
  --dry-run \
  --backup \
  --force
```

`--target` should default to the current working directory. This command generates strong router files, so it must not walk upward to a parent repository unless the user explicitly passes `--target` or `--repo`.

`scaffold.json` records the selected preset, central preset version, central preset content hash, mozyo-bridge version, generated router file hashes, and the rule path that the routers reference. It is a local installation record, not a copy of the central rules. The current schema is `schema_version: 2`; the `preset_hash` field (sha256 of the central `agent-workflow.md` at scaffold time) makes content-level drift detection possible. Pre-v2 manifests written by older mozyo-bridge releases are still readable but trigger a fail-and-upgrade signal from `scaffold status`.

## Drift Detection

Global central preset updates (e.g. after `pipx upgrade mozyo-bridge && mozyo-bridge rules install`) change the runtime guardrails that every scaffolded repo reads. The convenience of one update flowing into many repos is offset by the risk that an unintended change ships to repos whose owners did not review it. `mozyo-bridge scaffold status` resolves this by comparing the local `.mozyo-bridge/scaffold.json` against the currently installed central preset and against the on-disk router files.

```bash
mozyo-bridge scaffold status                       # implicit target = cwd
mozyo-bridge scaffold status --target /path/to/proj
mozyo-bridge scaffold status --target /path/to/proj --json
```

States reported per project:

- `manifest: present` plus `central status: ok` and all router files `ok` — clean; exit 0.
- `manifest: present` plus `central status: drifted-content` — the central preset's `agent-workflow.md` content changed since scaffold time; the repo's recorded behavior no longer matches what agents will actually read. Exit 1.
- `manifest: present` plus `central status: drifted-version` — the version label moved without a content change, or the hash check is unavailable. Exit 1.
- `manifest: present` plus `central status: missing` — the central preset is not installed on this machine; agents will fail at the read-central-preset guard. Run `mozyo-bridge rules install`. Exit 1.
- `manifest: present` plus `central status: ok-version-only` — the manifest is schema v1 and lacks `preset_hash`; the version matches but content drift cannot be detected. Regenerate the manifest by re-running `mozyo-bridge scaffold apply <preset> --backup`. Exit 1.
- Router file row `drifted` — the on-disk `AGENTS.md` or `CLAUDE.md` differs from the hash recorded at scaffold time; someone edited the generated file locally. Exit 1.
- `manifest: missing` — the target directory has no `.mozyo-bridge/scaffold.json`; no scaffold was ever run there. Exit 1.

`scaffold status` is for repo-local drift. The complementary `rules status` reports whether the central preset store itself is installed and up to date relative to the packaged version (i.e. it answers "does this host have the latest preset?"). The two commands have separate responsibilities: `rules status` for global install, `scaffold status` for per-repo manifest drift.

End-user flow after a release ships:

1. `pipx upgrade mozyo-bridge` (or the equivalent pip command).
2. `mozyo-bridge rules install` — updates `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/<preset>/` to the newly-packaged version.
3. In each scaffolded repo, run `mozyo-bridge scaffold status`. If it reports `drifted-content`, decide whether to accept the new guardrails (re-run `mozyo-bridge scaffold apply <preset> --backup`) or pin by regenerating from a specific older release.
4. CI can run `mozyo-bridge scaffold status --json` and fail the build on non-zero exit so unreviewed central-preset updates do not silently change agent behavior in production repos.

Detailed-flow rules are not vendored into each target repo by default. The thin routers (`AGENTS.md` / `CLAUDE.md`) stay thin; the heavy guardrails live in the central preset's `agent-workflow.md`. Repo-local mode (above) carries the preset file inside the target repo's `.mozyo-bridge/rules/presets/` directory for Dev Container portability, but the routers stay thin in either mode. If a project needs an immutable preset snapshot tied to a release tag (rather than a Dev Container portability move), that is still a separate "export"/"pin" feature, not a default of `scaffold apply`. Do not conflate the three paths.

## Beta Tester Verification

beta tester が GitHub `main` から CLI を install した後、user-global rules と repo-local scaffold が期待通り動いていることを確認する流れ。詳細な install 手順は `README.md` の `Beta Tester Install (GitHub main)` 節を見る。本節はそこに重複させずに、責務差と検証観点だけを残す。

`rules status` と `scaffold status` の責務差:

- `mozyo-bridge rules install` / `mozyo-bridge rules status` は user-global 配置 (`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/<preset>/`) を相手にする。"このホストに preset が install されているか / 古くないか" を答える。host 全体の状態を見る command であり、特定の scaffold 済 project を必要としない。
- `mozyo-bridge scaffold apply <preset>` / `mozyo-bridge scaffold status` は repo-local routers (`AGENTS.md` / `CLAUDE.md` / `.mozyo-bridge/scaffold.json`) を相手にする。"この repo の manifest が user-global preset と on-disk router と整合しているか" を答える。1 つの scaffold 済 project の drift を見る command。
- `rules status` clean でも `scaffold status` は drift を出しうる (例: 古い manifest が新しい preset hash を指していない)。
- `scaffold status` clean でも、`rules status` で missing が出ていれば agent は起動時 guard で停止する。両方の clean が揃ってはじめて tester 環境は通る。

tester smoke check 観点:

1. `mozyo-bridge rules install` 実行後、`mozyo-bridge rules status` が `presets.yaml` の全 preset を expected version で報告する。
2. **isolated target を preset ごとに作る**。`./tmp/mb-smoke-asana`、`./tmp/mb-smoke-redmine`、`./tmp/mb-smoke-redmine-governed`、`./tmp/mb-smoke-redmine-rails` 等 (`./tmp/` は本 repo の gitignore 配下) もしくは fresh clone 別 directory を使う。本 repo の working tree (`mozyo_bridge` root) で `scaffold apply` を `--target` 無しで実行しないこと。tracked `AGENTS.md` / `CLAUDE.md` を上書き候補にしない。
3. dummy target に対して registry の各 preset を `mozyo-bridge scaffold apply <preset> --target ./tmp/mb-smoke-<preset>` で実行する。片側だけで終わらせない (preset 間 boundary の確認が落ちる)。
4. 各 dummy target で `mozyo-bridge scaffold status --target ...` が `result: clean` を返す。`central status` が `ok`、`router files` が全て `ok` の両方が出ていることを確認する。
5. `mozyo-bridge doctor --target ./tmp/mb-smoke-<preset>` を 1 command の acceptance smoke として使う。`scaffold` section が `ok`、`rules` / `codex_skill` / `claude_skill` / `cli` / `tmux` の各 section status と `next_action` を確認する。CI / 機械的 smoke では `--json` で `{"ok": <bool>, "sections": {...}}` を取り、`jq '.sections.scaffold.status == "ok"'` 等で gate を組む。
6. 生成された `AGENTS.md` / `CLAUDE.md` が preset 期待値を持つ。例:
   - router は `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/<preset>/agent-workflow.md` と active anchor label を含む。
   - router は `Redmine Gate Lifecycle`、`Audit-Owned Commit Authority`、Rails review details などの本文を複製しない。

`mozyo-bridge doctor` は host 全体の `rules status`、対象 project の `scaffold status`、Codex / Claude skill install、CLI 自体の readiness を 1 command で見る 6-section diagnostic。`rules status` / `scaffold status` の責務差は前述の通りで、doctor はそれらを束ねる acceptance gate であり、検証手順の最終 1 行として使う。`--home <path>` を渡すと診断対象 home と一致した `next_action` (例: `mozyo-bridge rules install --home <path>`) が出るため、CI / fresh smoke でそのまま実行できる。

PyPI release との見分け:

- `mozyo-bridge --version` の出力は `pyproject.toml` の package version 文字列であり、GitHub `main` で未 bump の状態だと PyPI release と同じ string が表示されうる。版確認に `mozyo-bridge --version` だけを使わず、(a) `mozyo-bridge scaffold status --help` / `mozyo-bridge doctor --json` などの GitHub `main` で追加された sub-command / flag が存在するか、(b) 生成された router の文言が GitHub `main` の preset 内容と一致するか、で確認する。
- docs 上は、未 PyPI release の GitHub `main` 変更を、すでに PyPI で利用可能であるかのように書かない。tester onboarding は GitHub `main` 経路と PyPI 経路を別物として扱う。
- PyPI / TestPyPI release の検証は、本節と同じ tester smoke を install 経路 (`pipx install mozyo-bridge` または TestPyPI install) に置き換えて実行する。release 経路の上位フローは `vibes/docs/logics/release-flow.md` を正本にする。

詳細な drift 状態の意味は本節の `Drift Detection` を正本にする。

## Router Template Single Source

`src/mozyo_bridge/scaffold/presets/_router/AGENTS.md` と `CLAUDE.md` は手作業で双子保守すると drift するため、`src/mozyo_bridge/scaffold/canonical_sources/router.yaml` を **canonical source** として一元管理する。tool (`codex` / `claude`) に応じた conditional fragment を canonical renderer が連結し、両 template を byte-equal に生成する。設計は `vibes/docs/logics/canonical-renderer.md` を読む。

操作:

```bash
mozyo-bridge scaffold canonical          # 全 canonical source を render し template に書き戻す
mozyo-bridge scaffold canonical --check  # drift を検出 (exit 1 で fail)
```

- `_router/AGENTS.md` / `_router/CLAUDE.md` は **generated artifact** として扱う。手編集禁止。canonical source 側を編集して `scaffold canonical` で再生成する。
- 新規 canonical 出力 (skill router 雛形、preset workflow など) を増やす場合は canonical-renderer.md の Extending To New Outputs に従う。
- canonical render は `scaffold apply` の **上流**。`scaffold apply` の downstream pipeline (`apply_project_local_preservation` 等) は引き続き `_router/*.md` を template として読む。両 layer は独立。

## Test Strategy

Implementation tests should cover:

- Parser choices come from `presets.yaml`.
- Parser rejects unsupported ticket systems.
- Rendering creates both `AGENTS.md` and `CLAUDE.md` for each preset.
- Rendering creates thin routers that reference `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/<preset>/agent-workflow.md`.
- `rules install` installs central preset docs for every registry preset.
- `scaffold apply <preset>` reports a clear error when the central preset is missing.
- Default behavior refuses to overwrite either existing router.
- Existing one-file state is handled atomically and does not leave a mismatched pair.
- `.mozyo-bridge/scaffold.json` is written with preset, version, paths, and file hashes.
- `--dry-run` writes nothing.
- `--backup` preserves previous files before replacement.
- `--force` replaces previous files only when explicitly provided.
- Rendered templates contain no private Notion URLs, credentials, or source-project paths.
- Repo-local mode covers `rules install --repo-local`, `rules status --repo-local`, `scaffold apply --repo-local`, `scaffold diff --repo-local`, the auto-detecting `scaffold status`, the manifest `mode` field, and the `--home` / `--repo-local` mutual exclusion. Repo-local artifacts must not leak host absolute paths.
- Redmine templates mention issue and journal gates.
- Asana templates mention task, project, and comment based handoffs.
- Asana and Redmine central presets include a Ticket-ID Entrypoint section that requires fetching the durable record before acting on pane or chat framing, and the section preserves each system's vocabulary.
- `none` templates clearly state that no external execution queue exists.

Use filesystem temporary directories for write behavior tests. Override `MOZYO_BRIDGE_HOME` in tests so central rule installation never touches the real user home. Keep tests independent from live Asana, Redmine, Notion, or tmux state.
