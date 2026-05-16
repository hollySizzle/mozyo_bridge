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
- audit / design consultation を送ったら、受領方法を Asana task comment に必ず含める。受領方法は順序付きで考える: (1) **必須デフォルト** = standard path 通知 (`mozyo-bridge handoff send` / `mozyo-bridge message <target>` / `notify-*`) をまず試行する。(2) **precondition-gated fallback** = receiver pane が解決不能 (agent-name window が存在せず in-session で立ち上げられない) のときに限り `mozyo-bridge init <agent>` 案内 fallback。(3) **failure-only fallback** = standard path を実際に試行して delivery guard が hard failure を返した / 結果が使えないときに限り `未通知の明記` fallback。voluntary に standard path を skip して `未通知の明記` を選ぶのは禁止 (audit-only / revalidation / doc-only でも、receiver の `I will pull from the task record` 等 pickup 意思宣言があっても、standard path 試行義務は waiver されない)。receive-method comment には standard path の試行コマンドと結果 (試行不可なら不可と判断した precondition) を verbatim で残す。Asana comment / story id が利用可能ならそれを、利用できなければ task permalink + comment timestamp / context を受領 id として記録する。受領方法を書かずに handoff を完結させない。
- handoff chat (audit / 未通知 / 受領 pending 系) は state + task id の最小ポインタにとどめる。受領方法・retry 計画・試行コマンドは task comment 側に置き、chat に貼り直さない。
- agent pane handoff (`mozyo-bridge handoff send` / `handoff reply` / `notify-*` 標準 variants) は v0.4 以降 `--mode queue-enter` が default。Claude / Codex agent pane に限定され、`--force` 不可、typing 前に deterministic preflight (a) explicit `--target` は receiver の tmux window 配下、(b) target pane は sender と同じ tmux session、(c) target pane は所属 window の active split、(d) foreground process が receiver の allowlist (`claude` literal=claude-strong、`codex` literal=codex-strong、`node` literal と versioned native binary basename は両 receiver で weak admit) を要求する。1 つでも false なら typing 前に `blocked` で die する。preflight を通過した送信は marker 未観測でも Enter を発行し durable outcome は `sent` / `queue_enter` として記録する (confirmed landing の約束ではない; 受信側は引き続き Asana task comment を正本として読む)。strict landing observation が必要な送信 (regression check / brand-new pane / observability test / 厳格な landing evidence が監査要件) または default scope 外 (`mozyo-bridge message` / non-agent pane) のときは `--mode standard` を explicit fallback として明示する。strict rail の挙動は v0.1 と同じで、marker 未観測は `C-u` rollback + `blocked` / `marker_timeout` で fail-closed する。default を黙って弱化せず、strict fallback を選んだ理由は task comment に記録する。
- 詳細・例外・section templates は `${rule_path}` を読む。重複させない。
