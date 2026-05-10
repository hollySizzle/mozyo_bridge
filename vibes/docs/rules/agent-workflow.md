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
- `.agent_handoff/tasks.json` は retired queue の棚卸し用であり、standard notification fallback として扱わない。

## Audit Handoff (Claude → Codex)

- Claude が code、documentation、設定を作成、修正、削除した task は完了前に必ず Codex に audit を依頼する。documentation のみの変更でも省略しない。
- 依頼経路は `mozyo-bridge message codex <text>` を標準とする。`message` 送信前には同 pane を `mozyo-bridge read codex` で確認する (mozyo-bridge の message safety guard)。
- audit 依頼 message には次を含める。
  - Asana task の URL
  - 変更ファイルの一覧
  - 実施した verification
  - 重点的に audit してほしい観点
- Codex の audit feedback が Asana コメントまたは明示的な通知として返るまで、Claude 側の task を completed として扱わない。pane に echo されただけの応答を audit pass と判定しない。
- audit で issue が指摘された場合は修正 commit を打ち、同じ Codex pane に再 audit を依頼する。
- この mandatory audit rule は `mozyo_bridge` repository の project-local policy であり、shared skill や scaffold preset へ一般化しない。
- `mozyo-bridge scaffold rules <preset>` ではユーザーが ticket system preset を明示選択する。選択された preset の workflow だけを適用し、他 preset やこの repo 固有の audit policy を混ぜない。

## 禁止事項

- root の `AGENTS.md` / `CLAUDE.md` に詳細規約を大量貼り付けしない。
- `vibes/tools/mozyo_bridge` を runtime path として再導入しない。
- Redmine / Rails / vibes 前提の別 project 規約を、この repository に無断で持ち込まない。
- generated build outputs を commit しない: `build/`, `dist/`, `*.egg-info/`, `__pycache__/`。
