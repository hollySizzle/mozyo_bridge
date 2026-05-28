
## ClaudeCode 起動時の最小 reminder

- 迎合せず事実に基づいて結論を述べる。意見の不一致は `${rule_path}` が指定する durable record に残す。
- implementation done / implementation_done は completion ではない。review / audit / close 条件は `${rule_path}` に従う。
- pane 通知は通知でしかない。判断の正本は `${rule_path}` と active な `${ticket_anchor_label}` を読む。
- handoff を送る場合は `${rule_path}` の handoff startup decision / receive-method rule に従い、受領方法を durable record に残す。
- `mozyo-bridge status` / `mozyo-bridge doctor` / pane scrollback は operator/debug 用。durable anchor が利用可能なときに、それらから receiver state や ticket state を推測しない。
- handoff chat は state + durable anchor の最小ポインタにとどめる。受領方法・retry 計画・試行コマンドは durable record 側に置き、chat に貼り直さない。
- 詳細・例外・gate templates は `${rule_path}` を読む。router に重複させない。
