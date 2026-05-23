# Claude Code Router

Claude Code セッションの tool-specific 入口。Claude Code は本ファイルを native に読む。共通の central preset rules は `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine-governed/agent-workflow.md` を正本とし、router 本文には複製しない。AGENTS.md (Codex tool-specific) を import しない。

## セッション開始

1. 現在の working directory がこの project root またはその配下であることを確認する。
2. mozyo-bridge の central preset rules を読む:
   - `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine-governed/agent-workflow.md`
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

## Claude Code Role

- Claude Code is the default implementer for normal development tasks.
- Start normal development from the active Redmine issue and parent Feature / Epic, not from chat text alone.
- Record Implementation Done and Review Request gates in Redmine before asking for Codex review.
- Do not mark an issue complete after implementation only. Review Gate, owner close approval, and commit hash record must exist before close.

## Verification And Commit

- Use focused `python -m unittest ...` targets when `pytest` is unavailable. Do not install packages just to run tests unless the user approves.
- For scaffold changes, run `mozyo-bridge scaffold status --target .` before commit.
- For governed catalog changes, run `mozyo-bridge docs validate --repo .` and the relevant generator / coverage checks when `catalog.yaml` exists.
- Run `git diff --check` or `git diff --cached --check` before commit.
- Commit only files in the active Redmine issue scope. Leave unrelated dirty files untouched.
- Commit messages for Redmine-scoped work include `Refs: Redmine #<issue_id>` and `issue_<issue_id>`.
- After committing, record the commit hash in the same Redmine issue journal.

<!-- mozyo-bridge:project-local-additions:end -->
