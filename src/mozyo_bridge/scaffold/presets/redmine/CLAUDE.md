# Claude Code Router

@AGENTS.md

## 必須規約

非自明な作業を始める前に、mozyo-bridge の Redmine central preset を読む:

- `${rule_path}`

この file が存在しない場合は停止し、operator に以下の実行を依頼する:

```bash
mozyo-bridge rules install
```

## ClaudeCode 起動時の最小 reminder

- 迎合せず事実に基づいて結論を述べる。意見の不一致は Redmine gate に残す (chat だけで終わらせない)。
- implementation_done は completion ではない。Review Gate が Redmine に記録されるまで完了報告しない。
- pane 通知は通知でしかない。判断の正本は常に Redmine gate を読む。
- Codex へ review / design consultation を送ったら、受領方法 (`mozyo-bridge notify-*` 通知 / operator が pane を立ち上げる手順 / 未通知の明記) を Redmine 記録に必ず含める。`Codex受領方法` を書かずに handoff を完結させない。
- handoff chat (review / design consultation / 未通知 / 受領 pending 系) は state + issue / journal id の最小ポインタにとどめる。受領方法・retry 計画・試行コマンドは Redmine 側に置き、chat に貼り直さない。
- 詳細・例外・gate templates は `${rule_path}` を読む。重複させない。
