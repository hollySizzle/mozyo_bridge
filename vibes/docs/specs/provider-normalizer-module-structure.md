# Provider Normalizer Module Structure Draft

## Status

- Draft: `v0.1`
- Depends on:
  - `vibes/docs/specs/pty-first-agent-runtime.md`
  - `vibes/docs/specs/agentd-sidecar-ipc.md`
  - `vibes/docs/specs/pty-event-normalization.md`
- Purpose: provider normalizer 層の module 構造、責務分離、最小 interface、versioning 方針を決める
- Decision level: 実装凍結ではなく direction draft

## この文書の目的

`pty-event-normalization.md` は 5 段の pipeline (sidecar -> ingress -> provider normalizer -> event classifier -> store -> consumers) と、provider normalizer から出る internal logical token の語彙を定義した。
ただし「provider normalizer をどう packageに分けるか」「どこを共通化し、どこを provider ごとに分けるか」「rule の version up と test fixture の責務」は未確定として残されていた。

この spec はその欠落を埋める。

具体的には次を扱う。

- provider normalizer の責務を ingress / event classifier / application policy とどう分離するか
- `per-provider module` 案と `rule table + shared engine` 案の比較と推奨方針
- internal logical token contract と canonical `AgentEvent.event_type` への変換境界の再確認
- Claude / Codex / Gemini の差分をどの層で吸収するか
- package / module 粒度、最小 interface、shared utility の置き場
- normalizer rule の versioning 方針と test / fixture の責務
- intentionally unresolved として残す論点

## Scope

この文書が扱うのは、Python-owned `mozyo-agentd` の中の provider normalizer 層だけである。

扱わないもの:

- sidecar の PTY buffering 詳細 (`agentd-sidecar-ipc.md` 側)
- event classifier から先の durable schema 詳細
- ticket integration / summary generation
- attach 描画形式
- tmux migration の cutover plan

## Non-Goals

- provider CLI の出力 format を逆解析しきること
- 「すべての chunk を semantic token に落とす」ことを完了条件にすること
- provider 間の挙動を完全 1:1 で揃えること
- normalizer 層に state transition や operator action 判定を埋め込むこと

normalizer は薄い変換層であり、orchestration policy の置き場ではない。

## 責務境界の再確認

`pty-event-normalization.md` の pipeline を、本 spec のスコープに合わせて再掲し、各層の責務を簡潔に固定する。

```text
sidecar (Node)
   |
   |  runtime.output.chunk / runtime.process.exited / runtime.error / ...
   v
agentd ingress (Python)
   |
   |  envelope validation, ordering, session/turn binding, raw payload 保持
   v
provider normalizer (Python, provider-specific)
   |
   |  ANSI strip, frame assembly, sentinel detection, internal token + confidence
   v
event classifier (Python, shared)
   |
   |  internal token -> canonical AgentEvent.event_type, derived event 生成
   v
agent event store (durable)
   |
   v
application policy / consumers
   |
   |  state transition, heartbeat policy, operator.action_required, permission flow
```

### Ingress が持つもの

- sidecar envelope の validation
- `agent_session_id` / `prompt_turn_id` への binding
- chunk の到着順保証 (`seq` の単調性確認)
- raw payload (immutable record の元) の保持

### Provider normalizer が持つもの

- chunk からの ANSI escape / control sequence 除去
- chunk の line / frame 組み立て (chunk 境界をまたぐ部分文字列の縫合)
- provider-specific sentinel の検出 (assistant turn finish, permission request)
- 検出結果を internal logical token + `confidence` に変換
- 「分類できなかった chunk」を `unclassified_chunk` として明示

### Event classifier が持つもの

- internal token + confidence から canonical `AgentEvent.event_type` への対応付け
- state-aware な派生 event の生成 (例: `permission.cleared` を assistant_chunk 復帰から導出)
- raw payload を含む `AgentEvent` record の組み立て
- schema validation と `normalization_version` の刻印

### Application policy が持つもの

- `AgentSession.state` の transition
- heartbeat / idle timeout 由来の `runtime.heartbeat_missing`
- `operator.action_required` の emit 条件判断
- permission flow の `granted` / `denied`
- journal-driven prompt と manual prompt の差別化

### この分離が守ろうとしていること

- normalizer は state を最小限しか持たない (chunk 境界縫合のための per-turn buffer など)。session 全体の state を読まない
- session state を読む決定は event classifier より下流 (application policy) に置く
- 「provider 出力 format の変化」が durable schema や state machine に直撃しない

## 採用案: shared engine + per-provider detector

provider normalizer 層の組み立て方として、本 spec は次のハイブリッド案を推奨する。

- ANSI strip / frame assembly / heartbeat tick / token shape は shared engine が provide する
- 各 provider 固有の sentinel detector (assistant_turn_finished, permission_request_detected) は provider module に閉じる
- detector は engine が提供する utility を呼び出す。engine は detector の中身を知らない
- 「rule をすべて data table 化する」第三案は MVP の段階では採らない。将来の direction として open question に残す

これは「per-provider module」案を骨格にしつつ、共通機構を engine に逃がす形である。

### 案の比較

#### 案 A: pure per-provider module

各 provider が独立した module で、ANSI strip から sentinel detection、token emission まで全部やる。shared util は最小。

- 利点
  - provider drift が他 provider に波及しない
  - provider ごとに detector の状態機械を素直に書ける
  - test fixture を module 単位で完結させやすい
- 欠点
  - ANSI strip / frame assembly が module 間で重複する
  - 同等変更を 3 provider で同期する手作業が増える
  - cross-provider invariant test を書きにくい

#### 案 B: rule table + 完全共通 engine

ANSI strip も sentinel marker も regex table で定義し、engine が provider 名を見て table を選ぶ。

- 利点
  - rule 変更が data 変更で済む
  - provider 横断の挙動を engine 側で統一できる
- 欠点
  - stateful sentinel detection (sequence pattern, 多行 marker) を regex table だけで表現するのは早い段階で破綻する
  - provider 出力 format に大きな差が出たとき、table DSL を拡張するか escape hatch を増やすかになり、結局 provider module に戻る
  - 初期段階で 1 provider しか確証 marker を持たない見込みなので、共通 DSL を premature に固定するリスクが高い
  - debug 時に regex hit と internal token の対応が辿りにくい
  - provider 固有 state (例: claude の "human input 待ち" indicator) を rule table に持ち込むと engine が太る

#### 案 C: 採用案 - shared engine + per-provider detector (ハイブリッド)

ANSI strip / frame assembly / heartbeat tick / token shape は engine 側に持つ。
sentinel 判定だけを provider module の detector 関数として書く。detector は engine が提供する text utility を呼び出す。

- 利点
  - 共通機械的処理 (ANSI strip, line assembly) を 1 箇所で持てる
  - provider 固有の stateful 検出を素直に書ける
  - token output schema を engine が enforce できる
  - 後で「detector の中身を rule table 化する」拡張余地を残せる
- 欠点
  - 「engine と detector の境界」設計コストがかかる
  - engine が provider-agnostic に見えながら、暗黙の前提 (例: line 単位の chunk) を引きずるリスクがある

採用根拠は次。

- MVP の 3 provider のうち 2 つは sentinel marker の確証が薄く、検出 logic を data table で固定する根拠が無い
- ANSI strip と frame assembly は明確に共通化できる
- detector を provider module に閉じれば、provider drift のたびに engine を触る必要が無い
- 将来 rule table 化したい場面が来たら、detector の中身を table-driven に書き直せばよい。engine 境界はそのまま使える

つまり「rule table + shared engine」案は捨てるのではなく、detector の中身を 1 段奥に押し下げて、後から差し替え可能にする。

## Internal Logical Token Contract の再確認

provider normalizer の出力 token は `pty-event-normalization.md` で定義済みである。本 spec はそれを参照し、変更しない。

internal token (underscore notation):

- `assistant_chunk`
- `assistant_turn_finished`
- `assistant_turn_unconfirmed`
- `permission_request_detected`
- `unclassified_chunk`

`permission_cleared` と `runtime.heartbeat_missing` は provider normalizer から emit しない。前者は event classifier 層、後者は application policy 層が emit する。

internal token と canonical `AgentEvent.event_type` の対応表も `pty-event-normalization.md` に既出 (`Provider normalizer の最低契約` 節)。本 spec の責務は、その変換境界が event classifier に置かれていることを **provider normalizer 側からも** 明示することにある。

ルール:

- provider normalizer は canonical `event_type` を直接生成しない
- provider normalizer は dot notation を出力に使わない (識別性のため snake_case で固定する)
- 「token は canonical 名と 1:1」ではない。confidence によって canonical event_type が枝分かれする (例: `assistant_turn_finished` + `low` -> `prompt.assistant_turn_unconfirmed`)
- 翻訳責務は event classifier に閉じる

## Provider 差分の吸収位置

Claude / Codex / Gemini の差分をどこで吸収するかを層ごとに整理する。

### Provider normalizer 層が吸収するもの

- provider ごとの assistant turn finished marker (output 末尾の prompt 復帰、特定 summary 行、process が user input 待ちに戻った瞬間など)
- provider ごとの permission prompt の表現差
- provider ごとの ANSI escape の使い方差 (色付け / spinner / cursor 移動)
- provider ごとの chunk 区切り癖 (1 行ずつ / 部分行 / large block)
- detector を持たない provider (Gemini など) は「sentinel 不検出 -> unconfirmed」を素直に出すこと

### Event classifier 層が吸収するもの

- 「sentinel が来ない時間が一定を超えたら token を `assistant_turn_unconfirmed` に格下げする」ような時間軸の判断
  - ただし MVP では provider normalizer 側に短い timeout を置かない。沈黙は application policy 側の heartbeat で扱う
- internal token から canonical event_type への対応 (provider 別ではない、global table)
- `permission.cleared` のような state-aware な派生 event

### Application policy 層が吸収するもの

- provider ごとの「許容される沈黙時間」「permission 自動承認 policy」
- provider ごとの retry policy
- journal-driven prompt と manual prompt の差別化

ルールとして:

- 「provider 名で event_type を分岐する」ことは event classifier より下流で行わない。provider 差は normalizer 層で token + confidence に集約する
- normalizer は session state を読まない。session state-aware な判断は event classifier 以降に置く

## Module 構造案

採用案 (shared engine + per-provider detector) に基づいた最小構成。

```text
src/mozyo_bridge/normalizer/
    __init__.py
    base.py
    tokens.py
    pipeline.py
    text_utils.py
    versioning.py
    providers/
        __init__.py
        base_detector.py
        claude.py
        codex.py
        gemini.py
```

各 module の責務:

- `base.py`: `ProviderNormalizer` Protocol / ABC、`NormalizerContext` (per-turn buffer / detector state を畳む薄い容器)
- `tokens.py`: `InternalToken`, `ConfidenceLevel`, token kind の Literal 定義
- `pipeline.py`: 1 つの `AgentSession` に対する normalizer instance の lifecycle 管理。chunk in / token out をやるだけ
- `text_utils.py`: ANSI escape 除去、line / frame assembly helper、chunk 境界をまたぐ buffer
- `versioning.py`: `NORMALIZATION_VERSION` の定数、detector version の registry
- `providers/base_detector.py`: detector 共通 ABC、sentinel match の共通 helper、`Detector.feed_*` の最小 interface
- `providers/claude.py` / `codex.py` / `gemini.py`: provider 固有 detector 実装

`event classifier` と `application policy` は normalizer 配下に置かない。それぞれ別 module ツリー (例: `src/mozyo_bridge/events/`、`src/mozyo_bridge/policy/`) に独立させ、normalizer は token を返すだけにする。

### Test / fixture 配置

```text
tests/normalizer/
    test_tokens.py
    test_pipeline.py
    text/
        test_ansi.py
        test_frames.py
    providers/
        test_claude.py
        test_codex.py
        test_gemini.py
    fixtures/
        claude/
            <scenario>.chunks.jsonl
            <scenario>.tokens.json
        codex/...
        gemini/...
        shared/
            ansi_corpora.jsonl
```

- `tests/normalizer/fixtures/<provider>/<scenario>.chunks.jsonl`: 入力 `runtime.output.chunk` の sequence
- `tests/normalizer/fixtures/<provider>/<scenario>.tokens.json`: 期待される internal token の列
- fixture は `normalization_version` を file 内に持たせる
- shared fixtures (ANSI corpora) は provider に依存しない unit test 用

## 最小 Interface

provider normalizer の最小 interface を Python の Protocol で示す。実装凍結ではなく direction として読む。

```python
from dataclasses import dataclass
from typing import Iterable, Literal, Optional, Protocol

TokenKind = Literal[
    "assistant_chunk",
    "assistant_turn_finished",
    "assistant_turn_unconfirmed",
    "permission_request_detected",
    "unclassified_chunk",
]

ConfidenceLevel = Literal["high", "low", "unconfirmed"]


@dataclass(frozen=True)
class RawOutputChunk:
    runtime_session_id: str
    stream: Literal["stdout", "stderr"]
    seq: int
    text: str
    received_at: str


@dataclass(frozen=True)
class InternalToken:
    kind: TokenKind
    confidence: ConfidenceLevel
    text: Optional[str]
    detector: str
    detector_version: str
    source_seq_first: int
    source_seq_last: int
    metadata: dict


class ProviderNormalizer(Protocol):
    provider: str
    normalization_version: str

    def begin_turn(self, prompt_turn_id: Optional[str]) -> None: ...

    def feed_chunk(self, chunk: RawOutputChunk) -> Iterable[InternalToken]: ...

    def feed_stream_eof(self, stream: str) -> Iterable[InternalToken]: ...

    def feed_process_exit(
        self, exit_code: Optional[int], signal: Optional[str]
    ) -> Iterable[InternalToken]: ...

    def end_turn(self) -> Iterable[InternalToken]: ...
```

ルール:

- `feed_chunk` は **その chunk から派生する** internal token を 0 個以上返す。返さない (`[]`) ことは正当である
- `feed_process_exit` は exit 事実を normalizer に通知する hook である。normalizer はここから `assistant_turn_finished` を emit しない (`pty-event-normalization.md` の「sentinel が観測されたときだけ `prompt.assistant_turn_finished` を emit する」共通ルールを崩さないため)。MVP では `feed_process_exit` は空列を返してよい。進行中 turn を `assistant_turn_unconfirmed` で flush する余地を許すかは Open Question 6 に集約する
- Gemini のように sentinel が確定しない provider の主な完了 signal は、sidecar event `runtime.process.exited` から event classifier が導出する `session.runtime_exited` 側に置く。normalizer 経路で `assistant_turn_finished` を代用しない
- `begin_turn` / `end_turn` は detector に turn 境界を伝えるためのもの。turn ごとに sentinel 検索 state を reset する
- `source_seq_first` / `source_seq_last` は token がどの raw chunk seq に由来するかを保持する。後追い再 normalize のキー
- normalizer 実装は session state も `AgentSession.state` も読まない (Protocol に渡されない)

## Shared Utility の置き場

`text_utils.py` に置く想定の helper:

- `strip_ansi(text: str) -> str`
- `iter_lines(buffer: TextBuffer, chunk: str) -> Iterable[str]` (chunk 境界縫合用 buffer 付き)
- `match_sentinel(buffer: TextBuffer, pattern: SentinelPattern) -> Optional[SentinelMatch]`
- `TextBuffer`: 直近 N bytes / N lines を保持する ring 状の buffer

ルール:

- helper は state を引数として受け取り、global state を持たない
- provider detector は helper を import するが、helper は detector を import しない (依存方向の固定)
- helper の API 変更は normalization_version の bump 条件 (後述) になり得る

## Normalizer Rule Versioning

`pty-event-normalization.md` で `normalization_version` を normalized payload に含めることが決まっている。本 spec はその version の運用 rule を定義する。

### Version の単位

- normalizer 全体に対する `NORMALIZATION_VERSION` (`MAJOR.MINOR.PATCH` 風) を 1 つ持つ
- 各 detector は独自の `detector_version` を持つ (token に刻印される)
- `normalization_version` は detector_version 群と engine version の組として実体を定義する

### 増分 rule

- `PATCH`: 既存 detector の false positive 削減、ANSI strip の robustness 改善、test fixture 追加。token 出力 schema は変えない
- `MINOR`: 新規 provider 追加、新規 detector 追加、新規 token kind を internal vocabulary に追加 (event classifier に追従が必要)
- `MAJOR`: 既存 token kind の意味変更、token 出力 field の破壊的変更、shared engine API 破壊的変更

### 破壊的変更を避ける運用

- 既存 token kind の意味を変えない。新意味は新 token kind を追加する
- 既存 confidence level を変えない。意味の細分化が必要なら新 kind を追加する
- `event_type` 名の破壊的変更禁止 ruleは event classifier 側にあるが、それは provider normalizer 側の token kind 不変性に支えられている

### 旧 raw の再 normalize

- 同一 raw chunk sequence に対して新 version で normalize し直すことを禁止しない
- 過去 `AgentEvent` の normalized payload は書き換えない (append-only)
- 再 normalize した結果は新 event として store に append する (`normalization_version` が異なる別 record)

## Test / Fixture の責務

### Provider 単位 (fixture-driven)

- input: `runtime.output.chunk` の sequence (jsonl)
- expected: internal token の列 (json)
- 検査: token kind / confidence / text / source_seq 範囲
- fixture は representative scenario (turn 完了、permission request、permission 解除直前、長い沈黙、大量 ANSI escape) を最低限カバーする
- false positive を見つけたら fixture を追加してから detector を修正する (regression 化)

### Shared engine 単位

- ANSI strip: 入力 byte 列に対する idempotency、ANSI 残骸ゼロ
- frame assembly: chunk 境界をまたいだ行が壊れない
- sentinel match: pattern が hit したときに sentinel が確定すること、hit しないときに false negative にならないこと (corpus ベース)

### Cross-provider invariant

- 全 provider normalizer は token kind を canonical set 外に出さない
- 全 provider normalizer は confidence を `high` / `low` / `unconfirmed` 以外に出さない
- 全 provider normalizer の出力 token は `source_seq_first <= source_seq_last` を満たす
- 同一 chunk sequence を 2 回流しても (各 instance を作り直して) 同じ token 列が出る (determinism)

### 統合 test (将来)

- ingress -> normalizer -> event classifier の chain test
- 同じ chunk sequence から expected canonical `AgentEvent.event_type` 列を導けること
- これは normalizer 単体 test の範疇を超える。`tests/integration/` などに別出しする

## 既知の制約

- 「provider 出力をすべて semantic token に落とす」ことは MVP の完了条件にしない
- 検出精度が低い間は `unclassified_chunk` / `assistant_turn_unconfirmed` を多用するのが正しい
- shared engine が「line 単位で chunk が来る」前提を暗黙に持たないよう注意する
- provider 出力 format の代替 documentation を normalizer に背負わせない (fixture を正本として扱う)

## Intentionally Unresolved

実装着手前に詰めるべき論点を以下に残す。これらは MVP の構造としては「決めなくても動く」ので、本 spec では決定しない。

1. detector の中身を将来 rule DSL / table-driven に置き換えるか、Python code のまま運用するか
2. shared engine が `TextBuffer` を per-turn にするか per-session にするか (turn 境界をまたぐ partial line の扱い)
3. `permission_request_detected` の cool-down と false-positive 抑制を provider normalizer に持たせるか event classifier に持たせるか (`pty-event-normalization.md` の open question 4 と同根)
4. fixture format を jsonl のみにするか、生 PTY recording (typescript / asciinema 等) を別途保持するか
5. `RawOutputChunk` に sidecar の `received_at` を載せる粒度 (ms 単位 / 取得しない)
6. `feed_process_exit` で normalizer が emit を許される token の最小 set。MVP では emit しない方針 (空列返却) を仮置きしている。将来、進行中 turn を `assistant_turn_unconfirmed` で flush する用途や、`unclassified_chunk` で残り buffer を吐き出す用途を許すかは未確定。なお `assistant_turn_finished` は sentinel ベース原則を崩すため、本経路では emit を許さない
7. detector 実装言語を Python 以外 (例: 将来 Node sidecar 側に sentinel hint を出させる) に分散させるか。MVP では Python に閉じる
8. normalization version と event classifier version の同期 rule (本 spec は normalizer 側のみ規定)
9. attach client に internal token を直接見せるか、必ず event classifier 通過後の canonical event だけにするか (`pty-event-normalization.md` open question 5 と同根)

## 短い結論

- provider normalizer は薄い変換層であり、session state を読まない / canonical event_type を生成しない
- MVP は「shared engine + per-provider detector」を採る。「rule table + shared engine」は detector 内部の将来差し替え余地として残す
- internal token と confidence の語彙は `pty-event-normalization.md` から変更しない。canonical 名への翻訳は event classifier の責務
- provider 差は normalizer 層で token + confidence に集約し、event classifier 以降は provider-agnostic に保つ
- versioning は MAJOR で意味破壊、MINOR で追加、PATCH で精度改善を表す。過去 record の normalized payload は書き換えない
- test fixture は (provider, scenario) で keying し、regression 化する

これにより、provider CLI の表示変化を application schema に漏らさず、detector の差し替えと精度改善を継続できる構造を確保する。
