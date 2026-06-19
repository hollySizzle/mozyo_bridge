# Managed State Model (desired-state event log と正本境界)

Redmine #11695 / #11697。mozyo-bridge 管理下の session / workspace について、**desired state / managed lifecycle** を replay 可能な event log として正本化し、**observed liveness / handoff target existence** は live tmux runtime を正本のまま維持するための設計正本。設計経緯は #11639 journal #56299 (Design Consultation) / #56318 (Claude Answer) / #56330 (Codex synthesis)、依存棚卸しは #11696 journal #56361。

本 doc は **正本境界の集約点** である。既存 `session-inventory.md` / `workspace-registry.md` / `otel-event-store.md` は本 doc を参照し、判断材料を二重化しない (docs catalog governance)。

## 正本境界 (4 層モデル)

```yaml
正本マップ:
  workspace_identity:        # workspace_id / canonical session / display path / preset
    正本: home registry (registry.sqlite) -> workspace anchor -> path derivation
    根拠: #11429。registry が唯一の書込面。event log / projection に二重化しない
  desired_state:             # mozyo が何を・どのコマンドで作ろう/操作しようとしたか
    正本: managed event log (本 doc で新設)
    根拠: 現状どこにも正本がない欠落層。mozyo コマンド境界で完全観測でき外部要因で変わらない
  observed_liveness:         # pane が今 alive か / active-split / attach / target 実在
    正本: live tmux runtime (pane_lines / list-clients)
    根拠: #56088 制約2。command 境界外で変わる。stale 誤判定が安全事故 (#11666)
  fast_projection:           # UI/CLI 高速参照用の latest estimate
    正本: なし (inventory.sqlite は projection、単独正本にしない)
    根拠: #11422。消えても runtime listing で再構築する best-effort cache
```

判定原則: **「mozyo が意図した構成」は event log 正本化してよい。「今の実在/生死」は runtime 正本のまま。** activity (active/idle) は otel store の best-effort 層として既存どおり (沈黙=unknown→tmux 縮退、#56088)。

## event log の責務 / 非責務

```yaml
責務 (authoritative にしてよい):
  - managed session / window / pane を「いつ・どの mozyo コマンドで」作成/初期化/rename/adopt したか
  - その時点で観測した desired vs observed の差分 (記録時刻つき観測点)
  - managed marker を付与した事実
  - lifecycle 上の意図 (このユニットは mozyo 管理対象である、という宣言)
非責務 (authoritative にしてはいけない):
  - pane の現在の生死 (alive/dead) — live tmux 固定
  - handoff target の解決と全 preflight (session/cwd/process/active-split) — live tmux 固定 (#11666)
  - workspace identity (workspace_id / canonical session) — registry 固定 (#11429)
  - activity (active/idle) — otel store
  - pane discovery / 実在性列挙 (handoff 候補源) — live tmux 固定
```

棚卸し結論 (#56361): 既存 surface (`agents list` / `session list` / handoff / cockpit / recovery) は全て live tmux または registry を正本にしている。それらを event-log/projection-first に**反転させる前提はどこにも安全に成立しない**。event log は既存を置換せず、**欠落している desired-state 層だけを足す**。

## mozyo command 境界での persisted state doctrine

本 doc の「DB を唯一正本にしない」は、SQLite / 静的出力ファイルを軽い参考情報に落とす意味ではない。
mozyo-bridge が所有する persisted state は、分類された責務の範囲では正本である。重要なのは
**storage 種別ではなく、どの state kind の正本か**を固定することである。

```yaml
state_kinds:
  desired_state:
    authority: mozyo-owned persisted state (managed-events.sqlite 等)
    meaning: mozyo が command 境界で作成/採用/mark/rename しようとした構成・意図
  workspace_identity:
    authority: registry.sqlite + workspace anchor
    meaning: workspace_id / canonical session / checkout identity の durable identity
  last_observed_projection:
    authority: authority なし。inventory.sqlite / UI snapshot / reload 出力は timestamped projection
    meaning: 表示・診断・候補提示用の latest estimate。freshness / observed_at を伴う
  runtime_current_fact:
    authority: action-time live runtime observation
    meaning: pane exists / foreground process / cwd / repo / role / workspace/lane / active target
  side_effect_permission:
    authority: mozyo command implementation
    meaning: persisted desired state + durable workflow gate + action-time live preflight を照合した結果
```

したがって、避けるべき表現は「DB を単独使用してはいけない」ではない。この project では通常、
DB / 静的出力ファイルは mozyo command surface を通じて読まれ、更新される。正しい制約は次である。

- mozyo-owned persisted state は、mozyo command / documented API 境界を通して運用する。
- persisted state が `desired_state` / `workspace_identity` の正本なら、その分類内では信頼してよい。
- `last_observed_projection` は UI / cockpit / diagnostics に表示してよいが、stale / unreadable /
  contradictory を明示できる形にする。
- keystroke 送出、handoff、pane kill、window 作成、rename、user option 書込などの side-effecting
  command は、保存済み projection を行動許可にせず、実行直前に live preflight を行う。
- live preflight が読めない、矛盾する、ambiguous な場合は fail closed する。保存済み snapshot の
  新しさだけで `healthy` / `current` / action allowed を導出しない。

UI / cockpit の観点では、静的出力が SQLite であっても JSON であっても思想は同じである。UI は
mozyo command が更新した read model / projection を読める。ただし UI が表示した状態は action
permission ではなく、UI action は command 境界へ戻して action-time preflight を通す。
continuous polling / push observer / manual reload の採否は freshness UX の問題であり、この正本境界を
変更しない。

### storage scope 方針

SQLite state は原則として **global home scope** に置く。
ここでいう global home は `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}` であり、user ごとの mozyo-bridge
runtime state の集約点である。workspace / project ごとに独立した SQLite を増やす方針は default に
しない。

理由:

- cockpit / cross-project / multi-lane / coordinator は複数 workspace を横断して読むため、repo-local
  DB を横断 scan する設計にすると discovery / recovery / permission 境界が散る。
- Docker / devcontainer / ephemeral checkout では repo-local state が container lifecycle や mount
  policy に巻き込まれやすい。home scope に置けば user runtime として扱える。
- workspace identity、managed event、inventory projection は相互参照される。物理 DB が分かれていると、
  schema migration、backup、doctor、integrity check が複数 surface に散る。
- repo-local `.mozyo-bridge/**` は public / portable な scaffold・rules・anchor・config の置き場であり、
  user runtime DB の置き場とは性格が違う。

したがって長期推奨は、`registry.sqlite` / `managed-events.sqlite` / `inventory.sqlite` 等を
**1 つの home-scoped SQLite に統合する方向**である。ただしこれは正本境界を混ぜる意味ではない。
統合する場合も table / namespace / owner / recovery policy ごとに上記 state kind を保ち、
`projection` を `runtime_current_fact` や `side_effect_permission` に昇格させない。

現行の複数 SQLite は、段階実装で lifecycle / loss recovery を分けて安全に導入した結果であり、
永続的な理想形として固定しない。統合は migration / compatibility / downgrade / corruption blast radius
を伴うため、plugin foundation hardening の後続設計 task として扱い、既存 DB を即時に手動統合しない。

例外として repo-local に置いてよいものは、workspace anchor、scaffold / rules / docs catalog、project-local
desired presentation config など、repo とともに portability / reviewability を持つべき小さな static
artifact である。user runtime state や cross-workspace read model は home-scoped DB を原則にする。

## schema 案 (event log v1, append-only)

```sql
PRAGMA user_version = 1;
CREATE TABLE managed_events (
    id INTEGER PRIMARY KEY,
    recorded_at TEXT NOT NULL,        -- 記録時刻 UTC ISO8601 (= 観測点の時刻)
    command TEXT NOT NULL,            -- 発行した mozyo コマンド (mozyo / init / register 等)
    event_kind TEXT NOT NULL,         -- created | adopted | renamed | marked | observed
    socket TEXT NOT NULL DEFAULT 'default',  -- 複数 tmux server 拡張点 (#11628)
    pane_id TEXT,                     -- identity key は pane_id (#11628)。session は属性
    mozyo_session TEXT,               -- 観測時の session 名 (属性)
    workspace_id TEXT,                -- registry 由来 (FK 的参照。正本は registry)
    repo_root TEXT,                   -- 書込時に normalize_path_unicode (NFD 固定, #11625)
    intent_json TEXT NOT NULL         -- desired 構成の最小 record (agent / window 名等)
);
CREATE INDEX idx_managed_events_pane ON managed_events(pane_id, recorded_at);
CREATE INDEX idx_managed_events_ws ON managed_events(workspace_id, recorded_at);
```

- **append-only**。状態は event の畳み込みで導出 (latest estimate は projection 側へ)。
- 置き場: `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/managed-events.sqlite`。SQLite single-writer (#56088、Postgres/MQ 不採用)。
- identity key は `pane_id` (#11628)。`session` は属性。folding が必要なら `fold_agents_by_pane` を共有し二重実装しない。
- `repo_root` は **書込時点で** NFD 正規化 (#11625)。比較を読み出し側へ散らさない。
- `socket` は今 `'default'` 固定。schema に最初から持たせ、複数 server 対応時に `(socket, pane_id)` 複合キーへ無痛移行 (#11628 拡張点)。

## loss recovery 方針

```yaml
managed-events.sqlite 喪失時:
  方針: best-effort。失った履歴は失ったまま。identity は registry/anchor が正本なので壊れない
  禁止: event log を identity 正本に格上げして「両取り」すること (#11429 二重化 + 循環依存)
  再構築: 次の mozyo コマンド境界から append 再開。過去 desired state の完全復元は保証しない
  根拠: otel-events / inventory と同じ regenerable cache 姿勢 (#11422)。正本化する範囲を
        「registry anchor で再構築できない情報 = desired-state 履歴」に絞る代償として、
        その履歴は loss を許容する
```

identity (registry/anchor) と liveness (tmux) はどちらも event log に依存しないため、event log 喪失は**識別・生死・handoff のいずれも壊さない**。壊れるのは「過去に何を意図したかの履歴」だけ。

## managed marker 方針 (多層、既存資産優先)

```yaml
managed 判定の優先順位:
  一次: workspace registry anchor (.mozyo-bridge/workspace-anchor.json, #11429)
    理由: 既に「mozyo 管理下の workspace」の正本。新規 marker より identity と一体で堅牢
    互換: 旧名 .mozyo-bridge/workspace.json は read fallback として扱う
  二次: tmux user option (例: session/pane に `@mozyo_managed` を set)
    理由: mozyo が set した事実そのものを持てる。外部から見えにくく rename/導出差に強い
  補助: OTel resource attrs (mozyo.session 等, #11676)
    理由: telemetry join 用。push 型 best-effort で受信漏れ = unmanaged 誤判定のため正本不可
  表示補助のみ: session name prefix (mozyo-, #10796)
    理由: display/grouping identity であって権限境界ではない。prefix 単独を managed 判定の
          権限境界にすると prefix を真似た外部 session を managed と誤認する (明示却下)
```

## unmanaged / runtime-only session の扱い

- 外部から直接作られた / marker 不在の session・pane は **排除せず `unmanaged / runtime-only` として共存表示**する。
- 完全排除は非現実的 (operator が手で tmux を触る現実がある)。marker 不在 = event log に desired state を持たない、を **自動的に unmanaged 区分**として扱えば安全に共存できる。これは #11677 の「観測漏れ検出」と同型。
- managed/unmanaged は表示・運用の区別であり、**handoff の安全境界 (runtime 正本) は managed/unmanaged を問わず不変**。

## 却下した案 (記録)

- 単一 SQLite を全状態の唯一正本化 — best-effort / runtime-first 不変条件 (#11422/#11429/#11666) と衝突。
- command-boundary fetch を liveness 主信号にする — idle な正常 pane の生死が古くなる。liveness は OTel/tmux の受動観測が主、command-boundary は desired-state 更新が主、と trigger 役割を分ける (#56318 反対意見)。
- session name prefix を managed 判定の権限境界にする (#10796 と矛盾)。
- #11639 の既存 cockpit / OTel / launchd へ後付け混入 — audit 単位肥大化のため本 US で分離。

## PoC 実装状況 (#11698)

設計確定後の最小 PoC を実装した (liveness / handoff 不触が不変条件):

1. **marker PoC (#11699)**: `domain/managed_marker.py` — `classify_managed(repo_root, tmux_marker)` が registry anchor 一次 → tmux user option (`@mozyo_managed`) 二次 → `unmanaged/runtime-only` の順で判定。name prefix は引数にすら持たない (権限境界にしない、#10796)。tmux helper は `infrastructure/tmux_client.set_user_option` / `get_user_option` (非致死)。
2. **desired-state append PoC (#11700)**: `managed_events.py` — append-only `managed-events.sqlite` v1。`record_managed_event()` が command boundary 用の best-effort append surface (失敗は None、command を壊さない)。pane_id identity / `socket` 拡張点 / 書込時 NFD 正規化 / single-writer。

3. **command-boundary 配線 (#11726/#11727)**: pane を生成する唯一の mozyo boundary (`application/commands.new_agent_session_window` / `new_agent_window`) の pane_id 確定直後に `_record_managed_pane_created()` を呼び、`KIND_CREATED` event を append + 二次 marker (`mark_target`) を付与する。helper 全体が `try/except` で best-effort (append/marker 失敗でも pane 生成は壊れない)。pane_id を identity key、session を attribute として記録し、repo_root は `record_managed_event` 側で NFD 正規化。

PoC は既存 surface に projection-first 反転を加えていない (managed_events は read 経路を liveness/handoff に一切持たない — guard test で pin)。command-boundary 配線は append (desired-state 記録) のみで、liveness / handoff target resolve / preflight / tmux discovery は引き続き live tmux 正本 (#11726 不変条件)。残る後続段階は inventory / cockpit への managed/unmanaged 区分表示。

## owner 判断事項 (#56318 から継続)

- 外部 tmux 直接操作を「unmanaged 共存」(本 doc 採用) とするか「marker 強制」とするか。本 doc は共存を推奨するが、運用方針判断のため owner 確認。

## 検証 (本 doc は設計のみ。実装時に適用)

- 本 doc 自体は設計記録であり code を伴わない。`mozyo-bridge docs validate` で catalog 整合のみ確認。
- PoC / 実装段階では本 doc の正本境界表を pin とし、liveness/handoff を event-log 正本にしていないことを test で担保する。
