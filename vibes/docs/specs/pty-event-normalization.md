# PTY Event Normalization Draft

## Status

- Draft: `v0.1`
- Depends on:
  - `vibes/docs/specs/pty-first-agent-runtime.md`
  - `vibes/docs/specs/agentd-sidecar-ipc.md`
- Purpose: sidecar が emit する低レベル runtime event を、application が扱う `AgentEvent` に落とすための normalization 契約を定義する
- Decision level: 実装凍結ではなく direction draft

## この文書の目的

`pty-first-agent-runtime.md` は `AgentEvent` という application-side の event 抽象を導入した。
`agentd-sidecar-ipc.md` は sidecar が emit する runtime-level event taxonomy (`runtime.output.chunk` など) を定義したが、それを `AgentEvent` にどう変換するかは意図的に未定義としていた。

この spec はその欠落を埋める。

具体的には次を扱う。

- sidecar event から application event への変換段階
- raw payload と normalized payload の保存責務
- provider ごとの「assistant turn finished」検出方針
- permission / approval wait / operator action required の event 化
- human input と orchestrated prompt が混在するときの state transition signal
- sidecar event taxonomy と application event taxonomy の対応表
- ambiguous / unconfirmed 状態の表現

## Scope

この文書が扱うのは、Python-owned `mozyo-agentd` の中での normalization layer だけである。

扱わないもの:

- sidecar 内部の PTY buffering 詳細 (`agentd-sidecar-ipc.md` 側)
- durable SQLite schema 詳細
- attach 描画形式
- summary generation の prompt
- ticket system postback の本文整形

これらは別 spec に分ける。

## Non-Goals

- provider CLI の出力 format を逆解析しきること
- 「provider 出力をすべて semantic event に落とす」ことを完了条件にすること
- ACP adapter と PTY adapter の挙動を完全 1:1 で揃えること
- normalization layer に business decision を埋め込むこと

normalization layer は「runtime substrate から application を守る薄い変換層」であり、orchestration policy の置き場ではない。

## 中核方針

- sidecar が出すのは raw event。意味付けはしない
- application は normalized event の上で動く。raw byte の上で動かない
- normalization は段階的に行う。1 段ですべてを semantic event にしようとしない
- 確証が取れない signal は ambiguous として明示的に残す。沈黙させない
- provider ごとの parsing 差は normalization 層に閉じ込め、application schema に漏らさない

## Pipeline Layers

sidecar event から application が消費する `AgentEvent` までの段階は次のように分ける。

```text
sidecar (Node)
   |
   |  runtime.output.chunk / runtime.process.exited / runtime.error / ...
   v
agentd ingress (Python)
   |
   |  envelope validation, ordering, session/turn binding
   v
provider normalizer (Python, provider-specific)
   |
   |  line / frame assembly, ANSI strip, sentinel detection
   v
event classifier (Python, shared)
   |
   |  event_type assignment, AgentEvent record build
   v
agent event store (durable)
   |
   v
application consumers (attach, journal, summary, audit)
```

各 layer の責務:

- ingress: envelope を信用してよいか確認し、`agent_session_id` / `prompt_turn_id` に紐づける。raw payload はここで application memory に入る
- provider normalizer: ANSI 制御の除去、行 / frame の組み立て、provider-specific 終了 sentinel の検出。provider ごとに別実装
- event classifier: provider normalizer の結果を共通 `event_type` に落とす。schema は provider-agnostic
- store: `AgentEvent` を append-only に保存。raw と normalized の双方を持つ
- consumers: 必ず normalized payload を読む。raw を直接読まない

## Raw vs Normalized Payload

`AgentEvent` は両方を持つ (`pty-first-agent-runtime.md` の domain model 参照)。

### Raw payload

- 内容: sidecar が出した byte / text のまま、または provider normalizer が組み立てた逐次 frame
- 用途: 後追い解析、不具合再現、normalization rule の改善
- 編集: しない。`raw_payload` は immutable record

### Normalized payload

- 内容: application schema (event_type, fields, references) に揃った structured object
- 用途: attach 表示、state transition 判断、summary 生成、audit
- 編集: normalization rule の version up に伴い、新しい event を追記する。過去 event の `normalized_payload` を書き換えない

### 責務境界

- raw payload を作るのは ingress と provider normalizer
- normalized payload を作るのは event classifier
- 「raw が無いと再現できない」状態を許容する。逆に「raw を解釈するのは consumer」を許容しない

これにより、normalization rule が後から変わっても、過去の trace を再 normalize できる余地を残せる。

## Sidecar Event -> Application Event 対応

`agentd-sidecar-ipc.md` の sidecar event を、application 側の代表的な `event_type` に落とすときの初期マッピング。

| sidecar event              | 初期 application event_type                | 備考 |
|----------------------------|--------------------------------------------|------|
| `runtime.sidecar.ready`    | `runtime.sidecar.ready`                    | session に紐づかない。durable には残さない選択肢あり |
| `runtime.session.started`  | `session.runtime_started`                  | `runtime_session_id` を `AgentSession` に bind |
| `runtime.output.chunk`     | `output.raw_chunk` + 派生 normalized event | 派生は後述 |
| `runtime.output.eof`       | `output.stream_eof`                        | stream 終了。session 終了とは別 |
| `runtime.process.exited`   | `session.runtime_exited`                   | exit_code / signal を含める |
| `runtime.input.ack`        | `input.acknowledged`                       | `input_id` を載せる |
| `runtime.cancel.ack`       | `prompt.cancel_acknowledged`               | `prompt_turn_id` を載せる |
| `runtime.error`            | `runtime.error`                            | `error_code` を保持。consumer 向けに既知 code を使う |

`runtime.output.chunk` から派生する application event は固定対応ではない。provider normalizer の出力次第で、次の `event_type` のいずれかになる。

- `prompt.assistant_chunk`
- `prompt.assistant_turn_finished`
- `prompt.assistant_turn_unconfirmed`
- `permission.request_detected`
- `permission.request_suspected`
- `output.unclassified`

これらは「raw chunk が消えるわけではない」。raw は `output.raw_chunk` として残しつつ、別 record として normalized event を append する。

## Canonical Application event_type

application 側で durable に保存される `AgentEvent.event_type` の正式名は dot notation で、provider-agnostic に固定する。本 spec で言及する `event_type` は以下が canonical。別表記 (例: `runtime.output.unclassified`、`permission.approval_required`) は本 spec では使わない。

| event_type                              | 由来 / 用途 |
|-----------------------------------------|-------------|
| `runtime.sidecar.ready`                 | sidecar ingress 確立 (session 非依存) |
| `session.runtime_started`               | `runtime_session_id` を AgentSession に bind |
| `session.runtime_exited`                | `runtime.process.exited` から派生 |
| `output.raw_chunk`                      | 全 `runtime.output.chunk` を必ず残す raw record |
| `output.stream_eof`                     | stdout/stderr stream の EOF |
| `output.unclassified`                   | provider normalizer が分類できなかった chunk |
| `prompt.dispatched`                     | orchestrated prompt の送信確定 |
| `prompt.assistant_chunk`                | assistant 出力 chunk (信頼度付き) |
| `prompt.assistant_turn_finished`        | 終了 sentinel が confidence=high で観測された |
| `prompt.assistant_turn_unconfirmed`     | 終了 sentinel が観測できないまま長時間沈黙 |
| `prompt.cancel_acknowledged`            | `runtime.cancel.ack` から派生 |
| `permission.request_detected`           | permission prompt を confidence=high で検出 |
| `permission.request_suspected`          | permission prompt を confidence=low で検出 |
| `permission.cleared`                    | permission 待ち解除を検出 |
| `permission.granted`                    | application 経路 (人または policy) で承認 |
| `permission.denied`                     | application 経路で拒否 |
| `input.acknowledged`                    | `runtime.input.send` の ack |
| `operator.action_required`              | application policy 起点 (人手介入要求) |
| `runtime.heartbeat_missing`             | chunk も exit も来ない idle 時間が閾値超過 |
| `runtime.error`                         | sidecar 起点の error。`error_code` を保持 |

ルール:

- `event_type` は破壊的変更しない。新意味は新 `event_type` を作る (`Versioning` 節参照)
- 本 spec 外の comment / chat で同義の別表記を使ってよいが、durable record には canonical 名のみを書く
- 「runtime」prefix は sidecar 起源または sidecar が直接 emit したもの、「session」「prompt」「permission」「input」「operator」「output」prefix は application 解釈後の意味付け済み event に使う

## Provider 別 prompt_completed 検出

provider CLI ごとに「assistant turn が終わった」signal は揃っていない。
normalization layer は次の方針で扱う。

### 共通ルール

- 「終了 sentinel が観測されたとき」だけ `prompt.assistant_turn_finished` を emit する
- sentinel が観測できない場合は `prompt.assistant_turn_finished` を emit しない
- 代わりに次のいずれかを emit する
  - `runtime.process.exited` 後の `session.runtime_exited`
  - `prompt.assistant_turn_unconfirmed` (後述)
- 「沈黙したら終わったとみなす」推測はしない

### Claude CLI

- 候補 signal: 出力末尾の対話 prompt 復帰、特定の summary 行、process が再び user input 待ちに戻った瞬間
- 注意: 出力 styling は無告知で変わりうる
- MVP の暫定方針: provider normalizer で観測可能な復帰 marker を 1 つ確定し、それ以外は `prompt.assistant_turn_unconfirmed` に落とす

### Codex CLI

- 候補 signal: ターン終端の行構造、interactive prompt prefix の再出現、特定の終了行
- MVP の暫定方針: Claude と同等。確証が無い時間帯では `unconfirmed` に倒す

### Gemini CLI

- 候補 signal: 未調査
- MVP の暫定方針: 一旦 PTY からの assistant turn finish を `unconfirmed` のみで扱い、`runtime.process.exited` を主な完了 signal とする

### Provider normalizer の最低契約

provider normalizer の出力 token は **internal logical token** であり、durable な `AgentEvent.event_type` 文字列ではない。durable 名に翻訳するのは下流の event classifier の責務とする (`Pipeline Layers` 参照)。識別性のため、internal token は underscore notation を使い、durable `event_type` は dot notation を使うことで両者を視覚的に区別する。

- 入力: ordered `runtime.output.chunk` stream
- 出力: 次の internal logical token のいずれか
  - `assistant_chunk`
  - `assistant_turn_finished`
  - `assistant_turn_unconfirmed`
  - `permission_request_detected`
  - `unclassified_chunk`
- 出力の確度は `confidence` field で表現する (`high` / `low` / `unconfirmed`)

internal token から canonical `event_type` への対応は次の通り。

| provider normalizer token       | confidence | application event_type                  |
|---------------------------------|------------|-----------------------------------------|
| `assistant_chunk`               | -          | `prompt.assistant_chunk`                |
| `assistant_turn_finished`       | high       | `prompt.assistant_turn_finished`        |
| `assistant_turn_finished`       | low        | `prompt.assistant_turn_unconfirmed`     |
| `assistant_turn_unconfirmed`    | -          | `prompt.assistant_turn_unconfirmed`     |
| `permission_request_detected`   | high       | `permission.request_detected`           |
| `permission_request_detected`   | low        | `permission.request_suspected`          |
| `unclassified_chunk`            | -          | `output.unclassified`                   |

`permission_cleared` / `heartbeat_missing` などの解除・タイムアウト系 token は MVP では event classifier 層または application policy 層で生成する (provider normalizer から emit しない)。

application は `confidence != high` の durable event を「state を `idle` などへ前進させる decision の根拠」に使わない。

## Permission / Approval / Operator Action の event 化

provider CLI が「許可を求める対話 prompt」を出すケースは、free text のまま流さない。

### 検出方針

- provider normalizer が「permission を求めている」ことを示す候補テキストを検出する
- 確度 high で検出できたときに `permission.request_detected` を emit する
- 確度が low なときは `permission.request_suspected` を emit する
- どちらも raw chunk は `output.raw_chunk` として残す

### State transition

- `permission.request_detected` 受信時、`AgentSession.state` を `waiting_permission` に遷移する
- `permission.request_suspected` 受信時は、`AgentSession.state` を遷移しない。`runtime.warnings` カウンタだけ上げる
- `permission.granted` / `permission.denied` は application 側の `runtime.input.send` 経由で人間または policy engine が出す
- 応答後、`AgentSession.state == waiting_permission` の状態で provider normalizer から `assistant_chunk` などの通常出力 token が再開したことを event classifier が観測したら、`permission.cleared` を emit する (provider normalizer 自身は解除 token を emit しない。`Provider normalizer の最低契約` 参照)

### Operator action required

permission 以外にも、application が「人手介入が必要」と判定する場面がある。

- runtime が `runtime.error` を emit して session が継続不能
- `prompt.assistant_turn_unconfirmed` が一定時間継続する
- `runtime.process.exited` が想定外 exit code で発生

これらは `operator.action_required` event として application 層が emit する。sidecar には対応する event を作らない (これは application policy であり sidecar の責務ではない)。

## Human Input と Orchestrated Prompt の混在

attach 中の人間入力と、journal / task-driven の orchestrated prompt は、normalization layer から見て常に区別可能でなければならない。

### 入力 side の区別

- orchestrated prompt は `runtime.prompt.send` 経由 (`prompt_turn_id` 付き)
- human input は `runtime.input.send` 経由 (`input_id` 付き, `prompt_turn_id` 無し)
- sidecar は受信順を保つだけ。serialize policy は agentd 側

### Event side の対応

- `runtime.prompt.send` に対する ack -> `prompt.dispatched`
- `runtime.input.send` に対する ack -> `input.acknowledged`
- `prompt.assistant_chunk` は直近の active `prompt_turn_id` に紐づける
- どの prompt turn にも属さない時間帯の chunk は `output.raw_chunk` + `output.unclassified` として残す

### State transition の根拠

- `AgentSession.state` を `prompting` -> `running` に動かす根拠は `prompt.dispatched`
- `running` -> `idle` に動かす根拠は `prompt.assistant_turn_finished` (confidence high)
- `prompt.assistant_turn_unconfirmed` だけでは `idle` に戻さない
- human input 単独 (`input.acknowledged` のみ) では `prompting` 状態に遷移しない
- ただし「permission prompt への返答」として送られた human input は、`permission.granted` / `permission.denied` に昇格させ得る

つまり state transition は基本「orchestrated prompt の event」が動かす。human input は permission flow など限定された場面でのみ state を動かす。

## Ambiguous / Unconfirmed の表現

normalization は「確証が取れない」をきちんと残す。

### Event の確度

- `confidence: high`: provider normalizer が既知 marker で検出した
- `confidence: low`: 候補 marker は当たったが確証が無い
- `confidence: unconfirmed`: marker は当たっていない / 沈黙状態

### Ambiguous event の代表例

- `prompt.assistant_turn_unconfirmed`: 終了 sentinel が無いまま長時間 silent
- `permission.request_suspected`: permission っぽい行が見えたが確信無し
- `output.unclassified`: chunk は来ているが provider normalizer が分類できない
- `runtime.heartbeat_missing`: 一定時間 chunk も exit も来ない

### State 表現

`pty-first-agent-runtime.md` の session state 候補に、normalization 観点で次のうち少なくとも 1 つを正式に組み込む。

- `running_unconfirmed`: `running` から戻ってこないまま confidence high の終了 signal が取れていない
- もしくは `running` 状態を維持したまま `unconfirmed_since` timestamp を session record に持つ

どちらを採るかは未確定 (open question 参照)。決まるまでの暫定は後者 (state は増やさない / metadata で表現する) とする。

## Versioning

normalization rule は変わる。

- `event_type` 名は破壊的変更しない。新意味は新 event_type を作る
- raw payload schema は不変 (sidecar 側の責務)
- normalized payload schema には `normalization_version` を含める
- 同じ raw を別 version で normalize した結果を後から append できる構造にする (詳細は別 spec)

## 既知の制約

- 「provider 出力をすべて semantic event に落とす」ことは MVP の完了条件にしない
- ANSI escape を完全に除去できなくても、raw を保持していれば再解析可能
- detection rule の精度が低い間は `unconfirmed` を多用するのが正しい
- normalization layer は provider drift を吸収する場所だが、provider 出力 format の代替 documentation ではない

## Open Questions

実装凍結前に詰めるべき論点。

1. `running_unconfirmed` を session state に正式採用するか、`unconfirmed_since` を metadata field で済ませるか
2. provider normalizer は provider ごとに独立 module にするか、ルールテーブル + 共通 engine にするか
3. `output.raw_chunk` を durable storage にどこまで保存するか (期間 / 容量 policy)
4. `permission.request_suspected` の閾値や cool-down をどこに持つか (provider normalizer か application policy か)
5. attach client に対しては raw chunk を直接 stream するか、normalized event だけにするか
6. journal-driven prompt と manual prompt で「confidence high な完了 signal が必要な厳しさ」を変えるか

## 短い結論

- sidecar event は意味を持たない。意味は provider normalizer と event classifier が後段で付ける
- raw と normalized は同じ `AgentEvent` 内に両方残す。raw を再解釈してよいのは normalization layer だけ
- 「終わったかどうか分からない」を sentinel として正面から扱う
- permission / operator action は free text のままにせず、event として明示する
- state transition は orchestrated prompt の event に主導させる。human input は限定的にしか state を動かさない
- normalization は薄い変換層であり、orchestration policy の置き場ではない

これにより、provider CLI 表示の変化を application schema に漏らさず、PTY runtime のまま意味のある orchestration を構築できる。
