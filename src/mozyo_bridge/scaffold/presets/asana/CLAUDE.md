# Claude Code Router

@AGENTS.md

## 必須規約

非自明な作業を始める前に、mozyo-bridge の Asana central preset を読む:

- `${rule_path}`

この file が存在しない場合は停止し、operator に以下の実行を依頼する:

```bash
mozyo-bridge rules install
```

## ClaudeCode 起動時の最小 reminder

- 迎合せず事実に基づいて結論を述べる。意見の不一致は Asana task comment に残す (chat だけで終わらせない)。
- implementation done は task complete ではない。review / audit comment が Asana task に記録されるまで完了報告しない。
- pane 通知は通知でしかない。判断の正本は常に Asana task description と task comment を読む。
- audit / design consultation を送ったら、受領方法 (`mozyo-bridge message` / `notify-*` 通知 / operator が pane を立ち上げる手順 / 未通知の明記) を Asana task comment に必ず含める。Asana comment / story id が利用可能ならそれを、利用できなければ task permalink + comment timestamp / context を受領 id として記録する。受領方法を書かずに handoff を完結させない。
- 詳細・例外・section templates は `${rule_path}` を読む。重複させない。
