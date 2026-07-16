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

### repo-local artifact boundary (#12259)

Repo-local `.mozyo-bridge/**` は、checkout と一緒に review / copy / scaffold される static artifact の
置き場である。runtime DB や cross-workspace read model の default 置き場にしない。

Repo-local に残す:

- workspace anchor (`workspace-anchor.json`)。identity 復元の種であり、runtime listing や cache ではない。
- scaffold metadata、rules、docs catalog、generated conventions の source / output。
- portable project defaults と desired presentation config。private operator policy や live target state は入れない。
- human-readable docs / runbook。実行時の liveness、activity、handoff delivery、review/close truth は入れない。

Home-scoped DB に置く:

- workspace registry と state schema metadata。
- managed event / desired-state history。
- inventory / target observation / activity timeline などの rebuildable projection。
- cockpit grouping / unit desired current state など、operator が mutable に管理する presentation state。

Docker / devcontainer / ephemeral home では、home scope 自体が消えることを前提にする。消えても repo-local
anchor / scaffold / docs から workspace identity を再登録できるようにし、runtime projection は rebuildable
cache として扱う。逆に、home が消えるからといって repo-local runtime DB を default にしない。必要な場合は
explicit import / export / backup command を設計し、tracked repo artifact へ暗黙に runtime state を漏らさない。

## 現行 SQLite 棚卸し (#12257)

2026-06-19 時点の runtime SQLite はすべて `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}` 配下に置かれる。
repo-local `.mozyo-bridge/**` に置かない。各 file は導入時の blast radius を小さくするため分かれているが、
state kind は既に分離済みであり、single SQLite 統合時もこの分離を table namespace と recovery policy に
移す。

| legacy file | schema | owner / writer | state kind | loss / corruption policy |
| --- | --- | --- | --- | --- |
| `registry.sqlite` | v1 | `workspace_registry.register_workspace()` / guarded `init` | `workspace_identity` 正本。`workspaces` と `workspace_activity` | identity 正本なので write path は corrupt / unknown schema で die。operator が退避し、workspace anchor から再登録する。read path は anchor / derivation へ degrade |
| `managed-events.sqlite` | v1 | mozyo command boundary best-effort append | `desired_state` event log。`managed_events` append-only | loss は desired history loss として許容。identity / liveness / handoff は壊れない。append 失敗は caller を壊さない |
| `inventory.sqlite` | v2 | runtime inventory listing の snapshot replace | `last_observed_projection` cache。`panes` と `inventory_meta` | regenerable。corrupt は write path が recreate。newer schema は downgrade CLI が壊さず write skip。read path は cache なし扱い |
| `otel-events.sqlite` | v1 | loopback OTLP receiver single writer | activity / timeline cache。`otel_events` と `otel_meta` | `rebuildable_cache` / best-effort。receiver 停止中の event loss を許容。prompt / secret shaped attrs は保存しない。corrupt は operator が退避して receiver restart |

責務上の注意:

- `workspace_activity.last_seen` は registry file 内にあるが、identity table とは別の cache table である。
  single SQLite では `registry_workspaces` と `registry_workspace_activity` のように namespace を分け、
  activity cache を identity row に吸収しない。
- `inventory.panes` は process / cwd / pane state を持つが、これは stale 表示用 snapshot であり
  target existence や action permission ではない。side-effecting command は実行時に live tmux
  preflight を行う。
- `otel_events.cwd` は allowlist 通過後の minimal telemetry hint であり、prompt / message / token /
  secret / personal data を入れる経路を作らない。single SQLite 統合で attrs allowlist / deny list を
  弱めない。
- `managed_events.workspace_id` は registry identity への参照であり、workspace identity の二重正本ではない。

## state kind ownership / recovery matrix (#12258)

single SQLite 化では file 境界が消えるため、実装上の ownership は **state kind** と **table namespace** で
管理する。下表を component 設計の正本とし、後続実装は module 名・table 名・doctor output をこの語彙へ
寄せる。

| state kind | table namespace | owner module / writer | allowed readers | recovery / rebuildability | prohibited promotion |
| --- | --- | --- | --- | --- | --- |
| `workspace_identity` | `registry_*` | `workspace_registry`。write は `workspace register` と guarded `init` のみ | session/name resolver、inventory identity join、doctor、target preflight | authoritative。backup / restore / workspace anchor re-register で復旧。drop/rebuild 自動化は禁止 | runtime pane/process/cwd、presentation grouping、workflow completion を持たない |
| `desired_state` | `managed_events`, future `managed_unit_events` | command boundary append。session/window/pane/unit を作成・採用・mark・rename した command が writer | recovery suggestion、presentation current table rebuild、doctor/audit | append-only lossy。partial corruption は quarantine + gap marker。identity/liveness は依存しない | current liveness / handoff availability / completion truth にしない |
| `desired_current_state` | future `unit_*`, `presentation_*` | future presentation/unit commands。event fold or explicit command が writer | cockpit/grouping UI、launch planning、doctor/rebuild suggestion | rebuildable if source events are sufficient; otherwise operator-managed current stateとして backup restore。destructive auto reconcile 禁止 | routing authority、live target existence、private operator policy default にしない |
| `last_observed_projection` | `inventory_*`, future `target_observations` | runtime listing / reload / observation command が snapshot replace | UI/diagnostics/candidate display、doctor | `rebuildable_cache`。drop/rebuild 可。ただし stale / observed_at / source を必ず出す | action permission、healthy/current 判定、pane existence 正本にしない |
| `activity_timeline` | `otel_*` | loopback receiver single writer | activity model、event timeline、cockpit activity join、doctor | `rebuildable_cache` / best-effort。receiver down 中の loss を許容。retention/prune 可 | prompt/content storage、death/liveness truth、workflow progress truth にしない |
| `runtime_current_fact` | DB table なし | live tmux / future sidecar live query | side-effecting command preflight、handoff resolver、runtime observation reload | persisted recovery 対象ではない。読めなければ unknown / fail closed | DB snapshot や UI 表示へ正本委譲しない |
| `side_effect_permission` | DB table なし | mozyo command implementation | command execution only | persisted state + durable workflow gate + live preflight から毎回計算 | raw DB reader / UI / private consumer が直接許可判定しない |
| `workflow_truth` | DB table なし | Redmine / governed workflow | agent coordinator、review/close gate、doctor links | Redmine durable recordで復旧。runtime DB へ複製しない | DB health / event freshness から completion/approval を導出しない |

### writer rules

- `registry_*` writer は identity 専用であり、projection / presentation / runtime cache を同 transaction で
  更新しない。identity write と cache refresh を混ぜると corruption 時の復旧判断が壊れる。
- `managed_events` writer は append-only。current table を更新する場合も、event append と current table
  update は同一 command boundary の別責務として扱い、event log の失敗が side-effecting command の
  runtime safety を弱めない。
- `inventory_*` / `target_observations` writer は snapshot replace。差分 patch 型の cache 更新を標準にしない。
  partial row set は `readability=partial` / `stale_reason` で表示する。
- `otel_*` writer は receiver のみ。CLI や UI が telemetry event を捏造して書かない。
- future `presentation_*` writer は desired presentation command のみ。private cockpit policy を public default
  として seed しない。

### reader rules

- Read model は用途別に component を読む。便利だからという理由で `state.sqlite` 全体を JOIN して
  "current truth" を作らない。
- UI / WebViewer / private consumer は public-safe projection を読む。raw DB table から action permission を
  推測しない。
- Doctor は横断 read を許されるが、結果は component status / recommendation であり、workflow completion や
  owner approval ではない。
- Migration tooling は component 単位で read/write する。registry migrator が cache table を drop するなど、
  owner namespace 外の repair をしない。

### recovery policy vocabulary

後続実装は `state_schema_components.recovery_policy` に次の語彙を使う。

- `authoritative`: loss は user-visible identity loss。repair は backup restore / source anchor import /
  explicit operator command が必要。例: `registry_*`。
- `append_only_lossy`: history gap を許容するが、gap marker / quarantine record を残す。既存 side effect は
  巻き戻さない。例: `managed_events`。
- `rebuildable_cache`: drop/rebuild 可能。stale / missing / corrupt は機能停止ではなく degrade。例:
  `inventory_*`, `otel_*`, `target_observations`。
- `operator_current_state`: event だけでは完全復元できない desired current state。backup restore または
  explicit re-declare が必要。例: future pinned/hidden/group membership。

### naming alignment

Implementation naming は正本語彙と対応させる。

- `registry_*` は identity。`workspace_activity` 相当は `registry_workspace_activity` のように cache と分かる名にする。
- `inventory_*` / `target_observations` は projection / observation。`current`, `truth`, `active` を table 名に入れない。
- `otel_*` は event timeline / activity hint。`liveness` や `health` を table 名に入れない。
- `presentation_*` / `unit_*` は desired current state。`target` を含む場合も live target 正本を含意しない。
- Generic field 名に `completed`, `approved`, `delivered`, `accepted`, `current_status` を使わない。
  それらは Redmine / ACK contract など source-scoped field に限定する。

## home-scoped single SQLite 統合方針 (#12257)

長期の統合先は 1 file、例: `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/state.sqlite` とする。名前は実装 task で
確定するが、scope は home で固定する。repo-local DB を default にしない。

### namespace / table ownership

single DB 内では table prefix で ownership を分ける。SQLite `user_version` だけに全責務を載せず、
`state_schema_components` で component ごとの schema version / owner / migrator / recovery policy を持つ。

```sql
CREATE TABLE state_schema_components (
    component TEXT PRIMARY KEY,          -- registry | managed_events | inventory | otel | presentation
    schema_version INTEGER NOT NULL,
    owner TEXT NOT NULL,                 -- implementation module / command family
    recovery_policy TEXT NOT NULL,       -- authoritative | append_only_lossy | rebuildable_cache | operator_current_state
    migrated_from TEXT,
    updated_at TEXT NOT NULL
);
```

Table naming:

- `registry_workspaces`, `registry_workspace_activity`
- `managed_events`
- `inventory_panes`, `inventory_meta`
- `otel_events`, `otel_meta`
- `lane_metadata_records` — 最初の **native component** の table (#13356、j#73386 Q2)。component 名は
  **`lane_metadata`** (component 名 ≠ table 名の既存 convention: component `registry` が `registry_*`
  tables を所有するのと同型。`state_schema_components.component` に載るのは `lane_metadata`)。legacy
  file を持たず `state.sqlite` に直接生まれる lane 表示 metadata (token↔lane_label/issue/branch/worktree
  join)。owner module は `core/state/lane_metadata.py`、writer は `sublane create` / `sublane retire
  --execute` の command boundary、recovery policy は `operator_current_state` (token は一方向 path hash
  であり event から再構成できない。loss は表示の fail-open degrade (`lane_record_missing`) に留まり、
  復旧は explicit re-declare)。表示 join であり routing authority / liveness / workflow truth に昇格
  させない。`state_schema_components` へは `migrated_from` NULL で自己登録する (native component の
  登録形)。native component のみが載る `state.sqlite` は「partial migration」ではない (doctor は
  native-only を ok と分類し、legacy import の未実行は operator の選択として案内する)。
- `lane_lifecycle_records` — 2 つ目の **native component** の table (#13689、Design Answer j#76741)。
  component 名は **`lane_lifecycle`**、owner module は `core/state/lane_lifecycle.py`。lane unit
  `(repo_workspace_id, lane_id)` の **desired lifecycle** — `lane_disposition`
  (`active|superseded|hibernated|retired`) と `process_release`
  (`not_requested|requested|partial|released`) — を持つ。recovery policy は `operator_current_state`
  (coordinator の supersede / hibernate 判断は event から再構成できず、復旧は Redmine durable pointer
  からの explicit re-declare)。`migrated_from` NULL で自己登録する。
  - **`lane_metadata` とは別 component である**。`lane_metadata` は display join であり、その `upsert`
    は tombstone を意図的に revive し、CAS を持たない。lifecycle authority をそこへ載せると
    out-of-order な write が supersede / hibernate を黙って上書きする。両者の drift は **診断対象**で
    あり、片方から他方を暗黙修復しない。
  - 読み手は `lane_metadata` と異なり **fail-closed** する: 不読 / 不在は `unknown` であって `active`
    ではない (推定 active は superseded lane への send を再認可してしまう)。
  - write は CAS (`BEGIN IMMEDIATE` + expected state + exact revision + exact release action id)。
    container guard の接続は default-isolation なので、`acquire_generation_lease` と同型に自前の
    autocommit 接続で `BEGIN IMMEDIATE` を駆動する。
  - active owner は partial unique index `(repo_workspace_id, issue_id) WHERE
    lane_disposition='active' AND issue_id <> ''` で **workspace scope** に固定する。home-global な
    unique は、同じ issue 番号を正当に持つ別 project と衝突する。
  - `released` は command outcome / desired state であり **live absence の正本ではない**。process
    presence は従来どおり live inventory (`observed_liveness`) を読む。
  - **owner binding と decision anchor は別 field である** (schema v2、#13689 R2-F1)。`issue_id` は
    「この lane がどの issue を所有しているか」(unbound lane では **空**)、`decision_source` /
    `decision_issue_id` / `decision_journal` は「現在の state をどの durable record が決めたか」で
    **常に完全**である。Redmine の journal は issue 経由 (`/issues/<id>.json`) でしか addressing
    できないため、issue を欠く anchor は何も指さず、`operator_current_state` の復旧 (explicit
    re-declare) が成立しない。両者を 1 field に畳むと、unbound lane が再読不能な anchor を持てて
    しまう。`DecisionPointer` は source 語彙と **positive decimal な issue/journal id** を要求する。
    v1 row の anchor は issue を欠くため `decision` が `None` (再読不能) として **可視化**され、
    推測で back-fill しない。id validation は **ASCII decimal** で行う (`str.isdigit()` は `²` /
    全角 `１` / Arabic-Indic `١` を True にするため使わない)。
  - **container guard は component guard ではない** (#13689 R3-F1)。本 component は DDL/DML の前に
    自分の `state_schema_components.schema_version` を読み、**未知の newer version なら
    `LaneLifecycleError` で fail-closed** し、table / metadata / rows を一切変更しない (`### backup /
    downgrade / partial migration` の「古い CLI が新しい container **または component schema** を見たら
    unsupported として DB を書き換えない」の実装)。rows は lifecycle authority であり、metadata を
    v2 へ書き戻すと **newer semantics を知らない code が authority を更新できてしまう**。read/write は
    `unknown` / `None` / error へ落ち、active を推定しない。
  - **binding kind / lane generation / typed process pins** (schema v5、#13810 / Design Answer
    j#78386)。project-gateway 用の別 owner component は作らず、同一 `lane_lifecycle_records` row /
    同一 revision CAS を additive 拡張する (別 component にすると owner row と release/replacement
    row の revision が分裂し atomic CAS が失われる)。追加 field:
    - `binding_kind` (`issue` | `project_gateway`)。lane の所有対象。migration 前の全 row は `issue`。
      `project_gateway` lane は issue を持たず `project_scope` を所有する。`issue` kind かつ空 issue の
      row は `legacy_unbound` として **可視化**し、project scope を lane digest から自動補完しない。
    - `project_scope`。project-gateway lane の **canonical full scope** (digest ではない。derived
      `pgwv1_...` lane id から推測しない)。active な project owner は第二の partial unique index
      `(repo_workspace_id, project_scope) WHERE lane_disposition='active' AND
      binding_kind='project_gateway' AND project_scope <> ''` で **workspace scope** に固定する
      (issue owner index の双子)。
    - `lane_generation`。positive monotonic な incarnation。retired generation は terminal。同一
      semantic route の再起動は `retired -> active` の implicit revive **禁止**で、明示 CAS
      `open_next_generation` (generation+1、release/replacement axes reset) のみ。古い generation の
      approval / pin / action id は stale として無効化される。
    - `declared_slots`。宣言時点の versioned `ProcessGenerationPin` snapshot (各 slot は
      `role / provider / assigned_name / locator / runtime_revision / attested_at`)。**required
      identity は `role / provider / assigned_name / locator`** で、`locator` 単独ではなく
      identity tuple で照合する。`runtime_revision` と `attested_at` は **optional evidence**
      (#13810 R4-F1): herdr の process 世代 discriminant は **live locator** であり
      (`herdr-native-identity.md`、startup self-attestation store は runtime version を保存しない)、
      runtime version の観測 surface が無い場合は空とする (fabricate しない)。`attested_at` は
      検証済み startup self-attestation の `observed_at` を保存する。observation surface を持つ
      richer 宣言経路は runtime_revision を供給してよく `match_key` に入る。current liveness では
      なく **観測 snapshot** であり、存在確認は毎回 live inventory を読む。既存 release/replacement
      `ReleasePin` の後方互換 decode は維持。
    - **common declaration service** (`LaneDeclarationStore.declare_lane`) が issue / project 双方を
      fail-closed に宣言する。exact duplicate は idempotent (#13809 live-adopt)、既存 owner conflict /
      別 issue-or-scope / 不読・ambiguous inventory は zero-write。bulk / implicit backfill は禁止。
    - **bounded missing-field backfill** (`LaneDeclarationStore.backfill_active_binding`, #13809
      residual j#78944 / j#78945、review j#79015 F2)。legacy active owner row は issue を所有済みだが
      binding の一部が空で、`declare_lane` は gate 済み live adopt を divergent re-declare と読んで拒否し、
      `retire --execute` が `worktree_binding_unverified` で止まり typed pins も未記録のまま残る。2 つの
      reachable gap: **pre-#13754** の空 `worktree_identity`(かつ空 `declared_slots`)と、**v4→v5 migrated**
      で `worktree_identity` は #13754 で付与済みだが `declared_slots` snapshot が空の **pins-only gap**。この
      surface は **空の binding field だけ**を exact `expected_revision` CAS で補完する。書込は「row が active /
      `binding_kind='issue'` / この exact issue を所有 / project scope 無し」かつ「`worktree_identity` が空 or
      token と一致」かつ「`declared_slots` が空 or incoming set と一致」かつ「revision 一致」の全成立時のみ。
      **non-empty different worktree** および **non-empty different slot snapshot (recycled generation)** は
      上書きせず zero-write (`already_declared`)、両 field exact 一致は idempotent no-op、別 issue / non-active
      disposition は `unexpected_state`、revision race は `stale_revision`。`declare_lane` の「divergent
      re-declare は上書き禁止」を一般的に緩める surface ではなく、欠落 field 専用。live-adopt path は
      `declare_lane` 拒否時にのみこの CAS を試み、成功を `backfilled` として rowless declaration (`declared`) と
      区別して伝播する。disposition / generation / release / replacement / decision anchor は不変。
      ★adopt が gate failure / CAS refusal で終わったとき「既に確立済みゆえ dispatch 安全」と判定する
      条件は、**issue 所有だけでは不十分**で state DB owner row が **complete かつ exact な binding**
      — `worktree_identity` 非空 かつ token 一致、**かつ** `declared_slots` が decode-valid で
      **この adopt の provider pair** に紐づく `(GATEWAY_ROLE, gateway_provider)` / `(WORKER_ROLE, worker_provider)`
      pin を含む non-empty typed pin set — を持つことを要する (#13809 review j#78975 F1 / j#79015 F2 /
      j#79074 F3)。completeness は role 名だけでなく `(role, provider)` pair で照合し、role は揃うが provider が
      異なる **swapped/foreign snapshot**（provider_binding 切替後の旧 pin 等）は complete としない。locator /
      runtime_revision は照合対象外で、同 provider pair の recycled generation（別 locator）は complete を保つ。
      いずれかの軸が incomplete な legacy row (空 worktree、pins-only gap) や別 worktree / 別 provider binding では
      ambiguous / unattested / stale live pair・revision race・non-empty mismatch を `already_owned` に畳まず
      fail-closed で dispatch を止める (items 2/3 の安全 gate を維持)。herdr 全断の
      `unreadable_inventory` は無観測ゆえ別扱いで ownership authority proceed (R4-F3) を維持する。
    - **hibernated legacy retire migration** (`LaneRetireMigrationStore.retire_released_hibernated_legacy`,
      #13841、live evidence #13756 j#79114–j#79115)。**hibernated / released legacy** row —
      coordinator が hibernate 済み、process release が durable に `released` へ到達、live pair は消滅、
      だが `worktree_identity` が **空**（pre-#13754 で未記録）— は既存 path のどれでも retire できない:
      `retire --execute` は worktree binding を先に attest するため空 binding で恒久的に
      `worktree_binding_unverified`(閉じる live pair も無い)、#13809 backfill は **active** row 専用。
      new pair を起動して再退役するのは不要な actuation ゆえ採らず、この surface は該当 row を
      **直接** #13689 terminal `retired` disposition へ 1 本の bounded CAS で移す — **metadata only**、
      process launch/close/resume も worktree/branch 削除も伴わない。書込は「row 存在 かつ exact
      `expected_revision` 一致」かつ「`hibernated` / `binding_kind='issue'` / この exact issue 所有 /
      project scope 無し / `worktree_identity` **空**」かつ「process release が durable に `released`
      (unproven / in-flight は fail-closed) / replacement settled」の全成立時のみ。それ以外の shape
      (active/superseded/retired、別 issue、**non-empty (既 #13754-bound) worktree**、release 未証明、
      revision race、row 不在) は zero-write。`transition_disposition` の generic edge を使わない理由は、
      release proof と empty-worktree signature が **guard の一部**であって caller の promise ではないため。
      `released` は「release command 完了」であって slots 消滅の証明ではない (`### 正本境界`) ので、
      public high-level path (`sublane retire --migrate-hibernated-legacy`) は durable proof を
      **live-inventory zero read**（expected managed slot が 1 つでも live なら `live_pair_present`
      fail-closed）と、`--branch` の `--integration-branch` への **ancestry probe**(unknown/非 ancestor は
      fail-closed)と対にする。closed status / clean worktree / latest review / callback drain は command の
      `may_retire` preflight が上流で gate する。★**worktree↔branch identity**(#13841 review j#79150 F1):
      clean probe は `--worktree`、integration probe は `--branch` を測るので、`--worktree` の実 checkout
      branch(`git rev-parse --abbrev-ref HEAD`)が `--branch` と一致しない限り無関係 branch の clean/integrated
      証拠で退役しうる → 実 branch==`--branch` を action-time 要求、不一致/detached/解決不能/空 branch は
      `worktree_branch_mismatch` zero-write。★duplicate replay の冪等性(既 `retired` row をこの issue 所有で
      no-op success)は維持するが、**success を返す前に live-inventory zero を action-time 確認**する
      (#13841 review j#79150 F2): persisted `retired` は現在の非稼働性を証明しないので、retired 復元後に
      pair が再稼働/inventory unreadable なら idempotent replay も fail-closed。★`--migrate-hibernated-legacy`
      と `--execute` は競合する destructive intent ゆえ **両指定は command-time zero-write error**(#13841
      review j#79150 F3、黙って一方へ解決しない)。disposition / decision anchor 以外は不変。
    - **hibernated live-contradiction reconcile (retire-first)** (`LaneReconcileBindingStore.retire_reconciled_hibernated_legacy`,
      #13842、live evidence #13756 j#79188)。#13841 migration が **拒否**する側の隙間: **hibernated / released
      legacy** row（`worktree_identity` 空）だが action-time Herdr inventory に exact managed pair が **live** で
      残るケース。#13841 live-zero migration は `live_pair_present` で拒否、#13754 guarded close は
      `worktree_binding_unverified`、#13809 backfill は **active** row 専用 — 3 契約の間に収束経路が無く恒久停止する。
      public high-level path (`sublane retire --reconcile-hibernated-live`) が **close の前**に、exact live pair の
      `(workspace, lane, issue)` identity・`--worktree` 実 branch==`--branch`・integration ancestry・
      expected assigned names/roles/providers・per-slot startup self-attestation (generation-bound)・
      pair completeness/uniqueness・各 agent idle/turn-ended・pending composer 無し・settled replacement・
      exact lifecycle revision を **連言検証**（foreign/ambiguous/partial/duplicate/unattested/working pair・
      inventory unreadable・branch mismatch/detached/unintegrated・revision race は zero-write/zero-close）。★**retire-first**
      (#13842 review j#79282 R2, correction boundary option (b)): green 時のみ、**hibernated→retired への 1 本の bounded CAS**
      で worktree + `declared_slots` pins + reconcile decision を同時 write（`expected_revision` guard）。書込は「row 存在 かつ
      exact `expected_revision` 一致」かつ「`hibernated` / `binding_kind='issue'` / この exact issue 所有 / project scope 無し /
      `process_release='released'` / replacement settled」かつ「`worktree_identity` **空 かつ** `declared_slots` **空**」
      （＝**empty-binding legacy signature**、review j#79320 R1）の全成立時のみ。★**R1 scope** (review j#79320 R1):
      base signature と retire CAS 双方で worktree/declared_slots の **空**を必須化。ANY 既存 binding（#13754/#13809/#13810-bound
      row）は `not_reconcilable_state` zero-write ＝ **ordinary #13754 guarded close の領分**（非退行）。この legacy-contradiction
      surface は ticket が scope する empty-binding legacy row だけを reconcile する。verify 後の rehydrate/move で row が動いていれば
      revision(→`stale_revision`)/disposition/release(rehydrate は release を not_requested reset)で CAS 失敗 →
      `revision_race`/`not_reconcilable_state` で **zero-write かつ zero-close**（terminal write を **external close の前**に置くのが
      要: close 後の terminal CAS では閉じた pair を戻せない、review j#79282 R2）。★CAS 成功後は disposition が **retired（terminal）**
      = rehydrate/move 構造的不能 → **revision/generation は close 完了まで不変**（R2 option (b) を terminality で保証）。
      ★**close-time full re-verify (review j#79320 R2)**: close の前に **fresh inventory を再観測し `decide_pair_reconcile` を
      再実行**（idle/turn-ended・pending composer 無し・attestation・uniqueness・foreign を close 時点で再検証）、live pair が
      pinned pins と **exact locator 一致 かつ green** の時だけ `pin_matched_close_plan` で pin-matched close。initial green と close
      の間に busy 化/pending/duplicate/recycled locator になった agent は **zero-close**。★**whole-unit post-close measure
      (review j#79320 R3)**: close 後に **lane unit 全体の expected pair を fresh inventory で測定**（`.absent` = 全 expected slot
      不在）。old pins 消失だけでは成功にせず、recycled/duplicate/foreign が any locator で live なら **success withhold**（terminal
      retired + live newer pair の false success を排除）。★**collision-proof provenance を authoritative row 上に置く +
      same-flow resume (review j#79346 R5 / j#79363 R6)**: reconcile owed-close の provenance は **`lane_lifecycle` v6 additive
      column `reconcile_phase`**（default `''`、reconcile retire CAS が atomic に `'reconciled'` を set、`open_next_generation` が
      reset）で表す。★**R6**: この owed-state marker は「retired row が reconcile 由来か ordinary retire 由来か」を区別する **唯一の
      正本**で row から再構築不能ゆえ `rebuildable_cache` にできない。**authoritative row 上に co-locate** することで provenance は row と
      生死を共にし、loss = state.sqlite loss = component 自身の `operator_current_state` recovery（Redmine から re-declare）に subsume
      される（別 backup/doctor/repair surface / 単独 losable cache 不要）。★以前の isolated ledger（`lane_reconcile_owed.sqlite`）は
      「単独 loss で reconcile-retired+live row が恒久 stuck」＝one-replayable-flow 喪失ゆえ撤去。retire 済・pane close 未の crash は
      **同一 reconcile authority で resume**: retired-branch が **`record.reconcile_phase=='reconciled'` の時のみ** owed-close と認定
      （collision-proof: ordinary #13809/#13810-bound retired row は phase 空→resume せず、review j#79320 R4 維持）、
      record.declared_pins へ **同じ close-time full re-verify + whole-unit measure** を適用（recycled newer/busy/pending は zero-close）。
      ★**#13754 手動 fallback を撤去** (review j#79346 R5): #13754 ordinary close は name/provider-based で declared_slots generation
      pins を読まず idle/composer/attestation gate も無いため recycled newer generation を close する→crash replay を委ねない。reconcile
      自身の retired-branch resume で **one replayable flow** を完結。acceptance「**close済み** partial replay は positive absence +
      durable owed state から再開」= close 後 = retired + phase='reconciled' + absent → `already_reconciled`（idempotent, duplicate
      close せず）で満たす。phase 空（ordinary）の retired row は resume せず、absent → `already_reconciled`、live → `live_pair_present`
      withhold。「hibernated + live pair 無し」は retire せず `live_pair_absent` で #13841 へ route。★**typed outcome の事実性
      (review j#79363 R7)**: retire-first ゆえ blocked verdict でも retire/close は committed 済みうる。verdict に **`retired: bool` +
      `closed`** を持たせ実発生の durable mutation を保持、text/JSON は zero-write かつ zero-close の時だけ「nothing written or closed」を
      表示、retire/close 済なら実側効果（`lane retired` / closed targets）を出して post-close newer-generation / partial-close を
      audit 可能にする。`--reconcile-hibernated-live` は `--execute` / `--migrate-hibernated-legacy` と競合する destructive intent ゆえ
      **2 つ以上の同時指定は command-time zero-write error**。process launch/resume・worktree/branch 削除・raw Herdr/tmux・
      origin/main・production は伴わない（唯一の process mutation は 自 lane の exact managed pair への pin-matched close）。
    - v1–v5 → v6 migration は backup-first additive（v6 = `reconcile_phase`、#13842）。unknown / newer / partial / foreign schema は
      byte-unchanged fail-closed (上記 container/component guard と同じ)。project-gateway lifecycle
      adapter / generic exact-generation actuator は後続 (#13780 / #13806)。
- future `presentation_*` / `unit_*` tables from `unit-presentation-state-db.md`

Ownership rules:

- A component migrator owns only its namespace. Registry migration must not rewrite inventory / otel rows as a side effect.
- Cross-component reference is by value (`workspace_id`, `pane_id`, `target_key`, `source_event_id`) plus best-effort
  integrity checks. Do not require hard FK from `rebuildable_cache` tables into authoritative identity tables when that would
  make cache rebuild impossible after partial loss.
- `state_schema_components` is metadata, not workflow truth. Redmine / owner approval / review / completion remain outside DB.

### schema version / migration

Use a two-level version model:

1. SQLite `PRAGMA user_version` identifies the container layout and presence of `state_schema_components`.
2. `state_schema_components.schema_version` identifies each component's readable / writable shape.

Migration stance:

- Initial implementation should be read-only inspector / doctor first. Do not move writes before doctor can report old/new
  state side by side.
- Migration from legacy files is copy/import, not in-place mutation. Legacy files remain as rollback input until the new DB is
  verified.
- Each imported component records `migrated_from` and source schema version. Import is idempotent by component.
- A mixed state (new DB partially imported) is not silently treated as complete. Doctor must show component-level status and
  next action.
- Downgrade must not destroy newer DBs. An older CLI that sees a newer container or component schema reports unsupported and
  leaves the DB untouched. For caches, it may continue using legacy files if present; for registry identity, it must fail closed
  rather than rewrite unknown state.

#### read-compatible / write-migrating split (shared home, parallel-lane schema skew — #13844)

The home-scoped `state.sqlite` is a **single shared authority** while parallel repo lanes each run a **source CLI of a different
schema generation** (each issue worktree ships its own branch's code). If a newer-schema source CLI forward-migrates the shared
store on a mere READ — a status / handoff / review / callback / drain routing lookup — it fail-closes every concurrent
older-schema reader: the older reader (correctly) refuses to downgrade the now-newer store, so `standard` handoff stops with
`gateway_route_blocked` and the transport rail stalls permanently (live: #13842 `56d3a32` migrated the shared store to v6, then a
concurrent v5 reader could not send #13813 j#79382). A safe per-CLI fail-closed thus becomes a system-wide liveness failure.

The contract that prevents it, for a component whose rows are authority (e.g. `lane_lifecycle`):

- **Reads never migrate.** Status / handoff / review / callback / drain — any read-only projection (`workflow glance`), AND the
  store's own read methods (`LaneLifecycleStore.get` / `records` / `resolve_owner`, even the ones a mutating flow calls before it
  writes) — read the authority through a **read-only, version-compatible reader** (`LaneLifecycleReader` /
  `readonly_compatible_select`), opened `mode=ro`: no DDL, no `ALTER`, no version re-stamp, no backup. A newer build reads an
  **older KNOWN additive shape** by padding the columns that shape lacks with their **in-memory migration defaults** (the same
  value a forward `ALTER … DEFAULT` would have written), so it interprets the older store faithfully without touching a byte. The
  shared store stays at its current version and every concurrent older reader keeps working. (A read landing during a peer's
  migration commit waits it out via `busy_timeout` rather than fail-closing on a transient lock.)
- **Every mutation migrates through ONE explicit write gate.** No CAS opens the store with a bare `ensure`. Every schema-needing
  mutation — declaration / incarnation AND disposition / supersede / release / replacement / retire / reconcile — opens via the
  single choke point `LaneLifecycleStore._connect_write(writer_key)`, which runs the **compatibility preflight FIRST** (the active
  peer lanes a forward migration would fail-close, read on the PRE-migration store, the writer's own lane excluded), THEN the
  backup-first migration, capturing a **typed `LifecycleWritePreparation`** (the preflight + the `created` / `intact` /
  `migrated{from_version, backup_dir}` outcome) on `last_write_preparation`. So a migration is a chosen, visible act with legible
  peer risk for *any* mutation, never an implicit side effect of opening the store — and a read that precedes the write never
  migrates ahead of the preflight. The command surfaces (adopt / hibernate / resume / supersede / …) emit the shared
  `format_lifecycle_migration_advisory` to the operator when a mutation forward-migrated the shared store with peers at risk.
- **Fail-closed is preserved and made specific.** An unknown / newer / partial / malformed shape still fails closed (no
  downgrade, no misread), judged only by the **shape / capability table** (`_ALLOWED_SHAPES_BY_VERSION` / `_COLUMN_DEFS`), never a
  guessed compatibility. The specific NEWER sub-case is named `reader_upgrade_required`: the store is fine, THIS reader is stale,
  so the caller routes to the **current compatible high-level facade** (the up-to-date source CLI) rather than a raw DB downgrade.
- **Source CLI vs installed facade boundary.** An unintegrated issue-branch source CLI is a *reader* of the shared authority; it
  must not implicitly migrate it. A schema-changing write (a mutating command) should surface the **compatibility preflight**
  (`lifecycle_migration_preflight`) — read-only, version-compatible — which reports the other active lanes a forward migration
  would fail-close, so the migration is a chosen, visible act rather than an accidental side effect of a background notification.

### corruption blast radius / backup

Single file raises the blast radius, so recovery policy must be component-aware:

- Registry tables are authoritative. Corruption affecting registry tables blocks identity writes until operator backup /
  restore / anchor re-register is chosen.
- Cache tables (`inventory_*`, `otel_*`) are regenerable. Corruption limited to those tables can be repaired by dropping only
  that namespace after backup, never by deleting the whole DB by default.
- `managed_events` history is lossy append-only. Corruption limited to event history may be quarantined to a backup and append
  can resume with a gap marker; it must not block liveness / handoff.
- Before any destructive repair, create a timestamped backup copy under home (for example `backups/state-<timestamp>.sqlite`).
  Backup failure means repair does not proceed automatically.
- Doctor should run `PRAGMA integrity_check` / `quick_check`, component metadata checks, expected table checks, and minimal
  row-shape probes. It should report the smallest affected component and the safest repair route.

## migration / doctor / integrity check 方針 (#12260)

Migration は **read-only inspector first** と **write migration later** に分ける。#12260 は runtime migration
実装ではなく、後続実装が守る不変条件を固定する task である。CLI 名、JSON field の完全形、backup directory
名は後続実装 issue で確定する。

### legacy import

Legacy file から single DB への移行は component 単位の copy/import とし、legacy file を in-place mutate
しない。移行後もしばらく legacy file は rollback / downgrade input として残し、cleanup は別 issue で扱う。

| legacy file | target namespace | recovery policy |
| --- | --- | --- |
| `registry.sqlite` | `registry_*` | `authoritative` |
| `managed-events.sqlite` | `managed_events` | `append_only_lossy` |
| `inventory.sqlite` | `inventory_*` | `rebuildable_cache` |
| `otel-events.sqlite` | `otel_*` | `rebuildable_cache` |

Import rules:

- component ごとに idempotent にする。target table、source schema version、row-shape probe、
  `state_schema_components` が揃って初めて complete と扱う。
- component metadata には `migrated_from`、source schema version、import timestamp、backup id を残す。
- partial migration は成功扱いしない。doctor は component 単位で `partial` と next action を出す。
- cross-component reference は best-effort integrity check に留める。cache/history 側の不整合を理由に
  authoritative な registry row を自動修正しない。
- migration は projection / activity / history から `side_effect_permission`、`runtime_current_fact`、
  workflow completion、owner approval、Redmine gate state を導出しない。

### backup / downgrade / partial migration

Write migration と destructive repair は、必ず事前 backup を作る。backup は home scope に置き、single DB
が既にあればそれも含め、存在する legacy file を component ごとに退避する。backup failure 時は write
migration / repair を進めない。

Downgrade は非破壊にする。古い CLI が新しい container `user_version` または新しい component schema を見た場合、
`unsupported_schema` として報告し、DB を書き換えない。cache reader は legacy file が残っていれば fallback
してよいが、registry identity writer は unknown state を rewrite せず fail closed する。

Partial migration は component 単位で resumable にする。rerun は complete 済み component を skip してよいが、
authoritative component を黙って上書きしない。

### corruption quarantine / component repair

Doctor / repair は最小 component 単位で動く。single DB 全体の削除を default repair にしない。

- `authoritative`: 自動 drop/rebuild 禁止。backup restore、workspace anchor import、明示的な re-register を促す。
- `append_only_lossy`: unreadable history を quarantine し、gap marker を残して future append を再開できる。
  identity / liveness / handoff は block しない。
- `rebuildable_cache`: backup 後に namespace drop/rebuild してよい。rebuild は owning reload / receiver command
  に任せる。
- `operator_current_state`: backup restore または operator の explicit re-declare を要求する。destructive
  auto reconcile はしない。

Quarantine は affected file / table dump / unreadable row set を timestamped backup/quarantine location へ移す。
source path と component は記録するが、prompt text、credential、private payload content は記録しない。

### doctor / inspector output

Read-only inspector が最初の実装 target である。legacy files と future single DB を side-by-side に読むが、
create / migrate / drop / repair はしない。

Doctor は component ごとに最低限次を出す。

- component name (`registry`, `managed_events`, `inventory`, `otel`, future `presentation` / `unit`)
- legacy file state: `present` / `missing` / `unreadable` / `unsupported_schema` / `corrupt`
- single DB / component schema state: `absent` / `readable` / `partial` / `complete` / `unsupported`
- integrity: `ok` / `warning` / `error` / `unknown`
- next action: `inspect` / `backup` / `migrate_dry_run` / `migrate_write` / `repair_component` /
  `restore_backup` / `re_register` / `reload` / `restart_receiver` / `leave_untouched`

`ok` はその component が state kind の範囲で読めるという意味に限る。workflow complete、owner-approved、
action allowed を意味しない。`freshness` は projection / activity component にだけ使い、registry identity
を `fresh` / `current` と表現しない。

Doctor wording は component-scoped next action を中心にする。fresh cache だけから `healthy` / `current` を
出さない。side-effecting action の許可は doctor ではなく command-time live preflight が判定する。

### implementation order

後続実装は次の順序に分ける。

1. read-only inspector / doctor
2. dry-run migration planner
3. write migration
4. component repair commands
5. legacy cleanup / retirement

Write migration は、read-only inspector output、component status vocabulary、backup/quarantine behavior が
test-pinned されるまで始めない。migration command が corrupt / unsupported state を初めて発見する設計に
しない。

### access boundary

Persisted state は raw DB/file として外部へ広げず、mozyo command / documented API 境界で扱う。これは
third-party plugin API 公開を意味しない。まず built-in command surface と内部 access helper の責務分離を
固定する。

Command-mediated access の分担:

- state store facade / helper は component owner、schema version、recovery policy を知る。caller が任意 table
  を直接 JOIN して "current truth" を作らない。
- read model surface は用途別に分ける。doctor は component status / next action、UI / cockpit は
  public-safe projection、migration planner は component import plan を読む。
- write surface は component owner に限定する。registry writer、managed event appender、inventory reload、
  OTel receiver、future presentation command は互いの namespace を修復・更新しない。
- side-effecting command は read model を行動許可にしない。persisted desired state、durable workflow gate、
  action-time live preflight を command 境界で照合して毎回 `side_effect_permission` を計算する。

UI / private consumer / WebViewer は mozyo が出した read model / projection を読める。ただし raw DB table を
直接開いて action permission、workflow completion、owner approval、live target existence を推測してはならない。
UI action は mozyo command へ戻し、`runtime-observability-boundary.md` の Action-Time Live Preflight Boundary を
通す。

後続実装で必要な surface は、raw DB path の公開ではなく次の形へ寄せる:

- `inspect` / `doctor`: read-only。component status と next action を返す。
- `list` / `query` / `reload`: projection を更新または取得する。stale / observed_at / source を出す。
- `migrate --dry-run`: write せず import plan と backup plan を返す。
- `migrate --write` / `repair`: explicit operator command と backup を要求する。
- side-effecting commands: 実行直前に live tmux / future sidecar live query を読む。

## 実装分割 (#12257 -> #12258-#12261)

本 issue で即時に実装する範囲は docs / design record のみとする。runtime migration は行わない。

- #12258: state kind ごとの table ownership / recovery policy を詳細化する。上記 namespace 案を入力に、
  component ごとの migrator owner、FK 方針、drop/rebuild 可否、history gap marker を決める。
- #12259: global runtime DB と repo-local artifact の境界を固定する。repo-local static artifact と
  home runtime DB の例外 / 禁止事項 / public-private boundary を整理する。
- #12260: migration / doctor / integrity check を設計する。legacy import、backup、downgrade、partial
  migration、corruption quarantine の CLI / test 方針を決める。
- #12261: command-mediated state access boundary を実装する。raw DB access を避け、read model / doctor /
  action-time preflight の command surface へ寄せる。

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
  根拠: otel-events / inventory と同じ `rebuildable_cache` 姿勢 (#11422)。正本化する範囲を
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
