# Claude Code Router

@AGENTS.md

## 必須規約

非自明な作業を始める前に、mozyo-bridge の Asana central preset を読む:

- `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/asana/agent-workflow.md`

この file が存在しない場合は停止し、operator に以下の実行を依頼する:

```bash
mozyo-bridge rules install
```

## ClaudeCode 起動時の最小 reminder

- 迎合せず事実に基づいて結論を述べる。意見の不一致は Asana task comment に残す (chat だけで終わらせない)。
- implementation done は task complete ではない。review / audit comment が Asana task に記録されるまで完了報告しない。
- pane 通知は通知でしかない。判断の正本は常に Asana task description と task comment を読む。
- audit / design consultation を送ったら、受領方法を Asana task comment に必ず含める。受領方法は順序付きで考える: (1) **必須デフォルト** = standard path 通知 (`mozyo-bridge handoff send` / `mozyo-bridge message <target>` / `notify-*`) をまず試行する。(2) **precondition-gated fallback** = receiver pane が解決不能 (agent-name window が存在せず in-session で立ち上げられない) のときに限り `mozyo-bridge init <agent>` 案内 fallback。(3) **failure-only fallback** = standard path を実際に試行して delivery guard が hard failure を返した / 結果が使えないときに限り `未通知の明記` fallback。voluntary に standard path を skip して `未通知の明記` を選ぶのは禁止 (audit-only / revalidation / doc-only でも、receiver の `I will pull from the task record` 等 pickup 意思宣言があっても、standard path 試行義務は waiver されない)。receive-method comment には standard path の試行コマンドと結果 (試行不可なら不可と判断した precondition) を verbatim で残す。Asana comment / story id が利用可能ならそれを、利用できなければ task permalink + comment timestamp / context を受領 id として記録する。受領方法を書かずに handoff を完結させない。
- 送信は default で strict rail (`--mode standard`) を使う。codex TUI のように marker が wrap される既知 receiver で `marker_timeout` を踏んだときだけ、Claude / Codex agent pane 限定の `mozyo-bridge handoff send --mode queue-enter` (opt-in relaxed rail) に倒す。strict を default のまま黙って弱化しない。詳細は `vibes/docs/logics/tmux-send-safety-contract.md` の `## Relaxed Queue-Enter Rail` 節を読む。
- 詳細・例外・section templates は `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/asana/agent-workflow.md` を読む。重複させない。
