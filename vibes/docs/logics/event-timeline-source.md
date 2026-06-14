# Event Timeline Source (handoff / event timeline の text / JSON backend)

Redmine #11813。cockpit / private GUI / iTerm WebViewer などの表示側が利用する、
mozyo-bridge 側の **consumer 向け event timeline source** の設計正本。実装は
`src/mozyo_bridge/domain/event_timeline.py` (projection)、`src/mozyo_bridge/otel_store.py`
の `query_events` read 面、`mozyo-bridge events tail` / `events query --json` CLI。

mozyo-bridge は frontend を二重管理せず、**事実供給に寄せる**。表示・運用方針は
private consumer 側に置く (`vibes/docs/rules/public-private-boundary.md`、#11809)。本 logic は
その boundary rule の Examples が許可する「generic event tail / query source」の実装である。

## 位置づけ: 既存 OTel store との分界

```yaml
otel events:
  surface: mozyo-bridge otel events --json / activity
  目的: OTel raw event の debug / per-CLI depth 実測 (logic-otel-event-store)
  形: 受信 OTLP を normalize した内部 shape (signal / event_name / attrs_json 等)
events tail|query:
  surface: mozyo-bridge events tail / events query --json
  目的: 表示 consumer 向けの安定した timeline envelope
  形: source_layer tag 付き・redact 済み・consumer 安定 schema の TimelineEvent
不変条件:
  - events は otel events を置き換えない。otel events は OTLP に密結合した debug 面のまま
  - events は consumer に対して OTel 内部 shape を晒さない。OTel schema が変わっても
    envelope を安定に保つための projection 層である
```

`otel events` を consumer に直接読ませると、OTLP 内部 shape (attrs_json の生 key、signal
語彙) に表示側が結合してしまう。`events` 面はその間に **安定 envelope** を挟む。

## 三層 source layering

表示 consumer が扱う「timeline」は、信頼度の異なる 3 層の事実から成る。本 source は
各 event に `source_layer` tag を付け、consumer が trust 境界を判別できるようにする。

```yaml
source_layer:
  runtime:
    意味: OTel runtime event store 由来 (best-effort、正本ではない)
    feed: otel-events.sqlite (logic-otel-event-store)。受け口停止中は lost で受容
    本 issue で実装: yes
  delivery:
    意味: handoff delivery 事実 (notification を送ったという事実)。完了/review 真実ではない
    feed: 現状 durable persist されていない。DeliveryOutcome は ticket へ貼られる text で、
          local DB feed は持たない (domain/handoff.py)
    本 issue で実装: no (envelope に枠だけ予約。下記「未採用と design finding」を読む)
  anchor:
    意味: Redmine issue / journal id は durable anchor。pointer のみ
    feed: Redmine。本 source は anchor 内容を local に複製しない
    本 issue で実装: pointer field のみ。runtime event が anchor 風 attr を持てば
                     pointer として surface するが、現状 OTel allowlist に anchor key は無い
不変条件:
  - DB を唯一正本にしない。runtime event store は best-effort cache であり、
    timeline は「過去にこう発火した」を answer するだけ。gate 状態の正本は Redmine、
    liveness の正本は tmux (agents list / session list)
  - durable record (Redmine journal) を local DB に wholesale copy しない。
    anchor は id pointer のみ
  - delivery 事実を表示したくなっても、本 issue では新規 persist 層を足さない。
    必要なら design consultation で feed の owner / persist 境界を決める
```

## Consumer envelope (TimelineEvent)

consumer が安定して coding できる shape。OTel 内部 shape から decouple している。

```yaml
TimelineEvent:
  id: string              # store row id 由来の安定 id (consumer の dedup / cursor 用)
  source_layer: string    # runtime | delivery | anchor (現状は runtime のみ emit)
  observed_at: string|null # 受け口時計 (received_at, UTC ISO)。timeline の並び基準
  event_time: string|null  # payload 申告時刻 (あれば)
  category: string        # 粗い分類: tool | api | session | usage | event
  signal: string          # logs | metrics | traces (runtime 由来の素の signal)
  event_name: string      # eventName / metric 名 / span 名
  agent:                  # 識別子のみ。path / 自由文を含めない
    service: string|null      # service_name (claude-code 等)
    session: string|null      # CLI session id
    mozyo_session: string|null    # OTEL_RESOURCE_ATTRIBUTES join key (ASCII id)
    mozyo_agent: string|null
    mozyo_workspace_id: string|null
  workspace_hint: string|null # cwd の **leaf basename のみ** (full path は emit しない)
  usage:                  # numeric subset のみ (あれば)
    input_tokens / output_tokens / cache_read_tokens /
    cache_creation_tokens / total_tokens / cost_usd / duration_ms
  summary: string         # 短い人間向けラベル ("claude-code api_request" 等)
  anchor: object|null     # durable anchor pointer (現状 runtime event では常に null)
```

- envelope は **追加のみ** で進化させる。既存 key の意味・型を変えない (consumer 互換)。
- `id` は store row id 由来で単調。consumer は cursor / dedup に使える。ただし store は
  regenerable cache なので、再生成後に id が振り直ることを consumer は許容する
  (cursor は best-effort。durable な順序保証は提供しない)。

## Redaction posture (secret / private path を漏らさない)

acceptance criterion「JSON は secret / private path を漏らしにくい形」に対する設計。

```yaml
redaction:
  - store 側の allowlist + deny token (logic-otel-event-store) を第一防壁とし、
    projection 側で **二重に** 防ぐ (defense in depth)
  - path 値 (cwd / workspace.dir) を full path で emit しない。cwd は leaf basename
    だけを workspace_hint として出す。それ以外の path 風 attr は drop
  - deny token (prompt / content / message / secret / token_value / authorization /
    password / email ...) を projection でも再チェックし、attrs から落とす
  - emit するのは: 識別子 (service / session / mozyo.* ASCII id)、event 種別、
    numeric usage、basename hint のみ
不変条件:
  - prompt 本文は store にそもそも無い (store の不変条件)。projection も再現できない
  - private absolute path を JSON / text どちらにも出さない。
    workspace_hint は basename のみ (例: `/Users/alice/work/secret-proj` -> `secret-proj`)
```

basename すら出したくない consumer 向けに full redaction option を将来足す余地はあるが、
本 issue では basename hint を default とする (timeline で workspace を区別するのに必要な
最小情報であり、絶対 path を出さないことで boundary rule を満たす)。

## CLI

```yaml
events tail:
  form: mozyo-bridge events tail [--limit N] [--db PATH] [--json]
  動作: 直近 N 件 (既定 50) の TimelineEvent を時系列降順で出す
events query:
  form: mozyo-bridge events query [--since ISO] [--source SERVICE] [--limit N] [--db PATH] [--json]
  動作: --since (observed_at >= ISO) / --source (service_name 完全一致) で絞った
        TimelineEvent を出す。query primitive として SQL 段で filter する
出力:
  text: 1 行 1 event の compact table (consumer の human inspection / debug 用)
  json: TimelineEvent envelope の list (consumer 機械処理用の正路)
read-only: store を書かない。CLI read は read-only connection (writer を block しない)
degrade: store missing / corrupt / 未知 schema は空 list として返す (logic-otel-event-store)
```

`--db` は test / 別 store 指定用の override。既定は
`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/otel-events.sqlite`。

## 未採用と design finding (scope 外として明示)

acceptance criterion「GUI 表示は scope 外。localhost API / stream が必要なら design
consultation で範囲を決める」に従い、本 issue では次を **採用しない**。

```yaml
未採用:
  - localhost HTTP API / SSE / websocket stream:
      理由: 表示側の pull 間隔・auth・lifecycle は consumer の運用方針に依存する。
            CLI text/JSON source を先に出し、stream が要るなら design consultation で
            owner / contract / safety を決める (public-private-boundary の design
            consultation 明示項目に従う)
  - delivery 事実の persist feed:
      理由: DeliveryOutcome は現状 ticket へ貼る text であり local persist 層を持たない。
            timeline に delivery 層を足すには新規 persist 境界の決定が要る。
            DB を唯一正本にしない不変条件に触れるため、design consultation 案件
  - GUI / cockpit composition:
      理由: private presentation (public-private-boundary)。mozyo-bridge は source のみ
design_finding:
  - delivery 層 feed と localhost stream は同じ design consultation でまとめて
    owner / persist 境界 / contract を決めるのが妥当。本 issue は runtime 層の
    CLI source に絞って薄く出す
```

## 検証

- unit tests: `tests/test_event_timeline.py`
  (projection shape / source_layer tag / redaction で full path 非露出 / usage subset /
  query filter (since / source) / CLI text・JSON)
- `python3 -m unittest tests.test_event_timeline`
- docs: `mozyo-bridge docs validate --repo .` / `--check-file-coverage`
