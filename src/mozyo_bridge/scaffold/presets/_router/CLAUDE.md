# Claude Code Router

@AGENTS.md

## 必須規約

非自明な作業を始める前に、mozyo-bridge の central preset を読む:

- `${rule_path}`

この file が存在しない場合は停止し、operator に以下の実行を依頼する:

```bash
mozyo-bridge rules install
```

## ClaudeCode 起動時の最小 reminder

- 迎合せず事実に基づいて結論を述べる。意見の不一致は `${rule_path}` が指定する durable record に残す。
- implementation done / implementation_done は completion ではない。review / audit / close 条件は `${rule_path}` に従う。
- pane 通知は通知でしかない。判断の正本は `${rule_path}` と active な `${ticket_anchor_label}` を読む。
- handoff を送る場合は `${rule_path}` の handoff startup decision / receive-method rule に従い、受領方法を durable record に残す。
- `mozyo-bridge status` / `mozyo-bridge doctor` / pane scrollback は operator/debug 用。durable anchor が利用可能なときに、それらから receiver state や ticket state を推測しない。
- handoff chat は state + durable anchor の最小ポインタにとどめる。受領方法・retry 計画・試行コマンドは durable record 側に置き、chat に貼り直さない。
- 詳細・例外・gate templates は `${rule_path}` を読む。router に重複させない。

## Project-Local Additions

<!-- mozyo-bridge:project-local-additions:begin -->
<!--
このマーカー間は `mozyo-bridge scaffold apply` / `scaffold diff` で機械的に保持されます。
ClaudeCode 起動時に project-local で必ず思い出してほしい reminder (危険 command、
Doc-readonly 領域、project 固有 role boundary override 等) をここに追記してください。
マーカー外の内容は scaffold 再生成で上書きされます。
-->
<!-- mozyo-bridge:project-local-additions:end -->
