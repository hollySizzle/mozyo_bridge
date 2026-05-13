# PTY First Agent Runtime Draft

## Status

- Draft: `v0.1`
- Scope: `mozyo-bridge` の post-tmux architecture direction
- Decision level: 実装凍結ではなく architecture direction

## この文書の目的

現在の `mozyo-bridge` runtime は tmux-centric である。

- agent addressing が tmux の session / window / pane identity に依存している
- terminal に見えている表示と runtime state が結びついている
- notification delivery が pane reachability に引きずられる
- window / pane layout の選択が control plane に漏れている

現状の bridge CLI には十分でも、次の基盤としては弱い。

- durable multi-turn agent session
- role ごとの ticket-driven orchestration
- structured event logging
- attach / detach 後も失われない runtime state
- 将来の multi-provider support

この draft は、次の中間目標を定義する。

- MVP では PTY-first runtime
- session state と event log は agentd 側が所有
- CLI attach は display plane
- ACP は adapter path であり、MVP critical path ではない

## Non-Goals

この draft は次を前提にしない。

- 初期実装での VS Code extension
- VS Code terminal の自動分割
- 現行 tmux runtime の即時削除
- Claude / Codex / Gemini が同品質で provider-neutral abstraction に収まるという前提

## 中核方針

初期 runtime の方針は次とする。

- `PTY primary for MVP`
- `ACP preferred when available`
- `legacy tmux retained as compatibility runtime during migration`

これは `ACP primary, PTY fallback` とは意図的に異なる。

理由:

- 既存 CLI は今日すでに terminal app として動いている
- provider-native ACP support は uneven である
- orchestration で最初に必要なのは、きれいな protocol より reliable な spawn / stream / input / cancel / reconnect である

## Source of Truth Layers

target architecture では、責務を明示的に分ける。

- ticket system: 作業指示の source of truth
- mozyo DB: runtime state の source of truth
- provider runtime session: 実行コンテキスト
- attach client: human-visible display only

terminal UI は runtime state の source of truth にしない。

## Cardinality

`1 ticket = 1 session` を architecture に埋め込んではならない。

それは実運用には硬すぎる。安全なモデルは次である。

- `1 ticket -> 1..n agent sessions`
- `1 agent session -> 1 role`
- `1 agent session -> 1 active runtime session`
- `1 agent session -> 1..n prompt turns`
- `1 prompt turn -> 0..n agent events`

この柔軟性が必要になる例:

- 1 ticket に対して writer / reviewer が並走する
- 同じ role で retry が複数回発生する
- 1 ticket から implementation / audit の別 session が生える
- terminal restart 後に長寿命 session へ再 attach する

## 想定 runtime 形状

### High-Level Structure

```text
ticket / journal / task comment
        |
        v
    mozyo CLI
        |
        v
   local IPC / RPC
        |
        v
  mozyo-agentd (state owner)
        |
        +-- PTY runtime adapter
        |      |
        |      +-- Claude CLI
        |      +-- Codex CLI
        |      +-- Gemini CLI
        |
        +-- ACP runtime adapter
        |      |
        |      +-- provider-native ACP when available
        |
        +-- legacy tmux adapter
               |
               +-- migration / compatibility only
```

### Runtime Ownership

`mozyo-agentd` が所有するもの:

- session lifecycle
- prompt turn lifecycle
- event normalization と persistence
- runtime adapter selection
- attach stream fan-out
- journal cursor / polling state

terminal は session state を所有しない。

## なぜ PTY First か

### 利点

- provider protocol の成熟を待たず、今日動いている CLI を使える
- human-in-the-loop を自然に扱える
- tmux の UI multiplexer semantics を control plane から外せる
- VS Code integrated terminal の手動 split 運用と相性がよい
- provider が terminal-first であっても対応しやすい

### 欠点

- output parsing と prompt boundary detection は依然必要
- text injection は structured protocol input より弱い
- resize / reconnect / EOF / long-running process の扱いを明示設計する必要がある
- PTY だけでは domain model にならず、event layer は別途必要

PTY は最終 abstraction ではない。最初に最も実用的な substrate である。

## ACP の位置づけ

ACP は architecture から外さない。ただし MVP gate にもしない。

### ACP が得意なもの

- structured prompt submission
- structured updates
- cancellation semantics
- provider-neutral event mapping の改善

### ACP を MVP critical path にしない理由

- provider 間で ACP maturity が揃っていない
- 実際には wrapper や adapter が必要になる
- ACP が partial / unstable / unavailable でも orchestration 自体は動く必要がある

したがって正しい順序は次である。

- provider-agnostic runtime port を定義する
- PTY を先に実装する
- ACP adapter は provider ごとに「本当に十分なときだけ」足す

## 推奨 language split

今の repository からの最も保守的な移行案は次である。

- orchestration / CLI / rules / scaffold / ticket integration は Python に残す
- PTY-heavy runtime control だけ Node sidecar に逃がす

理由:

- 現在の repo には Python packaging / tests / release flow がすでにある
- `node-pty` は cross-platform PTY handling の有力候補である
- 全面 rewrite を避けつつ tmux 依存から抜けられる

### Suggested Boundary

Python が持つもの:

- CLI commands
- ticket integration
- durable state decisions
- repositories / SQLite models
- business logic

Node sidecar が持つもの:

- PTY process spawn
- terminal stream handling
- resize / signal forwarding
- input injection
- 必要なら attach stream fan-out の低レベル部

通信は local IPC とする。候補:

- Unix domain socket / named pipe
- stdio child process protocol
- localhost loopback restricted to current user

## Domain Model Draft

### AgentSession

durable な agent work unit。

最低限必要な field:

- `agent_session_id`
- `ticket_id` または generic work item id
- `role`
- `provider`
- `runtime_kind`
- `workspace_path`
- `worktree_path`
- `branch_name`
- `state`
- `runtime_session_ref`
- `created_at`
- `updated_at`

### PromptTurn

runtime へ送る 1 回の明示 instruction。

field:

- `prompt_turn_id`
- `agent_session_id`
- `source_kind` (`manual`, `journal`, `retry`, `system`)
- `source_ref`
- `prompt_text`
- `state`
- `retry_of_prompt_turn_id`
- `created_at`
- `started_at`
- `completed_at`

### AgentEvent

normalized runtime output と raw payload の両方を持つ event。

field:

- `agent_event_id`
- `agent_session_id`
- `prompt_turn_id`
- `event_type`
- `source`
- `raw_payload`
- `normalized_payload`
- `created_at`

## Runtime Port

runtime abstraction は狭く保つべきである。

```text
create_session
send_prompt
send_input
cancel
resize
subscribe_events
dispose
```

これにより、PTY と ACP を無理に 1:1 対応させずに application layer の契約を安定化できる。

## Attach Model

`mozyo attach` は read/write stream client であり、session owner ではない。

attach requirements:

- 複数 attach client を許可する
- attach 開始時に recent buffered history を返す
- その後 live event subscription に入る
- user input は新しい prompt turn か explicit control action に変換できる
- attach disconnect は session を kill しない

これは現在の「tmux pane に居続けないと attach できない」モデルの置き換えである。

## Session States

候補:

- `created`
- `starting`
- `idle`
- `prompting`
- `running`
- `waiting_permission`
- `cancelling`
- `cancelled`
- `completed`
- `failed`
- `detached`
- `dead`

重要なのは、`detached` は process state ではなく client/view state だという点である。

## tmux の migration 方針

tmux は「primary runtime substrate」から「compatibility adapter」へ下げる。

### しばらく残すもの

- 現行 notification flow
- 現行 repo-scoped session bootstrap
- 現行 smoke coverage

### これ以上拡張しないもの

- tmux pane identity を state の source of truth にしない
- 新しい session model が `capture-pane` 前提にならないようにする
- orchestration の新機能が `send-keys` だけに依存しないようにする

## MVP Phases

### Phase 1: Durable Session Core

- SQLite-backed `AgentSession`, `PromptTurn`, `AgentEvent`
- session CRUD
- event log read APIs
- provider coupling は stub のみ

### Phase 2: PTY Runtime

- provider CLI を PTY で spawn
- output を event 化
- explicit input / cancel
- process lifecycle handling

### Phase 3: CLI Attach

- attach to session
- history replay
- live event stream
- human input routing

### Phase 4: Ticket Integration

- journal polling または task comment polling
- durable ticket state から prompt generation
- summary generation と postback

### Phase 5: ACP Adapter

- provider-specific ACP support
- 同一 `AgentEvent` model への mapping
- provider ごとの runtime selection

### Phase 6: Migration / UI Expansion

- optional VS Code extension
- optional web UI
- optional remote agentd
- tmux dependency の段階縮小

## 早めに検証すべきリスク

### Prompt Boundary Detection

PTY output から provider ごとの「assistant turn 終了」を安定検出できるか。

### Human Input Races

automation と user が同じ interactive session に書き込むときの ownership。

### Permission Flows

permission prompt を free text のままにせず `AgentEvent` 化できるか。

### Long-Running Sessions

少なくとも次に耐える必要がある。

- terminal close
- agent CLI crash
- sidecar restart
- stalled output

### Provider Drift

provider CLI の表示挙動は marketing surface を変えずに壊れることがある。

だからこそ「ACP があるはず」で設計を固定しない。

## Open Questions

実装凍結前に詰めるべき論点:

1. Python が都度 Node sidecar を起動するのか、長寿命共有 sidecar にするのか
2. PTY mode で provider ごとの prompt-completed signal は何か
3. attach 中の人間入力と自動 journal prompt をどう serialize するか
4. core schema は Redmine-specific の `ticket_id` を持つのか、generic work-item key に寄せるのか
5. 既存 tmux label から将来の `AgentSession` id への migration contract は何か

## 短い結論

近い将来の architecture は「ACP everywhere now」ではない。

次である。

- durable orchestration state は mozyo-owned storage
- 最初の実用 runtime substrate は PTY
- ACP は強い provider から順に adapter として足す
- 最初の display plane は CLI attach
- tmux は migration / compatibility layer に下げる

これが、tmux-centric control plane から抜けつつ、provider protocol がまだ十分成熟しているふりをしない最小リスクの道筋である。
