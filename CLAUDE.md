# Claude Code Router

Claude Code セッションの tool-specific 入口。Claude Code は本ファイルを native に読む。共通の central preset rules は `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine-governed/agent-workflow.md` を正本とし、router 本文には複製しない。AGENTS.md (Codex tool-specific) を import しない。

## セッション開始

1. 現在の working directory がこの project root またはその配下であることを確認する。
2. mozyo-bridge の central preset rules を読む:
   - committed docs では portable 表記 `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine-governed/agent-workflow.md` を使う。
   - runtime で実ファイルを読む際は `mozyo-bridge rules home --resolved` の出力に `/rules/presets/redmine-governed/agent-workflow.md` を連結した絶対 path を読む。`--resolved` 出力は debug / runtime 用で、committed docs に貼らない。
   - resolved path や central preset を読めない場合は、読んだふりをせず停止し、`mozyo-bridge rules install` 等の復旧を operator に求める。
3. 非自明な作業を始める前に active な `Redmine issue / journal と project docs` を確認する。

`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine-governed/agent-workflow.md` が存在しない場合は、読んだふりをせず停止し、operator に `mozyo-bridge rules install` を依頼する。

## ClaudeCode 起動時の最小 reminder

- 迎合せず事実に基づいて結論を述べる。意見の不一致は `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine-governed/agent-workflow.md` が指定する durable record に残す。
- implementation done / implementation_done は completion ではない。review / audit / close 条件は `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine-governed/agent-workflow.md` に従う。
- pane 通知は通知でしかない。判断の正本は `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine-governed/agent-workflow.md` と active な `Redmine issue / journal と project docs` を読む。
- handoff を送る場合は `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine-governed/agent-workflow.md` の handoff startup decision / receive-method rule に従い、受領方法を durable record に残す。
- `mozyo-bridge status` / `mozyo-bridge doctor` / pane scrollback は operator/debug 用。durable anchor が利用可能なときに、それらから receiver state や ticket state を推測しない。
- handoff chat は state + durable anchor の最小ポインタにとどめる。受領方法・retry 計画・試行コマンドは durable record 側に置き、chat に貼り直さない。
- 詳細・例外・gate templates は `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine-governed/agent-workflow.md` を読む。router に重複させない。

## Project-Local Additions

<!-- mozyo-bridge:project-local-additions:begin -->

## mozyo_bridge Project-Local Rules

- Repository: `mozyo_bridge` Python package / CLI project.
- Runtime package path: `src/mozyo_bridge/`.
- Test path: `tests/`.
- Project docs namespace: `vibes/docs/`.
- Skill source: `skills/mozyo-bridge-agent/`.
- Plugin mirror: `plugins/mozyo-bridge-agent/`.
- Do not reintroduce `vibes/tools/mozyo_bridge` as a runtime path.
- Generated build outputs are not committed: `build/`, `dist/`, `*.egg-info/`, `__pycache__/`.

## Local Source Of Truth

- Redmine project: `giken-3800-mozyo-bridge`.
- Redmine issue / journal is the durable work record for new work.
- `AGENTS.md` and `CLAUDE.md` are thin routers. Keep detailed rules in the central preset, `.mozyo-bridge/rules/**`, or cataloged docs.
- Local references:
  - `skills/mozyo-bridge-agent/references/workflow.md`
  - `skills/mozyo-bridge-agent/references/project-map.md`
  - `skills/mozyo-bridge-agent/references/release.md`
  - `skills/mozyo-bridge-agent/references/safety.md`
  - `vibes/docs/logics/scaffold-rules.md`
  - `vibes/docs/logics/bootstrap.md`
  - `vibes/docs/rules/codex-autonomous-guardrail-lane.md`

## Claude Code Role

- Claude Code is the default implementer for normal development tasks AND for guardrail / docs / catalog scope work in this repository. Surfaces include:
  - Implementation files: `src/**`, `tests/**`, `docs/**`, `vibes/docs/**`, `README.md`, `RELEASE_NOTES.md`, release workflow, CLI behavior.
  - Guardrail / custom-instruction / scaffold-rule surfaces: `AGENTS.md`, `CLAUDE.md`, `.mozyo-bridge/rules/**`, `.mozyo-bridge/docs/catalog.yaml`, `.codex/skills/**`, `.claude/skills/**`, `skills/mozyo-bridge-agent/**`, `plugins/mozyo-bridge-agent/**`, scaffold packaged preset / router templates under `src/mozyo_bridge/scaffold/presets/**`.
- Repo-Local Guardrail Autonomous Lane (Redmine #10338, mozyo-bridge product-wide policy distributed via `redmine-governed` / `redmine-rails-governed` presets): `vibes/docs/rules/**`, `vibes/docs/logics/**`, `vibes/docs/specs/**`, `.mozyo-bridge/docs/catalog.yaml` are a Codex-autonomous carve-out from the standard `codex_direct_edit` gate. Codex may edit these paths without a pre-edit gate journal; instead the central preset's `### Repo-Local Guardrail Autonomous Lane` section requires a `codex_autonomous_edit` journal recorded with the commit (lane / changed_paths / intent / verification / commit_hash / follow_up_review_required). Claude remains an allowed implementer for these paths and follows the standard Implementation Done / Review Request flow when handed off. The carve-out does NOT extend to `AGENTS.md`, `CLAUDE.md`, `.mozyo-bridge/rules/**`, skills, plugins, scaffold preset templates, `src/**`, or `tests/**`. See `vibes/docs/rules/codex-autonomous-guardrail-lane.md` for the repo-specific adoption details and the required verification commands when `.mozyo-bridge/docs/catalog.yaml` is touched.
- Start normal development from the active Redmine issue and parent Feature / Epic, not from chat text alone.
- Standard audit granularity is the UserStory (central preset `### US-Level Audit Model`). Implement the US's child Task / Test / Bug issues as one unit, record implementation / verification / residual-risk journals per child issue, then record the US-level Implementation Done and Review Request (US audit request) gates and hand off to Codex. Per-task Codex review is NOT required unless a task-level exception applies (guardrail / workflow / preset / router / skill change, release / packaging / CI, credential, destructive operation / migration, architecture or compatibility change, implementer judgment doubt, or an explicit owner / auditor request). The US-level Codex audit before US close is mandatory, including for doc-only and rule-only scope. Standalone issues without a parent US keep the per-issue review flow.
- Do not mark a UserStory (or standalone issue) complete after implementation only. US-level audit Review Gate, owner close approval, and commit hash record must exist before close. Child Task / Test / Bug issues close on replayable implementation_done journals (verification and residual risk included) and are reopened if the US audit finds gaps.
- `.mozyo-bridge/docs/file_conventions.generated.yaml` is generator output. Do NOT hand-edit it from Claude either. Change `.mozyo-bridge/docs/catalog.yaml` and regenerate with `mozyo-bridge docs generate-file-conventions`, then verify with `--check`.
- When Codex receives a normal development or guardrail/docs/catalog request **outside the autonomous lane**, the default action is a Claude handoff. Short imperative phrases (`進めて`, `対応して`, `Codex でやって`, `go ahead`, `please do it`) do not authorize a Codex direct edit on those gated surfaces — only a Redmine `codex_direct_edit` gate journal with `allowed_paths` does. For autonomous-lane paths Codex may proceed directly and record `codex_autonomous_edit` instead. If Claude receives instructions targeting a gated surface, Claude implements; Codex does not get repurposed as the implementer for that surface.

## Verification And Commit

- Use focused `python -m unittest ...` targets when `pytest` is unavailable. Do not install packages just to run tests unless the user approves.
- For scaffold changes, run `mozyo-bridge scaffold status --target .` before commit.
- For governed catalog changes, run `mozyo-bridge docs validate --repo .` and the relevant generator / coverage checks when `catalog.yaml` exists.
- Run `git diff --check` or `git diff --cached --check` before commit.
- Commit only files in the active Redmine issue scope. Leave unrelated dirty files untouched.
- Commit messages for Redmine-scoped work include `Refs: Redmine #<issue_id>` and `issue_<issue_id>`.
- After committing, record the commit hash in the same Redmine issue journal.

<!-- mozyo-bridge:project-local-additions:end -->
