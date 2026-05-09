# Agent Workflow Rules

## 目的

この文書は `mozyo_bridge` repository で作業する AI agent の実行規約である。root の `AGENTS.md` / `CLAUDE.md` は router に留め、詳細規約はこの文書に置く。

## 作業開始

- セッション開始時に Notion のグローバル規約を fetch する。
- 現在の `cwd` が対象 repository root、またはその配下であることを確認する。
- Asana project `mozyo_bridge` の project notes を確認する。
- active な Asana task を確認する。該当 task がない場合は、実装前に作成する。

## Asana 運用

- Asana は実行キューである。
- Task は実行単位であり、目的、作業対象、成果物、完了条件を持つ。
- 作業が完了、block、または scope 変更された場合は、該当 task の comment または notes を更新する。
- chat message を durable な作業ログとして扱わない。
- task scope が膨らんだ場合は、黙って削らず follow-up task に分割する。

## Secret Handling

- PyPI / TestPyPI token、API key、personal credential、個人情報を repository、Asana、Notion に記録しない。
- `.env`、`.env.*`、`.pypirc` は local-only の secret surface とし、ignored のままにする。
- production publish は local token upload を標準 route にしない。

## mozyo-bridge の扱い

- `mozyo-bridge` は notification transport であり、review、completion、task state の source of truth ではない。
- pane message を受けた agent は、作業前に Asana task または明示された source of truth を確認する。
- marker が観測される前に Enter を送る safety behavior を壊さない。

## 禁止事項

- root の `AGENTS.md` / `CLAUDE.md` に詳細規約を大量貼り付けしない。
- `vibes/tools/mozyo_bridge` を runtime path として再導入しない。
- Redmine / Rails / vibes 前提の別 project 規約を、この repository に無断で持ち込まない。
- generated build outputs を commit しない: `build/`, `dist/`, `*.egg-info/`, `__pycache__/`。
