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
- Codex へ review / design consultation を送ったら、受領方法 (高レベル primitive `mozyo-bridge handoff send` / `mozyo-bridge handoff reply` / 上位 alias `mozyo-bridge reply` を必須デフォルトとして試行; `notify-*` は内部で同じ primitive を呼ぶ Redmine 互換 wrapper / operator が pane を立ち上げる手順 / 未通知の明記) を Redmine 記録に必ず含める。`mozyo-bridge read` + `mozyo-bridge message` + `mozyo-bridge type` / `keys` を手で組み立てる経路は operator/debug 用であり、standard handoff/reply の代替にしない。`Codex受領方法` を書かずに handoff を完結させない。
- `mozyo-bridge status` / `mozyo-bridge doctor` / pane scrollback は operator/debug 用。durable Redmine anchor (issue / 指定 journal) が利用可能なときに、それらから receiver state や issue state を推測しない。判断の正本は常に Redmine issue と journal を読む。
- handoff chat (review / design consultation / 未通知 / 受領 pending 系) は state + issue / journal id の最小ポインタにとどめる。受領方法・retry 計画・試行コマンドは Redmine 側に置き、chat に貼り直さない。
- 詳細・例外・gate templates は `${rule_path}` を読む。重複させない。
