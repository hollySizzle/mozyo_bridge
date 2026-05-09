# AGENTS

このファイルは、この repository で作業する AI agent 向けの root router である。詳細規約や運用手順はここに貼り付けず、必要な正本へ誘導する。

## 起動時に読むもの

1. Notion のグローバル規約 `global agent rules`
   - <private-notion-rules-url>
   - Notion MCP で取得できない場合は、読んだふりをせずユーザーへ通知して停止する。
2. 現在の `cwd`
3. Asana project `mozyo_bridge`
   - <private-asana-project-url>
4. active な Asana task

## この repo の router

- agent workflow: `vibes/docs/rules/agent-workflow.md`
- project map: `vibes/docs/specs/project-map.md`
- release / verification logic: `vibes/docs/logics/release-flow.md`
- Asana task template: `vibes/docs/temps/asana-task.md`
- package metadata: `pyproject.toml`
- usage / safety: `README.md`
- CI / publish workflows: `.github/workflows/`
- tmux smoke test: `smoke/real_tmux_notify_smoke.py`

## 最小原則

- Asana を実行キュー、Notion を規約・知識の正本、この repository を code と release artifact の正本として扱う。
- 非自明な作業は Asana task から始める。task がなければ先に作る。
- credential、token、個人情報を commit しない。`.env` は local-only とする。
- `mozyo-bridge` の pane message は通知であり、authoritative state ではない。
- root の `AGENTS.md` / `CLAUDE.md` を詳細規約置き場にしない。

## 注意

`vibes/docs/` はこの repository の documentation namespace であり、runtime path ではない。`vibes/tools/mozyo_bridge` を runtime として再導入しない。
