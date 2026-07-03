# ACK / Completion / Receiver-State Boundaries

## Status

- Draft: `v0.2`
- Scope: 現行 `mozyo-bridge` の tmux runtime において、`delivery ACK` / `task completion` / `receiver-state observability` を別概念として固定し、長期 PTY runtime (`mozyo_bridge_pty`) との接続を明示する doctrine
- Decision level: architecture / doctrine boundary。CLI / preset / skill 実装の挙動変更は本 doc の射程外
- 関連 task: Asana `1214767912195770` (handoff ack / receiver state observability 議論を vibes/docs/logics に反映する)
- Redmine #12194: receiver ACK / completion signal groundwork。#12223 runtime observation snapshot、#12224 reload、#12226 action-time preflight、#12227 future sidecar split、#12229 duplicate receiver advisory の close/integration 後に、本 doc へ signal layer 境界を追記する
- 上流 contract (前提): `mozyo_bridge_pty/vibes/docs/specs/transport-agnostic-ack-state-contract.md`、`mozyo_bridge_pty/vibes/docs/specs/receiver-state-inspector-contract.md`

## この文書の目的

`mozyo-bridge` の handoff / notify 経路で、`Enter-send` / `marker_timeout` / `--no-submit` retry / `un-notified` 分岐の運用判断が、しばしば三つの別概念を取り違える形で歪む。

- 「送信が届いた (ACK)」と「task が完了した (completion)」を同じ概念として扱う
- 「pane に文字列が出てこなかった」「prompt が idle に見える」だけで receiver が完了したと推定する
- `tmux capture-pane` の rendered text を ACK / completion の **正本** として暗黙に昇格させる
- 「completion detector を強くする」方向に問題を矮小化し、本当に必要な receiver-state observability を後回しにする

本 doc は、現行 tmux runtime に残しつつ、

1. ACK と completion を別物として定義し直す
2. terminal / pane text を完了 (completion) の source of truth から外す
3. receiver-state observability を別 axis として明示する
4. tmux 経路の rendered-text 観測を長期的には fallback として扱う
5. 長期方向 (sidecar / control-event / `mozyo_bridge_pty`) との接続を doctrine として固定する

ことを目的とする。CLI / preset / skill / 実装ファイルへの挙動変更を伴う改修は別 task が所有し、本 doc は伴わない。

> 配布注記 (#13060): 三概念の分離 (delivery ACK / receiver state / task completion) と、それを取り違えない運用規範の portable 正本は、配布側 `skills/mozyo-bridge-agent/references/workflow.md` の `## ACK / delivery / completion の分離` にある。本 doc はその repo-local doctrine 深部 — signal layer model、runtime event 語彙、`DeliveryOutcome` 射影、`mozyo_bridge_pty` workstream との接続段階 — の正本であり続ける。

## Non-Goals

- `wait_for_text` / `marker` 観測 / `C-u` rollback / queue-enter rail の挙動変更 (`vibes/docs/logics/tmux-send-safety-contract.md` が正本)
- `--no-submit` retry budget や `Notification fails` 分岐の precondition 改定 (該当 task / preset が正本)
- provider 別 assistant turn completion 判定 (`mozyo_bridge_pty/vibes/docs/specs/pty-event-normalization.md` が正本)
- inspector query / subscribe surface の最終仕様 (`mozyo_bridge_pty/vibes/docs/specs/receiver-state-inspector-contract.md` が正本)
- task completion の judging logic (ticket system 側の acceptance criteria が正本)

## 用語整理 (本 doc が固定する分離)

混同が運用判断を腐らせるため、次を別物として固定する。`mozyo_bridge_pty/vibes/docs/specs/receiver-state-inspector-contract.md` の `用語整理` と同じ分離を、本 doc は現行 `mozyo-bridge` 側 doctrine として宣言する。

- **delivery ACK** — sender 側から見た「receiver runtime へ input を staged / submitted した」事実。`mozyo_bridge_pty/vibes/docs/specs/transport-agnostic-ack-state-contract.md` の 7 state (`staging` / `submitted` / `acknowledged` / `pending_submit` / `rolled_back` / `delivery_failed` / `stage_failed`) を扱う。prompt-turn-level。`mozyo-bridge` 側の `DeliveryOutcome` taxonomy はこの上流 ACK contract への射影として `vibes/docs/logics/tmux-send-safety-contract.md` で固定済み。
- **runtime input ack** — receiver runtime 内部から見た「stdin / structured input を受理した」signal。PTY sidecar の `runtime.input.ack`。低レベル signal であり durable に書く粒度ではない。
- **runtime control event** — sidecar / PTY runtime / provider normalizer が emit する machine-readable event。例: `runtime.input.ack`、`runtime.process.exited`、`runtime.output.eof`。receiver-state observability の source にはなり得るが、workflow gate / owner approval / task completion ではない。
- **receiver-state observability** — sender から見て「receiver runtime が今どうなっているか」を read-only に問い合わせるための surface。session-level の弱い projection を扱う。詳細は `mozyo_bridge_pty/vibes/docs/specs/receiver-state-inspector-contract.md`。
- **assistant turn completion** — provider が応答ターンを終えた事実。`mozyo_bridge_pty/vibes/docs/specs/pty-event-normalization.md` が `prompt.assistant_turn_finished` (confidence high) として扱う。本 doc の対象外。
- **workflow truth** — Redmine / Asana 等の ticket system に残る issue / journal / comment / status / owner approval。実装完了、review、owner close approval、close 判断の source of truth。
- **task completion** — ticket system 側の acceptance criteria。Asana / Redmine の durable record (task description + comment + state) が正本。本 doc の対象外。

key boundary:

- delivery ACK は「sender が押した時点の事実」を 1 ターン分残す
- receiver-state observability は「いま receiver はどうなっているか」をその時点で軽く覗くもの
- task completion は durable record が正本であり、上の二つから derive できない

三者は同じ pipeline (agentd / sidecar、もしくは現状 tmux 経路) から養分を取るが、契約は別である。inspector が ACK の代替にはならないし、ACK が inspector の代替にもならないし、どちらも task completion の代替にはならない。

## Receiver Signal Layer Model (Redmine #12194)

#12194 では、#12223〜#12229 で確定した runtime observation / reload / action-time preflight /
future sidecar / duplicate receiver advisory の成果を受け、receiver ACK / completion signal の
groundwork を次の **signal layer** として固定する。layer を分ける目的は、stronger runtime
signal が入ってきた時に、それを workflow truth や task completion へ誤昇格させないことである。

```yaml
receiver_signal_layers:
  layer_0_sender_delivery_ack:
    owns:
      - DeliveryOutcome.status / reason / mode
      - submitted_at 相当の sender-side事実
      - pending_submit / rolled_back / stage_failed の durable wording
    does_not_own:
      - receiver runtime 内部の受理確認
      - assistant turn completion
      - workflow gate / task completion
  layer_1_runtime_receiver_signal:
    owns:
      - runtime.input.ack
      - runtime.process.exited
      - runtime.output.eof
      - last_input.ack_status / last_output_at / operator_action_required 等の inspector projection
    source:
      - future sidecar / control event / provider normalizer
    does_not_own:
      - review verdict
      - owner close approval
      - issue close / acceptance criteria
  layer_2_provider_turn_signal:
    owns:
      - prompt.assistant_turn_finished
      - provider-specific turn boundary normalization
    source:
      - provider normalizer / event classifier
    does_not_own:
      - task completion
      - ticket-system status
  layer_3_workflow_truth:
    owns:
      - Redmine issue / journal / status
      - implementation_done / review_request / review / owner_close_approval / close
      - acceptance criteria disposition
    source:
      - ticket provider adapter
```

禁止する shortcut:

- `runtime.input.ack` を `implementation_done` と読むこと。
- `runtime.output.eof` / `runtime.process.exited` を task completion と読むこと。
- provider の `assistant_turn_finished` を Redmine close / owner approval と読むこと。
- Redmine journal の存在を receiver runtime ACK と読むこと。

許可する接続:

- layer 0 の `DeliveryOutcome` は layer 1 の `last_input` projection へ部分写像できる。ただし tmux
  runtime では `acknowledged_at` を持てないため、`submitted_at` 以上の receiver-side ACK を仮装しない。
- layer 1 の runtime receiver signal は runtime observation snapshot の `source: sidecar` /
  `method: live_query | poll | imported_event` として表示・診断できる。ただし `strength:
  strong_runtime_signal` は workflow truth を意味しない。
- layer 3 の workflow truth は ticket provider adapter 経由で normalized record として読める。
  provider が API / URL / status update mechanics を所有しても、approval / close semantics は core /
  governed workflow が所有する。

### 実測: `sent` / `ok` は turn 開始を保証しない (Redmine #13166)

2026-07-03 の実測 (Redmine #13166): codex 宛の `handoff send --mode standard` が 3 件連続で
`sent` / `ok` を報告したにもかかわらず、受信側 codex TUI は turn を開始していなかった。後発の再通知で
内容は処理されたため、通知だけが失われる偽陽性 delivery であった。原因は、当時の strict standard rail の
成功判定が「landing marker の pane 内観測 + Enter keypress 発行」で止まっており、submit 完了 (受信 TUI の
turn 開始) を保証していなかったこと。busy / redraw 状態の composer に Enter が吸われても `sent` / `ok` に
倒れていた。

これは本 doc の layer 分離で言えば **layer 0 delivery ACK の精度問題** (submitted か not-submitted か) で
あり、layer 1〜3 (runtime ack / assistant turn completion / task completion) の話ではない。修正 (#13166) は
その layer 0 の正直さを上げるもので、completion detector を作る方向ではない:

- codex `--mode standard` rail に限り、marker 観測 + Enter 発行の **後** に、受信 pane の新規出力活動を
  read-only で観測する turn-start 検証を追加した。活動が観測できれば従来どおり `sent` / `ok`、観測できなければ
  `blocked` / `turn_start_unconfirmed` (既存の `marker_timeout` 語彙に揃えた新 reason) で fail-closed し、
  既存の blocked 導線に乗せる。C-u rollback も自動再送も行わず、marker+body は一度だけ type する。
- signal に「composer からの marker 消失 (marker-absence)」ではなく「新規出力活動 (presence)」を採ったのは
  本 doc の doctrine と C-u rollback の capture-absence 注意に整合させるため。submit 成功時、codex TUI では
  送信済み marker が transcript に user message として残るので、marker-absence は成功の証拠にならない
  (成功時にも present であり得る)。
- claude rail と queue-enter rail の挙動は不変。behavioral 正本は
  `vibes/docs/logics/tmux-send-safety-contract.md` の v0.6 節。本節はその **実測と ACK-layer 位置づけ** を
  記録するだけであり、rail の挙動仕様を再定義しない。
- これは tmux capture 依存の compat hardening であり、long-term direction ではない。`tmux capture-pane` を
  観測しなくても submit / turn 開始が分かる本命は、依然として sidecar / control-event ベースの
  receiver-state observability (段階 1 以降) と durable-ledger 側にある。#13166 の候補 2 (pending delivery
  ledger) は本 fix の non-goal として明示的に後回しにされた。

### Minimal future runtime event vocabulary

本 doc は実装 wire format を定義しないが、将来 `mozyo_bridge_pty` / sidecar / provider normalizer
と接続する時の **意味語彙** を予約する。

```yaml
runtime_receiver_events:
  runtime.input.ack:
    meaning: receiver runtime が input を受理した
    layer: runtime_receiver_signal
    can_update:
      - last_input.ack_status
      - last_input.acknowledged_at
    cannot_update:
      - implementation_done
      - review_request
      - task_completion
  runtime.process.exited:
    meaning: receiver process が終了した
    layer: runtime_receiver_signal
    can_update:
      - runtime_liveness
      - receiver_state: exited | unknown
    cannot_update:
      - issue_status
      - close_decision
    fail_safe:
      - unexpected exit => unknown / operator_action_required
  runtime.output.eof:
    meaning: observed output stream が EOF に到達した
    layer: runtime_receiver_signal
    can_update:
      - output_stream_state
      - last_output_at
    cannot_update:
      - assistant_turn_completion unless provider normalizer separately classifies it
      - task_completion
  prompt.assistant_turn_finished:
    meaning: provider normalizer が assistant turn boundary を高信頼で分類した
    layer: provider_turn_signal
    can_update:
      - assistant_turn_state
    cannot_update:
      - Redmine implementation_done / close
```

これらは `runtime-observability-boundary.md` の snapshot envelope に入る場合でも、`completed` /
`approved` / `current_status` / `delivered` / `accepted` の generic field に変換しない。必要なら
`runtime_liveness`、`delivery_ack_status`、`assistant_turn_state`、`workflow_gate` のように source
を scope した field 名で扱う。

### Ticket-system signal / provider boundary

Redmine / Asana / future tracker の signal は terminal runtime adapter ではなく **ticket provider
adapter** の責務で扱う。これは `plugin-ready-adapter-boundary.md` の `Ticket adapter` と
`src/mozyo_bridge/domain/ticket_adapter.py` の境界に従う。

- provider owns: API calls、issue / journal / comment fetch、status update mechanics、URL formatting。
- core / governed workflow owns: gate vocabulary、owner approval separation、close prerequisites、
  role boundary、secret / private data rule。
- provider output は `IssueRef` / `JournalRef` / `CommentRef` / `WorkflowGate` /
  `OwnerApproval` のような normalized record に畳む。provider 固有 API response を runtime
  receiver-state と混ぜない。
- `WorkflowGate` は provider-observable な durable journal fact であって、receiver process が input
  を受理した事実ではない。
- `OwnerApproval` は provider が「取得」できる record だが、approval が close 条件を満たすかは
  core / governed workflow の decision である。provider が close authority を持たない。

この境界により、将来 WebSocket / webhook / Redmine API polling / Asana event stream のような
ticket-system signal が増えても、それは layer 3 workflow truth の freshness / projection を強くする
だけであり、layer 1 receiver runtime ACK や layer 2 assistant turn completion にはならない。

## なぜ pane text / stdout silence を completion truth に昇格させてはいけないか

現行 tmux runtime は `tmux capture-pane` の rendered text を低 cost で観測できるため、運用上の便宜から次のような短絡が起きやすい。

- `wait_for_text(marker)` が `true` を返した → 「届いた」→ 「処理された」と推定する
- prompt が idle に戻った / `stdout` が静止した → 「turn が終わった」→ 「task が完了した」と推定する
- pane に追加出力が出ない時間が続いた → 「receiver は何もすることがない」→ 「completion」と推定する

いずれも doctrine 違反である。理由:

1. **rendered text は描画レイヤであり source of truth ではない**。TUI redraw / character wrap / scrollback 流出 / terminal width 差で揺れる。`vibes/docs/logics/tmux-send-safety-contract.md` の `Fail-Closed Conditions` も同じ理由で「rendered text を ACK の正本にしない」を明文化している。
2. **`stdout` silence は inference にすぎない**。provider 内部で reasoning 中、permission prompt 待ち、tool 実行待ち、長時間 streaming 中の沈黙、いずれも「何も出ていない」と区別できない。「沈黙 → 完了」と倒すと false positive を構造的に量産する。
3. **task completion は acceptance criteria に対する判定であり、turn boundary より長い時間軸を持つ**。ひとつの handoff が成功裏に staged されても、receiver 側の実装・検証・記録が durable record に書き終わるまで task は完了していない。pane に何が出ていたとしても、durable record に completion が書かれていない限り completion ではない。
4. **completion を pane / stdout から推定すると、receiver-state observability の弱さを完了判定の強さで埋めようとする逆方向の設計圧がかかる**。本来は別 axis (どこまで届いたか / 今 receiver はどんな状態か / acceptance criteria を満たしたか) を別 surface で取るべきで、completion detector を強化しても根本問題は解けない。

従って、本 doc は次を doctrine として固定する。

- **task completion の source of truth は durable record (Asana task description + comment + state、または Redmine issue description + journal + status)** であり、pane text / stdout / rendered output ではない。
- **delivery ACK と task completion は別物**であり、`sent` / `ok` / `submitted` 等の ACK 状態は completion を含意しない。
- **`stdout` silence / prompt idle / pane の追加出力なしを completion detector として使わない**。fallback としても使わない。「観測できない」ことは「完了した」ことではない。

## tmux capture-pane の位置づけ (fallback であり long-term contract ではない)

現行 `mozyo-bridge` の handoff / notify 経路は `tmux capture-pane` を通じた marker observation に依存している。これは現実的な compat layer として正しい選択だが、長期 contract として固定する対象ではない。

- 短期的責務 (現行 tmux runtime に残す): `vibes/docs/logics/tmux-send-safety-contract.md` の `Fail-Closed Conditions` / `Queue-Enter Default Rail` / `### Deterministic Preflight Admission Control` が定める範囲で `wait_for_text` を **observability のため** に呼ぶ。Enter 発行の根拠は strict rail では marker 観測、queue-enter rail では Layer B preflight 通過と durable anchor 整合。いずれの rail でも rendered text は ACK / completion の正本にしない。
- 長期方向: rendered text 観測は **fallback** であり、本命は sidecar / control-event ベースの machine-readable signal (`runtime.input.ack` / `runtime.process.exited` / `runtime.output.eof` 等) を、provider normalizer / event classifier 経由で agentd 内 durable event store に正規化する経路。詳細は `mozyo_bridge_pty/vibes/docs/specs/agentd-sidecar-ipc.md`、`mozyo_bridge_pty/vibes/docs/specs/pty-event-normalization.md`、`mozyo_bridge_pty/vibes/docs/specs/event-classifier-module-structure.md`。

「capture-pane を強化する」「marker wrap 補修を入れる」方向の改善は、短期 wrap shape 起因の `marker_timeout` を救うための **compat fix** であって long-term contract の昇格ではない。doctrine 上の long-term direction は「rendered text を観測しなくても良くなる側」であり、それは PTY-first runtime の sidecar / control-event 経路でしか整わない。

## Receiver-State Observability を別 axis として priority に置く

ここまでの三つの分離の中で、運用価値が高いのに現行 `mozyo-bridge` で最も弱いのが **receiver-state observability** である。具体例:

- 直前 input は受理されたか (`last_input.ack_status`)
- 直近 output はいつだったか (`last_output_at`)
- 今 permission prompt を出して止まっているか
- runtime process は生きているか / 落ちたか
- いま busy なのか idle なのか、それとも沈黙していて判断できないのか

これらは「completion を当てる魔法」ではない。むしろ「completion を当てない代わりに、状態だけ軽く覗ける」ことが contract の強さである。長期方向は次の通り。

- read-only な inspector surface を `mozyo_bridge_pty/vibes/docs/specs/receiver-state-inspector-contract.md` が定義する。query / subscribe / stream 経路を含む。
- 各 field の source of truth は sidecar control event / 正規化済み event / 意図的に `unconfirmed` のいずれか。`stdout` silence からの idle 推定は **意図的に non-goal**。
- 現行 `mozyo-bridge` の `DeliveryOutcome.last_input` projection は、上流 inspector contract の `last_input` field への部分 projection として既に位置づけられている (`vibes/docs/logics/tmux-send-safety-contract.md` `## Queue-Enter Default Rail` の `### State / Outcome Mapping` および上流 `mozyo_bridge_pty/vibes/docs/specs/receiver-state-inspector-contract.md` の `## Field-Level Source of Truth Map` 参照)。`reason="queue_enter"` は wording-layer 差であって `ack_status` 上の別 state ではない。

doctrine としての priority 表明:

- **completion detector を強くする方向に時間を使わない**。
- **「届いた」「受理された」「今どんな状態」「acceptance criteria を満たした」をそれぞれ別 surface で取る方向に時間を使う**。
- 現行 `mozyo-bridge` の handoff / notify 改修は、completion detector を作る方向ではなく、ACK の durable wording を強化する方向、または observability surface を sidecar 経路に橋渡しする方向に倒す。

## Sidecar の位置づけ (runtime helper であり hook entrypoint ではない)

handoff ACK / receiver-state observability の議論で「sidecar」「hook」「wrapper」が同じ意味で語られることがあるが、本 doc では次を分離する。

- **sidecar** — Claude / Codex 等の TUI agent process を **外側から包んで** stdin / stdout / structured event を観測・橋渡しする runtime helper (`mozyo_bridge_pty/vibes/docs/specs/agentd-sidecar-ipc.md` / `mozyo_bridge_pty/vibes/docs/specs/pty-first-agent-runtime.md`)。`runtime.input.ack` 等の control event を emit する責務を持つ。provider 公式 hook が露出していてもいなくても、sidecar 層自体は必要。
- **hook** — provider (Claude Code / Codex / 他 agent runtime) が公式に提供する extension point。存在すれば sidecar / classifier がそれを **追加 signal として** 消費できる。sidecar は hook の依存物ではないし、hook が sidecar の代替にもならない。
- **wrapper / launcher** — agent process を起動する thin entrypoint。session id 付与や PTY 確保はここで行うが、観測責務は持たない。sidecar / agentd と混同しない。

doctrine としての position:

- sidecar は「the hook entrypoint」ではない。Claude/Codex を包む runtime helper であり、provider hook の有無に関わらず必要。
- raw output 監視だけで sidecar を完結させると `tmux capture-pane` の焼き直しになりうる。本命は raw chunk + control event を別 channel として持ち、event classifier で正規化することで「rendered text に依存しない signal」を increment ずつ増やす方向。
- 現行 `mozyo-bridge` から見た sidecar は「将来 inspector / 正規化済み event の source になる layer」であり、現時点では `mozyo-bridge` のコードベースに sidecar 実装は存在しない。`mozyo_bridge_pty` worktree の workstream として独立に進む。

## 現行 `mozyo-bridge` と長期 runtime workstream の接続

現行 `mozyo-bridge` 単独で本 doc の長期方向を全て実現することは scope 外である。接続は段階的に行う (`mozyo_bridge_pty/vibes/docs/specs/receiver-state-inspector-contract.md` の `## Bridge from Current mozyo-bridge Handoff Primitive` が canonical な段階分けを定義する)。本 doc は同 spec の段階を doctrine として承認し、現行 repo 側の責務だけを再掲する。

- **段階 0 (現在)**: `mozyo-bridge` の `DeliveryOutcome` が `last_input` projection を担う (ACK 由来の `submitted_at` のみ)。pane text からの completion 推定は行わない。
- **段階 1**: sidecar control event を subscribe して `last_input.ack_status` などの receiver-side signal を inspector に増やす。`mozyo-bridge` 側は projection 形を維持し、新 field の意味付けは inspector contract が所有する。
- **段階 2**: provider normalizer / event classifier 経由で `runtime.session.busy` / `runtime.session.idle_likely` などの semantic event が classifier から流れ込む。`mozyo-bridge` の `DeliveryOutcome` taxonomy 自体は変更しない。
- **段階 3**: application policy 層が `operator_action_required` / `heartbeat_stale` などの policy decision を inspector 経由で問い合わせる。`mozyo-bridge` 経路はここまで来ても completion judging には関与しない。
- **段階 4**: ticket provider adapter が Redmine / Asana / future tracker の gate / approval / status
  records を normalized workflow records として読む。これは receiver runtime signal の上位互換ではなく
  別 layer の workflow truth である。terminal runtime adapter や sidecar は ticket-system truth を所有しない。

現行 `mozyo-bridge` で意識すべき不変条件:

- `DeliveryOutcome` taxonomy (`Status` / `Reason` / `AckStatus` / `next_action_owner`) は本 bridge 経路でも勝手に拡張しない。新 receiver-state vocabulary は inspector contract 側で扱う。
- `tmux capture-pane` からの sentinel 検出を増やして completion を推定する方向には倒さない (`mozyo_bridge_pty/vibes/docs/specs/receiver-state-inspector-contract.md` `## Anti-Patterns`)。
- `--no-submit` legacy fallback、`notify-*-legacy-task`、non-agent pane 送信は queue-enter default 射程外であり、receiver-state observability の対象でもない (それらは pane 上の operator-driven 経路として独立に扱う)。
- ticket provider / Redmine API / journal polling 由来の signal を terminal receiver-state と混ぜない。
  workflow truth は workflow truth として読み、runtime ACK / assistant turn completion へ backfill しない。

## 運用への帰結 (現行 `mozyo-bridge` の挙動規範)

配布節 `## ACK / delivery / completion の分離` (portable 規範の正本; #13060) を、現行 `mozyo-bridge` の機構 (`DeliveryOutcome` / `--no-submit` retry / inspector contract) へ結線した repo 適用。挙動変更を伴う改修は別 task が所有するが、repo 内判断の正本としての規範は本節が宣言する。

1. **task completion を pane / stdout / chat から判定しない**。durable record (Asana task / comment / state) を読むことで判定する。chat や pane 経由の「終わった」「OK」「completed」は notification にすぎず、durable record に書かれて初めて completion 扱いになる。
2. **delivery ACK を task completion の代理として扱わない**。`sent` / `ok` / `submitted` は「receiver runtime に渡し終えた」だけの事実であり、その後の `acknowledged` / `processed` / `task completed` を含意しない。
3. **`stdout` silence / prompt idle / pane 追加出力なしを completion detector として使わない**。`mozyo-bridge` の handoff ACK / `--no-submit` retry / `un-notified` 発動条件はすべて pane silence ではなく durable record と `DeliveryOutcome` 経由の judgement に従う。
4. **rendered text 観測の弱さを完了判定の強さで埋めない**。`marker_timeout` / `--no-submit` retry / wrap shape 補修は ACK 経路の改修であり、completion detector ではない。
5. **receiver-state observability が必要になった場合、`mozyo-bridge` 内に独自 detector を生やさない**。`mozyo_bridge_pty` の inspector contract に follow-up task を切り、本 repo の `DeliveryOutcome` taxonomy はそのまま維持する。
6. **handoff の durable wording は ACK 層の正本**として書く。completion / processing の含意を持たせない。受領契約は引き続き「receiver は durable anchor を読む」であり、pane の rendered text に依存させない。
7. **owner approval / review / close を runtime signal で自動化しない**。`runtime.input.ack`、`runtime.output.eof`、`assistant_turn_finished`、ticket webhook のいずれも、Review Gate / owner close approval / Close Gate の代替ではない。
8. **ticket-system signal は provider 境界に閉じる**。Redmine / Asana の status、journal、approval record を読む場合は ticket provider adapter / governed workflow の layer 3 record として扱い、terminal runtime adapter や sidecar の ACK state に混ぜない。

## Cross-References

- Repo-local 関連 doctrine:
  - `vibes/docs/logics/tmux-send-safety-contract.md` (現行 tmux runtime の send safety / queue-enter rail / `DeliveryOutcome` 射影の正本)
  - `vibes/docs/logics/autonomous-ticket-entrypoint.md` (ticket-ID 経由 entrypoint と pane / chat を source of truth から外す規範)
  - `vibes/docs/logics/runtime-observability-boundary.md` (snapshot envelope / reload / action-time preflight / future sidecar scope)
  - `vibes/docs/logics/plugin-ready-adapter-boundary.md` (ticket provider / terminal runtime provider / workflow authority 境界)
- 上流 PTY runtime / inspector contract (`mozyo_bridge_pty` worktree):
  - `mozyo_bridge_pty/vibes/docs/specs/pty-first-agent-runtime.md`
  - `mozyo_bridge_pty/vibes/docs/specs/transport-agnostic-ack-state-contract.md`
  - `mozyo_bridge_pty/vibes/docs/specs/receiver-state-inspector-contract.md`
  - `mozyo_bridge_pty/vibes/docs/specs/agentd-sidecar-ipc.md`
  - `mozyo_bridge_pty/vibes/docs/specs/pty-event-normalization.md`
  - `mozyo_bridge_pty/vibes/docs/specs/event-classifier-module-structure.md`

本 doc は doctrine boundary を宣言するに留め、上記いずれの spec の挙動 / 実装 / wire format も再定義しない。
