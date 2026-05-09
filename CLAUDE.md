# agent and leader project router

**重要**: あなたは AI agent である。ユーザーに迎合せず、事実確認に基づいて技術的に正直な結論を述べる。

@AGENTS.md

## Session Bootstrap

1. `AGENTS.md` の「起動時に読むもの」を確認する。
2. Notion グローバル規約を fetch する。
3. `cwd`、Asana project、active task を確認する。
4. 実作業の詳細は `vibes/docs/` 配下の該当文書を読む。

## Router

- 作業規約: `vibes/docs/rules/agent-workflow.md`
- project 構造: `vibes/docs/specs/project-map.md`
- release 判断と検証: `vibes/docs/logics/release-flow.md`
- skill 配布方針: `vibes/docs/logics/skill-distribution.md`
- Asana task 作成テンプレート: `vibes/docs/temps/asana-task.md`
- shared skill: `skills/mozyo-bridge-agent/SKILL.md`

## Guardrails

- root router を詳細規約で肥大化させない。
- Asana task なしに非自明な作業を始めない。
- real token を記録・commit しない。
- local token upload より GitHub Actions Trusted Publishing を優先する。
- `mozyo-bridge` の通知は作業開始の合図にすぎない。必ず source of truth を確認する。
