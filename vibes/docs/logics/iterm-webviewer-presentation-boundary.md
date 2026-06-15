# iTerm / WebViewer Presentation Boundary

Redmine #11808。handoff / event feed / cockpit attention を iTerm Toolbelt /
WebViewer / private cockpit UI で表示する時の責務分界を定義する。

## 結論

`mozyo-bridge` は presentation UI の product owner にならない。

`mozyo-bridge` が持つのは、次の reusable contract である。

- Unit / Target discovery (`agents targets` text / JSON)
- event timeline source (`events tail` / `events query --json`)
- OTel / activity cache (`otel serve` / `otel events`)
- attention projection source (`agents attention-project`, `@mozyo_attention_*`)
- safe cockpit primitives (`mozyo cockpit`, append / adopt / reset / layout)

iTerm Toolbelt、WebViewer、private cockpit dashboard、comment-stream view は
consumer / presentation plane の責務である。どの project family を同じ画面に
載せるか、どの色を使うか、どの operator workflow を優先するかは private
consumer 側に置く。

## Source Of Truth

```text
workflow state          -> Redmine issue / journal / status
workspace identity      -> registry.sqlite + workspace anchor
runtime liveness        -> live tmux
target projection       -> agents targets / inventory cache
event timeline          -> events tail/query envelope
attention state         -> derived AttentionRecord / tmux user-option cache
iTerm / WebViewer UI    -> projection only
```

iTerm window、tmux control mode、Toolbelt WebViewer、pane color、browser UI は
source of truth ではない。表示が壊れた時は Redmine / tmux / `agents targets`
/ event source に戻って判断する。

## mozyo-bridge Boundary

`mozyo-bridge` に入れてよいもの:

- public-safe JSON / text schema
- loopback-only generic UI/debug endpoint
- generic event / attention / target facts
- handoff safety preflight
- target ambiguity / stale / unknown の fail-safe reporting
- iTerm から呼べる documented local endpoint / CLI contract

`mozyo-bridge` に入れないもの:

- private project grouping policy
- operator 固有の lane / priority / color policy
- private repository path or private dashboard implementation detail
- iTerm profile / Toolbelt script の user-global 自動配置
- business UX としての comment stream composition
- GUI を routing / authorization / completion の正本にする logic

## iTerm / WebViewer Consumer Boundary

iTerm / WebViewer 側は `mozyo-bridge` の contract を読む consumer である。

許容される consumer action:

- loopback endpoint を表示する。
- `events query --json` 相当の envelope を poll / render する。
- `agents targets --json` の TargetRecord / UnitRecord 語彙で unit を並べる。
- `@mozyo_attention_*` または `agents targets` の `attention` を表示する。
- operator-local preference (色、列順、filter、pin / hide) を private state として持つ。

禁止する coupling:

- WebViewer の DOM state を Redmine gate state として扱う。
- iTerm window / tab / split を routing identity として扱う。
- Toolbelt script に private policy を public scaffold default として入れる。
- UI 上の既読 / 色 / badge を owner approval や review completion の代替にする。

## Existing Surfaces

### `mozyo-bridge otel serve`

既存の cockpit web UI は loopback-only generic debug / operation surface である。
任意ブラウザと iTerm Toolbelt WebViewer の両方で使える。これは private
dashboard ではなく、generic event / inventory / action contract の実装である。

この UI に business-specific comment stream を肥大化させない。必要なら
private consumer が `events` / `agents targets` / attention contract を読んで
別 view を作る。

### `events tail` / `events query --json`

consumer-stable event envelope。OTel raw shape を consumer に直接読ませないための
projection である。delivery feed persist / localhost stream は未確定であり、
追加する場合は別 design consultation で owner / persist / auth / lifecycle を決める。

### `agents targets`

Unit / Target 語彙の canonical projection。cockpit / local / cross-project の
見た目差を resolver semantics に漏らさないため、consumer はこの語彙を読む。

### `agents attention-project`

attention state を tmux user option へ投影する cache writer。色や pane border は
この cache からの projection であり、routing / handoff / close 判断には使わない。

## Design Decision For #11808

#11808 の残論点は次のように閉じる。

- handoff/event feed の UI owner は private consumer / presentation plane。
- `mozyo-bridge` は text / JSON / loopback generic endpoint / tmux projection
  contract までを持つ。
- iTerm Toolbelt / WebViewer は supported consumer であり、core identity ではない。
- private UI が必要とする新しい事実は、まず public-safe primitive として
  `mozyo-bridge` に切り出せるかを判定する。
- private policy / private grouping / private dashboard は private project 側へ置く。

## Follow-up Triggers

次を見つけたら、#11808 の設計に混ぜず follow-up issue に分ける。

- delivery layer の durable feed が必要になった。
- localhost API / SSE / WebSocket stream が必要になった。
- private WebViewer が `events query --json` では不足する field を要求した。
- attention state の owner/review/stalled derivation に Redmine read model が必要になった。
- iTerm Toolbelt script を配布物として扱いたくなった。
- UI action の auth / CSRF / Origin boundary を広げる必要が出た。

## Verification

- public/private boundary に反しないこと。
- generated / tracked docs に private path、secret-shaped value、operator 固有 policy を
  入れないこと。
- docs catalog から本設計を解決できること。
- UI / presentation の変更を routing / workflow completion の正本として使っていないこと。
