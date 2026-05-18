# tmux Send Safety Contract And Fallback Policy

## Status

- Draft: `v0.4` (durable policy; 実装 freeze ではない)
- Scope: 現行 `mozyo-bridge` の tmux compatibility layer に残す send safety contract と fallback policy
- Parent task: Asana `1214768200741140` (v0.1〜v0.3.1) / Asana `1214825156046950` (v0.4 pivot)
- Owning task: Asana `1214768200549398` (v0.1〜v0.3.1) / Asana `1214824751741628` (v0.4 policy / contract pivot)
- v0.2 で追加: `queue-enter` rail の contract 定義 (Asana `1214782240666065` / parent `1214782240916053`)。strict `standard` rail は変更しない。
- v0.3 で追加: `queue-enter` rail の deterministic preflight admission control を `## Queue-Enter Default Rail` の `### Deterministic Preflight Admission Control` に固定 (Asana `1214799760140962` / parent `1214785523058579`)。LLM 判断ではなく pane / arg / session の machine-checkable signals だけで admit / reject を決める。strict `standard` rail の挙動と wire 上の `Status` / `Reason` / `AckStatus` / `next_action_owner` enum はすべて変更しない (新規 `Reason` 値は追加しない)。
- v0.3.1 で訂正: Step 12 の per-receiver foreground process allowlist で `node` literal を **両 receiver の weak identity** として再分類 (Asana `1214785367563471` 実装中に判明; Claude Code TUI と Codex CLI が共通して `node` 上で動作する事実への正直な対応)。`node` を `claude` 専用の strong identity と扱っていた v0.3 ドラフトは実装上不可能だったため、本訂正で contract と実装を同期する。wire enum と Step 10 / 11 はすべて変更なし。
- v0.4 で pivot (Asana `1214824751741628` / parent `1214825156046950`): default delivery rail を strict `standard` から `queue-enter` に再配置し、product promise を `confirmed landing` 中心から `strong preflight 付き practical queued submission` 中心に切り替える (`## Default Delivery Promise (v0.4)` 参照)。strict `standard` rail は contract から削除せず、明示的 fallback として残す。strict rail の挙動・wire enums (`Status` / `Reason` / `AckStatus` / `next_action_owner`)・queue-enter rail の Layer B admission gate (Step 1–14) はいずれも変更しない (新規 `Reason` 値も追加しない)。本 task の射程は contract wording の pivot のみであり、CLI / handoff 実装 (Asana `1214825307842391`)、distributed docs / rules / skill refs / preset surfaces (Asana `1214825156844993`)、tests / smoke / workflow verification (Asana `1214825156769677`) は別 child task が所有する。
- Upstream contract (前提): `mozyo_bridge_pty/vibes/docs/specs/transport-agnostic-ack-state-contract.md`
  - Owning task: Asana `1214768334252326` / approved by comment `1214768792089818`
  - Landed by commit `dcf9c6b` (`_pty` workspace) — "Define transport-agnostic ACK state contract"
- 非射程の related work:
  - 短期 wrap 補修: Asana `1214765093829972`
  - gate vs codex TUI compatibility tracker: Asana `1214749106025548`
  - queue-enter rail 自体の CLI / code 実装 (v0.2 lineage、queue-enter rail の追加): Asana `1214782240686275` (本 contract v0.2 に従って実装済み)
  - queue-enter rail を v0.4 normative default に flip する CLI / handoff 実装: Asana `1214825307842391` (parent `1214825156046950`、v0.4 lineage、commit `93dc953` で land 済み; 本 contract の `### Contract Default vs CLI Default (Transient Gap)` を解消した)
  - queue-enter rail を反映する README / rules / skill refs / `CLAUDE.md` / packaged preset surface (v0.2 lineage): Asana `1214782227597692`
  - v0.4 default 反映のための同 surface 群更新: Asana `1214825156844993` (parent `1214825156046950`、v0.4 lineage)
  - queue-enter rail の tests / smoke / workflow verification (v0.2 lineage): Asana `1214782185227306`
  - v0.4 default 反映のための tests / smoke / workflow verification: Asana `1214825156769677` (parent `1214825156046950`、v0.4 lineage)
  - queue-enter rail の observability / durable annotation 形式化 (任意): Asana `1214782185308162` (defer 決定済)

## この文書の目的

`mozyo-bridge` の現行 tmux compatibility layer は、長期的には PTY-first runtime に再設計されることが既に決まっている。一方で、現行 tmux runtime は今後しばらく実運用に残る。本文書は、その「現行 tmux runtime に残す safety contract と fallback policy」を durable に固定する。

具体的には次を定義する。

- `mozyo-bridge handoff send` / `handoff reply`、`notify-*` standard variants、`mozyo-bridge message` という現行 send 系 CLI が、どの safety contract に乗り、どこが legacy compatibility として残るか
- 上流 ACK state machine (`submitted` / `acknowledged` / `pending_submit` / `rolled_back` / `delivery_failed` / `stage_failed`) を、現行 tmux CLI surface (`--mode queue-enter` (v0.4 normative default、CLI binary 上も `default=MODE_QUEUE_ENTER`) / `--mode standard` (strict explicit fallback) / `--mode pending` / 手動 `read` / `--no-submit`) にどう射影するか。flip land の経緯は `## Default Delivery Promise (v0.4)` の `### Contract Default vs CLI Default (Transient Gap)` 節 (resolved) を参照。
- receiver pane unavailable / landing marker timeout / non-agent pane / read marker expiry が起きたときの durable receive method
- blind Enter を避けつつ fail-closed を保つ条件
- 短期 wrap 補修 task との non-goal 境界

`acknowledged` を含む 7 state full contract は PTY-first runtime 側で扱う。本文書は tmux 6 state までを扱う。

## Non-Goals

本文書は次を扱わない。

- PTY-first runtime / `mozyo-agentd` の receiver-state inspector 設計 (Asana `1214768334590758` が所有)
- 短期 wrap fix の実装方針 (Asana `1214765093829972` が所有)
- gate vs codex TUI 観測互換性そのものの修正 (Asana `1214749106025548` が継続所有)
- provider 別 assistant turn completion 判定
- ticket-system schema 詳細
- `read-next --wait` / Stop hook handoff wait の復活

これらは別 task / 別 spec が所有する。短期 wrap fix の進行は本 contract と独立であり、wrap fix が contract そのものを変更する方向に逸脱した場合に限り、上流 ACK spec と本 contract が優先する。

## Default Delivery Promise (v0.4)

v0.4 以降の `mozyo-bridge` 現行 tmux compatibility layer は、agent pane 向け send 系 CLI の default delivery promise を「**strong preflight 付き practical queued submission**」と定義する。

- **Default rail**: `queue-enter` (本 contract `## Queue-Enter Default Rail` 参照)。typing 前の deterministic admission control (`### Deterministic Preflight Admission Control` の Layer B: window-name binding / same-session binding / active-split binding / per-receiver foreground process allowlist など本 contract に定義済みの preflight signals) を通った agent pane に対して、`wait_for_text(marker)` の事前観測の成否に依存せず Enter を発行する。Enter は preflight 通過の事実を根拠に発行し、observability のための `wait_for_text(marker)` は引き続き呼ぶ。観測あり → `sent` / `ok`、観測なし → `sent` / `queue_enter`。
- **Promise の限界 (honestly stated)**: 本 default は **confirmed landing ではない**。tmux 経路は原理的に receiver runtime 内部の prompt 受理判定を取得できないため、queue 受理されたかどうかを sender 側で確証することはできない。receiver は引き続き durable record (Asana task comment / Redmine journal) を source of truth として読む。pane に届いた notification は pointer であり、ACK の正本ではない。
- **Strict explicit fallback**: `--mode standard` は contract から削除しない。strict fallback として明示的に保持する。marker observation を Enter の必要条件とし、未観測時は `C-u` rollback + `marker_timeout` で fail-closed する従来の挙動を v0.4 でも維持する。

### このピボットを行う理由 (Durable Rationale)

2026-05-15 時点の運用観察 (parent task `1214825156046950` / coordination comment `1214824751966498`、関連 feedback `1214825156046757`) で、strict `standard` rail を default に保っていたことに起因する `marker_timeout` 再試行と手動 Enter フォローが日常的に発生していた。受信側 (特に codex TUI) の character-wrap shape 起因の marker miss は `1214765093829972` / `1214749106025548` で継続追跡しているが、これは tmux 経路の rendered-text 観測そのものに依存する弱点で短期に解消できる前提を置けず、strict default を維持し続けるほど transport の体感価値が削れる構造になっていた。

product 判断として、本 contract は次を選ぶ:

- strict rail の理論的純度 (Enter 発行前に marker を観測する) を default として preserve するより、preflight 強度 (Layer B deterministic admission gate) を保ったまま Enter を発行する path を default にし、運用上の practical delivery を product promise の中心に置く。
- これは tmux 経路で `acknowledged` を仮装することではない。durable record の source-of-truth 性、`acknowledged_at` を populate しない制約、inspector projection を strict と同一に保つ制約はすべて維持する (`## Queue-Enter Default Rail` の State / Outcome Mapping および `### 強い境界 (Strong Boundaries)` 参照)。
- strict rail を消すと、強い landing evidence を要求する明示的 send (regression check / brand-new pane / observability test) ができなくなるため、strict は `--mode standard` で明示的 fallback として残す。default 変更を根拠に strict rail を黙って弱化することは禁止する。

### Default の射程 (Scope)

- 本 default promise は `mozyo-bridge handoff send` / `handoff reply` / `notify-*` 標準 variants の Claude / Codex agent pane 経路に適用する。`mozyo-bridge message --no-submit` legacy fallback、`notify-*-legacy-task`、non-agent pane への send には適用しない (queue-enter rail の許容 target は引き続き `## Queue-Enter Default Rail` の `### 許容ターゲット (Allowed Targets)` が固定する)。
- `--mode pending` (operator が submit を保留する経路) は default の射程外。operator が pane で Enter を押下する責任を負う独立 path として残す。
- 本 default は universal blind Enter の許可ではない。Layer B preflight が 1 つでも fail すれば typing 前に `stage_failed` で die する (`## Queue-Enter Default Rail` の `### Deterministic Preflight Admission Control` 参照)。strong preflight は本 default の必須前提であり、preflight を抜きにして Enter を発行する rail は本 contract が定義しない。default の射程外で send したい場合は、明示的に `--mode standard` を選ぶか、send を中止する。

### Downstream Surfaces (land 済み)

本 contract のピボットを反映する CLI / handoff 実装、distributed docs / rules / skill refs / preset surfaces、tests / smoke / workflow verification は別 child task で land 済み。本 contract の `## Default Delivery Promise (v0.4)` が source-of-truth であり、downstream は本文書に従う。

- CLI / handoff 実装 (CLI binary 上の `--mode` default flip): Asana `1214825307842391` — commit `93dc953` で land。
- distributed docs / rules / skill refs / preset surfaces: Asana `1214825156844993` — commit `bd8696e` で land。
- tests / smoke / workflow verification: Asana `1214825156769677` — commit `d4378f4` で land。

新規の contract 変更で downstream surfaces を再同期する必要が生じた場合は、本節を更新し、対応する commit / Asana task id を残すこと (audit replay 用)。

### Contract Default vs CLI Default (Transient Gap) — RESOLVED

v0.4 contract land 直後は、本文書が `queue-enter` を normative default に固定する一方で、shipped CLI binary の `--mode` 実装 default が依然 `standard` だったため、両者の間に意図的に許容される transient gap が存在していた。このギャップは現時点で **closed** である:

- CLI flip: commit `93dc953` (Asana `1214825307842391`) で `src/mozyo_bridge/application/cli.py` の handoff `--mode` default が `MODE_QUEUE_ENTER` に置き換わった。
- distributed docs / rules / preset / skill refs sync: commit `bd8696e` (Asana `1214825156844993`)。
- tests / smoke / workflow verification: commit `d4378f4` (Asana `1214825156769677`)。

これ以降、本文書が言う「`--mode queue-enter` (default since v0.4)」は normative contract と shipped binary の両方で一致する。operator が `--mode` を省略した send は、本文書の `## Default Delivery Promise (v0.4)` Scope に当てはまる限り queue-enter rail に乗る。strict landing observation が必要な send は明示的に `--mode standard` を選ぶ (`## Default Delivery Promise (v0.4)` の strict explicit fallback 節参照)。

本節は audit replay のために残してある。新規に gap を再導入しない: shipped CLI binary の default を `standard` に戻したい場合は、本 contract と downstream surfaces を同期させた上で行うこと。

## 用語の対応 (上流 ACK spec → tmux runtime)

上流 ACK spec は prompt-turn-level の state を 7 state で定義している。tmux compatibility layer はそのうち 6 state を実装し、`acknowledged` だけは原理的に取得できないことを正面から認める。

| 上流 ACK state | tmux 経路の意味 | `DeliveryOutcome.status` / `reason` (現行 CLI 出力) |
| --- | --- | --- |
| `staging` | sender が `send-keys -l` で typing 中。Enter / `C-u` のいずれも未発行。transient。 | (CLI 出力には現れない。runtime 内部のみの transient state) |
| `submitted` | `wait_for_text(marker)` が true を返し、Enter を発行した。 | `sent` / `ok` |
| `pending_submit` | typing 完了。設計上 Enter を押していない (`--mode pending` または `message --no-submit`)。入力は prompt に staged。 | `pending_input` / `ok` |
| `rolled_back` | `wait_for_text` が landing_timeout 内に marker を観測できなかった。`C-u` で入力をクリア。Enter は押していない。 | `blocked` / `marker_timeout` |
| `delivery_failed` | typing 後の transport / tmux 側 unrecoverable error。残骸の有無は adapter ごとに未定義。 | (tmux 経路ではほぼ起き得ない。`send-keys` 失敗等の真の transport error 用に予約) |
| `stage_failed` | typing 開始前に target resolution / agent gate / anchor / args validation が失敗。pane には何も typed されていない。 | `blocked` / `target_unavailable` / `target_not_agent` / `invalid_anchor` / `invalid_args` |
| `acknowledged` (PTY-only) | tmux 経路では取得不可能 (rendered text 観測しかできないため)。 | — |
| `submitted` (queue-enter, marker 観測あり) | `queue-enter` rail で typing 後 `wait_for_text(marker)` が true を返し、Enter を発行した。strict rail と同じ outcome に倒す。 | `sent` / `ok` |
| `submitted` (queue-enter, marker 未観測) | `queue-enter` rail で marker が landing_timeout 内に observe できなかったが、target が agent pane であることを根拠に Enter を発行した。ACK 到達は strict と同じ `submitted`。`reason="queue_enter"` (v0.2 新規) は sender が pre-Enter で landing を確認していない事実を wording-layer で残すための差分情報。strict rail はこの分岐を持たない。 | `sent` / `queue_enter` (新規 reason; v0.2 で追加) |

tmux 経路の最終到達は `submitted` を超えない。これは contract の弱点ではなく、tmux が原理的に receiver runtime 内部を覗けないことの正直な表現である。`queue-enter` rail の `marker 未観測 + Enter` 経路も `submitted` に到達する。`sender が pre-Enter 観測を持っていない` ことは `DeliveryOutcome.reason="queue_enter"` と durable record narrative で表現するが、`last_input` projection は strict `sent + ok` と同じ `submitted_at = outcome timestamp / ack_status="submitted"` を採る (上流 `receiver-state-inspector-contract.md` の Field-Level Source of Truth Map に従い `ack_status` は `submitted_at` / `acknowledged_at` から derive されるため、`submitted_at` を持ちながら `ack_status="unobserved"` を返す projection は構造上不可能)。詳細は `## Queue-Enter Default Rail` を参照。

## CLI Surface の射影

現行 CLI surface はどれも上記 state machine の同じ射影に乗る。違いは「durable anchor を CLI 引数として持つかどうか」「marker shape」「`--no-submit` を持つかどうか」だけである。

### `mozyo-bridge handoff send` / `handoff reply` (`orchestrate_handoff`)

- 本 contract の **標準経路**。Asana / Redmine anchor を引数として要求し、marker shape は `[mozyo:handoff:source=<src>:task=<id>:comment=<id>:kind=<label>:to=<receiver>]` を採る。
- `--mode queue-enter` (**v0.4 normative default**; CLI binary 上も `default=MODE_QUEUE_ENTER`、commit `93dc953` で land 済み): Claude / Codex agent pane に対する queue-oriented delivery 経路。typing → `wait_for_text(marker)` を引き続き呼ぶ (observability のため) が、未観測でも `C-u` rollback せず Enter を発行する。観測あり → `sent` / `ok` (`submitted`)、観測なし → `sent` / `queue_enter` (`submitted`)。Layer B preflight 違反 (window-name / same-session / active-split / per-receiver foreground process) は typing 前に `stage_failed` で die する。詳細・適用条件・許容ターゲット・durable wording は `## Queue-Enter Default Rail` を正本として参照する。default rail としての位置づけは `## Default Delivery Promise (v0.4)` 参照。
- `--mode standard` (**strict explicit fallback**): typing → `wait_for_text(marker)` → Enter。成功で `sent` / `ok` (`submitted`)。marker_timeout で `C-u` rollback + `blocked` / `marker_timeout` (`rolled_back`)。marker observation を Enter の必要条件とする strict rail を明示的に選ぶ送信用 (例: regression check、brand-new pane で queue-pickup 確率が未確認、observability test、strict landing evidence が監査要件)。v0.4 contract で default ではなくなったが contract からは削除しない。挙動は v0.1 以降一切変更しない。
- `--mode pending`: typing 後、`wait_for_text` も Enter も発行しない。`pending_input` / `ok` (`pending_submit`)。入力は prompt に残る。operator 判断で submit する経路。`## Default Delivery Promise (v0.4)` の Scope 射程外。
- pre-flight (target_unavailable / target_not_agent / invalid_anchor / invalid_args) は typing 開始前に死ぬ。いずれも `stage_failed` 経路で `blocked` / `<reason>` を emit する。
- 出力は `DeliveryOutcome` 構造化 + durable delivery-record markdown。両方を Asana / Redmine record に貼り付け可能な形で書き出す (`--record-format both` 既定)。

### `notify-codex` / `notify-claude` / `notify-codex-review` / `notify-claude-review-result`

- `_notify_standard_via_handoff` 経由で同じ `orchestrate_handoff` rail に乗る。Redmine-shaped CLI flags (`--issue` / `--journal` / `--type`) を `source=redmine` anchor + `kind` に正規化して forwarded namespace を作る。
- safety contract は handoff と同一: marker は handoff の marker shape、wait gate と rollback も同じ。
- 後方互換のため、成功時のみ `notified <agent>: journal=...` の legacy line を併出する。これは外部 script / smoke test 用の courtesy であり、契約上の正本は引き続き `DeliveryOutcome` + delivery record。
- legacy `--type` 値 (`KIND_LABELS` に存在しない値) は default kind に倒し、`--summary` に `legacy --type=<value>` を載せて anchor の表現を維持する。

### `notify-codex-legacy-task` / `notify-claude-legacy-task`

- 旧 `.agent_handoff/tasks.json` queue を読む `notify_agent` 経路。durable anchor を CLI で受け取れない legacy callers のためのみ残してある。
- safety contract は handoff と同等 (marker wait + `C-u` rollback + Enter で fail-closed) だが、`DeliveryOutcome` を emit しない。durable record は呼び出し側で書く責任を持つ。
- 新規 caller は使わない。`mozyo-bridge handoff send` または標準 `notify-*` を使う。

### `mozyo-bridge message`

- 旧来の汎用 send。marker shape は `[mozyo-bridge from:<sender>... pane:<pane>... at:<location>]` で、handoff 系の marker と別 namespace。
- default は `--submit` (true) で、handoff と同じ wait gate + `C-u` rollback + Enter のセットを通る。失敗時は `die` で exit するため `DeliveryOutcome` を emit しない (legacy 出口)。
- `--no-submit` は wait gate も Enter も skip し、typing だけを行う。これは `pending_submit` 相当の意図的経路だが、durable anchor を CLI が知らないため、structured outcome を emit できない (legacy 出口)。
- 用途は次に限定する。
  - `handoff send` の `marker_timeout` を codex TUI の rendered-text 観測差 (Asana `1214749106025548` / `1214765093829972` 進行中の wrap shape 問題) で取り損ねたときの **手動 fallback**。
  - operator が `mozyo-bridge read codex` で read marker を再取得した上で `message --no-submit codex "<本文>"` を撃ち、receiver pane で Enter を operator が押下する flow。
  - memory `project_codex_message_no_submit_workaround.md` で固定済みの retries 上限 3、Asana に試行記録。
- `message` 経路は ACK contract には乗るが、CLI surface としては legacy compatibility 扱い。新規 sender flow は handoff 経路に寄せる。

### 低レベル primitives (`read` / `type` / `keys`)

- これらは contract を持たない pane操作 utility。`read` で read marker を取り、`type` / `keys` は `require_read` を通って `send-keys` を発行する。
- safety contract の中では「`--no-submit` fallback の前段で read marker を refresh する経路」としてのみ位置づけ、`type` / `keys` だけで durable handoff を成立させない。

## State Transition と Pane の見え方

operator が pane を見たとき、receiver agent が durable record を読むとき、各 state で何が見えるかを揃える。

| State | pane の見え方 | operator の次アクション | receiver agent の前提 |
| --- | --- | --- | --- |
| `submitted` | marker + body が入り、Enter が押下されている。receiver は anchor を読むはず。 | 何もしない。Asana / Redmine record を待つ。 | pane notification は pointer。durable anchor を読み直して着手。 |
| `pending_submit` | marker + body が入っているが、receiver の prompt に staged のまま (Enter 未押下)。 | pane を見て submit 可否を判断し、Enter を押下するか手動で書き換える。durable record にも `pending_input` / `ok` を残す。 | pane に既に入っている場合は operator 押下を待つ。durable anchor を読み直すことも可。 |
| `rolled_back` | pane には何も残らない (`C-u` で除去済み)。 | durable record を読み、handoff を別経路で再開する。retry は contract 外の判断。 | pane には何も出てこないことが正常。durable record を直接読みに行く。 |
| `delivery_failed` | 残骸の有無は未定義。adapter 側 die error が durable record に残る。 | durable record を読み、operator 判断で復旧。 | pane を信用せず、durable record だけを読む。 |
| `stage_failed` | pane には何も typed されていない (typing 開始前死亡)。 | sender に修正を依頼 (target name / anchor 引数 / agent gate)。 | 何も受け取らない。durable record にも明示の失敗が残るはず。 |
| `submitted` (queue-enter, marker 観測あり) | strict `submitted` と同じ pane 像。marker + body + Enter。 | 何もしない。Asana / Redmine record を待つ。 | strict と同じ。pane notification は pointer。durable anchor を読み直して着手。 |
| `submitted` (queue-enter, marker 未観測) | marker は scrollback 外 / wrap shape 差で観測されないが、body は prompt に landed され Enter が押下されている。receiver の TUI が queue 受理した場合は次 turn で処理される。 | strict `submitted` と同じく **何もしない** が一次 next-action。Asana / Redmine record を待つ。durable record の `Operator note:` 行 (本 contract `## Queue-Enter Default Rail` の Durable Wording Requirements 参照) に、queue 経路 fallback として `mozyo-bridge handoff send --mode standard` への切り戻しを「receiver pickup が一定時間 ない場合の任意 escalation」として記載する。escalation 判断は operator 側だが、`next_action_owner` は引き続き `receiver`。 | strict と同じ受領契約。durable anchor を読み直して着手する。pane に `[mozyo:handoff:...]` らしき断片が残っていても rendered text を ACK 正本にしない。 |

このマッピングは「receiver-state observability の MVP」として、receiver agent が durable record を読むだけで判断できる粒度を保つ。queue-enter rail の `next_action_owner` を strict `sent` と揃えるのは意図的な設計選択であり、`## Queue-Enter Default Rail` の State / Outcome Mapping と `## 既存実装との対応` の `next_action_for` 仕様と一致する。pane-side escalation は「contract の next-action」ではなく「durable wording に記載される operator-facing fallback hint」として扱う。

## Fallback Policy

現行 tmux 経路で発生する各失敗ケースの receive method を durable に決めておく。pane 通知が届かないことを失敗とは扱わず、必ず durable record 側に逃がす。

### Receiver pane unavailable (`stage_failed` / `target_unavailable`)

- 起点: `pane_info(target)` が SystemExit になる。`orchestrate_handoff` は typing 前に `blocked` / `target_unavailable` の `DeliveryOutcome` を emit して die する。
- 受領方法: pane への notification は **発生しない**。durable record (Asana task comment / Redmine journal) に attempted command と `DeliveryOutcome` を残す。
- next action: sender。`mozyo` または `mozyo-bridge init <agent>` で window を作り直す。

### Non-agent pane (`stage_failed` / `target_not_agent`)

- 起点: `ensure_agent_target` が non-agent process を検知し、`--force` 無しで SystemExit。
- 受領方法: pane への notification は発生しない。durable record に「pane が agent 起動を持っていない」事実を残す。
- next action: sender。pane で agent を起動するか、operator-approved な経路で `--force` を指定する判断を durable record に残してから retry。

### Anchor / args validation 失敗 (`stage_failed` / `invalid_anchor` / `invalid_args`)

- 起点: `normalize_anchor` / `build_notification_body` / `--kind` validation。
- 受領方法: pane へは何も送らない。durable record に CLI 上の修正点だけを残し、retry に必要な情報を sender に返す。
- next action: sender。引数を直し、`mozyo-bridge handoff send` を再投入。

### Landing marker timeout (`rolled_back` / `marker_timeout`)

- 起点: `wait_for_text(marker)` が landing_timeout 以内に true を返さない。`C-u` rollback、Enter は **押さない**。
- 現実的な原因 (2026-05-13 時点):
  - codex TUI の character-wrap shape (空白なし長 marker が prompt 幅で折られる; Asana `1214765093829972`)
  - codex TUI gate の rendered shape 差 (Asana `1214749106025548`)
  - target が一時的に busy で marker が scrollback 外に流れる
- 受領方法: durable record に attempted command + `DeliveryOutcome` (`blocked` / `marker_timeout`) を残す。pane には何も残らない。
- 標準 retry: contract 上の自動 retry は **しない**。同じ marker shape で再投入しても同じ症状を踏む確率が高い。
- 許容 fallback: `mozyo-bridge read <agent>` で read marker を refresh し、`mozyo-bridge message --no-submit <agent> "<本文>"` で receiver pane に typing だけ行う。operator が pane で Enter を押下することで delivery を完結する。retries 上限は memory `project_codex_message_no_submit_workaround.md` に従い 3 回。fallback 試行と Enter 押下責任は durable record に明記する。
- next action owner: sender (durable record に状況を記録) + operator (pane で Enter を押下する場合のみ)。

### Read marker expired (`message --no-submit` の場合)

- 起点: `mozyo-bridge message` / `type` / `keys` 等で `require_read` が「read marker が古い」と判定し exit 2。
- 受領方法: pane には何も typed されていない。tmux の read marker 自体が「直前に capture-pane を観測した」signal なので、再観測なしで `send-keys` を進めるのは未観測 Enter と同じ意味を持ち、本 contract が許容しない。
- 標準回復手順: `mozyo-bridge read <target>` を発行して read marker を再取得した直後に `message --no-submit` を再実行する。
- next action owner: sender。

### `--mode pending` (`pending_submit`)

- 起点: 設計上の意図的経路。`orchestrate_handoff` は `wait_for_text` も Enter も発行せず、typing だけを行う。
- 受領方法: marker + body が receiver pane に staged のまま残る。durable record に `pending_input` / `ok` を残す。
- next action owner: operator (pane を見て submit 判断)。
- 用途: handoff を automatically submit したくない場面 (operator が文面を見てから送りたい / receiver agent が現在別 turn を実行中で割り込みたくない、など)。標準 handoff フローには使わない。

## Blind Enter を避ける条件

「観測なしの Enter」を本 contract は許容しない。具体的な禁止と要件は次の通り。

1. typing 前に少なくとも 1 度 `capture-pane` を実施し、pane の文脈を確認した状態 (= read marker 取得済み) でしか typing しない。`orchestrate_handoff` は preflight で `capture_pane(target, read_lines)` を呼ぶ。`message` / `type` / `keys` は `require_read` を通る。
2. typing 完了後、Enter を発行する経路では landing marker を `wait_for_text` で観測する。marker は send 側で構成した完全一致文字列とし、wrap 補修 (`_WRAP_INDENT` 等) の正規化を経た上で substring 一致するもののみを true とする。`wait_for_text` の `True` を Enter の必要条件とする。
3. landing_timeout 内に marker を観測できない場合は、必ず `C-u` で入力をクリアし、Enter は発行しない。die 出口を確保し、`DeliveryOutcome` も `blocked` / `marker_timeout` で正確に emit する。
4. `--mode pending` / `message --no-submit` は意図的に Enter を発行しないが、ここでも typing 後の prompt 状態は durable record に "operator pending" として残す。pane の最終 Enter 押下は operator のみが行う。
5. tmux 経路は `capture-pane` の rendered text を ACK の **正本** にしない。あくまで `submitted` / `pending_submit` の補助観測。wrap / indent / TUI redraw 差で揺れるのは正常な現象として contract が許容する (揺れたら `rolled_back` に倒す)。

`queue-enter` rail (v0.4 以降の default) だけは条件 2 の「`wait_for_text` の `True` を Enter の必要条件とする」を緩める。ただし条件 1 (typing 前 capture-pane / read marker)、条件 3 のうち rollback 方針 (queue-enter rail では `C-u` rollback はせず Enter を発行)、条件 4 (operator pending の durable 記録)、条件 5 (rendered text を ACK 正本にしない) はすべて維持する。queue-enter rail の Enter 発行は「target が agent pane であり Layer B deterministic admission gate (`## Queue-Enter Default Rail` の `### Deterministic Preflight Admission Control` Step 1〜14) をすべて通過した」という別根拠を Enter の precondition として要求し、`reason="queue_enter"` で「pre-Enter landing 観測なし」を durable に明示する。これにより、queue-enter rail でも未観測 Enter を「無条件 blind Enter」に倒さない。strong preflight は default rail の必須前提であり、default 化を根拠にこの preflight 自体を緩めることはしない (`### 強い境界 (Strong Boundaries)`)。

これにより、`wait_for_text` の精度がどれだけ揺れても、strict rail では `submitted` 判定が揺れるだけで contract そのものは揺れず、queue-enter rail では `submitted` (queue-enter, marker 観測あり) と `submitted (unobserved)` (queue-enter, marker 未観測) の wording 区別が揺れるだけで `Status` enum は揺れない。

## Fail-Closed Conditions

次のいずれかが起きた場合、tmux runtime は **delivery を成立させず**、durable record にだけ事実を残す。

- `wait_for_text` が landing_timeout 内に true を返さない → `rolled_back`、Enter なし、`C-u` rollback あり。
- target pane resolve 失敗 → `stage_failed`、typing なし。
- `ensure_agent_target` 失敗 → `stage_failed`、typing なし。
- `normalize_anchor` / `build_notification_body` / `--kind` validation 失敗 → `stage_failed`、typing なし。
- typing 後の `send-keys` 系 transport error → `delivery_failed`、durable record に raw error を残す。pane に残骸が残る可能性があるため、operator が `mozyo-bridge read <target>` で確認できるようにしておく。
- `message --no-submit` で read marker 期限切れ → 何も typing しない。`require_read` が exit 2 で死ぬ。

これらは fail-open に倒さない (= 観測なしで Enter を押さない、anchor 不明のままで durable record だけ書かない)。

`queue-enter` rail だけは「marker 未観測でも Enter を発行する」例外を contract として明示する。これは fail-open ではなく、別 rail として別 outcome (`reason="queue_enter"`) を emit する設計であり、v0.4 以降は agent pane handoff の default rail である。詳細は次節を参照。strict marker observation を Enter の必要条件として要求する経路は `--mode standard` で明示的 fallback として選択できる。

## Queue-Enter Default Rail

v0.2 で導入し v0.4 で default rail に再配置した queue-oriented delivery rail。strict `--mode standard` の挙動は v0.4 でも一切変更せず、明示的 fallback として contract 内に維持する。本節が default rail の正本。

### 目的

codex / claude TUI の queue-oriented prompt は、現在 turn を実行中であっても `Enter` 受理時に prompt を queue として保留し、turn 完了後に順次処理する。この前提下では、`wait_for_text(marker)` が landing_timeout 内に rendered text 上で marker を捉えられないケース (codex TUI の character-wrap shape / busy turn 中の scrollback 流出 / TUI redraw 差) でも、prompt 自体は受理されており、Enter を押せば queue 後段で処理される可能性が高い。

v0.3 までは strict `standard` rail を default に保ち、上記ケースで `C-u` rollback + `marker_timeout` を default の fail-closed としていた。これは strict rail の理論的純度を default に置く設計だったが、operator が手動 Enter で fold する事例が日常化し、transport そのものの product value を削る形になっていた (`## Default Delivery Promise (v0.4)` Durable Rationale 参照)。v0.4 はこの判断を反転し、preflight 強度 (Layer B deterministic admission gate) を保ったまま Enter を発行する queue-enter rail を default に位置づけ、strict rail は明示的 fallback (`--mode standard`) として残す。本 rail は `marker 未観測のまま Enter を押下した` という事実そのものを `reason="queue_enter"` として durable に記録する。

本 rail は strict rail の弱化ではない。strict rail は marker observation を Enter の必要条件として保ったまま残し、`--mode standard` で明示的に選択できる。queue-enter rail は別 mode flag、別 outcome wording (marker 未観測時) で分離する。queue-enter は **default rail** だが、確認した landing を sender に約束しない: `## Default Delivery Promise (v0.4)` の "Promise の限界 (honestly stated)" を参照。

### 適用条件 (Entry Conditions)

本節は queue-enter default rail の **selection 条件** (Layer A: sender が default として `--mode queue-enter` 経路に乗せてよい状況) を記述する。CLI 側の deterministic admission gate (Layer B) は `### Deterministic Preflight Admission Control` が正本であり、Layer B はここで列挙する descriptive な条件のうち machine-checkable な部分を Step ごとに固定する。

queue-enter は v0.4 以降の default rail であり、本 contract の `## Default Delivery Promise (v0.4)` の Scope に当てはまる send は明示的 opt-in なしでこの rail に乗る。下記 1〜4 が満たされない send は default 射程外であり、明示的に `--mode standard` を選ぶか、send を中止する。default rail を黙って弱化して射程外でも通す形には倒さない。

1. send 経路が agent pane 向け handoff である (`mozyo-bridge handoff send` / `handoff reply` / `notify-*` 標準 variants)。`mozyo-bridge message --no-submit` / `notify-*-legacy-task` / non-agent pane への send は default 射程外であり、queue-enter rail に乗らない (`## Default Delivery Promise (v0.4)` Scope 参照)。
2. target が Claude / Codex agent pane と判定されている (`ensure_agent_target` が true)。`--force` を使って non-agent pane に強制 send することは queue-enter rail では許容しない (strict より厳格)。v0.3 で Layer B はさらに per-receiver foreground process allowlist を要求する (`### Deterministic Preflight Admission Control` Step 12)。
3. durable anchor (`source` + `task_id` / `comment_id` などの組) が `orchestrate_handoff` の入力として揃っている。`mozyo-bridge message` の legacy marker shape では queue-enter rail を提供しない (`message` は durable anchor を CLI が知らないため structured outcome を emit できない)。
4. typing 前に `capture-pane` で read marker を取得している (strict rail と同じ blind-typing 禁止条件)。

scope 内であっても上記 2 / 3 / 4 のいずれかが Layer B preflight で fail したら、CLI は default rail を黙って弱化せず `### Deterministic Preflight Admission Control` の Failure Matrix に従って `stage_failed` で die する。sender は preflight 違反の原因修正 (target window 名 / session 名 / active split など) か、`--mode standard` への明示的 fallback を選択する。

### 許容ターゲット (Allowed Targets)

明示的に enumerate する。実装 task はこのリストを `agent_kind` enum 等で機械的に gate する。

- `claude` agent pane (Claude Code TUI など、Enter で prompt を queue 受理する agent)
- `codex` agent pane (codex TUI、character-wrap 起因の marker miss が現実に発生している receiver)

明示的に **不許容**:

- 汎用 shell pane / REPL / editor / pager
- non-agent process が前面の tmux pane (たとえ `--force` で agent gate を bypass しても queue-enter rail は使えない)
- ticket-system 通知用の write-only pane (anchor を持たないため)
- queue-enter rail を「全 tmux target に対する universal Enter」として一般化することは禁止する

実装 task が target class を拡張したい場合は、本 contract の改訂を経由する。CLI 側で勝手に拡張しない。

### Marker Semantics

queue-enter rail でも `wait_for_text(marker)` は引き続き呼ぶ。これは observability のためであり、Enter の発行可否を決める条件ではない (strict rail との決定的な差)。

- marker が landing_timeout 内に observe された場合: `sent` / `ok` を emit する (strict `submitted` と同一の outcome)。`mode="queue-enter"` フィールドは `DeliveryOutcome.mode` に残るため、後段で「strict と同じ marker observation を経た queue-enter rail の使用」を区別可能。
- marker が landing_timeout 内に observe されなかった場合: `C-u` rollback は行わず、Enter を発行する。`sent` / `queue_enter` を emit する。pane の rendered text は ACK の正本ではないため、marker miss 自体は失敗ではなく「sender が pre-Enter で landing を確認できなかった」という事実情報として扱う。
- typing 自体が `send-keys` 系 transport error で失敗した場合: strict rail と同じく `delivery_failed` 経路。queue-enter rail は transport error を覆い隠さない。

`wait_for_text` 実装は queue-enter rail のために挙動を変える必要はない。Enter 発行ロジックだけが mode 分岐する。

### State / Outcome Mapping

新規追加する wording-layer 値:

- `Reason` enum に `queue_enter` を追加する (実装 task で `Literal[..., "queue_enter"]` に拡張)。`Status` enum (`sent` / `pending_input` / `blocked`) は変更しない。
- `DeliveryOutcome.mode` に `queue-enter` (CLI flag 値と一致) が入る。既存の `standard` / `pending` 値は変更しない。
- `_header_label("sent", "queue_enter")` → `"sent (queue-enter, marker unobserved)"` を実装することを推奨。`_outcome_narrative("sent", "queue_enter")` は「Landing marker was not observed before timeout, but Enter was issued under the queue-enter rail because the target is a registered agent pane.」相当の文面を返す。
- `_receiver_contract_line("sent", "queue_enter", receiver)` は strict `sent` と同じ「receiver は durable anchor を読む」契約を返す。queue 経由で受理されるかどうかは sender からは確認不能であるため、receiver-side 契約自体は strict と変わらない。
- `next_action_for("sent", "queue_enter", receiver)` は strict `sent` と同じ owner / phrase (`receiver` / `"read the durable anchor and act from that record as <receiver>"`)。これは contract 上の単一の owner / action として固定する。`## State Transition と Pane の見え方` の同行 (queue-enter, marker 未観測) も同じく `next_action_owner = receiver` に倒す。pane-side の任意 escalation (operator が pickup しないことを観測したら `--mode standard` で再送する経路) は contract の `next_action` ではなく、durable record の `Operator note:` 行に記載する operator-facing fallback hint として扱う (Durable Wording Requirements 参照)。
- `project_last_input` の mapping: `("sent", "queue_enter") → submitted_at = outcome timestamp / acknowledged_at = None / ack_status = "submitted"`。strict `("sent", "ok")` と同じ projection を採る。理由は上流 `mozyo_bridge_pty/vibes/docs/specs/receiver-state-inspector-contract.md` Field-Level Source of Truth Map により `last_input.ack_status` は `submitted_at` / `acknowledged_at` から **derived** されるためで、`submitted_at` を持ちながら `ack_status="unobserved"` を返す projection は構造上不可能 (上流 contract 違反)。さらに同 spec の Transport-Specific Capability Matrix が tmux compat でも `last_input.submitted_at` を populate することを明示している (`delivery ACK 由来; tmux でも submitted は出る`)。queue-enter rail も `submitted` 到達 rail である以上、submitted_at を null に倒すと `pending_input/ok` (Enter 未押下) と区別できなくなる。`sender が pre-Enter で landing を確認していない` という wording-layer の差は `DeliveryOutcome.reason="queue_enter"` と durable record narrative に集約し、inspector projection に流出させない (= inspector は `DeliveryOutcome.reason` の差を見ない)。

既存の wire fields (`Status` enum / `AckStatus` enum / `next_action_owner` enum) は v0.2 で変更しない。queue-enter rail のための拡張は `Reason` enum への新規値 1 つと、`mode` 文字列への新規値 1 つに留める。inspector projection は strict `sent + ok` と完全に同一であり、queue-enter rail は inspector 側の解釈を一切変更しない。

### Durable Wording Requirements

`build_delivery_record` が queue-enter rail 出力で必ず含める文面:

1. `Mode:` 行に `queue-enter` が出る (既存 `standard` / `pending` と同じ位置)。
2. `Outcome:` 行が `sent (queue-enter, marker unobserved)` または `sent (queue-enter, marker observed)` を区別する。
3. `Receiver-side contract:` 行は strict `sent` と同じ文面。queue 経由で受理されない可能性を示すのは次の `Operator note:` 行で行う。`next_action_owner` は strict `sent` と同じ `receiver`、`next_action` も同じ phrase であり、contract の next-action 行 (`- Next action owner:`) も strict `sent` と完全一致。
4. `Operator note:` 行 (新規): marker 未観測の場合のみ追加。「Marker was not observed before Enter; if the receiver does not pick up the prompt within <observation_window>, fall back to `--mode standard` and re-attempt with the recovered read marker.」相当の文面。本行は contract の `next_action` を上書きしない。あくまで operator-facing escalation hint であり、receiver の primary 受領契約 (durable anchor を読む) は strict `sent` と同じ。
5. Asana / Redmine への貼り付け文 (`record-format text`) は上記 5 行を含む。`record-format json` も `mode` / `reason` / `next_action_owner` をそのまま emit する (新規 reason 値だけが追加)。

durable record の文面初期化は v0.2 lineage の実装 task `1214782240686275` が実施済み (queue-enter rail 自体の wording 層の追加)。v0.4 default flip を実施する task `1214825307842391` は、この文面要件を default 化 (default rail に乗る送信が増えることに伴う durable record 量の増加) に対しても維持することを前提とする。いずれの task が手を入れるにしても、上記 5 点を欠いた文面は本 contract に違反する。

### Failure Modes

queue-enter rail 適用時にも以下は strict rail と同じ semantics を維持する。

- `stage_failed` 群 (`target_unavailable` / `target_not_agent` / `invalid_anchor` / `invalid_args`): typing 開始前 die。queue-enter rail を選んでも pre-flight gate は strict と同じ。
- `delivery_failed`: typing 後の transport error。`C-u` rollback の試みは strict と同じ未定義のまま。queue-enter rail はここを暗黙緩和しない。
- `marker_timeout`: queue-enter rail では `marker_timeout` を `Reason` として **emit しない**。代わりに `queue_enter` を emit する。これは「marker miss」と「Enter rollback」を意味的に切り離すための明示的な分離。strict rail での `marker_timeout` は引き続き `rolled_back` を意味する。
- `pending_submit` の意図的経路 (operator が submit を保留する場合) は queue-enter rail では選択不可。`--mode queue-enter` と `--mode pending` は排他であり、両方が同時に意味を持つ組み合わせは作らない。

### Deterministic Preflight Admission Control

v0.3 で追加。`### 適用条件 (Entry Conditions)` を「`--mode queue-enter` 経路に乗ったときに CLI が同時に守る machine-checkable な admission gate」として固定する。本節は実装 task `1214785367563471` が直接の実装対象とする仕様であり、CLI 側の admit / reject は本節の signal セットだけで決まる。v0.4 contract が `--mode queue-enter` を agent pane handoff の normative default に置き、CLI binary も commit `93dc953` (Asana `1214825307842391`) で `default=MODE_QUEUE_ENTER` に flip 済みのため、明示的 opt-in なしの send でも本節の admission gate を通過しないと typing は起きない (旧 transient gap は `### Contract Default vs CLI Default (Transient Gap)` で resolved として記録)。

#### Principle (deterministic over judgment)

`--mode queue-enter` の admission control は、sender / receiver / operator / LLM の状況判断には依存しない。同じ tmux state と同じ CLI args が来たら、admit / reject の決定は同一になる。判断と admission を 2 層に分ける:

- **Layer A — selection (sender の判断)**: `--mode queue-enter` 経路に乗せるかどうか。v0.4 以降は default rail のため、`## Default Delivery Promise (v0.4)` の Scope (agent pane handoff) に当てはまる send は明示的 opt-in なしで本経路を選択し、Scope 外 (`mozyo-bridge message` / non-agent pane / strict landing observation 要件) のときに sender が `--mode standard` を明示する。これは `CLAUDE.md` / `agent-workflow.md` / memory の運用規約に従う sender 側の judgment であり、CLI からは検証できない。
- **Layer B — admission (CLI の判断)**: `--mode queue-enter` が選択された **後**、typing を開始する前に、本節で enumerate する signals がすべて満たされているかを CLI が機械的に検査する。1 つでも欠ければ `send-keys -l` を発行する前に die する。

本 contract が言う "deterministic preflight" は Layer B のことを指す。Layer A は本 contract の射程外であり、運用文書 (`CLAUDE.md` / `agent-workflow.md` / memory) が責任を持つ。Layer A の判断ミス (例: strict landing observation を本来要求する送信で `--mode standard` を明示せず default queue-enter のまま投入してしまう、Scope 外 send に queue-enter を流用しようとする) は本 contract で防げない。本 contract は「Layer A の判断後、Layer B が deterministic に admit / reject すれば、各 rail の fail-closed 制約が成立する」という保証だけを提供する。

#### Preflight Signals (queue-enter で必須・順序固定)

CLI は以下を順番に評価する。順序は副作用 (typing / `send-keys -l`) を未発生に保つために重要。任意の段で reject した場合、typing は一切起こらない (`stage_failed` 経路) し、pane は触らない (`capture-pane` の preflight 観測は 8 番目より後)。

`tmux-send-safety-contract:v0.3:preflight-signals` (この identifier は実装 task と本節の対応を anchor 化するためのもので、CLI 上では emit しない)。

| Step | Signal | Source | Check | 対象 rail |
| --- | --- | --- | --- | --- |
| 1 | `--mode` literal | CLI args | `mode in MODES` (現行 frozenset `{standard, pending, queue-enter}`) | 全 rail |
| 2 | `--to` literal | CLI args | `to in RECEIVERS` (現行 `{claude, codex}`) | 全 rail |
| 3 | `--source` literal | CLI args | `source in SOURCES` (現行 `{asana, redmine}`) | 全 rail |
| 4 | `--kind` literal | CLI args | `kind in KIND_LABELS` | 全 rail |
| 5 | `--force` 排他 | CLI args | queue-enter のとき `force == False` (現行実装ずみ) | queue-enter のみ |
| 6 | `--mode` 排他 | CLI args | `queue-enter` と `pending` は同時指定不可 (Failure Modes 節と整合) | queue-enter のみ |
| 7 | Anchor 構造 | CLI args | `normalize_anchor` が `AnchorError` を投げない | 全 rail |
| 8 | Pane resolution | `pane_info(target_arg)` (`target_arg = --target or receiver`) | resolve が `SystemExit` しない | 全 rail |
| 9 | Window-name binding | `pane.window_name` | `pane.window_name == receiver` (現行実装ずみ。explicit `--target` でも適用) | queue-enter のみ |
| 10 | Same-session binding | `pane.location.split(":", 1)[0]` vs `current_session_name()` | 両者が等しい。`current_session_name()` が `None` (tmux 外起動) の場合は reject | queue-enter のみ (v0.3 新規) |
| 11 | Active-pane binding | `pane.pane_active` | `pane.pane_active == "1"` (受信 window 内で foreground split である) | queue-enter のみ (v0.3 新規) |
| 12 | Foreground process binding (per-receiver) | `pane.command` (`pane_current_command`) | basename が receiver-specific allowlist にある (下記 `Per-Receiver Foreground Process Allowlist`) | queue-enter のみ (v0.3 新規) |
| 13 | Capture-pane preflight | `capture_pane(target, read_lines)` | tmux からの読み取りに成功する (現行実装ずみ) | 全 rail |
| 14 | Body 構築 | `build_notification_body(anchor, kind, summary, receiver)` | `AnchorError` を投げない (kind=`custom` で summary 空などをここで弾く) | 全 rail |

Step 1–8、13–14 は strict / pending と共通の deterministic 段で、既に CLI に存在する。Step 9 は v0.2 で queue-enter に対して追加済み。Step 10–12 が v0.3 で queue-enter に対して新しく要求する admission 段。

Step 10–12 はすべて `pane_lines()` が既に返している field を読むだけで足り、追加の tmux invocation は不要。

#### Per-Receiver Foreground Process Allowlist (Step 12)

queue-enter は generic な `ensure_agent_target` (= `is_agent_process(command)` が真) より厳格な per-receiver allowlist を要求する。ただし以下の **明示的な弱さ** を持つ: 受信側 identity を確証できるのは literal receiver basename (`claude` / `codex`) の場合だけで、`node` literal および versioned native binary basename (例: `1.0.32-arm64`) は Claude Code TUI と Codex CLI の両方が共通して採る foreground process shape のため、Step 12 単体では receiver を区別できず、cross-binding 検出力は Step 9 (window-name binding) と operator discipline (Layer A) に retreat する。本節はこの弱さを正直に契約し、過剰な保証を作らない。

| Receiver | Allowed `pane.command` basename | identity 確証度 |
| --- | --- | --- |
| `claude` | literal `claude` | **strong** — basename が literal `claude` で前面なら、receiver と一致していると Layer B が断言できる。 |
| `codex` | literal `codex` | **strong** — basename が literal `codex` で前面なら、receiver と一致していると Layer B が断言できる。 |
| `claude` / `codex` (both) | literal `node` | **weak** — Claude Code TUI と Codex CLI はどちらも Node ランタイム上で動作するため、`node` foreground は両 receiver で観測される。Step 12 単体ではどちらの receiver か区別できない。cross-binding 検出力は Step 9 の window-name binding と Layer A の operator discipline (受信 window を勝手に rename しない、claude window に codex を起動しない) に依存する。 |
| `claude` / `codex` (both) | `VERSIONED_NATIVE_BINARY_RE` (`\d+\.\d+\.\d+(?:[-+].*)?`) にマッチする basename | **weak** — どちらの CLI が native distribution として配布された場合も同じ regex shape を採るため receiver-agnostic。`node` 行と同じ条件で Step 9 + Layer A に retreat する。 |

不許容 (queue-enter は reject する。strict `--mode standard` は影響を受けない):

- shell 系 (`zsh`, `bash`, `fish`, `sh`, …) — `is_agent_process` が偽。pane が agent ではないので queue-enter を成立させない。
- 空 / `-` — pane の foreground process が捕捉できないケース。Layer B では reject。
- literal receiver basename が **他 receiver の literal basename と一致する** (例: receiver=`claude` で前面が literal `codex` process、receiver=`codex` で前面が literal `claude` process) — generic `ensure_agent_target` は通すが、Step 12 は cross-binding として **reject する**。これが Step 12 の strong identity ケースの典型用途。
- agent process basename が `is_agent_process` を満たすが上記 receiver 行のいずれにも一致しない場合 — reject。

明示的な弱点 (`node` と versioned native binary の cross-binding): 現状の Claude Code TUI と Codex CLI はどちらも Node ベースで動作するため、`node` literal の foreground process は両 receiver で日常的に観測される (例: 本 repo の `%111` codex window 内 pane は `node` を foreground にする)。将来の native distribution が versioned native binary basename を採用した場合も同様に receiver-agnostic な regex shape になる。これらのケースでは:

- Step 9 (`window_name == receiver`) と Step 10 (same-session binding) が一次防御線として残る。operator が `mozyo` startup と `mozyo-bridge init <agent>` の規律を守る限り、window 名は receiver と一致し続けるため、`node` / versioned native binary が前面でも誤投入は起きない。
- 一方で、operator が `tmux rename-window` などで window 名を勝手に書き換えた場合、Step 9 はそれに合わせて admit してしまうため、`node` / versioned native binary 上では Step 12 でも cross-binding を検出できない。本契約はこのケースを Step 12 単体では検出不可と明記し、Layer A の運用規律 (window 名を agent identity と乖離させない) と将来の追加 signal (Open Question 8) に委ねる。

`AGENT_PROCESSES = {claude, codex, node}` 定数自体は変更しない。`pane_resolver.is_agent_process` も strict rail のためにそのまま残す。queue-enter 用の per-receiver 判定は別関数として実装する (例: `pane_resolver.is_receiver_agent_process(command, receiver)`)。実装は上記表の strong / weak 区別をコードコメントとして明記し、weak ケースのために偽の確証文言を CLI 出力に出さない。

#### Stale / Unknown Pane Cases

queue-enter が前提とする pane signals が崩れているケースを Layer B で固定的に reject する:

- **Pane resolution unknown** (`pane_info` SystemExit, target 名が解決不能): Step 8 で `blocked` / `target_unavailable`。typing 前死亡。
- **Pane foreground stale** (Claude/Codex 起動後に exit して shell に戻った pane): Step 12 で `blocked` / `target_not_agent`。typing 前死亡。
- **Pane window misbound** (`window_name != receiver`、explicit `--target` で受信 window 外を指している): Step 9 で `blocked` / `invalid_args`。typing 前死亡。
- **Pane session misbound** (sender と異なる tmux session の pane): Step 10 で `blocked` / `invalid_args`。typing 前死亡。
- **Pane active=0** (window 内で別 split が前面): Step 11 で `blocked` / `invalid_args`。典型的には operator が誤って inactive split の pane id を渡したケース。typing 前死亡。
- **`mozyo-bridge` を tmux 外で実行**: `current_session_name()` が `None` を返す → Step 10 で reject。queue-enter は tmux ランタイム上の admission gate にしか成立させない。

「Pane が一時的に busy」「Pane が前 turn を処理中」など runtime 状態は Layer B では検査しない。これらは receiver runtime 側の状態であり、tmux の pane signals だけからは deterministic に決められないため、本 contract の Open Question 6 / 観測タスク (`1214782185308162`) の射程として残す。

#### Failure Matrix → DeliveryOutcome / Durable Record

新しい `Reason` enum 値は追加しない。Step 10–12 の reject はいずれも既存 `Reason` 値に集約する。これにより、inspector projection (`project_last_input`) 側の挙動も unchanged のままにできる。

| Failed step | `Status` | `Reason` | `mode` | `next_action_owner` | `next_action` 文面 (CLI が die する直前の wording) |
| --- | --- | --- | --- | --- | --- |
| 1 | (`make_outcome` 到達前に CLI 直 die) | — | — | — | `--mode must be one of {…}` |
| 2 | (`make_outcome` 到達前に CLI 直 die) | — | — | — | `--to must be one of {…}` |
| 3 | (`make_outcome` 到達前に CLI 直 die) | — | — | — | `--source must be one of {…}` |
| 4 | `blocked` | `invalid_args` | `queue-enter` | `sender` | `--kind must be one of {…}` (現行 wording) |
| 5 | `blocked` | `invalid_args` | `queue-enter` | `sender` | `--force is not allowed under --mode queue-enter; …` (現行 wording) |
| 6 | `blocked` | `invalid_args` | `queue-enter` | `sender` | `--mode queue-enter and --mode pending are mutually exclusive; choose one rail per send.` (v0.3 新規 wording) |
| 7 | `blocked` | `invalid_anchor` | `queue-enter` | `sender` | `AnchorError` の文言を die にそのまま流す (現行) |
| 8 | `blocked` | `target_unavailable` | `queue-enter` | `sender` | `pane_info` 失敗時の die 文言 (現行) |
| 9 | `blocked` | `invalid_args` | `queue-enter` | `sender` | `--mode queue-enter requires the explicit --target pane to live in the receiver's window; …` (現行 wording) |
| 10 | `blocked` | `invalid_args` | `queue-enter` | `sender` | `--mode queue-enter requires the target pane to live in the sender's tmux session; …` (v0.3 新規 wording) |
| 11 | `blocked` | `invalid_args` | `queue-enter` | `sender` | `--mode queue-enter requires the target pane to be the active split of its window; …` (v0.3 新規 wording) |
| 12 | `blocked` | `target_not_agent` | `queue-enter` | `sender` | `--mode queue-enter requires the foreground process to match the <receiver> agent; got <basename>` (v0.3 新規 wording) |
| 13 | `blocked` | `target_unavailable` | `queue-enter` | `sender` | `capture_pane` 失敗時の die 文言 |
| 14 | `blocked` | `invalid_args` | `queue-enter` | `sender` | `AnchorError` (body 構築側) の文言 (現行) |

Step 1–3 は `make_outcome` を経由せずに CLI が直接 die する現行挙動を保つ。これは args validation が anchor / pane 解決より前に走るためで、structured outcome を emit するための pane / anchor 情報がそもそも揃っていない。

Step 4–14 はいずれも `make_outcome(status="blocked", reason=…, mode="queue-enter", …)` を経由し、`DeliveryOutcome` と durable delivery-record を emit してから die する。durable record はそのまま Asana / Redmine への貼り付け文として使える。

`build_delivery_record` の出力に対する追加要求:

- `Outcome:` 行は `_header_label` の現行分岐をそのまま使い、`blocked` 側は `not delivered (<reason>)` の形になる。queue-enter 専用の追加文言は出さない。
- `Receiver-side contract:` 行は `_receiver_contract_line` の現行分岐 (`sent` / `marker_timeout` のみ wording を返す) を守る。queue-enter の preflight reject 群 (`invalid_args` / `invalid_anchor` / `target_unavailable` / `target_not_agent`) では None を返し、行が出力されない。
- `Next action owner:` 行は `next_action_for` が返す `sender` + 既存 phrase をそのまま使う。queue-enter 専用 phrase を新設しない。
- delivery-record 末尾の die 文言 (CLI が表示する 1 行) が、運用上「なぜ reject されたか」の人間可読 source-of-truth になる。durable record (Asana comment / Redmine journal) には die 文言を併記する運用は CLI 利用者 (`mozyo-bridge handoff send` の caller) が守る。

#### Out of Scope (observability / runtime checks)

以下の signals は意図的に Layer B の admission gate から外す。これらは観測 / runtime / completion の射程であり、`1214782185308162` (observability) と Open Question 6 / 7 で扱う。

- 「receiver TUI が現在別 turn を処理中か」「prompt が空かどうか」など receiver runtime の内部状態。
- 「直前の strict 試行で `marker_timeout` を踏んだか」 — sender の history 情報であり、tmux pane signals では検査できない。CLAUDE.md の Layer A 運用ルール側で扱う。
- 「Enter 押下後、receiver が pickup したかどうか」 — `1214782185308162` の `queue_enter_observed=true/false` 系拡張で扱う。本 contract の admit / reject 判断には使わない。
- 「直近 N 秒以内に同じ pane に queue-enter を撃った」などの throttling — admission gate ではなく rate-limit 領域。本 contract では扱わない。
- 「pane の cwd が sender の repo root の下にあるか」 — Step 10 (same-session) で実運用上の repo scope は担保される (`mozyo` startup が repo basename を session 名にし、session 内 pane の cwd を repo root から派生させる前提)。cwd 個別検査は false positive (subdir で作業中) を生むため Layer B では行わない。

これらを後から Layer B に追加するときは、本節の signal table を改訂し、admission gate の deterministic 性を維持する形 (= 同じ pane / cwd / time から同じ admit / reject を返す形) でだけ拡張する。

#### Why Deterministic over Judgment

queue-enter は strict rail の `wait_for_text` 成功条件を緩めるため、admission を sender 判断 (LLM 判断含む) に任せると、Layer A の判断バイアス (例: 「とりあえず queue-enter を試す」) が strict rail の fail-closed を実質的に弱める結果になる。具体的には次の差が生まれる:

- LLM / sender 判断ベースの admission: 「receiver が claude / codex agent と判断できる」を sender 側で言語化させる → wording の揺れと過剰 admit を生む。再現性がない。
- Deterministic admission (本 contract): pane の `window_name` / `pane_active` / `pane_current_command` / `location` という 4 値 + CLI args + tmux session 名で機械的に決まる → 同じ pane 状態と同じ args なら同じ判定。再現性あり。

本節は「Layer A の運用判断ミス」を救えない。queue-enter を選ぶべきでないケースで sender が `--mode queue-enter` を立てたら、Layer B が条件を満たす限り CLI は通す。これは Layer A / Layer B を意図的に分離した結果であり、Layer B を Layer A の補正に拡張しない (= 例えば「直前の試行履歴を見て auto-downgrade」は本 contract の射程外)。

逆に、Layer A の運用ルールで以下を `CLAUDE.md` / memory に固定し続けることが、Layer B の決定論と組み合わさって queue-enter default rail の安全性を実用上担保する: (a) default 射程 (agent pane handoff) を逸脱して non-agent pane / legacy `message` 経路で queue-oriented 挙動を再現しない、(b) strict landing observation が監査要件である送信 (regression check / brand-new pane / observability test) では `--mode standard` を明示的に選ぶ、(c) receiver agent window 名や session 構造を勝手に書き換えて Layer B の admission gate を空洞化させない、(d) 本 contract が定義しない CLI surface (例: `--mode relaxed` / `--force-enter` / `--unsafe-send`) で default rail を bypass しない。Layer A の規律は本 contract の `### 強い境界 (Strong Boundaries)` 節および root `CLAUDE.md` が引き続き責任を持つ。downstream surface (`CLAUDE.md` / `agent-workflow.md` / `safety.md`) への反映は Asana `1214825156844993` が所有する。

### 強い境界 (Strong Boundaries)

- strict `standard` rail の挙動は本 contract のいかなる版 (v0.2 / v0.3 / v0.3.1 / v0.4) でも変更しない。`wait_for_text` の戻り値が False のとき strict rail は引き続き `C-u` rollback + `marker_timeout` で fail-closed する。
- strict `standard` rail を contract から削除しない。v0.4 contract が normative default を queue-enter に倒した後 (および CLI binary が Asana `1214825307842391` で flip した後) も `--mode standard` は明示的 fallback として残し、strict landing observation を必要とする send (`## Default Delivery Promise (v0.4)` Scope 節の例) に提供する。default 変更を根拠に strict rail を黙って弱化することは禁止する。
- queue-enter rail を universal pane behavior として拡張しない。許容 target は `### 許容ターゲット (Allowed Targets)` で enumerate した agent class に限る。v0.4 で default に昇格してもこの enumerate を緩めない。`--mode queue-enter` の selection 条件 (`### 適用条件 (Entry Conditions)`) と Layer B admission gate (`### Deterministic Preflight Admission Control`) は default 化に伴って弱化しない。
- queue-enter rail の追加・default 化によって receiver runtime / receiver-state inspector の semantics は変更しない。`AckStatus` enum、`runtime_phase` 関連、`process.exited` 関連はすべて触らない。`project_last_input` mapping への追加 (`("sent", "queue_enter")`) は strict `("sent", "ok")` と **完全に同一の projection** (`submitted_at = outcome timestamp / ack_status = "submitted"`) を採る。これは上流 `receiver-state-inspector-contract.md` が `ack_status` を `submitted_at` / `acknowledged_at` から derive すると規定しており、`submitted_at` 既知のとき `ack_status="unobserved"` を返すことが構造上不可能であるため、また同 spec の Capability Matrix が tmux compat でも `submitted_at` を populate することを明示しているためである。queue-enter rail の wording-layer 差 (`reason="queue_enter"` / 別 narrative) は `DeliveryOutcome` と durable record にだけ存在し、inspector projection に流出させない。`acknowledged` を仮装することは引き続き許さない (queue-enter rail でも `acknowledged_at` は populate しない)。
- queue-enter rail を runtime completion / task completion の判定に使わない。pane queue 受理は task 完了の signal ではなく、durable record (Asana task comment / Redmine journal) のみが完了の正本である点は strict と同じ。default 化はこの不変点を一切緩めない。
- 本 contract が定義していない名称 (例: `--mode relaxed`、`--force-enter`、`--unsafe-send`) で同等の挙動を別 CLI surface に実装することは禁止する。queue-enter rail は本 contract の `queue-enter` mode 経路に統一する。

### 配布対象 surface (Distributed Surface Note)

本 contract が要求する CLI / docs / rules / preset への波及は **本 task の射程外**。以下は durable に記録するのみで、本 task では編集しない。

- `cloud.md` は本 repository に存在しないことを確認済み (`find . -name cloud.md` 0 件、2026-05-14 時点)。queue-enter rail を文書化する router/distribution surface は `CLAUDE.md` (root) と `src/mozyo_bridge/scaffold/presets/asana/CLAUDE.md` / `AGENTS.md` / `agent-workflow.md`、および `skills/mozyo-bridge-agent/references/safety.md` / `plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/references/safety.md` が候補となる。
- v0.4 contract pivot の distributed surface 反映は Asana `1214825156844993` が所有する。本 task では実装しない。(v0.2 lineage では Asana `1214782227597692` が同じ surface 群への queue-enter rail 導入時の docs 反映を扱っていた; v0.4 normative default の反映は別 task のため、ownership を `1214825156844993` に切り替える。)
- `README.md` 側の `--mode` 説明 (v0.4 normative default 反転) も Asana `1214825156844993` が所有する。本 task では touch しない。
- queue-enter rail の test / smoke / workflow verification の v0.4 default 反映は Asana `1214825156769677` が所有する。本 task では追加しない。(v0.2 lineage では Asana `1214782185227306` が queue-enter rail 導入時の verification を扱っていた; v0.4 default 反映は別 task のため、ownership を `1214825156769677` に切り替える。)
- queue-enter rail の observability / durable annotation 形式化 (任意) は Asana `1214782185308162` が所有する (defer 決定済; `## Queue-Enter Observability — Defer Rationale` 参照)。本 task では着手しない。

## 短期 wrap 補修との関係 (Non-Goal Boundary)

Asana `1214765093829972` の wrap 補修 (`wait_for_text` を character-wrap shape にも対応させる) は、本 contract と **独立** に進めて構わない。理由:

- wrap 補修は `wait_for_text` 内部の rendered-text 解釈を強くするだけで、本 contract の state machine も DeliveryOutcome 射影も触らない。
- wrap 補修が成立すれば `rolled_back` 頻度は下がるが、contract 上で `submitted` の意味は変わらない。
- 逆に wrap 補修が `wait_for_text` 周辺で marker shape そのものを変える方向に踏み込んだ場合は、上流 ACK spec と本 contract が優先する。`acknowledged` を勝手に名乗らない、`marker_timeout` 経路で blind Enter を許容しない、`message --no-submit` を契約化された fallback として残す、の 3 点は本 contract 側の不変点として保持する。
- gate vs codex TUI compatibility の継続トラッキングは Asana `1214749106025548` が所有する (memory `project_codex_tui_marker_observability_task.md`)。本 task で重複 task を起こさない。

## 既存実装との対応 (strict rail は改造を要しない / queue-enter rail は実装 task で追加)

strict 部分 (`standard` / `pending`) について本 contract は新しい挙動を導入しない。`src/mozyo_bridge/domain/handoff.py` の現行 `DeliveryOutcome` / `next_action_for` / delivery-record builder と、`src/mozyo_bridge/application/commands.py` の `orchestrate_handoff` / `cmd_message` / `_notify_standard_via_handoff` / `notify_agent` の挙動は、上記マッピングを正確に満たしている。

文書化の対象として明示しておく strict 対応:

- `make_outcome(status="sent", reason="ok", ...)` ↔ `submitted`
- `make_outcome(status="pending_input", reason="ok", ...)` (`--mode pending`) ↔ `pending_submit`
- `make_outcome(status="blocked", reason="marker_timeout", ...)` ↔ `rolled_back`
- `make_outcome(status="blocked", reason="target_unavailable" / "target_not_agent" / "invalid_anchor" / "invalid_args", ...)` ↔ `stage_failed`
- `build_delivery_record` の outcome narrative はこのマッピングに沿った文面を既に出している (`Outcome:` 行 / `Receiver-side contract:` 行)。本 contract はそれらの文言意味を固定する。

`next_action_for` の owner / action は本 contract の `next action owner` 列と一致する:

- `submitted` → receiver (anchor を読む)
- `pending_submit` → operator (pane で submit 判断)
- `rolled_back` (`marker_timeout`) → sender (durable record に記録)
- `stage_failed` (`target_unavailable` / `target_not_agent` / `invalid_anchor` / `invalid_args`) → sender

`message --no-submit` (legacy) は durable record を emit しないため、operator が Asana / Redmine 側に試行ログを残す責任を負う。

queue-enter rail について (Asana `1214782240686275` (v0.2 lineage) で queue-enter rail 自体の実装は完了済み; v0.4 normative default 化に伴う `--mode` default flip は Asana `1214825307842391` (v0.4 lineage、parent `1214825156046950`) が所有; 本 task では実装しない):

- `Reason` enum (`Literal[...]`) に `"queue_enter"` を追加。既存 6 値は変更しない。
- `MODES` set / `MODE_*` 定数群に `MODE_QUEUE_ENTER = "queue-enter"` (最終名は実装 task が決定) を追加。`MODE_STANDARD` / `MODE_PENDING` の値は変更しない。
- `make_outcome(status="sent", reason="queue_enter", ...)` ↔ `submitted (queue-enter, marker unobserved)`。
- `make_outcome(status="sent", reason="ok", mode="queue-enter", ...)` ↔ `submitted (queue-enter, marker observed)` — `Reason` は strict と同じ `ok` を使い、wording-layer の差は `mode` field で吸収する。
- `_header_label` / `_outcome_narrative` / `_receiver_contract_line` に `queue_enter` 分岐を追加 (本 contract `Durable Wording Requirements` 節を参照)。`_receiver_contract_line` は strict `sent` と同一文面を返す (queue-enter 専用文面を作らない)。
- `next_action_for("sent", "queue_enter", receiver)` は strict `sent` と同じ `(receiver, "read the durable anchor and act from that record as <receiver>")` を返す。本 contract が要求する単一の owner / action ペアであり、`## State Transition と Pane の見え方` の同行と一致する (operator-side escalation は `Operator note:` 行に記載するのみで `next_action` の値は変更しない)。
- `project_last_input` に `("sent", "queue_enter") → submitted_at = outcome timestamp / acknowledged_at = None / ack_status = "submitted"` の mapping を追加する (strict `("sent", "ok")` と **完全に同一**の projection)。`submitted_at` を null に倒したり `ack_status` を `unobserved` に倒したりしない: 上流 `receiver-state-inspector-contract.md` Field-Level Source of Truth Map / Transport-Specific Capability Matrix の規定により、tmux compat は `submitted` 到達 ACK で `submitted_at` を populate し、`ack_status` は `submitted_at` / `acknowledged_at` から derive されるため、`submitted_at` を持ちながら `ack_status="unobserved"` を返すことは構造上不可能 (上流 contract 違反)。queue-enter rail の sender-side wording 差は `DeliveryOutcome.reason="queue_enter"` と durable record narrative に集約し、inspector projection には流出させない。
- `orchestrate_handoff` の `--mode queue-enter` 分岐: pre-flight は v0.2 では strict と同条件を採っていたが、v0.3 (`### Deterministic Preflight Admission Control` / Asana `1214785367563471`) で strict より厳格な admission gate (per-receiver foreground process allowlist / same-session binding / `pane_active == "1"`) を要求する。typing 後 `wait_for_text(marker)` を呼び、観測の有無に関わらず Enter を発行する点は v0.2 と同じ (観測あり → `make_outcome("sent", "ok", mode="queue-enter")`、観測なし → `make_outcome("sent", "queue_enter", mode="queue-enter")`)。
- CLI surface (`--mode` choices) に `queue-enter` を追加。`--mode pending` との同時指定はエラー (`### Deterministic Preflight Admission Control` Step 6)。
- v0.3 で追加する preflight reject の `make_outcome` 経路: Step 10 (same-session) → `blocked` / `invalid_args`、Step 11 (`pane_active != "1"`) → `blocked` / `invalid_args`、Step 12 (per-receiver foreground process) → `blocked` / `target_not_agent`。いずれも新しい `Reason` 値は追加しない。inspector projection (`project_last_input`) は `blocked` 群を `None` に倒す現行挙動のままで unchanged。

実装 task は本対応表との差分を Asana に記録する。差分が出た場合は本 contract の改訂を経由する (= CLI 実装側で contract を黙って書き換えない)。

## Open Questions

実装凍結前に詰める論点。本 task の射程外として明記しておく。

1. `notify-*-legacy-task` (`.agent_handoff/tasks.json` queue 経由) を deprecation 宣言する時期。durable anchor 引数を持たないため、本 contract の delivery-record emit に乗らない。caller が消えた時点で削除する候補。
2. `message --no-submit` を `mozyo-bridge handoff send --mode pending` に統一する道筋。`message` の marker shape (`[mozyo-bridge from:... pane:... at:...]`) は handoff の marker shape と別 namespace で、現状 codex TUI 側のシンプルな visibility に貢献しているが、長期は 1 つの marker shape に寄せたい。
3. `delivery_failed` の残骸ハンドリング規約。`send-keys` 系 transport error が起きた場合、`C-u` rollback を試みるか、pane を信用しないかの policy を Asana で明文化するか。
4. tmux 経路でも将来 wrapper shell script 経由で sentinel file を読めるようにしたとき、`acknowledged` を tmux 側でも emit するか、それとも本 contract のまま `submitted` 止まりを維持するか (上流 spec Open Question 5 と同一)。
5. `queue-enter` rail の最終 CLI flag 名 (`--mode queue-enter` / `--mode queue` / `--mode agent-queue`) と、`MODE_*` 定数の最終名を確定するか。実装 task `1214782240686275` の判断に委ねるが、`message --no-submit` fallback を将来 `handoff send --mode queue-enter` に寄せるかどうか (Open Question 2 との収束点) は本 contract と揃える必要がある。
6. `queue-enter` rail で Enter 発行後に「実際に receiver が pickup したかどうか」を sender 側で確認する手段を contract に組み込むか。現状は durable record 経由で fold する設計だが、observability task `1214782185308162` が形式化 (例: `queue_enter_observed=true/false` の追記 field、retry hint) を提案するなら本 contract に折り込む必要がある。
7. `queue-enter` rail の許容 target を Claude / Codex 以外の agent (将来追加され得る AI agent / 自作 agent runner) に拡張する基準。現状は本 contract で enumerate した 2 種に固定し、追加は contract 改訂を経由する。
8. v0.3 Step 12 の weak identity branch (Codex audit `1214785691963721` 起源、v0.3.1 で `node` literal も同 branch に再分類) の cross-binding gap。Claude Code TUI と Codex CLI の両方が `node` literal foreground または `VERSIONED_NATIVE_BINARY_RE` shape の foreground を採るため、Step 12 単体では receiver identity を確証できず Step 9 + Layer A 運用規律に retreat する。これを閉じるための追加 signal の候補 (例: pane title への receiver marker 埋め込み、receiver-installed binary path prefix 検査、wrapper 経由の sentinel 出力) と、それぞれの operator burden / install assumption trade-off は別 task で評価する。本 contract の v0.3 / v0.3.1 では「弱点を明記して契約する」までを射程とし、closure は contract 改訂を経由する。

これらの Open Question を解く別 task は親 `1214825156046950` (v0.4 lineage、本 contract の現行親) 配下に切る。v0.2 lineage の親 `1214768200741140` および v0.3 lineage の親 `1214782240916053` は完了済み task chain の歴史的 anchor であり、Open Question 5・8 のように v0.2 / v0.3 実装に対する歴史参照が必要な場合の文脈として残しているにすぎない。v0.4 contract land 以降に切る follow-up は v0.4 lineage 親 `1214825156046950` 配下で扱う。本 contract の land をもって閉じない。

## Queue-Enter Observability — Defer Rationale (Asana `1214782185308162`)

Optional follow-up task `1214782185308162` は「queue-enter fallback の observability / durable annotation を formalize するか」を問うものだった。本節は **追加の形式化を行わず defer する** 判断を durable に記録する。本判断は contract 意味論を変更しないため、Status の version bump はしない。

### 既存 surface が既に first-class に表現している項目

queue-enter rail を使用した事実は、本 contract が要求する以下の surface で既に programmatic / human-readable の両形で観測可能である。新規 field の追加は不要。

| 観測 surface | 表現 | 受け手 |
| --- | --- | --- |
| `DeliveryOutcome.mode` (wire JSON) | literal `"queue-enter"` (`MODE_STANDARD` / `MODE_PENDING` と排他) | sender script / CI / post-hoc audit ツール |
| `DeliveryOutcome.reason` (wire JSON) | `"queue_enter"` (marker 未観測時) または `"ok"` (marker 観測時、`mode="queue-enter"` と組で識別) | 同上 |
| `build_delivery_record` の `Outcome:` 行 | `sent (queue-enter, marker observed)` または `sent (queue-enter, marker unobserved)` | operator が Asana / Redmine に paste した textual record |
| `build_delivery_record` の `Mode:` / `Reason:` 行 | `Mode: queue-enter`、`Reason: queue_enter` または `ok` | 同上 |
| `build_delivery_record` の `Operator note:` 行 | marker 未観測時のみ、queue-enter rail への fallback retry hint を含む | 同上 |
| `_outcome_narrative` 出力 | `queue-enter` rail への明示参照と `No rollback was triggered.` を narrative に持つ | 同上 |
| Asana / Redmine durable record | operator paste 経由で上記 5 surface の文字列が全て durable comment に保存される。本 task chain の dogfood (例: Asana comment `1214785766321371` / `1214785682971732` / `1214787930025646` / `1214788211932108`) で既に reproducible | post-hoc audit、後続 task のレビュー |

これらにより、後から「あの handoff は queue-enter rail を使ったか」「marker は事前観測されたか」を grep ベースで再構成できる。本節 land 時点で実運用上の事例 4 件が既に Asana 上に存在し、grep 可能性は dogfood 済み。

### 意図的に first-class にしない項目

- **`LastInputProjection.ack_status` を queue-enter で別値にしない**: 上流 `mozyo_bridge_pty/vibes/docs/specs/receiver-state-inspector-contract.md` Field-Level Source of Truth Map / Transport-Specific Capability Matrix は `ack_status` を `submitted_at` / `acknowledged_at` から **derive** すると規定する。tmux compat は `submitted_at` を populate する経路であるため、`ack_status="unobserved"` を返すことは構造上不可能 (`submitted_at != None` ⇒ `ack_status` は `submitted` から派生)。queue-enter の wording-layer 差を inspector projection に流出させると上流 contract 違反になる。
- **新規 `queue_enter_used: bool` などの top-level wire field**: `mode` と `reason` の組ですでに一意に判定可能なため、redundant な field を追加しない。projection 経由で leak しないことも上記理由から維持。
- **telemetry / metrics 系の追加**: 本 repo には structured counter / metric emit の infrastructure がなく、本 task の禁止事項にも「broad runtime plumbing は射程外」と明記されている。
- **operator-side escalation を `next_action_owner` に組み込まない**: contract `## Queue-Enter Default Rail` の `State / Outcome Mapping` および `Durable Wording Requirements` 節が明示しているとおり、`next_action_owner` は strict `sent` と同じ `receiver` を保つ。operator-side escalation hint は `Operator note:` 行に専用化済み (durable record の wording layer に閉じる)。

### Real frontier (Open Question 6 — pickup observability)

「queue-enter rail で Enter 後に receiver が実際に pickup したか」を sender 側から確認する手段は本 task の射程外。これは receiver runtime の内部状態を読む必要があり、tmux 経路の pane signal だけでは決定論的に取得できない。本問題は本 contract `## Open Questions` 6 として既に列挙されており、closure は別 task (receiver-side runtime inspector / PTY-first 経路) を経由する。本 defer はこちらの開発機会を塞がない: `mode` + `reason` 観測が wire 上で first-class であるため、pickup probe 機構を追加する側はこの 2 field を即座に手がかりにできる。

### 何が「再オープン」のシグナルか

将来以下の事実が観測されたら、本 defer を再評価する:

- queue-enter rail の使用頻度が増え、Asana durable record の grep だけでは後追いが困難になった (= 専用 dashboard / aggregator が欲しくなった)
- receiver-state inspector (PTY-first 経路) が `last_input.ack_status` の derivation を queue-enter の wording-layer 差に拡張する必要が生じた (= 上流 spec が `unobserved-submitted` 等の中間 ack_status を導入)
- queue-enter pickup の post-hoc 検証手段が確立し、`queue_enter_observed=true/false` 等の追加 field を inspector / durable record に持たせる意義が出た (= Open Question 6 の closure)

これらの兆候のいずれかが顕在化したときに、本 task を `Reopen` するか、新規 task を切る。それまでは現状の wording-layer 観測で十分とする。

## Follow-up Tasks (推奨)

本 contract 投入後に切るべき task。

- 上流 ACK spec の Migration 方針 step 2 (tmux adapter outcome wording を `pending_submit` / `rolled_back` の語彙に揃える) と本 contract の対応表を再点検する task。`DeliveryOutcome.status` / `reason` の wire は変えず、`build_delivery_record` の文言を contract 用語にさらに寄せる小さな PR。
- `notify-*-legacy-task` の caller 在庫調査と deprecation plan task (上記 Open Question 1)。
- `message --no-submit` の使用 log を Asana 上で集約し、上記 Open Question 2 の判断材料にするメタ task。
- 上流 ACK spec の Open Question 1 / 5 と本 contract の Open Question 4 を統合して検討する task (transport 透過性の議論)。
- queue-enter rail を v0.4 normative default としての CLI / handoff 実装に flip する task: Asana `1214825307842391` (parent `1214825156046950`、本 contract の `## Queue-Enter Default Rail` および `## Default Delivery Promise (v0.4)` を仕様の正本として参照する)。v0.2 lineage の CLI 実装 task `1214782240686275` は queue-enter rail 自体の追加を扱っており、v0.4 default 反転は本 task が所有する別経路として切り出されている。
- queue-enter rail を v0.4 default として docs / rules / skill refs / preset surface に反映する task: Asana `1214825156844993` (parent `1214825156046950`; README、`vibes/docs/rules/agent-workflow.md`、`skills/.../safety.md`、`plugins/.../safety.md`、`CLAUDE.md` router、`src/mozyo_bridge/scaffold/presets/asana/` の AGENTS / CLAUDE / agent-workflow)。v0.2 lineage の同 surface 群への docs 反映は Asana `1214782227597692` が扱っていたが、v0.4 default 反映は別 task。
- queue-enter rail の test / smoke / workflow verification を v0.4 default 前提に更新する task: Asana `1214825156769677` (parent `1214825156046950`)。v0.2 lineage では Asana `1214782185227306` が queue-enter rail 導入時の verification 追加を扱っていたが、v0.4 default 反映は別 task。
- queue-enter rail の observability / durable annotation を形式化する task (任意): Asana `1214782185308162` — **defer 決定済み** (本 contract `## Queue-Enter Observability — Defer Rationale` 節参照)。pickup observability (Open Question 6) は別 task / 別経路 (receiver-side runtime inspector) に委ねる。

これらはすべて別 Asana task として親 `1214825156046950` (v0.4 lineage) 配下に切る。v0.2 lineage の親 `1214768200741140` や v0.3 lineage の親 `1214782240916053` は完了済み task chain の歴史的 anchor であり、v0.4 pivot 後の follow-up は v0.4 lineage 親に紐付ける。本 contract 内で「実装に進めた」と扱わない。

## 短い結論

- 現行 tmux compatibility layer は 6 state (`staging` transient + `submitted` / `pending_submit` / `rolled_back` / `delivery_failed` / `stage_failed`) を実装し、`acknowledged` を取らない。これは tmux が原理的に receiver runtime 内部を覗けないことの正直な表現。
- `mozyo-bridge handoff send` / `handoff reply`、`notify-*` standard variants は同じ safety contract に乗る。`notify-*-legacy-task` と `mozyo-bridge message` は legacy compatibility として残し、新規 caller は handoff 経路に寄せる。
- **Normative default rail (v0.4 contract 以降)** は `--mode queue-enter`: Claude / Codex agent pane に対する queue-oriented delivery 経路。Layer B deterministic admission gate (window-name / same-session / active-split / per-receiver foreground process allowlist など) が admit したときに限り Enter を発行し、marker 未観測でも `C-u` rollback を行わない。観測あり → `sent` / `ok`、観測なし → `sent` / `queue_enter` (新規 reason)。default delivery promise は `confirmed landing` ではなく `strong preflight 付き practical queued submission`。durable record (Asana task comment / Redmine journal) は引き続き source of truth であり、queue 受理を task 完了の signal にしない。CLI binary 上も commit `93dc953` (Asana `1214825307842391`) で `default=MODE_QUEUE_ENTER` に flip 済み (旧 transient gap は `### Contract Default vs CLI Default (Transient Gap)` で resolved として記録)。詳細は `## Default Delivery Promise (v0.4)` および `## Queue-Enter Default Rail` を参照。
- **Strict explicit fallback (v0.4 contract)** は `--mode standard`: blind Enter は許容しない。typing 前の `capture-pane` 観測 + landing marker `wait_for_text` 成功を Enter の必要条件にし、marker 未観測時は `C-u` rollback で `rolled_back` に倒す (v0.2 / v0.3 / v0.3.1 / v0.4 で一切変更しない)。strict landing observation を要求する send (regression check / brand-new pane / observability test / strict landing evidence が監査要件) で明示的に選ぶ。v0.4 contract で normative default ではなくなったが contract からは削除しない。
- `Status` enum / `AckStatus` enum / `next_action_owner` enum は変更せず、queue-enter rail は `Reason` enum 1 値と `mode` 文字列 1 値の追加だけで wording-layer を表現する。`next_action_for("sent", "queue_enter", receiver)` は strict `sent` と同じ `receiver` owned。`project_last_input` は strict `("sent", "ok")` と完全に同一の `submitted_at = outcome timestamp / ack_status = "submitted"` を採る (上流 inspector contract が `ack_status` を `submitted_at` / `acknowledged_at` から derive する規定に従う; `submitted_at` を null に倒すと `pending_input/ok` と区別できなくなり、また同 spec の Capability Matrix が tmux compat でも `submitted_at` populate を明示しているため)。queue-enter rail の wording-layer 差は `DeliveryOutcome.reason` と durable record narrative にだけ存在し、inspector projection には流出させない。
- pane への notification が届かないことを失敗とは扱わず、durable record (Asana task comment / Redmine journal) を正本とする。`message --no-submit` は codex TUI gate fallback として retries 3 / Asana 試行記録の手順を memory に従って運用する。長期は `--mode queue-enter` default への統一が候補 (Open Question 5)。
- 短期 wrap 補修 (`1214765093829972`) と gate compatibility tracker (`1214749106025548`) は本 contract と独立に進む。本 contract は `wait_for_text` の精度向上に依存しない。queue-enter default rail も `wait_for_text` 改修と独立。
- 上流 ACK spec の現行 mapping (`sent` ↔ `submitted` / `pending_input` ↔ `pending_submit` / `blocked + marker_timeout` ↔ `rolled_back` / `blocked + target_*/invalid_*` ↔ `stage_failed`) を本 contract の正本マッピングとして固定する。v0.2 で追加するのは `sent + queue_enter` ↔ `submitted (queue-enter, marker unobserved)` 1 行のみ。v0.3 は wire enum を一切変更せず、`### Deterministic Preflight Admission Control` で queue-enter 専用の machine-checkable な admission gate (per-receiver foreground process / same-session / `pane_active == "1"`) を追加し、reject はすべて既存 `blocked + invalid_args` / `blocked + target_not_agent` / `blocked + target_unavailable` に集約する。Step 12 (per-receiver foreground process allowlist) は literal receiver basename (`claude` / `codex`) では strong cross-binding 検出を提供するが、`node` literal および versioned native binary basename (`VERSIONED_NATIVE_BINARY_RE` match) では Claude Code TUI / Codex CLI のどちらも採る receiver-agnostic な shape のため Layer B 単体で receiver identity を確証できず、Step 9 + Layer A の operator discipline に retreat する弱点を明記して契約する (Open Question 8; v0.3.1 で `node` を strong から weak へ訂正)。
