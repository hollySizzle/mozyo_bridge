# tmux Send Safety Contract And Fallback Policy

## Status

- Draft: `v0.1` (durable policy; 実装 freeze ではない)
- Scope: 現行 `mozyo-bridge` の tmux compatibility layer に残す send safety contract と fallback policy
- Parent task: Asana `1214768200741140`
- Owning task: Asana `1214768200549398`
- Upstream contract (前提): `mozyo_bridge_pty/vibes/docs/specs/transport-agnostic-ack-state-contract.md`
  - Owning task: Asana `1214768334252326` / approved by comment `1214768792089818`
  - Landed by commit `dcf9c6b` (`_pty` workspace) — "Define transport-agnostic ACK state contract"
- 非射程の related work:
  - 短期 wrap 補修: Asana `1214765093829972`
  - gate vs codex TUI compatibility tracker: Asana `1214749106025548`

## この文書の目的

`mozyo-bridge` の現行 tmux compatibility layer は、長期的には PTY-first runtime に再設計されることが既に決まっている。一方で、現行 tmux runtime は今後しばらく実運用に残る。本文書は、その「現行 tmux runtime に残す safety contract と fallback policy」を durable に固定する。

具体的には次を定義する。

- `mozyo-bridge handoff send` / `handoff reply`、`notify-*` standard variants、`mozyo-bridge message` という現行 send 系 CLI が、どの safety contract に乗り、どこが legacy compatibility として残るか
- 上流 ACK state machine (`submitted` / `acknowledged` / `pending_submit` / `rolled_back` / `delivery_failed` / `stage_failed`) を、現行 tmux CLI surface (`--mode standard` / `--mode pending` / 手動 `read` / `--no-submit`) にどう射影するか
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

tmux 経路の最終到達は `submitted` を超えない。これは contract の弱点ではなく、tmux が原理的に receiver runtime 内部を覗けないことの正直な表現である。

## CLI Surface の射影

現行 CLI surface はどれも上記 state machine の同じ射影に乗る。違いは「durable anchor を CLI 引数として持つかどうか」「marker shape」「`--no-submit` を持つかどうか」だけである。

### `mozyo-bridge handoff send` / `handoff reply` (`orchestrate_handoff`)

- 本 contract の **標準経路**。Asana / Redmine anchor を引数として要求し、marker shape は `[mozyo:handoff:source=<src>:task=<id>:comment=<id>:kind=<label>:to=<receiver>]` を採る。
- `--mode standard` (default): typing → `wait_for_text(marker)` → Enter。成功で `sent` / `ok` (`submitted`)。marker_timeout で `C-u` rollback + `blocked` / `marker_timeout` (`rolled_back`)。
- `--mode pending`: typing 後、`wait_for_text` も Enter も発行しない。`pending_input` / `ok` (`pending_submit`)。入力は prompt に残る。operator 判断で submit する経路。
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

このマッピングは「receiver-state observability の MVP」として、receiver agent が durable record を読むだけで判断できる粒度を保つ。

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

これにより、`wait_for_text` の精度がどれだけ揺れても、`submitted` 判定が揺れるだけで contract そのものは揺れない。

## Fail-Closed Conditions

次のいずれかが起きた場合、tmux runtime は **delivery を成立させず**、durable record にだけ事実を残す。

- `wait_for_text` が landing_timeout 内に true を返さない → `rolled_back`、Enter なし、`C-u` rollback あり。
- target pane resolve 失敗 → `stage_failed`、typing なし。
- `ensure_agent_target` 失敗 → `stage_failed`、typing なし。
- `normalize_anchor` / `build_notification_body` / `--kind` validation 失敗 → `stage_failed`、typing なし。
- typing 後の `send-keys` 系 transport error → `delivery_failed`、durable record に raw error を残す。pane に残骸が残る可能性があるため、operator が `mozyo-bridge read <target>` で確認できるようにしておく。
- `message --no-submit` で read marker 期限切れ → 何も typing しない。`require_read` が exit 2 で死ぬ。

これらは fail-open に倒さない (= 観測なしで Enter を押さない、anchor 不明のままで durable record だけ書かない)。

## 短期 wrap 補修との関係 (Non-Goal Boundary)

Asana `1214765093829972` の wrap 補修 (`wait_for_text` を character-wrap shape にも対応させる) は、本 contract と **独立** に進めて構わない。理由:

- wrap 補修は `wait_for_text` 内部の rendered-text 解釈を強くするだけで、本 contract の state machine も DeliveryOutcome 射影も触らない。
- wrap 補修が成立すれば `rolled_back` 頻度は下がるが、contract 上で `submitted` の意味は変わらない。
- 逆に wrap 補修が `wait_for_text` 周辺で marker shape そのものを変える方向に踏み込んだ場合は、上流 ACK spec と本 contract が優先する。`acknowledged` を勝手に名乗らない、`marker_timeout` 経路で blind Enter を許容しない、`message --no-submit` を契約化された fallback として残す、の 3 点は本 contract 側の不変点として保持する。
- gate vs codex TUI compatibility の継続トラッキングは Asana `1214749106025548` が所有する (memory `project_codex_tui_marker_observability_task.md`)。本 task で重複 task を起こさない。

## 既存実装との対応 (改造を要する箇所はない)

本 contract は新しい挙動を導入しない。`src/mozyo_bridge/domain/handoff.py` の現行 `DeliveryOutcome` / `next_action_for` / delivery-record builder と、`src/mozyo_bridge/application/commands.py` の `orchestrate_handoff` / `cmd_message` / `_notify_standard_via_handoff` / `notify_agent` の挙動は、上記マッピングを正確に満たしている。

文書化の対象として明示しておく対応:

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

## Open Questions

実装凍結前に詰める論点。本 task の射程外として明記しておく。

1. `notify-*-legacy-task` (`.agent_handoff/tasks.json` queue 経由) を deprecation 宣言する時期。durable anchor 引数を持たないため、本 contract の delivery-record emit に乗らない。caller が消えた時点で削除する候補。
2. `message --no-submit` を `mozyo-bridge handoff send --mode pending` に統一する道筋。`message` の marker shape (`[mozyo-bridge from:... pane:... at:...]`) は handoff の marker shape と別 namespace で、現状 codex TUI 側のシンプルな visibility に貢献しているが、長期は 1 つの marker shape に寄せたい。
3. `delivery_failed` の残骸ハンドリング規約。`send-keys` 系 transport error が起きた場合、`C-u` rollback を試みるか、pane を信用しないかの policy を Asana で明文化するか。
4. tmux 経路でも将来 wrapper shell script 経由で sentinel file を読めるようにしたとき、`acknowledged` を tmux 側でも emit するか、それとも本 contract のまま `submitted` 止まりを維持するか (上流 spec Open Question 5 と同一)。

これらは別 task として親 `1214768200741140` 配下で扱う。本 contract の land をもって閉じない。

## Follow-up Tasks (推奨)

本 contract 投入後に切るべき task。

- 上流 ACK spec の Migration 方針 step 2 (tmux adapter outcome wording を `pending_submit` / `rolled_back` の語彙に揃える) と本 contract の対応表を再点検する task。`DeliveryOutcome.status` / `reason` の wire は変えず、`build_delivery_record` の文言を contract 用語にさらに寄せる小さな PR。
- `notify-*-legacy-task` の caller 在庫調査と deprecation plan task (上記 Open Question 1)。
- `message --no-submit` の使用 log を Asana 上で集約し、上記 Open Question 2 の判断材料にするメタ task。
- 上流 ACK spec の Open Question 1 / 5 と本 contract の Open Question 4 を統合して検討する task (transport 透過性の議論)。

これらはすべて別 Asana task として親 `1214768200741140` 配下に切る。本 contract 内で「実装に進めた」と扱わない。

## 短い結論

- 現行 tmux compatibility layer は 6 state (`staging` transient + `submitted` / `pending_submit` / `rolled_back` / `delivery_failed` / `stage_failed`) を実装し、`acknowledged` を取らない。これは tmux が原理的に receiver runtime 内部を覗けないことの正直な表現。
- `mozyo-bridge handoff send` / `handoff reply`、`notify-*` standard variants は同じ safety contract に乗る。`notify-*-legacy-task` と `mozyo-bridge message` は legacy compatibility として残し、新規 caller は handoff 経路に寄せる。
- blind Enter は許容しない: typing 前の `capture-pane` 観測 + landing marker `wait_for_text` 成功を Enter の必要条件にし、marker 未観測時は `C-u` rollback で `rolled_back` に倒す。
- pane への notification が届かないことを失敗とは扱わず、durable record (Asana task comment / Redmine journal) を正本とする。`message --no-submit` は codex TUI gate fallback として retries 3 / Asana 試行記録の手順を memory に従って運用する。
- 短期 wrap 補修 (`1214765093829972`) と gate compatibility tracker (`1214749106025548`) は本 contract と独立に進む。本 contract は `wait_for_text` の精度向上に依存しない。
- 上流 ACK spec の現行 mapping (`sent` ↔ `submitted` / `pending_input` ↔ `pending_submit` / `blocked + marker_timeout` ↔ `rolled_back` / `blocked + target_*/invalid_*` ↔ `stage_failed`) を本 contract の正本マッピングとして固定する。
